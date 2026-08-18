"""Client-controlled, demographic-safe reuse of previously registered profile UIDs."""

from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_FLOOR

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from surveys.models import ProfileReuseEvent, ProfileReuseMonthlyCounter, SurveyAttempt

from .constants import DATABASE_ALIAS
from .models import PrescreenerSubmission
from .services import _question_snapshots


AGE_GROUP_RANGES = {
    "13-17": (13, 17), "18-24": (18, 24), "25-29": (25, 29), "30-34": (30, 34),
    "35-39": (35, 39), "40-44": (40, 44), "45-49": (45, 49), "50-54": (50, 54),
}
GENDER_ALIASES = {"male": ("male", "m", "man"), "female": ("female", "f", "woman")}


def effective_profile_uid(attempt) -> str:
    return str(attempt.provider_profile_uid or attempt.prescreener_uid or "").strip()


def _calendar_bounds(reference=None):
    reference = timezone.localtime(reference or timezone.now())
    current_date = reference.date().replace(day=1)
    previous_date = (current_date - timedelta(days=1)).replace(day=1)
    return (
        timezone.make_aware(datetime.combine(previous_date, time.min)),
        timezone.make_aware(datetime.combine(current_date, time.min)),
        current_date,
    )


def _target_from_baseline(integration, baseline):
    percentage = Decimal(str(integration.profile_reuse_monthly_percentage or 0))
    return int((Decimal(baseline) * percentage / Decimal("100")).to_integral_value(
        rounding=ROUND_FLOOR
    ))


def _monthly_target(integration, reference=None):
    previous_start, current_start, period_start = _calendar_bounds(reference)
    baseline = SurveyAttempt.objects.filter(
        survey__integration_id=integration.pk,
        initiated_at__gte=previous_start,
        initiated_at__lt=current_start,
    ).count()
    return period_start, baseline, _target_from_baseline(integration, baseline)


def profile_reuse_month_status(integration, reference=None):
    _, _, period_start = _calendar_bounds(reference)
    counter = ProfileReuseMonthlyCounter.objects.filter(
        integration_id=integration.pk, period_start=period_start
    ).first()
    if counter:
        baseline = counter.baseline_attempts
        target = _target_from_baseline(integration, baseline)
        used = counter.allocated_reuses
    else:
        _, baseline, target = _monthly_target(integration, reference)
        used = 0
    return {
        "period": period_start.isoformat(), "previous_month_attempts": baseline,
        "target_reuses": target, "used_reuses": used,
        "remaining_reuses": max(0, target - used),
    }


def _claim_month_slot(integration):
    _, _, period_start = _calendar_bounds()
    with transaction.atomic():
        counter, created = ProfileReuseMonthlyCounter.objects.select_for_update().get_or_create(
            integration_id=integration.pk, period_start=period_start,
            defaults={"baseline_attempts": 0, "target_reuses": 0},
        )
        if created:
            _, baseline, _ = _monthly_target(integration)
            counter.baseline_attempts = baseline
        else:
            baseline = counter.baseline_attempts
        target = _target_from_baseline(integration, baseline)
        counter.target_reuses = target
        if target <= 0:
            counter.save(update_fields=["baseline_attempts", "target_reuses", "updated_at"])
            return None
        if counter.allocated_reuses >= target:
            counter.save(update_fields=["baseline_attempts", "target_reuses", "updated_at"])
            return None
        counter.allocated_reuses += 1
        counter.save(update_fields=["baseline_attempts", "target_reuses", "allocated_reuses", "updated_at"])
        return counter.pk


def _release_month_slot(counter_id):
    if counter_id:
        ProfileReuseMonthlyCounter.objects.filter(pk=counter_id, allocated_reuses__gt=0).update(
            allocated_reuses=F("allocated_reuses") - 1
        )


