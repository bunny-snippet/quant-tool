"""Client-controlled, requirement-safe reuse of registered panelist UIDs."""

from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_FLOOR

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from surveys.age_rules import OPEN_ENDED_AGE_MAX

from surveys.models import (
    ProfileReuseEvent,
    ProfileReuseMonthlyCounter,
    ProfileReuseProjectUsage,
    ProfileReuseState,
    SurveyAttempt,
)

try:
    from .cache import invalidate_vault_cache
except ImportError:  # Quant deployments without the optional vault cache layer.
    def invalidate_vault_cache():
        return None
from .constants import DATABASE_ALIAS
from .models import PrescreenerSubmission
from .services import _age_from_value, _age_group, _normalize_profile_value, _question_snapshots


AGE_GROUP_RANGES = {
    "13-17": (13, 17),
    "18-24": (18, 24),
    "25-29": (25, 29),
    "30-34": (30, 34),
    "35-39": (35, 39),
    "40-44": (40, 44),
    "45-49": (45, 49),
    "50-54": (50, 54),
}
GENDER_ALIASES = {
    "male": ("male", "m", "man"),
    "female": ("female", "f", "woman"),
}
FIRST_REUSE_POOL = "first"
RETURNING_REUSE_POOL = "returning"
MAX_CANDIDATE_SCAN = 500


class ReuseReservationConflict(RuntimeError):
    """A concurrent request consumed a candidate after it was shortlisted."""


def effective_profile_uid(attempt) -> str:
    """Return the provider-facing UID without changing the journey's RID/PID."""

    return str(attempt.provider_profile_uid or attempt.prescreener_uid or "").strip()


def _calendar_bounds(reference=None):
    reference = timezone.localtime(reference or timezone.now())
    current_date = reference.date().replace(day=1)
    previous_date = (current_date - timedelta(days=1)).replace(day=1)
    current_start = timezone.make_aware(datetime.combine(current_date, time.min))
    previous_start = timezone.make_aware(datetime.combine(previous_date, time.min))
    return previous_start, current_start, current_date


def _target_from_baseline(integration, baseline):
    percentage = Decimal(str(integration.profile_reuse_monthly_percentage or 0))
    return int((Decimal(baseline) * percentage / Decimal("100")).to_integral_value(
        rounding=ROUND_FLOOR
    ))


