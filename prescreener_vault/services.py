import copy
import re
from datetime import date, datetime

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from surveys.survey_flow import ensure_attempt_prescreener_uid

from .constants import DATABASE_ALIAS
from .models import PrescreenerAnswer, PrescreenerAnswerValue, PrescreenerSubmission


class PrescreenerVaultError(RuntimeError):
    pass


class PrescreenerVaultDisabled(PrescreenerVaultError):
    pass


def operational_answer_value(answers):
    """Do not duplicate new answer payloads in the operational DB once enabled."""
    return {} if settings.PRESCREENER_VAULT_ENABLED else answers


def _clean_token(value) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _canonical_attribute(question_key, question_text, question_category="") -> str:
    source = " ".join(
        (_clean_token(question_key), _clean_token(question_text), _clean_token(question_category))
    )
    if re.search(r"\b(date_of_birth|birth_date|birthday|dob)\b", source):
        return "date_of_birth"
    if re.search(r"\b(age|years_old)\b", source):
        return "age"
    if re.search(r"\b(gender|sex)\b", source):
        return "gender"
    if re.search(r"\b(ethnicity|ethnic|race)\b", source):
        return "ethnicity"
    if re.search(r"\b(postal|postcode|zip|pincode|pin_code)\b", source):
        return "postal_code"
    if re.search(r"\b(country|nation)\b", source):
        return "country"
    if re.search(r"\b(language|locale)\b", source):
        return "language"
    return ""


def _normalize_profile_value(attribute: str, value) -> str:
    cleaned = str(value or "").strip()
    if attribute == "postal_code":
        return re.sub(r"[\s-]+", "", cleaned).upper()[:191]
    if attribute in {"gender", "ethnicity", "country", "language"}:
        return re.sub(r"\s+", " ", cleaned).strip().lower()[:191]
    return cleaned.lower()[:191]


def _age_group(age: int | None) -> str:
    if age is None:
        return ""
    if age < 18:
        return "under-18"
    for lower, upper in ((18, 24), (25, 34), (35, 44), (45, 54), (55, 64)):
        if lower <= age <= upper:
            return f"{lower}-{upper}"
    return "65+"