def _profile_signature(attempt, answers):
    _, _, age, age_group, gender, _, _ = _question_snapshots(attempt, answers)
    normalized_gender = str(gender or "").strip().lower()
    for canonical, aliases in GENDER_ALIASES.items():
        if normalized_gender in aliases:
            normalized_gender = canonical
            break
    return {
        "country_code": str(attempt.survey.country_code or "").strip().upper(),
        "age": age, "age_group": age_group, "gender": normalized_gender,
    }


def _reserve_vault_profile(attempt, signature, minimum_days):
    threshold = timezone.now() - timedelta(days=minimum_days)
    gender_values = GENDER_ALIASES.get(signature["gender"], (signature["gender"],))
    with transaction.atomic(using=DATABASE_ALIAS):
        candidate = (
            PrescreenerSubmission.objects.using(DATABASE_ALIAS).select_for_update(skip_locked=True)
            .filter(
                country_code=signature["country_code"],
                respondent_age_group=signature["age_group"],
                respondent_gender__in=gender_values,
                submitted_at__lte=threshold,
            )
            .exclude(uid=attempt.prescreener_uid)
            .order_by("usage_count", "submitted_at", "uid")
            .first()
        )
        if candidate is None:
            return None
        PrescreenerSubmission.objects.using(DATABASE_ALIAS).filter(pk=candidate.pk).update(
            usage_count=F("usage_count") + 1
        )
        candidate.usage_count += 1
        return candidate


def _undo_vault_reservation(uid):
    if uid:
        PrescreenerSubmission.objects.using(DATABASE_ALIAS).filter(uid=uid, usage_count__gt=1).update(
            usage_count=F("usage_count") - 1
        )


def maybe_assign_reusable_profile(attempt, answers):
    integration = getattr(attempt.survey, "integration", None)
    if not settings.PRESCREENER_VAULT_ENABLED or integration is None or not integration.profile_reuse_enabled:
        return None
    existing = ProfileReuseEvent.objects.filter(attempt_id=attempt.pk).first()
    if existing:
        if attempt.provider_profile_uid != existing.reused_uid:
            SurveyAttempt.objects.filter(pk=attempt.pk).update(provider_profile_uid=existing.reused_uid)
            attempt.provider_profile_uid = existing.reused_uid
        return existing

    signature = _profile_signature(attempt, answers)
    if not signature["country_code"] or signature["age_group"] not in AGE_GROUP_RANGES or signature["gender"] not in GENDER_ALIASES:
        return None
    allowed_countries = {str(value).strip().upper() for value in (integration.profile_reuse_country_codes or []) if str(value).strip()}
    if allowed_countries and signature["country_code"] not in allowed_countries:
        return None
    if signature["age_group"] not in (integration.profile_reuse_age_groups or []):
        return None
    if signature["gender"] not in (integration.profile_reuse_genders or []):
        return None

    counter_id = _claim_month_slot(integration)
    if not counter_id:
        return None
    candidate = None
    try:
        candidate = _reserve_vault_profile(attempt, signature, int(integration.profile_reuse_eligible_after_days))
        if candidate is None:
            _release_month_slot(counter_id)
            return None
        with transaction.atomic():
            updated = SurveyAttempt.objects.filter(pk=attempt.pk, provider_profile_uid="").update(
                provider_profile_uid=candidate.uid
            )
            if not updated:
                raise RuntimeError("This attempt already has a provider profile UID.")
            event = ProfileReuseEvent.objects.create(
                integration_id=integration.pk, attempt_id=attempt.pk,
                registered_uid=attempt.prescreener_uid, reused_uid=candidate.uid,
                source_registered_at=candidate.submitted_at,
                source_usage_number=candidate.usage_count,
                country_code=signature["country_code"], age_group=signature["age_group"],
                gender=signature["gender"],
            )
        attempt.provider_profile_uid = candidate.uid
        return event
    except Exception:
        _release_month_slot(counter_id)
        _undo_vault_reservation(candidate.uid if candidate else "")
        raise
