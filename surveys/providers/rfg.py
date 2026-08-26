import hashlib
import hmac
import json
import re
import time
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from surveys.age_rules import OPEN_ENDED_AGE_MAX, age_range_dict
from surveys.models import Survey, SurveyQuota, TargetingQuestion
from surveys.rfg_text import clean_rfg_display_text

from .base import (
    NormalizedSurvey,
    ProviderConfigurationError,
    ProviderError,
    SurveyProvider,
    environment_value,
)

RFG_TARGETING_ADAPTER_VERSION = 5


class ResearchForGoodProvider(SurveyProvider):
    code = "rfg"
    label = "Research For Good"
    default_base_url = "https://api.researchforgood.com/API"
    minimum_sync_interval_seconds = 60
    credential_fields = (("apid", "APID environment key"), ("secret", "Secret environment key"))
    explorer_commands = frozenset({
        "test/copy/1",
        "livealert/listDatapoints/1",
        "livealert/inventory/1",
        "livealert/targeting/1",
        "livealert/datapoint/1",
        "livealert/createLink/1",
        "livealert/duplicateCheck/1",
        "livealert/duplicateChecks/1",
        "livealert/log/1",
        "livealert/stats/1",
        "livealert/zipToGeo/1",
    })

    def __init__(self, integration, *, session=None, clock=None):
        super().__init__(integration, session=session or requests.Session())
        refs = integration.credential_env_keys or {}
        self.apid = environment_value(refs.get("apid"), "RFG apid")
        self.secret = environment_value(refs.get("secret"), "RFG secret")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", self.secret):
            raise ProviderConfigurationError("RFG secret must resolve to a 32-character hexadecimal value.")
        # The documentation links to /API/, but the live endpoint returns 404
        # for that path. RFG accepts signed POST requests at /API exactly.
        self.base_url = (integration.base_url or self.default_base_url).rstrip("/")
        parsed_base = urlsplit(self.base_url)
        if (
            parsed_base.scheme != "https"
            or parsed_base.hostname != "api.researchforgood.com"
            or parsed_base.path != "/API"
            or parsed_base.query
            or parsed_base.fragment
        ):
            raise ProviderConfigurationError("RFG base URL must be https://api.researchforgood.com/API.")
        self.timeout = int((integration.config or {}).get("timeout_seconds", 30))
        self.clock = clock or time.time

    def _command(self, payload: dict) -> dict:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        timestamp = str(int(self.clock()))
        signature = hmac.new(
            bytes.fromhex(self.secret),
            f"{timestamp}{body}".encode("utf-8"),
            hashlib.sha1,
        ).hexdigest()
        try:
            response = self.session.post(
                self.base_url,
                params={"apid": self.apid, "time": timestamp, "hash": signature},
                data=body.encode("utf-8"),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            # Requests exceptions often include the fully signed URL. Never copy
            # that URL (APID/hash) into API responses or persistent audit logs.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f" (HTTP {status})" if status else ""
            raise ProviderError(f"Research For Good request failed{suffix}.") from exc
        except ValueError as exc:
            raise ProviderError("Research For Good returned invalid JSON.") from exc
        if not isinstance(data, dict):
            raise ProviderError("Research For Good returned an invalid JSON response.")
        if data.get("result") != 0:
            raise ProviderError(str(data.get("message") or f"Research For Good result={data.get('result')}"))
        result = data.get("response") or {}
        if not isinstance(result, dict):
            raise ProviderError("Research For Good response payload must be an object.")
        return result

    def explorer_read(self, command: str, **parameters) -> dict:
        """Run an explicitly allow-listed RFG command for the admin explorer."""
        if command not in self.explorer_commands:
            raise ProviderConfigurationError("This RFG command is not available in the read-only explorer.")
        return self._command({"command": command, **parameters})

    def test_connection(self) -> dict:
        marker = f"quest-tool-{int(self.clock())}"
        response = self._command({"command": "test/copy/1", "marker": marker})
        return {"provider": self.code, "authenticated": True, "echo_received": response.get("marker") == marker}

    def inventory(self) -> list[dict]:
        config = self.integration.config or {}
        command = {"command": "livealert/inventory/1", "allowRecontacts": bool(config.get("allow_recontacts", False)), "type": 1}
        if config.get("country"):
            command["country"] = str(config["country"]).upper()
        if config.get("category") in {"B2B", "B2C"}:
            command["category"] = config["category"]
        projects = self._command(command).get("projects") or []
        if not isinstance(projects, list):
            raise ProviderError("Research For Good inventory projects must be a list.")
        return [row for row in projects if isinstance(row, dict) and row.get("rfg_id")]

    @staticmethod
    def _datetime(value):
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.replace(tzinfo=dt_timezone.utc) if parsed.tzinfo is None else parsed.astimezone(dt_timezone.utc)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _money(value):
        try:
            cleaned = re.sub(r"[^0-9.\-]", "", str(value or ""))
            return Decimal(cleaned) if cleaned else None
        except (InvalidOperation, ValueError):
            return None

    def normalize_inventory_item(self, payload, seen_at):
        desired = max(0, int(payload.get("desiredCompletes") or 0))
        completed = max(0, int(payload.get("currentCompletes") or 0))
        state = int(payload.get("state") or 0)
        modified = self._datetime(payload.get("lastModified"))
        phone = int(payload.get("phoneSupported") or 0)
        tablet = int(payload.get("tabletSupported") or 0)
        group_type = str(payload.get("category") or "").strip().upper()
        devices = ["Desktop"]
        if phone == 1:
            devices.append("Mobile")
        if tablet == 1:
            devices.append("Tablet")
        return NormalizedSurvey(
            source_key=str(payload["rfg_id"]),
            numeric_source_id=None,
            modified_at=modified,
            raw_data=payload,
            values={
                "company_name": self.integration.client.name,
                "name": str(payload.get("title") or ""),
                "status": Survey.Status.LIVE if state == 2 else Survey.Status.CLOSED,
                "sample_size": desired,
                "completes": completed,
                "remaining": max(0, desired - completed),
                "cpi": self._money(payload.get("cpi")),
                "loi": max(0, int(payload.get("estimatedLOI") or 0)),
                "incidence_rate": self._money(payload.get("estimatedIR")),
                "country": str(payload.get("country") or "").upper(),
                "country_code": str(payload.get("country") or "").upper(),
                "group_type": group_type,
                "buyer_id": str(payload.get("buyerId") or payload.get("buyer_id") or "").strip(),
                "survey_type": group_type if group_type in {"B2B", "B2C"} else group_type,
                "device_type": ", ".join(devices),
                "job_category": str(payload.get("category") or ""),
                "is_pii_required": bool(payload.get("collectsPII")),
                "is_recontact": bool(payload.get("isRecontact")),
                "source_modified_at": modified,
                "last_seen_at": seen_at,
                "raw_data": payload,
            },
        )

    def targeting(self, source_key):
        return self._command({"command": "livealert/targeting/1", "rfg_id": source_key, "zipsOnly": False})

    def datapoint(self, name):
        return self._command({"command": "livealert/datapoint/1", "name": name})

    def zip_to_geo(self, country_code, postal_code):
        """Resolve one postal code to RFG's derived geographic choice IDs."""

        return self._command({
            "command": "livealert/zipToGeo/1",
            "countryCode": str(country_code or "").upper(),
            "zip": re.sub(r"\s", "", str(postal_code or "").upper()),
        })

    @staticmethod
    def _localized_text(values, preferred_locale, fallback=""):
        """Return the best provider translation without exposing raw IDs."""

        if not isinstance(values, dict):
            return clean_rfg_display_text(fallback)
        preferred = str(preferred_locale or "").strip()
        candidates = [preferred, "en-US"]
        if preferred and "-" in preferred:
            language = preferred.split("-", 1)[0].lower()
            candidates.extend(
                key for key in values
                if str(key).lower().startswith(f"{language}-")
            )
        candidates.extend(
            key for key in values
            if key not in candidates and key != "disposition"
        )
        for key in candidates:
            value = values.get(key)
            if isinstance(value, str) and value.strip():
                return clean_rfg_display_text(value)
        return clean_rfg_display_text(fallback)

    @staticmethod
    def _range_label(value):
        minimum, maximum = value.get("min"), value.get("max")
        if minimum is None and maximum is None:
            return ""
        if minimum == maximum:
            return str(minimum)
        if minimum is None:
            return f"Up to {maximum}"
        if maximum is None:
            return f"{minimum}+"
        return f"{minimum}–{maximum}"

    def _readable_targeting_details(self, datapoints, metadata_for, locale):
        """Decode quota targeting, including derived datapoints hidden from UI."""

        details = []
        for datapoint in datapoints if isinstance(datapoints, list) else []:
            if not isinstance(datapoint, dict):
                continue
            name = str(datapoint.get("name") or datapoint.get("property") or "Targeting")
            metadata = metadata_for(name)
            question = metadata.get("question") if isinstance(metadata.get("question"), dict) else {}
            display_name = self._localized_text(
                question,
                locale,
                metadata.get("name") or name,
            )
            answers = metadata.get("answers") if isinstance(metadata.get("answers"), list) else []
            is_age = self._profile_dimension(name, metadata.get("property"), display_name) == "age"
            values = []
            for value in datapoint.get("values") or []:
                if is_age and (normalized_age := age_range_dict(value)):
                    label = self._range_label(normalized_age)
                elif not isinstance(value, dict):
                    label = str(value)
                else:
                    label = self._range_label(value)
                    choice = value.get("choice")
                    if not label and choice is not None:
                        try:
                            answer = answers[int(choice)]
                        except (IndexError, TypeError, ValueError):
                            answer = None
                        label = self._localized_text(answer, locale, f"Choice {choice}")
                    if not label:
                        free_value = value.get(
                            "value",
                            value.get(
                                "text",
                                value.get("freeList", value.get("freelist")),
                            ),
                        )
                        if free_value not in (None, ""):
                            label = str(free_value)
                if label and label not in values:
                    values.append(label)
            details.append({
                "name": display_name or clean_rfg_display_text(name),
                "values": values or ["Provider-defined segment"],
            })
        return details

    @classmethod
    def _geo_targeting_requirement(
        cls, datapoint, metadata, locale, *, scope="project"
    ):
        """Decode one derived-geo or postal-code rule for respondent display."""

        try:
            question_type = int(metadata.get("type") or 0)
        except (TypeError, ValueError):
            return None
        if question_type not in {13, 16, 18}:
            return None

        name = clean_rfg_display_text(
            datapoint.get("name") or metadata.get("name") or "Geographic area"
        )
        values = []
        choice_ids = []
        if question_type == 13:
            answers = metadata.get("answers") if isinstance(metadata.get("answers"), list) else []
            for value in datapoint.get("values") or []:
                if not isinstance(value, dict) or value.get("choice") is None:
                    continue
                choice = value.get("choice")
                choice_ids.append(str(choice))
                try:
                    answer = answers[int(choice)]
                except (IndexError, TypeError, ValueError):
                    answer = None
                label = cls._localized_text(answer, locale, f"Choice {choice}")
                if label and label not in values:
                    values.append(label)
        else:
            for value in datapoint.get("values") or []:
                if not isinstance(value, dict):
                    continue
                if question_type == 16:
                    free_list = value.get("freelist", value.get("freeList"))
                    if free_list not in (None, ""):
                        for item in str(free_list).split(","):
                            item = clean_rfg_display_text(item.strip().strip("\"'"))
                            if item and item not in values:
                                values.append(item)
                    for item in value.get("ziplist") or []:
                        item = str(item).strip()
                        if item and item not in values:
                            values.append(item)
                else:
                    zip_values = value.get("zip4list") or value.get("ziplist") or []
                    for item in zip_values:
                        item = str(item).strip()
                        if item and item not in values:
                            values.append(item)

        if not values:
            return None
        lowered_name = name.lower()
        uses_wildcards = bool(datapoint.get("usesWildcards"))
        if "dma" in lowered_name:
            dimension_label = "DMA"
        elif question_type == 18:
            dimension_label = "ZIP+4 codes"
        elif uses_wildcards:
            dimension_label = "ZIP codes/patterns"
        elif question_type == 16 or "zip" in lowered_name or "postal" in lowered_name:
            dimension_label = "ZIP codes"
        elif "region" in lowered_name:
            dimension_label = "region"
        elif "state" in lowered_name:
            dimension_label = "state"
        elif "county" in lowered_name:
            dimension_label = "county"
        elif "city" in lowered_name:
            dimension_label = "city"
        else:
            dimension_label = name
        label = (
            f"Open quota {dimension_label}"
            if scope == "quota"
            else f"Required {dimension_label}"
        )
        return {
            "name": name,
            "property": str(metadata.get("property") or name),
            "question_type": question_type,
            "label": label,
            "values": values,
            "choice_ids": choice_ids,
            "uses_wildcards": uses_wildcards,
            "scope": scope,
        }

    @staticmethod
    def _target_choice_ids(datapoint):
        return [
            str(item["choice"])
            for item in datapoint.get("values") or []
            if isinstance(item, dict) and item.get("choice") is not None
        ]

    @classmethod
    def _children_signature(cls, datapoint):
        normalized = []
        for value in datapoint.get("values") or []:
            if not isinstance(value, dict):
                continue
            try:
                gender = int(value.get("gender") or 0)
            except (TypeError, ValueError):
                gender = 0
            try:
                unit = int(value.get("unit") or 0)
            except (TypeError, ValueError):
                unit = 0
            normalized.append({
                "gender": gender,
                "min": value.get("min"),
                "max": value.get("max"),
                "unit": unit,
            })
        normalized.sort(key=lambda item: json.dumps(item, sort_keys=True, default=str))
        digest = hashlib.sha256(
            json.dumps(normalized, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        return f"RFG_CHILDREN_MATCH_{digest.upper()}"

    @staticmethod
    def _children_description(datapoint):
        descriptions = []
        for value in datapoint.get("values") or []:
            if not isinstance(value, dict):
                continue
            try:
                gender = int(value.get("gender") or 0)
            except (TypeError, ValueError):
                gender = 0
            try:
                unit = int(value.get("unit") or 0)
            except (TypeError, ValueError):
                unit = 0
            child_label = {1: "boy", 2: "girl"}.get(gender, "child")
            range_label = ResearchForGoodProvider._range_label(value)
            unit_label = "month" if unit == 1 else "year"
            if range_label:
                descriptions.append(f"{child_label} aged {range_label} {unit_label}s")
            else:
                descriptions.append(child_label)
        return "; or ".join(dict.fromkeys(descriptions)) or "the required child profile"

    def _normalized_targeting_datapoint(self, datapoint, metadata, localized_question=""):
        try:
            question_type = int(metadata.get("type") or 0)
        except (TypeError, ValueError):
            question_type = 0
        name = str(datapoint.get("name") or metadata.get("name") or "")
        property_name = str(metadata.get("property") or name)
        normalized_values = (
            datapoint.get("values") if isinstance(datapoint.get("values"), list) else []
        )
        if self._profile_dimension(name, property_name, localized_question) == "age" or question_type == 15:
            normalized_values = [
                normalized
                for value in normalized_values
                if (normalized := age_range_dict(value)) is not None
            ]
        normalized = {
            "name": name,
            "property": property_name,
            "type": question_type,
            "values": normalized_values,
            "usesWildcards": bool(datapoint.get("usesWildcards")),
            "profile_dimension": self._profile_dimension(
                name, property_name, localized_question
            ),
        }
        if question_type == 17:
            normalized["question_key"] = self._children_signature(datapoint)
        return normalized

    def create_link(self, source_key):
        return str(self._command({"command": "livealert/createLink/1", "rfg_id": source_key}).get("link") or "")

    @staticmethod
    def _question_id(value):
        return -int(hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:15], 16)

    @staticmethod
    def _profile_dimension(*values):
        combined = " ".join(
            re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
            for value in values
        )
        if re.search(r"\b(gender|sex)\b", combined):
            return "gender"
        if re.search(r"\b(date of birth|birthday|dob|age)\b", combined):
            return "age"
        if re.search(r"\b(postal code|postcode|zip code|zipcode|zip)\b", combined):
            return "postal"
        return ""

    def refresh_details(self, survey):
        """Replace RFG questions/quotas and persist locally evaluable rules."""

        targeting = self.targeting(survey.source_key)
        datapoints = targeting.get("datapoints") if isinstance(targeting.get("datapoints"), list) else []
        exclude_non_matching = bool(targeting.get("excludeNonMatching"))
        locale = str((self.integration.config or {}).get("locale", "en-US"))
        metadata_cache = {}

        def metadata_for(name):
            key = str(name or "")
            if key not in metadata_cache:
                metadata_cache[key] = self.datapoint(key)
            return metadata_cache[key]

        age_ranges = []
        gender_choices = []
        project_age_targeted = False
        project_gender_targeted = False
        questions = [
            TargetingQuestion(
                survey=survey,
                question_id=self._question_id("rfg-birthday"),
                key="RFG_BIRTHDAY",
                text="What is your date of birth?",
                question_type="date",
                category="Required profile",
                options=[],
                raw_data={
                    "adapter_version": RFG_TARGETING_ADAPTER_VERSION,
                    "mandatory_link_parameter": "birthday",
                    "targeting_age_ranges": age_ranges,
                    "respondent_input": "date_mask",
                },
            ),
            TargetingQuestion(
                survey=survey,
                question_id=self._question_id("rfg-gender"),
                key="RFG_GENDER",
                text="What is your gender?",
                question_type="single",
                category="Required profile",
                options=[
                    {"OptionId": "M", "OptionText": "Male"},
                    {"OptionId": "F", "OptionText": "Female"},
                ],
                raw_data={
                    "adapter_version": RFG_TARGETING_ADAPTER_VERSION,
                    "mandatory_link_parameter": "gender",
                    "targeting_choices": gender_choices,
                },
            ),
            TargetingQuestion(
                survey=survey,
                question_id=self._question_id("rfg-postal"),
                key="RFG_POSTAL_CODE",
                text="What is your postal code?",
                question_type="text",
                category="Required profile",
                options=[],
                raw_data={
                    "adapter_version": RFG_TARGETING_ADAPTER_VERSION,
                    "mandatory_link_parameter": "postalCode",
                    "country": survey.country_code,
                },
            ),
        ]
        questions_by_key = {question.key: question for question in questions}
        geo_requirements = []
        geo_requirement_keys = set()

        def add_geo_requirement(requirement):
            if not requirement:
                return
            key = (
                str(requirement.get("name") or "").lower(),
                tuple(str(value) for value in requirement.get("values") or []),
            )
            if key not in geo_requirement_keys:
                geo_requirement_keys.add(key)
                geo_requirements.append(requirement)

        def add_question_for(datapoint, metadata, *, scope):
            nonlocal age_ranges, gender_choices
            nonlocal project_age_targeted, project_gender_targeted
            try:
                question_type = int(metadata.get("type") or 0)
            except (TypeError, ValueError):
                question_type = 0
            question_texts = metadata.get("question") if isinstance(metadata.get("question"), dict) else {}
            localized_question = self._localized_text(
                question_texts,
                locale,
                datapoint.get("name") or metadata.get("name") or "Targeting question",
            )
            normalized = self._normalized_targeting_datapoint(
                datapoint, metadata, localized_question
            )
            allowed = {
                int(item["choice"])
                for item in datapoint.get("values") or []
                if isinstance(item, dict) and str(item.get("choice", "")).isdigit()
            }
            profile_dimension = self._profile_dimension(
                datapoint.get("name"),
                metadata.get("property"),
                localized_question,
            )
            if profile_dimension == "gender":
                if scope == "project" and allowed:
                    project_gender_targeted = True
                    gender_choices = sorted(allowed)
                    questions[1].raw_data["targeting_choices"] = gender_choices
                elif (
                    scope == "quota"
                    and exclude_non_matching
                    and allowed
                    and not project_gender_targeted
                ):
                    gender_choices = sorted(set(gender_choices).union(allowed))
                    questions[1].raw_data["targeting_choices"] = gender_choices
                return normalized
            if profile_dimension == "age" or question_type == 15:
                discovered_ranges = [
                    normalized_range
                    for item in datapoint.get("values") or []
                    if (normalized_range := age_range_dict(item)) is not None
                ]
                if scope == "project" and discovered_ranges:
                    project_age_targeted = True
                    age_ranges = discovered_ranges
                    questions[0].raw_data["targeting_age_ranges"] = age_ranges
                elif (
                    scope == "quota"
                    and exclude_non_matching
                    and discovered_ranges
                    and not project_age_targeted
                ):
                    for discovered in discovered_ranges:
                        if discovered not in age_ranges:
                            age_ranges.append(discovered)
                    questions[0].raw_data["targeting_age_ranges"] = age_ranges
                return normalized
            if question_type in {13, 16, 18} or profile_dimension == "postal":
                return normalized
            if question_type == 17:
                question_key = normalized["question_key"]
                description = self._children_description(datapoint)
                question = questions_by_key.get(question_key)
                if question is None:
                    question = TargetingQuestion(
                        survey=survey,
                        question_id=self._question_id(question_key),
                        key=question_key,
                        text=f"Do you have at least one {description}?",
                        question_type="single",
                        category="RFG targeting",
                        options=[
                            {"OptionId": "1", "OptionText": "Yes"},
                            {"OptionId": "0", "OptionText": "No"},
                        ],
                        raw_data={
                            "adapter_version": RFG_TARGETING_ADAPTER_VERSION,
                            "platform_only": True,
                            "children_targeting": normalized,
                            "project_required": scope == "project",
                            "targeting_note": (
                                f"Required child profile: {description}"
                                if scope == "project"
                                else f"Quota child profile: {description}"
                            ),
                        },
                    )
                    questions.append(question)
                    questions_by_key[question_key] = question
                elif scope == "project":
                    question.raw_data["project_required"] = True
                    question.raw_data["targeting_note"] = (
                        f"Required child profile: {description}"
                    )
                return normalized

            answers = metadata.get("answers") if isinstance(metadata.get("answers"), list) else []
            options = []
            for index, answer in enumerate(answers):
                if index == 0 or not isinstance(answer, dict) or int(answer.get("disposition") or 0) == 3:
                    continue
                options.append({
                    "OptionId": index,
                    "OptionText": self._localized_text(answer, locale, f"Choice {index}"),
                    "Disposition": int(answer.get("disposition") or 0),
                })
            outbound_property = normalized["property"]
            normalized["question_key"] = outbound_property
            question = questions_by_key.get(outbound_property)
            if question is None:
                question = TargetingQuestion(
                    survey=survey,
                    question_id=self._question_id(outbound_property),
                    key=outbound_property,
                    text=localized_question,
                    question_type="multi" if question_type == 1 else "single",
                    category="RFG targeting",
                    options=options,
                    raw_data={
                        "adapter_version": RFG_TARGETING_ADAPTER_VERSION,
                        "outbound_property": outbound_property,
                        "targeting": datapoint if scope == "project" else {},
                        "datapoint": metadata,
                        "targeting_choices": (
                            sorted(allowed)
                            if scope == "project" or exclude_non_matching
                            else []
                        ),
                        "project_targeting": scope == "project",
                        "quota_only": scope == "quota",
                    },
                )
                questions.append(question)
                questions_by_key[outbound_property] = question
            elif scope == "project":
                question.raw_data.update({
                    "targeting": datapoint,
                    "targeting_choices": sorted(allowed),
                    "project_targeting": True,
                    "quota_only": False,
                })
            elif not question.raw_data.get("project_targeting"):
                combined = set(question.raw_data.get("targeting_choices") or [])
                if exclude_non_matching:
                    combined.update(allowed)
                question.raw_data["targeting_choices"] = sorted(combined)
            return normalized

        for target in datapoints:
            if not isinstance(target, dict) or not target.get("name"):
                continue
            initial_dimension = self._profile_dimension(target.get("name"))
            if initial_dimension == "age":
                metadata = {"name": target["name"], "property": target["name"], "type": 15}
            elif initial_dimension == "gender":
                metadata = {"name": target["name"], "property": target["name"], "type": 0}
            else:
                metadata = metadata_for(target["name"])
            add_geo_requirement(self._geo_targeting_requirement(target, metadata, locale))
            add_question_for(target, metadata, scope="project")

        quotas = targeting.get("quotas") if isinstance(targeting.get("quotas"), list) else []
        quota_rows = []
        for index, quota in enumerate(quotas):
            if not isinstance(quota, dict):
                continue
            raw_remaining = quota.get("completesLeft", quota.get("startsLeft", 0))
            raw_target = quota.get("limit", quota.get("quotaTarget", quota.get("sampleSize")))
            raw_completed = quota.get("currentCompletes", quota.get("completes", quota.get("completed")))
            try:
                target = max(0, int(raw_target)) if raw_target is not None else 0
            except (TypeError, ValueError):
                target = 0
            try:
                completed = max(0, int(raw_completed)) if raw_completed is not None else 0
            except (TypeError, ValueError):
                completed = 0
            try:
                remaining = max(0, int(raw_remaining or 0))
            except (TypeError, ValueError):
                remaining = 0
            limit_type = str(quota.get("quotaLimitBy") or targeting.get("quotaLimitBy") or "completes")
            key = hashlib.sha256(json.dumps(quota, sort_keys=True, default=str).encode()).hexdigest()
            quota_datapoints = quota.get("datapoints") or []
            normalized_quota_datapoints = []
            for quota_datapoint in quota_datapoints:
                if not isinstance(quota_datapoint, dict) or not quota_datapoint.get("name"):
                    continue
                quota_dimension = self._profile_dimension(quota_datapoint.get("name"))
                if quota_dimension == "age":
                    quota_metadata = {
                        "name": quota_datapoint["name"],
                        "property": quota_datapoint["name"],
                        "type": 15,
                    }
                elif quota_dimension == "gender":
                    quota_metadata = {
                        "name": quota_datapoint["name"],
                        "property": quota_datapoint["name"],
                        "type": 0,
                    }
                else:
                    quota_metadata = metadata_for(quota_datapoint["name"])
                normalized_quota_datapoints.append(
                    add_question_for(quota_datapoint, quota_metadata, scope="quota")
                )
                if remaining > 0 and quota.get("quotaThrottle") != 1:
                    add_geo_requirement(self._geo_targeting_requirement(
                        quota_datapoint,
                        quota_metadata,
                        locale,
                        scope="quota",
                    ))
            readable_targeting = self._readable_targeting_details(
                quota_datapoints,
                metadata_for,
                locale,
            )
            quota_rows.append(SurveyQuota(
                survey=survey,
                source_key=key,
                title=f"Quota {index + 1}",
                name=f"{limit_type.replace('_', ' ').title()} quota",
                sample_size=target,
                completes=completed,
                remaining=remaining,
                status=("Throttled" if quota.get("quotaThrottle") == 1 else "Full" if remaining == 0 else "Open"),
                targeting={
                    "datapoints": quota_datapoints,
                    "normalized_datapoints": normalized_quota_datapoints,
                    "targeting_details": readable_targeting,
                },
                raw_data={
                    **quota,
                    "targeting_details": readable_targeting,
                    "normalized_datapoints": normalized_quota_datapoints,
                    "project_exclude_non_matching": exclude_non_matching,
                    "_target_known": raw_target is not None,
                    "_completed_known": raw_completed is not None,
                },
            ))
        if geo_requirements:
            questions[2].raw_data["targeting_requirements"] = geo_requirements
            questions[2].raw_data["targeting_note"] = " · ".join(
                f"{item['label']}: {', '.join(item['values'])}"
                for item in geo_requirements
            )
        link = survey.entry_link or self.create_link(survey.source_key)
        now = timezone.now()
        with transaction.atomic():
            survey.targeting_questions.all().delete()
            survey.quotas.all().delete()
            TargetingQuestion.objects.bulk_create(questions)
            SurveyQuota.objects.bulk_create(quota_rows)
            survey.entry_link = link
            survey.has_quota = bool(quota_rows)
            survey.targeting_synced_at = now
            survey.quota_synced_at = now
            survey.detail_synced_at = now
            survey.save(update_fields=[
                "entry_link", "has_quota", "targeting_synced_at", "quota_synced_at",
                "detail_synced_at", "updated_at",
            ])

    def duplicate_check(self, survey, attempt, ip_address, fingerprint="0"):
        fingerprint = str(fingerprint or "0").strip()
        if fingerprint != "0" and not re.fullmatch(r"[0-9a-fA-F]{32,128}", fingerprint):
            fingerprint = "0"
        response = self._command({"command": "livealert/duplicateCheck/1", "rfg_id": survey.source_key, "fingerprint": 0 if fingerprint == "0" else fingerprint, "rid": attempt.rid, "ip": ip_address or ""})
        return bool(response.get("isDuplicate"))

    @staticmethod
    def _answer_map(answers):
        return {str(item.get("question_key") or ""): item.get("upstream_values") or item.get("values") or [] for item in answers.values()}

    def build_outbound_url(self, survey, attempt, answers):
        values = self._answer_map(answers)
        age_or_birthday = (values.get("RFG_BIRTHDAY") or [""])[0]
        gender = (values.get("RFG_GENDER") or [""])[0]
        postal = re.sub(
            r"[\s-]", "", str((values.get("RFG_POSTAL_CODE") or [""])[0]).upper()
        )
        try:
            birthday = self._birthday_from_age_or_date(age_or_birthday)
        except (TypeError, ValueError) as exc:
            raise ProviderError(f"Enter a valid age between 1 and {OPEN_ENDED_AGE_MAX}.") from exc
        if str(gender).upper() not in {"M", "F", "1", "2"}:
            raise ProviderError("Select a valid gender for Research For Good.")
        if not postal:
            raise ProviderError("Postal code is required for Research For Good.")
        parts = urlsplit(survey.entry_link)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query.update({
            "rid": attempt.rid,
            "country": str(survey.country_code or "").upper(),
            "postalCode": postal,
            "gender": str(gender).upper(),
            "birthday": birthday,
            "integration": str(self.integration.pk),
            "code": survey.local_id,
        })
        for key, selected in values.items():
            if key.startswith("RFG_") or not selected:
                continue
            query[key] = ",".join(str(value) for value in selected)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    @staticmethod
    def _age_on(birthday, today=None):
        born = datetime.strptime(str(birthday), "%Y-%m-%d").date()
        today = today or date.today()
        return today.year - born.year - ((today.month, today.day) < (born.month, born.day))

    @staticmethod
    def _birthday_from_age_or_date(value, today=None):
        """Convert UI age into RFG's mandatory birthday query parameter.

        Legacy YYYY-MM-DD answers remain supported for attempts opened before the
        age-input UI was deployed.
        """
        raw_value = str(value or "").strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_value):
            age = ResearchForGoodProvider._age_on(raw_value, today=today)
            if not 1 <= age <= OPEN_ENDED_AGE_MAX:
                raise ValueError("age outside supported range")
            return raw_value
        age = int(raw_value)
        if not 1 <= age <= OPEN_ENDED_AGE_MAX:
            raise ValueError("age outside supported range")
        today = today or date.today()
        try:
            birthday = today.replace(year=today.year - age)
        except ValueError:
            birthday = today.replace(year=today.year - age, day=28)
        return birthday.isoformat()

    @classmethod
    def _age_from_age_or_date(cls, value, today=None):
        raw_value = str(value or "").strip()
        if raw_value.isdigit():
            age = int(raw_value)
            if not 1 <= age <= OPEN_ENDED_AGE_MAX:
                raise ValueError("age outside supported range")
            return age
        age = cls._age_on(raw_value, today=today)
        if not 1 <= age <= OPEN_ENDED_AGE_MAX:
            raise ValueError("age outside supported range")
        return age

    @staticmethod
    def _postal_is_valid(country, postal):
        compact = re.sub(r"[\s-]", "", str(postal or "").upper())
        patterns = {
            "AU": r"\d{4}", "ZA": r"\d{4}",
            "US": r"\d{5}", "EG": r"\d{5}", "FR": r"\d{5}", "DE": r"\d{5}",
            "ID": r"\d{5}", "IT": r"\d{5}", "MY": r"\d{5}", "MX": r"\d{5}",
            "SA": r"\d{5}", "ES": r"\d{5}", "TH": r"\d{5}", "TR": r"\d{5}",
            "CN": r"\d{6}", "KR": r"\d{6}", "RU": r"\d{6}", "SG": r"\d{6}", "VN": r"\d{6}",
            "BR": r"\d{8}", "CA": r"[A-Z]\d[A-Z]\d[A-Z]\d", "AR": r"[A-Z]\d{4}[A-Z]{3}",
            "GB": r"(?:[A-Z]{2}\d[A-Z]\d[A-Z]{2}|[A-Z]\d[A-Z]\d[A-Z]{2}|[A-Z]\d{2}[A-Z]{2}|[A-Z]\d{3}[A-Z]{2}|[A-Z]{2}\d{2}[A-Z]{2}|[A-Z]{2}\d{3}[A-Z])",
        }
        pattern = patterns.get(str(country or "").upper())
        return bool(compact and (pattern is None or re.fullmatch(pattern, compact)))

    @staticmethod
    def _postal_target_values(rule):
        """Extract direct postal and ZIP+4 values from one normalized rule."""

        question_type = int(rule.get("type", rule.get("question_type", 0)) or 0)
        extracted = []
        raw_values = rule.get("values") or []
        if raw_values and all(not isinstance(item, dict) for item in raw_values):
            extracted.extend(str(item) for item in raw_values)
        for value in raw_values:
            if not isinstance(value, dict):
                continue
            if question_type == 16:
                free_list = value.get("freelist", value.get("freeList"))
                if free_list not in (None, ""):
                    extracted.extend(str(free_list).split(","))
                extracted.extend(value.get("ziplist") or [])
            elif question_type == 18:
                extracted.extend(value.get("ziplist") or [])
                extracted.extend(value.get("zip4list") or [])
        cleaned = []
        for item in extracted:
            normalized = re.sub(
                r"[\s-]", "", str(item).strip().strip("\"'").upper()
            )
            if normalized and normalized not in cleaned:
                cleaned.append(normalized)
        return cleaned

    @classmethod
    def _postal_matches_rule(cls, postal, rule):
        """Match exact or provider-declared trailing-wildcard postal targets."""

        postal = re.sub(r"[\s-]", "", str(postal or "").upper())
        targets = cls._postal_target_values(rule)
        if not targets:
            return None
        uses_wildcards = bool(
            rule.get("usesWildcards", rule.get("uses_wildcards", False))
        )
        for target in targets:
            if uses_wildcards and target.endswith("*"):
                if postal.startswith(target[:-1]):
                    return True
            elif postal == target:
                return True
            elif int(rule.get("type", rule.get("question_type", 0)) or 0) == 18:
                if len(postal) == 5 and target.startswith(postal) and len(target) > 5:
                    return True
        return False

    def _geo_values_for_postal(self, country_code, postal):
        """Resolve/caches derived geo IDs; return ``None`` on unsupported/outage."""

        country_code = str(country_code or "").upper()
        if country_code not in {
            "US", "MX", "JP", "IT", "GB", "FR", "ES", "DE", "CA", "BR", "AU",
        }:
            return None
        compact_postal = re.sub(r"\s", "", str(postal or "").upper())
        digest = hashlib.sha256(
            f"{self.integration.pk}:{country_code}:{compact_postal}".encode("utf-8")
        ).hexdigest()
        cache_key = f"rfg:zip-to-geo:{digest}"
        cached = cache.get(cache_key)
        if isinstance(cached, dict) and isinstance(cached.get("values"), dict):
            return cached["values"]
        try:
            values = self.zip_to_geo(country_code, compact_postal)
        except ProviderError:
            return None
        if not isinstance(values, dict):
            return None
        timeout = max(
            60,
            int((self.integration.config or {}).get("geo_cache_seconds", 43200)),
        )
        cache.set(cache_key, {"values": values}, timeout=timeout)
        return values

    @staticmethod
    def _derived_geo_match(rule, geo_values):
        """Compare zipToGeo's datapoint choice ID with a type-13 rule."""

        if geo_values is None:
            return None
        name = str(rule.get("name") or "").casefold()
        property_name = str(rule.get("property") or "").casefold()
        actual = None
        for key, value in geo_values.items():
            normalized_key = str(key).casefold()
            if normalized_key in {name, property_name}:
                actual = value
                break
        if actual is None:
            return None
        allowed = {
            str(value)
            for value in (
                rule.get("choice_ids")
                or ResearchForGoodProvider._target_choice_ids(rule)
            )
        }
        return str(actual) in allowed if allowed else None

    @classmethod
    def _targeting_rule_match(
        cls, rule, values, *, age, gender_choice, postal, geo_values
    ):
        """Return True/False for a rule, or None when it cannot be evaluated."""

        try:
            question_type = int(rule.get("type", rule.get("question_type", 0)) or 0)
        except (TypeError, ValueError):
            return None
        dimension = str(rule.get("profile_dimension") or "")
        if dimension == "age" or question_type == 15:
            ranges = [
                normalized
                for item in rule.get("values") or []
                if (normalized := age_range_dict(item)) is not None
            ]
            if not ranges:
                return None
            return any(item["min"] <= age <= item["max"] for item in ranges)
        if dimension == "gender":
            allowed = set(cls._target_choice_ids(rule))
            return gender_choice in allowed if allowed else None
        if question_type == 13:
            return cls._derived_geo_match(rule, geo_values)
        if question_type in {16, 18} or dimension == "postal":
            return cls._postal_matches_rule(postal, rule)
        if question_type == 17:
            selected = values.get(str(rule.get("question_key") or ""), [])
            if not selected:
                return None
            return str(selected[0]) == "1"

        selected = {
            str(value)
            for value in values.get(
                str(rule.get("question_key") or rule.get("property") or rule.get("name") or ""),
                [],
            )
        }
        allowed = set(cls._target_choice_ids(rule))
        if not selected or not allowed:
            return None
        return bool(selected.intersection(allowed))

    def _validate_quota_matches(
        self, survey, values, *, age, gender_choice, postal, geo_values
    ):
        """Evaluate quota datapoints as AND rules and quotas as provider-defined sets."""

        quotas = list(survey.quotas.all())
        if not quotas:
            return True, ""
        exclude_non_matching = any(
            bool((quota.raw_data or {}).get("project_exclude_non_matching"))
            for quota in quotas
        )
        unknown_match = False
        open_match = False
        for quota in quotas:
            raw = quota.raw_data or {}
            if "normalized_datapoints" not in raw:
                unknown_match = True
                continue
            matches = []
            for rule in raw.get("normalized_datapoints") or []:
                if not isinstance(rule, dict):
                    matches.append(None)
                    continue
                matches.append(self._targeting_rule_match(
                    rule,
                    values,
                    age=age,
                    gender_choice=gender_choice,
                    postal=postal,
                    geo_values=geo_values,
                ))
            if any(match is False for match in matches):
                continue
            if any(match is None for match in matches):
                unknown_match = True
                continue
            is_closed = (
                str(raw.get("quotaThrottle") or "") == "1"
                or str(quota.status or "").lower() in {"full", "throttled"}
                or quota.remaining == 0
            )
            if is_closed:
                return False, "A matching RFG quota is currently full or throttled."
            open_match = True
        if exclude_non_matching and not open_match and not unknown_match:
            return False, "The answers do not match any currently open RFG quota."
        return True, ""

    def validate_prescreener(self, survey, answers):
        """Apply required-profile, geographic, and open-quota RFG rules."""

        values = self._answer_map(answers)
        age_or_birthday = (values.get("RFG_BIRTHDAY") or [""])[0]
        gender = str((values.get("RFG_GENDER") or [""])[0]).upper()
        postal = re.sub(r"[\s-]", "", str((values.get("RFG_POSTAL_CODE") or [""])[0]).upper())
        try:
            age = self._age_from_age_or_date(age_or_birthday)
        except (TypeError, ValueError):
            return False, "Please enter a valid age."
        if gender not in {"M", "F", "1", "2"}:
            return False, "Please select a valid gender."
        if not self._postal_is_valid(survey.country_code, postal):
            return False, f"The postal code is not valid for {survey.country_code or 'this market'}."

        strict_targeting = bool((self.integration.config or {}).get("enforce_local_targeting", True))
        if not strict_targeting:
            return True, ""

        questions = list(survey.targeting_questions.all())
        postal_question = next(
            (question for question in questions if question.key == "RFG_POSTAL_CODE"),
            None,
        )
        geo_requirements = (
            (postal_question.raw_data or {}).get("targeting_requirements") or []
            if postal_question
            else []
        )
        normalized_quota_rules = [
            rule
            for quota in survey.quotas.all()
            for rule in ((quota.raw_data or {}).get("normalized_datapoints") or [])
            if isinstance(rule, dict)
        ]
        needs_derived_geo = any(
            int(rule.get("type", rule.get("question_type", 0)) or 0) == 13
            for rule in [*geo_requirements, *normalized_quota_rules]
        )
        geo_values = (
            self._geo_values_for_postal(survey.country_code, postal)
            if needs_derived_geo
            else None
        )
        gender_choice = "1" if gender in {"M", "1"} else "2"

        for question in questions:
            raw = question.raw_data or {}
            selected = {str(value) for value in values.get(question.key, [])}
            if question.key == "RFG_BIRTHDAY":
                ranges = [
                    normalized
                    for item in raw.get("targeting_age_ranges") or []
                    if (normalized := age_range_dict(item)) is not None
                ]
                if ranges and not any(
                    item["min"] <= age <= item["max"] for item in ranges
                ):
                    return False, "The respondent's age does not match this survey's targeting requirements."
            elif question.key == "RFG_GENDER":
                allowed = {str(value) for value in raw.get("targeting_choices") or []}
                if allowed and gender_choice not in allowed:
                    return False, "The respondent's gender does not match this survey's targeting requirements."
            elif question.key == "RFG_POSTAL_CODE":
                for requirement in geo_requirements:
                    if not isinstance(requirement, dict) or requirement.get("scope") != "project":
                        continue
                    match = self._targeting_rule_match(
                        requirement,
                        values,
                        age=age,
                        gender_choice=gender_choice,
                        postal=postal,
                        geo_values=geo_values,
                    )
                    if match is False:
                        label = clean_rfg_display_text(
                            requirement.get("label") or requirement.get("name") or "geographic"
                        )
                        return False, f"The postal code does not match the survey's {label.lower()} targeting."
            elif question.key.startswith("RFG_CHILDREN_MATCH_"):
                if raw.get("project_required") and "1" not in selected:
                    return False, "The respondent's child profile does not match this survey's requirements."
            elif question.key.startswith("RFG_"):
                continue
            else:
                allowed = {str(value) for value in raw.get("targeting_choices") or []}
                profile_dimension = self._profile_dimension(question.key, question.text)
                if profile_dimension == "gender":
                    selected = selected or {gender_choice}
                elif profile_dimension == "age":
                    ranges = [
                        normalized
                        for item in raw.get("targeting_age_ranges") or []
                        if (normalized := age_range_dict(item)) is not None
                    ]
                    if ranges and not any(
                        item["min"] <= age <= item["max"] for item in ranges
                    ):
                        return False, "The respondent's age does not match this survey's targeting requirements."
                    continue
                elif profile_dimension == "postal":
                    continue
                if allowed and not selected.intersection(allowed):
                    display_text = clean_rfg_display_text(question.text or question.key)
                    return False, f"The answer to '{display_text}' does not match this survey's requirements."
                exclusive = {
                    str(option.get("OptionId")) for option in question.options
                    if int(option.get("Disposition") or 0) in {4, 5}
                }
                if len(selected) > 1 and selected.intersection(exclusive):
                    display_text = clean_rfg_display_text(question.text or question.key)
                    return False, f"Select the exclusive answer by itself for '{display_text}'."
        return self._validate_quota_matches(
            survey,
            values,
            age=age,
            gender_choice=gender_choice,
            postal=postal,
            geo_values=geo_values,
        )