def _age_from_value(value, submitted_at) -> int | None:
    text = str(value or "").strip()
    try:
        age = int(text)
        return age if 0 <= age <= 125 else None
    except (TypeError, ValueError):
        pass
    try:
        born = datetime.strptime(text, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    reference = timezone.localtime(submitted_at).date() if submitted_at else date.today()
    age = reference.year - born.year - ((reference.month, reference.day) < (born.month, born.day))
    return age if 0 <= age <= 125 else None


def _question_snapshots(attempt, answers):
    question_ids = [int(key) for key in answers if str(key).isdigit()]
    questions = {
        str(question.pk): question
        for question in attempt.survey.targeting_questions.filter(pk__in=question_ids)
    }
    snapshots = []
    dimensions = {
        "country": [attempt.survey.country_code or attempt.survey.country] if (attempt.survey.country_code or attempt.survey.country) else [],
        "language": [attempt.survey.language_code or attempt.survey.language] if (attempt.survey.language_code or attempt.survey.language) else [],
    }
    age = None
    gender = ethnicity = postal_code = ""
    submitted_at = attempt.submitted_at or timezone.now()

    for position, (record_id, payload) in enumerate(answers.items(), start=1):
        payload = payload if isinstance(payload, dict) else {}
        question = questions.get(str(record_id))
        values = [str(value) for value in (payload.get("values") or [])]
        upstream_values = [str(value) for value in (payload.get("upstream_values") or [])]
        option_map = {}
        if question:
            for option in question.options or []:
                if isinstance(option, dict) and option.get("OptionId") is not None:
                    option_map[str(option["OptionId"])] = str(option.get("OptionText") or option["OptionId"])
        labels = [option_map.get(value, value) for value in values]
        question_key = str(payload.get("question_key") or (question.key if question else ""))
        question_text = str(payload.get("question_text") or (question.text if question else ""))
        question_type = str(question.question_type if question else "")
        question_category = str(question.category if question else "")
        canonical = _canonical_attribute(question_key, question_text, question_category)
        reusable_values = labels or values or upstream_values
        normalized_values = [_normalize_profile_value(canonical, value) for value in reusable_values]
        normalized_values = [value for value in normalized_values if value]
        if canonical and normalized_values:
            dimensions.setdefault(canonical, []).extend(normalized_values)
        if canonical in {"age", "date_of_birth"} and reusable_values:
            detected_age = _age_from_value(reusable_values[0], submitted_at)
            age = detected_age if detected_age is not None else age
        elif canonical == "gender" and normalized_values:
            gender = normalized_values[0]
        elif canonical == "ethnicity" and normalized_values:
            ethnicity = normalized_values[0]
        elif canonical == "postal_code" and normalized_values:
            postal_code = normalized_values[0]
        snapshots.append({
            "position": position,
            "question_record_id": str(record_id),
            "question_id": str(payload.get("question_id") or (question.question_id if question else "")),
            "question_key": question_key,
            "question_text": question_text,
            "question_type": question_type,
            "question_category": question_category,
            "canonical_attribute": canonical,
            "answer_values": values,
            "answer_labels": labels,
            "upstream_values": upstream_values,
            "normalized_values": normalized_values,
        })

    if age is not None:
        dimensions["age"] = [str(age)]
        dimensions["age_group"] = [_age_group(age)]
    dimensions = {key: list(dict.fromkeys(value for value in values if value)) for key, values in dimensions.items() if values}
    return snapshots, dimensions, age, _age_group(age), gender, ethnicity, postal_code


def capture_prescreener_submission(attempt, answers, *, submitted_at=None):
    """Persist one immutable, idempotent RID/UID submission in the vault."""
    if not settings.PRESCREENER_VAULT_ENABLED:
        raise PrescreenerVaultDisabled("The prescreener vault is not enabled.")
    if not answers:
        raise PrescreenerVaultError("Cannot capture an empty prescreener submission.")

    uid = ensure_attempt_prescreener_uid(attempt)
    submitted_at = submitted_at or timezone.now()
    attempt.submitted_at = submitted_at
    snapshots, dimensions, age, age_group, gender, ethnicity, postal_code = _question_snapshots(attempt, answers)
    survey = attempt.survey
    integration = survey.integration if survey.integration_id else None
    client = attempt.client if attempt.client_id else (survey.client if survey.client_id else None)
    platform_user = attempt.platform_user if attempt.platform_user_id else None
    raw_answers = copy.deepcopy(answers)

    try:
        with transaction.atomic(using=DATABASE_ALIAS):
            existing = PrescreenerSubmission.objects.using(DATABASE_ALIAS).filter(uid=uid).first()
            if existing:
                if existing.rid != attempt.rid:
                    raise PrescreenerVaultError("Vault UID is already mapped to a different RID.")
                if existing.raw_answers != raw_answers:
                    raise PrescreenerVaultError("This RID/UID already has a different immutable submission.")
                return existing, False
            rid_owner = PrescreenerSubmission.objects.using(DATABASE_ALIAS).filter(rid=attempt.rid).first()
            if rid_owner:
                raise PrescreenerVaultError("Vault RID is already mapped to a different UID.")

            submission = PrescreenerSubmission.objects.using(DATABASE_ALIAS).create(
                uid=uid,
                rid=attempt.rid,
                source_attempt_id=attempt.pk,
                platform_user_id=str(attempt.platform_user_id or attempt.user_id or ""),
                platform_user_email=(platform_user.email if platform_user else ""),
                platform_user_name=(platform_user.get_full_name() if platform_user else ""),
                provider_code=(integration.provider_code if integration else ""),
                client_id=(client.pk if client else None),
                client_name=(client.name if client else survey.company_name),
                survey_local_id=survey.local_id,
                survey_source_key=str(survey.source_key or survey.source_id or ""),
                survey_name=survey.name,
                country=survey.country,
                country_code=survey.country_code.upper(),
                language=survey.language,
                language_code=survey.language_code,
                respondent_age=age,
                respondent_age_group=age_group,
                respondent_gender=gender,
                respondent_ethnicity=ethnicity,
                respondent_postal_code=postal_code,
                profile_dimensions=dimensions,
                raw_answers=raw_answers,
                answer_count=len(snapshots),
                submitted_at=submitted_at,
            )
            for snapshot in snapshots:
                normalized_values = snapshot.pop("normalized_values")
                answer = PrescreenerAnswer.objects.using(DATABASE_ALIAS).create(
                    submission=submission,
                    **snapshot,
                )
                source_values = snapshot["answer_values"] or snapshot["upstream_values"]
                labels = snapshot["answer_labels"] or source_values
                for value_position, value in enumerate(source_values, start=1):
                    label = labels[value_position - 1] if value_position <= len(labels) else value
                    normalized = (
                        normalized_values[value_position - 1]
                        if value_position <= len(normalized_values)
                        else _normalize_profile_value(snapshot["canonical_attribute"], label)
                    )
                    PrescreenerAnswerValue.objects.using(DATABASE_ALIAS).create(
                        answer=answer,
                        position=value_position,
                        value=str(value),
                        label=str(label),
                        normalized_value=normalized,
                        canonical_attribute=snapshot["canonical_attribute"],
                        country_code=survey.country_code.upper(),
                    )
            return submission, True
    except PrescreenerVaultError:
        raise
    except Exception as exc:
        raise PrescreenerVaultError(f"Prescreener vault write failed: {exc}") from exc