def _legacy_pool_targets(integration, total_target):
    """Keep historical API counters readable while the runtime uses one queue."""

    if not integration.profile_rereuse_enabled:
        return total_target, 0
    repeat_percentage = Decimal(str(integration.profile_rereuse_percentage or 0))
    repeat_target = int(
        (Decimal(total_target) * repeat_percentage / Decimal("100")).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    return max(0, total_target - repeat_target), repeat_target


def _monthly_baseline(integration, reference=None):
    """Use last month normally and current traffic for a brand-new integration."""

    previous_start, current_start, period_start = _calendar_bounds(reference)
    previous_attempts = SurveyAttempt.objects.filter(
        survey__integration_id=integration.pk,
        initiated_at__gte=previous_start,
        initiated_at__lt=current_start,
    ).count()
    if previous_attempts > 0:
        return period_start, previous_attempts, previous_attempts, "previous_month"
    current_attempts = SurveyAttempt.objects.filter(
        survey__integration_id=integration.pk,
        initiated_at__gte=current_start,
    ).count()
    return period_start, previous_attempts, current_attempts, "current_month_bootstrap"


def profile_reuse_month_status(integration, reference=None):
    """Read-only budget and flexible per-UID policy shown on the integration card."""

    period_start, previous_attempts, live_baseline, baseline_source = _monthly_baseline(
        integration, reference
    )
    counter = ProfileReuseMonthlyCounter.objects.filter(
        integration_id=integration.pk, period_start=period_start
    ).first()
    baseline = live_baseline
    used = first_used = repeat_used = 0
    if counter:
        if baseline_source == "current_month_bootstrap":
            baseline = max(counter.baseline_attempts, live_baseline)
        used = counter.allocated_reuses
        first_used = counter.first_reuse_allocated
        repeat_used = counter.repeat_reuse_allocated
    target = _target_from_baseline(integration, baseline)
    first_target, repeat_target = _legacy_pool_targets(integration, target)
    return {
        "period": period_start.isoformat(),
        "previous_month_attempts": previous_attempts,
        "baseline_attempts": baseline,
        "baseline_source": baseline_source,
        "target_reuses": target,
        "used_reuses": used,
        "remaining_reuses": max(0, target - used),
        "first_reuse_used": first_used,
        "repeat_reuse_used": repeat_used,
        "first_reuse_target": first_target,
        "first_reuse_remaining": max(0, first_target - first_used),
        "repeat_reuse_target": repeat_target,
        "repeat_reuse_remaining": max(0, repeat_target - repeat_used),
        "first_delay_minutes": integration.profile_reuse_first_delay_minutes,
        "minimum_interval_minutes": integration.profile_reuse_min_interval_minutes,
        "max_uses_per_window": integration.profile_reuse_max_uses_per_window,
        "window_minutes": integration.profile_reuse_window_minutes,
    }


def _claim_month_slot(integration):
    period_start, _, live_baseline, baseline_source = _monthly_baseline(integration)
    with transaction.atomic():
        counter, created = ProfileReuseMonthlyCounter.objects.select_for_update().get_or_create(
            integration_id=integration.pk,
            period_start=period_start,
            defaults={"baseline_attempts": 0, "target_reuses": 0},
        )
        if created or baseline_source == "previous_month":
            counter.baseline_attempts = live_baseline
        elif live_baseline > counter.baseline_attempts:
            counter.baseline_attempts = live_baseline
        counter.target_reuses = _target_from_baseline(integration, counter.baseline_attempts)
        if counter.target_reuses <= 0 or counter.allocated_reuses >= counter.target_reuses:
            counter.save(update_fields=["baseline_attempts", "target_reuses", "updated_at"])
            return None
        counter.allocated_reuses += 1
        counter.save(update_fields=[
            "baseline_attempts", "target_reuses", "allocated_reuses", "updated_at",
        ])
        return counter.pk


def _release_month_slot(counter_id):
    if counter_id:
        ProfileReuseMonthlyCounter.objects.filter(
            pk=counter_id, allocated_reuses__gt=0
        ).update(allocated_reuses=F("allocated_reuses") - 1)


def _mark_pool(counter_id, pool):
    if not counter_id:
        return
    field = "first_reuse_allocated" if pool == FIRST_REUSE_POOL else "repeat_reuse_allocated"
    ProfileReuseMonthlyCounter.objects.filter(pk=counter_id).update(**{field: F(field) + 1})


def _normalized_gender(value):
    value = str(value or "").strip().lower()
    for canonical, aliases in GENDER_ALIASES.items():
        if value in aliases:
            return canonical
    return value


def _profile_signature(attempt, answers):
    _, dimensions, age, age_group, gender, _, _ = _question_snapshots(attempt, answers)
    normalized = {
        str(key): sorted({str(item).strip().lower() for item in values if str(item).strip()})
        for key, values in (dimensions or {}).items()
        if values
    }
    normalized_gender = _normalized_gender(gender)
    if normalized_gender:
        normalized["gender"] = [normalized_gender]
    if age is not None:
        normalized["age"] = [str(age)]
        normalized["age_group"] = [age_group]
    return {
        "country_code": str(attempt.survey.country_code or "").strip().upper(),
        "language_code": str(attempt.survey.language_code or "").strip().upper(),
        "age": age,
        "age_group": age_group,
        "gender": normalized_gender,
        "dimensions": normalized,
    }


def _candidate_dimensions(candidate, reference=None):
    """Return current provider-neutral values, ageing a stored DOB at read time."""

    dimensions = {
        str(key): sorted({str(item).strip().lower() for item in values if str(item).strip()})
        for key, values in (candidate.profile_dimensions or {}).items()
        if isinstance(values, (list, tuple, set)) and values
    }
    dimensions.setdefault("country", [str(candidate.country_code or candidate.country).lower()])
    dimensions.setdefault("language", [str(candidate.language_code or candidate.language).lower()])
    if candidate.respondent_gender:
        dimensions.setdefault("gender", [_normalized_gender(candidate.respondent_gender)])
    current_age = None
    for dob in dimensions.get("date_of_birth", []):
        current_age = _age_from_value(dob, reference or timezone.now())
        if current_age is not None:
            break
    if current_age is None:
        current_age = candidate.respondent_age
    if current_age is not None and 1 <= current_age <= OPEN_ENDED_AGE_MAX:
        dimensions["age"] = [str(current_age)]
        dimensions["age_group"] = [_age_group(current_age)]
    if candidate.respondent_ethnicity:
        dimensions.setdefault(
            "ethnicity", [_normalize_profile_value("ethnicity", candidate.respondent_ethnicity)]
        )
    if candidate.respondent_postal_code:
        dimensions.setdefault(
            "postal_code", [_normalize_profile_value("postal_code", candidate.respondent_postal_code)]
        )
    return {key: sorted({value for value in values if value}) for key, values in dimensions.items()}


def _matches_all_requirements(candidate, signature):
    """Require every mapped answer to match; missing data is never a match."""

    candidate_dimensions = _candidate_dimensions(candidate)
    required = signature["dimensions"]
    for key, required_values in required.items():
        # DOB is represented by its current age so birthdays advance naturally
        # without requiring two people to share an exact calendar birth date.
        if key == "date_of_birth" and required.get("age"):
            continue
        candidate_values = candidate_dimensions.get(key)
        if not candidate_values or set(candidate_values) != set(required_values):
            return False
    return True


def _candidate_age_groups(age):
    if age is None:
        return []
    # The vault keeps the registration-time age while DOB-based matching ages
    # the profile dynamically.  The configured timing fields are capped at two
    # years, so include the two preceding ages before the exact Python check.
    return list({
        group for value in (age, age - 1, age - 2) if (group := _age_group(value))
    })


def _reserve_vault_profile(attempt, signature, excluded_uids=None):
    """Lock and reserve the fairest fully matching vault row."""

    now = timezone.now()
    integration = attempt.survey.integration
    first_threshold = now - timedelta(minutes=int(integration.profile_reuse_first_delay_minutes))
    interval_threshold = now - timedelta(minutes=int(integration.profile_reuse_min_interval_minutes))
    client_code = str(integration.client.code or "").strip().lower()
    gender_values = GENDER_ALIASES.get(signature["gender"], (signature["gender"],))
    with transaction.atomic(using=DATABASE_ALIAS):
        candidates = (
            PrescreenerSubmission.objects.using(DATABASE_ALIAS)
            .select_for_update(skip_locked=True)
            .filter(
                source_client_code=client_code,
                country_code=signature["country_code"],
                respondent_gender__in=gender_values,
                submitted_at__lte=first_threshold,
            )
            .filter(Q(last_reused_at__isnull=True) | Q(last_reused_at__lte=interval_threshold))
            .exclude(uid=attempt.prescreener_uid)
        )
        age_groups = _candidate_age_groups(signature["age"])
        if age_groups:
            candidates = candidates.filter(respondent_age_group__in=age_groups)
        postal_values = signature["dimensions"].get("postal_code", [])
        if len(postal_values) == 1:
            candidates = candidates.filter(respondent_postal_code=postal_values[0])
        ethnicity_values = signature["dimensions"].get("ethnicity", [])
        if len(ethnicity_values) == 1:
            candidates = candidates.filter(respondent_ethnicity=ethnicity_values[0])
        if excluded_uids:
            candidates = candidates.exclude(uid__in=excluded_uids)
        rows = list(candidates.order_by(
            "usage_count", "last_reused_at", "submitted_at", "uid"
        )[:MAX_CANDIDATE_SCAN])
        if not rows:
            return None

        uids = [row.uid for row in rows]
        used_on_project = set(ProfileReuseProjectUsage.objects.filter(
            integration_id=integration.pk,
            survey_id=attempt.survey_id,
            reused_uid__in=uids,
        ).values_list("reused_uid", flat=True))
        original_or_reused = SurveyAttempt.objects.filter(
            survey_id=attempt.survey_id,
        ).filter(
            Q(prescreener_uid__in=uids) | Q(provider_profile_uid__in=uids)
        ).values_list("prescreener_uid", "provider_profile_uid")
        for registered_uid, provider_uid in original_or_reused:
            if registered_uid:
                used_on_project.add(registered_uid)
            if provider_uid:
                used_on_project.add(provider_uid)
        window_start = now - timedelta(minutes=int(integration.profile_reuse_window_minutes))
        recent_counts = dict(
            ProfileReuseEvent.objects.filter(
                integration_id=integration.pk,
                reused_uid__in=uids,
                created_at__gte=window_start,
            ).values("reused_uid").annotate(total=Count("id")).values_list("reused_uid", "total")
        )
        max_uses = int(integration.profile_reuse_max_uses_per_window)
        candidate = next((
            row for row in rows
            if row.uid not in used_on_project
            and recent_counts.get(row.uid, 0) < max_uses
            and _matches_all_requirements(row, signature)
        ), None)
        if candidate is None:
            return None
        previous_last_reused_at = candidate.last_reused_at
        PrescreenerSubmission.objects.using(DATABASE_ALIAS).filter(pk=candidate.pk).update(
            usage_count=F("usage_count") + 1,
            last_reused_at=now,
            respondent_age=signature["age"],
            respondent_age_group=signature["age_group"],
        )
        candidate.usage_count += 1
        candidate.last_reused_at = now
        candidate.respondent_age = signature["age"]
        candidate.respondent_age_group = signature["age_group"]
        transaction.on_commit(invalidate_vault_cache, using=DATABASE_ALIAS)
        return candidate, previous_last_reused_at


def _undo_vault_reservation(uid, previous_last_reused_at=None):
    if not uid:
        return
    PrescreenerSubmission.objects.using(DATABASE_ALIAS).filter(
        uid=uid, usage_count__gt=1
    ).update(
        usage_count=F("usage_count") - 1,
        last_reused_at=previous_last_reused_at,
    )
    invalidate_vault_cache()


def _locked_state(integration, candidate):
    """Create once, then row-lock a UID state for concurrent web requests."""

    event_query = ProfileReuseEvent.objects.filter(
        integration_id=integration.pk, reused_uid=candidate.uid
    )
    defaults = {
        "last_reused_at": event_query.order_by("-created_at").values_list(
            "created_at", flat=True
        ).first(),
        "total_reuses": event_query.count(),
    }
    try:
        # Keep the possible create race inside its own savepoint.  Without the
        # nested atomic block an IntegrityError can mark the caller's outer
        # transaction as broken before we acquire the row lock below.
        with transaction.atomic():
            ProfileReuseState.objects.get_or_create(
                integration_id=integration.pk, reused_uid=candidate.uid, defaults=defaults
            )
    except IntegrityError:
        pass
    return ProfileReuseState.objects.select_for_update().get(
        integration_id=integration.pk, reused_uid=candidate.uid
    )


def _commit_reuse(attempt, integration, candidate, signature, pool):
    now = timezone.now()
    interval_start = now - timedelta(minutes=int(integration.profile_reuse_min_interval_minutes))
    window_start = now - timedelta(minutes=int(integration.profile_reuse_window_minutes))
    with transaction.atomic():
        locked_attempt = SurveyAttempt.objects.select_for_update().get(pk=attempt.pk)
        if locked_attempt.provider_profile_uid:
            raise ReuseReservationConflict("Attempt already selected a profile.")
        state = _locked_state(integration, candidate)
        if state.last_reused_at and state.last_reused_at > interval_start:
            raise ReuseReservationConflict("UID minimum interval is still active.")
        if SurveyAttempt.objects.filter(survey_id=attempt.survey_id).exclude(
            pk=attempt.pk
        ).filter(
            Q(prescreener_uid=candidate.uid) | Q(provider_profile_uid=candidate.uid)
        ).exists():
            raise ReuseReservationConflict("UID was already used on this project.")
        try:
            ProfileReuseProjectUsage.objects.create(
                integration_id=integration.pk,
                survey_id=attempt.survey_id,
                reused_uid=candidate.uid,
                first_attempt_id=attempt.pk,
            )
        except IntegrityError as exc:
            raise ReuseReservationConflict("UID was already used on this project.") from exc
        recent_count = ProfileReuseEvent.objects.filter(
            integration_id=integration.pk,
            reused_uid=candidate.uid,
            created_at__gte=window_start,
        ).count()
        if recent_count >= int(integration.profile_reuse_max_uses_per_window):
            raise ReuseReservationConflict("UID rolling-window limit was reached.")
        locked_attempt.provider_profile_uid = candidate.uid
        locked_attempt.save(update_fields=["provider_profile_uid", "updated_at"])
        event = ProfileReuseEvent.objects.create(
            integration_id=integration.pk,
            survey_id=attempt.survey_id,
            attempt_id=attempt.pk,
            registered_uid=attempt.prescreener_uid,
            reused_rid=candidate.rid,
            reused_uid=candidate.uid,
            source_registered_at=candidate.submitted_at,
            source_usage_number=candidate.usage_count,
            reuse_pool=pool,
            country_code=signature["country_code"],
            age_group=signature["age_group"],
            gender=signature["gender"],
        )
        state.last_reused_at = now
        state.total_reuses = F("total_reuses") + 1
        state.save(update_fields=["last_reused_at", "total_reuses", "updated_at"])
    attempt.provider_profile_uid = candidate.uid
    return event


def maybe_assign_reusable_profile(attempt, answers):
    """Reuse a matching UID or safely leave this journey on its fresh UID.

    The journey RID/PID is always new. A provider-facing UID is reused only
    when client, country, all mapped answers, timing policy, rolling limit,
    monthly budget, and permanent same-project exclusion all pass.
    """

    integration = getattr(attempt.survey, "integration", None)
    if (
        not settings.PRESCREENER_VAULT_ENABLED
        or integration is None
        or not integration.profile_reuse_enabled
    ):
        return None
    existing = ProfileReuseEvent.objects.filter(attempt_id=attempt.pk).first()
    if existing:
        if attempt.provider_profile_uid != existing.reused_uid:
            SurveyAttempt.objects.filter(pk=attempt.pk).update(provider_profile_uid=existing.reused_uid)
            attempt.provider_profile_uid = existing.reused_uid
        return existing

    signature = _profile_signature(attempt, answers)
    if (
        not signature["country_code"]
        or signature["age_group"] not in AGE_GROUP_RANGES
        or signature["gender"] not in GENDER_ALIASES
        or signature["age_group"] not in (integration.profile_reuse_age_groups or [])
        or signature["gender"] not in (integration.profile_reuse_genders or [])
    ):
        return None

    counter_id = _claim_month_slot(integration)
    if not counter_id:
        return None
    excluded_uids = set()
    try:
        for _ in range(5):
            reservation = _reserve_vault_profile(attempt, signature, excluded_uids)
            if not reservation:
                _release_month_slot(counter_id)
                return None
            candidate, previous_last_reused_at = reservation
            pool = FIRST_REUSE_POOL if candidate.usage_count == 2 else RETURNING_REUSE_POOL
            try:
                event = _commit_reuse(attempt, integration, candidate, signature, pool)
            except (ReuseReservationConflict, IntegrityError):
                _undo_vault_reservation(candidate.uid, previous_last_reused_at)
                excluded_uids.add(candidate.uid)
                continue
            _mark_pool(counter_id, pool)
            return event
        _release_month_slot(counter_id)
        return None
    except Exception:
        _release_month_slot(counter_id)
        raise
