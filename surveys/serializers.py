from urllib.parse import urlencode
from django.conf import settings
from django.urls import reverse
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from accounts.access import effective_permission_codes, has_function_access
from vendors.access import vendor_scope_user_id
from vendors.models import VendorAPIKey
from vendors.security import generate_delivery_token
from vendors.services import organization_client_ids_for_user, survey_pricing_for_user

from .age_rules import normalize_age_range
from .models import (
    FinalIDUpload,
    Survey,
    SurveyAttempt,
    SurveyQuota,
    SyncRun,
    TargetingQuestion,
    TolunaReferenceQuestion,
)
from .outcomes import provider_outcome
from .report_pricing import viewer_attempt_cpi
from .rfg_text import clean_rfg_display_text, clean_rfg_options


def _display_source_identifier(survey):
    """Return the identifier shown in UI without changing provider routing keys."""
    if (
        survey.integration_id
        and survey.integration.provider_code == "toluna"
        and ":" in str(survey.source_key or "")
    ):
        return str(survey.source_key).split(":", 1)[0]
    return survey.source_identifier


class SurveyQuotaSerializer(serializers.ModelSerializer):
    display_name = serializers.SerializerMethodField(help_text="Readable quota title without exposing provider-internal quota IDs.")
    status = serializers.SerializerMethodField(help_text="Current quota state; RFG zero-remaining quotas are reported as Full.")
    target_known = serializers.SerializerMethodField(help_text="True only when the provider supplied a target total.")
    completed_known = serializers.SerializerMethodField(help_text="True only when the provider supplied a completed total.")
    limit_type = serializers.SerializerMethodField(help_text="Provider quota unit, such as Completes or Starts.")
    scope_label = serializers.SerializerMethodField(help_text="Human-readable overall or targeted quota scope.")
    targeting_details = serializers.SerializerMethodField(help_text="Quota targeting decoded into readable datapoint names and answer labels.")
    toluna_layers = serializers.SerializerMethodField(
        help_text=(
            "Toluna-only quota composition preserving every AND layer, OR subquota, "
            "provider-supplied subquota capacity and culture-specific answer label."
        )
    )

    class Meta:
        model = SurveyQuota
        fields = [
            "id", "quota_id", "title", "name", "display_name", "sample_size", "remaining", "completes",
            "clicks", "status", "targeting", "target_known", "completed_known", "limit_type",
            "scope_label", "targeting_details", "toluna_layers", "updated_at",
        ]

    @staticmethod
    def _is_rfg(obj) -> bool:
        return bool(obj.survey.integration_id and obj.survey.integration.provider_code == "rfg")

    @staticmethod
    def _is_toluna(obj) -> bool:
        return bool(obj.survey.integration_id and obj.survey.integration.provider_code == "toluna")

    @staticmethod
    def _toluna_question_rows(obj) -> list:
        raw = obj.raw_data or {}
        layers = raw.get("Layers")
        if not isinstance(layers, list):
            layers = (obj.targeting or {}).get("layers")
        rows = []
        for layer in layers if isinstance(layers, list) else []:
            if not isinstance(layer, dict):
                continue
            for subquota in layer.get("SubQuotas") or []:
                if not isinstance(subquota, dict):
                    continue
                rows.extend(
                    item for item in (subquota.get("QuestionsAndAnswers") or [])
                    if isinstance(item, dict)
                )
        return rows

    @staticmethod
    def _toluna_layers_from(obj) -> list:
        raw = obj.raw_data or {}
        layers = raw.get("Layers")
        if not isinstance(layers, list):
            layers = (obj.targeting or {}).get("layers")
        return layers if isinstance(layers, list) else []

    @staticmethod
    def _toluna_display_value(question_id: str, value) -> str:
        label = str(value or "").strip()
        if question_id == "1001538":
            if parsed := normalize_age_range(label):
                return f"{parsed[0]}\u2013{parsed[1]}"
        return label

    def _toluna_reference_map(self, obj) -> dict:
        survey = obj.survey
        culture = str(
            ((survey.raw_data or {}).get("_toluna") or {}).get("culture_code") or ""
        ).strip().lower().replace("_", "-")
        if not survey.integration_id or not culture:
            return {}
        cache_key = (survey.integration_id, culture)
        cache = getattr(self, "_toluna_reference_maps", None)
        if cache is None:
            cache = self._toluna_reference_maps = {}
        entry = cache.setdefault(cache_key, {"rows": {}, "loaded_ids": set()})

        quota_rows = [obj]
        prefetched = getattr(survey, "_prefetched_objects_cache", {}).get("quotas")
        if prefetched is not None:
            quota_rows = prefetched
        else:
            parent_instance = getattr(getattr(self, "parent", None), "instance", None)
            if parent_instance is not None:
                quota_rows = [
                    quota for quota in parent_instance
                    if getattr(quota, "survey_id", None) == survey.pk
                ] or quota_rows
        question_ids = {
            int(question_id)
            for quota in quota_rows
            for row in self._toluna_question_rows(quota)
            if (question_id := str(row.get("QuestionID") or "").strip()).isdigit()
        }
        missing_ids = question_ids - entry["loaded_ids"]
        if missing_ids:
            entry["rows"].update({
                str(row.question_id): row
                for row in TolunaReferenceQuestion.objects.filter(
                    integration_id=survey.integration_id,
                    culture_code=culture,
                    question_id__in=missing_ids,
                )
            })
            entry["loaded_ids"].update(missing_ids)
        return entry["rows"]

    def _toluna_targeting_question_map(self, obj) -> dict:
        survey = obj.survey
        cache = getattr(self, "_toluna_targeting_question_maps", None)
        if cache is None:
            cache = self._toluna_targeting_question_maps = {}
        if survey.pk not in cache:
            cache[survey.pk] = {
                str(row.question_id): row
                for row in survey.targeting_questions.all()
            }
        return cache[survey.pk]

    def _toluna_targeting_details(self, obj, rows) -> list:
        question_map = self._toluna_targeting_question_map(obj)
        reference_map = self._toluna_reference_map(obj)
        grouped = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            question_id = str(row.get("QuestionID") or "").strip()
            if not question_id:
                continue
            question = question_map.get(question_id)
            reference = reference_map.get(question_id)
            readable_name = (
                (reference.display_name or reference.internal_name) if reference else ""
            ) or (question.text if question else "") or "Toluna qualification"
            detail = grouped.setdefault(question_id, {
                "question_id": question_id,
                "name": str(readable_name),
                "values": [],
                "is_routable": False,
            })
            detail["is_routable"] = detail["is_routable"] or bool(row.get("IsRoutable"))

            option_labels = {}
            for option_source in (
                question.options if question else [],
                reference.options if reference else [],
            ):
                for option in option_source or []:
                    if not isinstance(option, dict) or option.get("OptionId") is None:
                        continue
                    option_id = str(option.get("OptionId"))
                    option_labels[option_id] = self._toluna_display_value(
                        question_id,
                        option.get("OptionText") or option.get("Translation") or option_id,
                    )

            values = row.get("AnswerValues") or []
            if isinstance(values, str):
                values = [part.strip() for part in values.split(",") if part.strip()]
            readable = [self._toluna_display_value(question_id, value) for value in values]
            readable.extend(
                option_labels.get(str(value), "Provider-defined answer")
                for value in (row.get("AnswerIDs") or [])
            )
            for value in readable:
                if value and value not in detail["values"]:
                    detail["values"].append(value)
        return list(grouped.values())

    @staticmethod
    def _optional_integer(value):
        if value in (None, "") or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def get_toluna_layers(self, obj) -> list:
        if not self._is_toluna(obj):
            return []
        result = []
        for layer_index, layer in enumerate(self._toluna_layers_from(obj), start=1):
            if not isinstance(layer, dict):
                continue
            subquota_rows = []
            layer_names = []
            for subquota_index, subquota in enumerate(layer.get("SubQuotas") or [], start=1):
                if not isinstance(subquota, dict):
                    continue
                details = self._toluna_targeting_details(
                    obj, subquota.get("QuestionsAndAnswers") or []
                )
                for detail in details:
                    if detail["name"] not in layer_names:
                        layer_names.append(detail["name"])

                target_known = (
                    "MaxTargetCompletes" in subquota
                    and subquota.get("MaxTargetCompletes") is not None
                )
                completed_known = (
                    "CurrentCompletes" in subquota
                    and subquota.get("CurrentCompletes") is not None
                )
                target = self._optional_integer(subquota.get("MaxTargetCompletes"))
                completed = self._optional_integer(subquota.get("CurrentCompletes"))
                remaining = (
                    max(target - completed, 0)
                    if target_known and completed_known and target is not None and completed is not None
                    else None
                )
                subquota_rows.append({
                    "position": subquota_index,
                    "subquota_id": subquota.get("SubQuotaID"),
                    "target_known": bool(target_known and target is not None),
                    "completed_known": bool(completed_known and completed is not None),
                    "remaining_known": remaining is not None,
                    "target": target,
                    "completed": completed,
                    "remaining": remaining,
                    "status": (
                        "Full" if remaining == 0 else "Open" if remaining is not None else "Unknown"
                    ),
                    "targeting_details": details,
                })
            result.append({
                "position": layer_index,
                "layer_id": layer.get("LayerID"),
                "name": " + ".join(layer_names) if layer_names else f"Layer {layer_index}",
                "match_rule": "any_subquota",
                "subquotas": subquota_rows,
            })
        return result

    def get_display_name(self, obj) -> str:
        if self._is_toluna(obj):
            return self.get_scope_label(obj)
        return obj.name or obj.title or "Survey quota"

    def get_status(self, obj) -> str:
        raw = obj.raw_data or {}
        if self._is_rfg(obj):
            if raw.get("quotaThrottle") == 1:
                return "Throttled"
            if obj.remaining <= 0:
                return "Full"
        return obj.status or "Open"

    def get_target_known(self, obj) -> bool:
        raw = obj.raw_data or {}
        if "_target_known" in raw:
            return bool(raw["_target_known"])
        if not self._is_rfg(obj):
            return True
        return obj.sample_size > 0 or any(
            raw.get(key) is not None for key in ("limit", "quotaTarget", "sampleSize")
        )

    def get_completed_known(self, obj) -> bool:
        raw = obj.raw_data or {}
        if "_completed_known" in raw:
            return bool(raw["_completed_known"])
        if not self._is_rfg(obj):
            return True
        return any(raw.get(key) is not None for key in ("currentCompletes", "completes", "completed"))

    def get_limit_type(self, obj) -> str:
        raw = obj.raw_data or {}
        return str(raw.get("quotaLimitBy") or "completes").replace("_", " ").strip().title()

    def _quota_datapoints(self, obj) -> list:
        raw = obj.raw_data or {}
        datapoints = raw.get("datapoints")
        if not isinstance(datapoints, list):
            datapoints = (obj.targeting or {}).get("datapoints")
        return datapoints if isinstance(datapoints, list) else []

    def get_scope_label(self, obj) -> str:
        if self._is_toluna(obj):
            return "Targeted respondent quota" if self._toluna_question_rows(obj) else "Overall survey quota"
        raw = obj.raw_data or {}
        quota_type = str(raw.get("SurveyQuotaType") or "").strip().lower()
        if quota_type:
            return "Overall survey quota" if quota_type == "total" else f"{quota_type.title()} quota"
        return "Targeted respondent quota" if self._quota_datapoints(obj) else "Overall survey quota"

    @staticmethod
    def _range_label(value, *, age=False) -> str:
        if age and (parsed := normalize_age_range(value)):
            return f"{parsed[0]}\u2013{parsed[1]}"
        minimum, maximum = value.get("min"), value.get("max")
        if minimum is None and maximum is None:
            return ""
        if minimum == maximum:
            return str(minimum)
        if minimum is None:
            return f"Up to {maximum}"
        if maximum is None:
            return f"{minimum}+"
        return f"{minimum}\u2013{maximum}"

    def get_targeting_details(self, obj) -> list:
        normalized = (obj.raw_data or {}).get("targeting_details")
        if isinstance(normalized, list):
            return normalized
        if self._is_toluna(obj):
            return self._toluna_targeting_details(obj, self._toluna_question_rows(obj))
        questions = list(obj.survey.targeting_questions.all())
        details = []
        for datapoint in self._quota_datapoints(obj):
            if not isinstance(datapoint, dict):
                continue
            name = str(datapoint.get("name") or datapoint.get("property") or "Targeting")
            question = next((item for item in questions if (
                str((item.raw_data or {}).get("targeting", {}).get("name") or "") == name
                or item.key == name
            )), None)
            option_labels = {
                str(option.get("OptionId")): clean_rfg_display_text(option.get("OptionText"))
                for option in (question.options if question else [])
                if isinstance(option, dict)
            }
            values = []
            for value in datapoint.get("values") or []:
                if not isinstance(value, dict):
                    values.append(str(value))
                    continue
                range_label = self._range_label(value, age=name.strip().lower() == "age")
                if range_label:
                    values.append(range_label)
                    continue
                choice = value.get("choice")
                if choice is not None:
                    if name.lower() == "gender":
                        values.append({"1": "Male", "2": "Female"}.get(str(choice), str(choice)))
                    else:
                        values.append(option_labels.get(str(choice), f"Choice {choice}"))
                    continue
                free_value = value.get("value", value.get("text", value.get("freeList")))
                if free_value not in (None, ""):
                    values.append(str(free_value))
            details.append({"name": clean_rfg_display_text(name), "values": values or ["Provider-defined segment"]})
        return details


class TargetingQuestionSerializer(serializers.ModelSerializer):
    text = serializers.SerializerMethodField()
    options = serializers.SerializerMethodField()
    targeting_note = serializers.SerializerMethodField(help_text="Readable RFG qualifying-answer or age rule for internal project details.")

    class Meta:
        model = TargetingQuestion
        fields = ["id", "question_id", "key", "text", "question_type", "category", "options", "targeting_note", "updated_at"]

    def get_text(self, obj) -> str:
        return clean_rfg_display_text(obj.text)

    def get_options(self, obj) -> list:
        options = clean_rfg_options(obj.options)
        normalized_key = str(obj.key or "").strip().upper()
        normalized_text = str(obj.text or "").strip().lower()
        if normalized_key == "AGE" or "your age" in normalized_text:
            normalized_options = []
            for option in options:
                normalized_option = (
                    option
                    if isinstance(option, dict)
                    else {"OptionId": option, "OptionText": str(option)}
                )
                parsed = normalize_age_range(normalized_option)
                normalized_options.append(
                    {
                        **normalized_option,
                        "ageStart": parsed[0],
                        "ageEnd": parsed[1],
                        "OptionText": f"{parsed[0]}\u2013{parsed[1]}",
                    }
                    if parsed else normalized_option
                )
            options = normalized_options
        raw = obj.raw_data or {}
        if "targeting_choices" not in raw:
            return options
        allowed = {str(value) for value in raw.get("targeting_choices") or []}
        if obj.key == "RFG_GENDER":
            allowed = {"M" if value == "1" else "F" if value == "2" else value for value in allowed}
        return [
            {**option, "Qualifies": str(option.get("OptionId")) in allowed}
            for option in options
        ]

    def get_targeting_note(self, obj) -> str:
        raw = obj.raw_data or {}
        provider_note = clean_rfg_display_text(raw.get("targeting_note") or "")
        if provider_note:
            return provider_note
        ranges = raw.get("targeting_age_ranges") or []
        if ranges:
            labels = [
                SurveyQuotaSerializer._range_label(item, age=True)
                for item in ranges if isinstance(item, dict)
            ]
            labels = [label for label in labels if label]
            if labels:
                return f"Qualifying age: {', '.join(labels)}"
        if "targeting_choices" in raw:
            qualifying = [
                str(option.get("OptionText")) for option in self.get_options(obj)
                if option.get("Qualifies") is True
            ]
            return (
                f"Qualifying answer{'s' if len(qualifying) != 1 else ''}: {', '.join(qualifying)}"
                if qualifying else "No fixed answer restriction was returned by RFG."
            )
        if obj.category == "Required profile":
            return "Required respondent profile field."
        return ""


class SurveyListSerializer(serializers.ModelSerializer):
    source_id = serializers.SerializerMethodField()
    display_source_id = serializers.SerializerMethodField(
        help_text="Project-page survey identifier with provider-only routing suffixes hidden."
    )
    survey_id = serializers.SerializerMethodField(help_text="External delivery identifier selected on the authenticated API key.")
    provider_code = serializers.SerializerMethodField()
    client_name = serializers.CharField(source="client.name", read_only=True, allow_null=True)
    display_company_name = serializers.SerializerMethodField()
    country_label = serializers.SerializerMethodField()
    progress_percent = serializers.SerializerMethodField()
    completes = serializers.SerializerMethodField()
    source_created_display = serializers.SerializerMethodField()
    source_modified_display = serializers.SerializerMethodField()
    start_link = serializers.SerializerMethodField()
    cpi = serializers.SerializerMethodField()
    cpi_cut_percent = serializers.SerializerMethodField()
    vendor_pricing = serializers.SerializerMethodField()

    class Meta:
        model = Survey
        fields = [
            "id", "local_id", "client", "client_name", "display_company_name", "source_id", "display_source_id", "survey_id", "provider_code", "company_name", "name", "status", "sample_size", "completes", "remaining",
            "starts", "cpi", "cpi_cut_percent", "vendor_pricing", "loi", "incidence_rate", "country", "country_code", "country_label",
            "language", "language_code", "group_type", "buyer_id", "survey_type", "device_type", "entry_link", "start_link", "has_quota",
            "source_created_at", "source_modified_at", "source_created_display", "source_modified_display",
            "detail_synced_at", "quota_synced_at", "targeting_synced_at", "created_at", "updated_at",
            "progress_percent",
        ]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        can_view_client_name = self.context.get("can_view_project_client_name")
        if can_view_client_name is None and request:
            can_view_client_name = has_function_access(
                request.user, "projects.column.client_name"
            )
        if request and not can_view_client_name:
            data["client_name"] = ""
            data["display_company_name"] = ""
            data["company_name"] = ""
        return data

    def get_country_label(self, obj) -> str:
        return " ".join(part for part in [obj.country_code, obj.language_code] if part) or obj.country

    @extend_schema_field({"oneOf": [{"type": "integer"}, {"type": "string"}]})
    def get_source_id(self, obj):
        return obj.source_identifier

    @extend_schema_field({"oneOf": [{"type": "integer"}, {"type": "string"}]})
    def get_display_source_id(self, obj):
        # Toluna's stable provider key remains SurveyID:WaveID in source_id and
        # survey_id. Only this presentation value hides the WaveID.
        return _display_source_identifier(obj)

    @extend_schema_field({"oneOf": [{"type": "integer"}, {"type": "string"}]})
    def get_survey_id(self, obj):
        request = self.context.get("request")
        api_key = getattr(request, "auth", None) if request else None
        if isinstance(api_key, VendorAPIKey) and api_key.survey_id_mode == VendorAPIKey.SurveyIdMode.PROJECT_ID:
            return obj.local_id
        return obj.source_identifier

    def get_provider_code(self, obj) -> str:
        return obj.integration.provider_code if obj.integration_id else getattr(obj.client, "provider_code", "innovatemr")

    def get_display_company_name(self, obj) -> str:
        request = self.context.get("request")
        client_scoped = self.context.get("project_client_scoped")
        if client_scoped is None and request:
            client_scoped = bool(
                vendor_scope_user_id(request.user)
                or organization_client_ids_for_user(request.user) is not None
            )
        if request and client_scoped and obj.client:
            return obj.client.name
        return obj.company_name

    def get_progress_percent(self, obj) -> float:
        completes = self.get_completes(obj)
        return round((completes / obj.sample_size) * 100, 1) if obj.sample_size else 0

    def get_completes(self, obj) -> int:
        """Return combined platform completes for every user on this survey."""

        return int(getattr(obj, "platform_completes", obj.completes) or 0)

    def get_source_created_display(self, obj) -> str | None:
        return obj.raw_data.get("createdDate") or None

    def get_source_modified_display(self, obj) -> str | None:
        return obj.raw_data.get("modifiedDate") or obj.raw_data.get("lastModified") or None

    def _pricing(self, obj):
        cache = getattr(self, "_pricing_cache", None)
        if cache is None:
            cache = self._pricing_cache = {}
        cache_key = obj.pk if obj.pk is not None else id(obj)
        if cache_key in cache:
            return cache[cache_key]
        request = self.context.get("request")
        cache[cache_key] = (
            survey_pricing_for_user(request.user, obj)
            if request and request.user.is_authenticated
            else (obj.cpi, None)
        )
        return cache[cache_key]

    @extend_schema_field(serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True))
    def get_cpi(self, obj):
        return self._pricing(obj)[0]

    @extend_schema_field(serializers.DecimalField(max_digits=5, decimal_places=2, allow_null=True))
    def get_cpi_cut_percent(self, obj):
        return self._pricing(obj)[1]

    def get_vendor_pricing(self, obj) -> bool:
        request = self.context.get("request")
        vendor_pricing = self.context.get("vendor_pricing")
        if vendor_pricing is None:
            vendor_pricing = bool(request and vendor_scope_user_id(request.user))
        return bool(vendor_pricing)

    def get_start_link(self, obj) -> str | None:
        """Return the shareable platform pre-screener URL, never the supplier entry URL."""
        request = self.context.get("request")
        can_copy_link = self.context.get("can_copy_survey_link")
        if can_copy_link is None and request and request.user.is_authenticated:
            can_copy_link = has_function_access(request.user, "survey_links.copy")
        if not request or not request.user.is_authenticated or not can_copy_link:
            return None
        # The start endpoint intentionally accepts only live inventory. Never
        # offer a link in Projects that the same backend will reject because
        # the provider closed the survey after its last inventory update.
        if obj.status != Survey.Status.LIVE:
            return None
        supports_lazy_entry_link = bool(
            obj.integration_id and obj.integration.provider_code in {"rfg", "toluna", "cint"}
        )
        if obj.integration_id and obj.integration.provider_code == "cint":
            redirect_state = obj.raw_data or {}
            if not (
                obj.entry_link
                and redirect_state.get("_cint_redirect_verified_at")
                and str(redirect_state.get("_cint_redirect_supplier_code") or "")
                == str(obj.integration.supplier_code or "")
            ):
                return None
        if not obj.entry_link and not supports_lazy_entry_link:
            return None
        api_key = getattr(request, "auth", None)
        external_delivery = isinstance(api_key, VendorAPIKey)
        exposed_survey_id = (
            obj.local_id
            if external_delivery and api_key.survey_id_mode == VendorAPIKey.SurveyIdMode.PROJECT_ID
            else obj.source_identifier
        )
        query_values = {
            "surveyId": exposed_survey_id,
            "supplierCode": settings.PUBLIC_SUPPLIER_CODE,
            "userId": request.user.pk,
            "code": obj.local_id,
        }
        if external_delivery:
            query_values["delivery"] = generate_delivery_token(api_key.pk, obj.pk)
        query = urlencode(query_values)
        path = f"{reverse('survey-start')}?{query}"
        return request.build_absolute_uri(path) if request else path


class SurveyDetailSerializer(SurveyListSerializer):
    quotas = SurveyQuotaSerializer(many=True, read_only=True)
    targeting_questions = TargetingQuestionSerializer(many=True, read_only=True)

    class Meta(SurveyListSerializer.Meta):
        fields = SurveyListSerializer.Meta.fields + [
            "test_entry_link", "job_category", "is_pii_required", "is_recontact", "quotas", "targeting_questions"
        ]


class SyncRunSerializer(serializers.ModelSerializer):
    duration_seconds = serializers.SerializerMethodField()

    class Meta:
        model = SyncRun
        fields = [
            "id", "integration", "started_at", "finished_at", "duration_seconds", "status", "fetched_full", "fetched_paged",
            "unique_surveys", "created", "updated", "unchanged", "closed", "detail_failures", "error",
        ]

    def get_duration_seconds(self, obj) -> float | None:
        return round((obj.finished_at - obj.started_at).total_seconds(), 3) if obj.finished_at else None


class SyncTriggerResponseSerializer(serializers.Serializer):
    run_id = serializers.IntegerField()
    status = serializers.ChoiceField(choices=SyncRun.Status.choices)
    created = serializers.IntegerField()
    updated = serializers.IntegerField()
    unchanged = serializers.IntegerField()
    closed = serializers.IntegerField()
    detail_failures = serializers.IntegerField()


class RFGCallbackResponseSerializer(serializers.Serializer):
    ok = serializers.BooleanField()
    rid = serializers.CharField(max_length=10)
    status = serializers.CharField()


class UserHitDeviceCountsSerializer(serializers.Serializer):
    total = serializers.IntegerField(min_value=0, allow_null=True)
    desktop = serializers.IntegerField(min_value=0, allow_null=True)
    mobile = serializers.IntegerField(min_value=0, allow_null=True)
    tablet = serializers.IntegerField(min_value=0, allow_null=True)
    unclassified = serializers.IntegerField(min_value=0, allow_null=True)


class UserHitRowSerializer(serializers.Serializer):
    user_id = serializers.IntegerField()
    user_name = serializers.CharField()
    username = serializers.CharField()
    user_email = serializers.EmailField(allow_blank=True)
    branch = serializers.CharField(allow_blank=True)
    sub_branch = serializers.CharField(allow_blank=True)
    shift = serializers.CharField(allow_blank=True)
    date = serializers.DateField()
    hits = UserHitDeviceCountsSerializer()
    completes = UserHitDeviceCountsSerializer()


class UserHitSummarySerializer(serializers.Serializer):
    hits = UserHitDeviceCountsSerializer()
    completes = UserHitDeviceCountsSerializer()
    active_users = serializers.IntegerField(min_value=0, allow_null=True)
    days = serializers.IntegerField(min_value=0)
    conversion_rate = serializers.FloatField(min_value=0, allow_null=True)
    incidence_rate = serializers.FloatField(min_value=0, allow_null=True)


class UserHitsResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField(min_value=0)
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    results = UserHitRowSerializer(many=True)
    summary = UserHitSummarySerializer()


class DashboardSummarySerializer(serializers.Serializer):
    hits = serializers.IntegerField(min_value=0, allow_null=True)
    completes = serializers.IntegerField(min_value=0, allow_null=True)
    conversion_rate = serializers.FloatField(min_value=0, allow_null=True)
    incidence_rate = serializers.FloatField(min_value=0, allow_null=True)
    active_users = serializers.IntegerField(min_value=0, allow_null=True)
    average_loi_seconds = serializers.IntegerField(min_value=0, allow_null=True)
    revenue = serializers.DecimalField(max_digits=18, decimal_places=2, allow_null=True)
    average_cpi = serializers.DecimalField(max_digits=18, decimal_places=2, allow_null=True)
    rpc = serializers.DecimalField(max_digits=18, decimal_places=2, allow_null=True)
    revenue_currency = serializers.CharField(allow_null=True)


class DashboardRangeSerializer(serializers.Serializer):
    key = serializers.CharField()
    label = serializers.CharField()
    bucket_label = serializers.CharField()
    start = serializers.DateTimeField()
    end = serializers.DateTimeField()
    financial_year = serializers.IntegerField(allow_null=True)


class DashboardComparisonMetricsSerializer(serializers.Serializer):
    hits = serializers.FloatField(allow_null=True)
    completes = serializers.FloatField(allow_null=True)
    conversion_rate = serializers.FloatField(allow_null=True)
    incidence_rate = serializers.FloatField(allow_null=True)
    active_users = serializers.FloatField(allow_null=True)
    average_loi_seconds = serializers.FloatField(allow_null=True)
    revenue = serializers.FloatField(allow_null=True)
    average_cpi = serializers.FloatField(allow_null=True)
    rpc = serializers.FloatField(allow_null=True)


class DashboardComparisonSerializer(serializers.Serializer):
    label = serializers.CharField()
    values = DashboardComparisonMetricsSerializer()
    deltas = DashboardComparisonMetricsSerializer()


class DashboardFinancialYearSerializer(serializers.Serializer):
    start_year = serializers.IntegerField()
    value = serializers.CharField()
    label = serializers.CharField()


class DashboardPerformancePointSerializer(serializers.Serializer):
    key = serializers.CharField()
    label = serializers.CharField()
    short_label = serializers.CharField()
    hits = serializers.IntegerField(min_value=0)
    completes = serializers.IntegerField(min_value=0)
    conversion_rate = serializers.FloatField(min_value=0)
    incidence_rate = serializers.FloatField(min_value=0)
    revenue = serializers.DecimalField(max_digits=18, decimal_places=2, allow_null=True)
    average_cpi = serializers.DecimalField(max_digits=18, decimal_places=2, allow_null=True)
    rpc = serializers.DecimalField(max_digits=18, decimal_places=2, allow_null=True)


class DashboardClientShareSerializer(serializers.Serializer):
    client_id = serializers.IntegerField(allow_null=True)
    name = serializers.CharField()
    hits = serializers.IntegerField(min_value=0)
    completes = serializers.IntegerField(min_value=0)
    share_percent = serializers.FloatField(min_value=0, max_value=100)
    conversion_rate = serializers.FloatField(min_value=0)
    revenue = serializers.DecimalField(max_digits=18, decimal_places=2, allow_null=True)


class DashboardStatusBreakdownSerializer(serializers.Serializer):
    initiated = serializers.IntegerField(min_value=0)
    completed = serializers.IntegerField(min_value=0)
    terminated = serializers.IntegerField(min_value=0)
    quota = serializers.IntegerField(min_value=0)
    security = serializers.IntegerField(min_value=0)


class DashboardDeviceBreakdownSerializer(serializers.Serializer):
    desktop = serializers.IntegerField(min_value=0)
    mobile = serializers.IntegerField(min_value=0)
    tablet = serializers.IntegerField(min_value=0)
    unclassified = serializers.IntegerField(min_value=0)


class DashboardDeviceMetricSerializer(serializers.Serializer):
    hits = serializers.IntegerField(min_value=0)
    completes = serializers.IntegerField(min_value=0)
    conversion_rate = serializers.FloatField(min_value=0)


class DashboardDevicePerformanceSerializer(serializers.Serializer):
    desktop = DashboardDeviceMetricSerializer()
    mobile = DashboardDeviceMetricSerializer()
    tablet = DashboardDeviceMetricSerializer()
    unclassified = DashboardDeviceMetricSerializer()


class DashboardTopUserSerializer(serializers.Serializer):
    user_id = serializers.IntegerField()
    name = serializers.CharField()
    hits = serializers.IntegerField(min_value=0)
    completes = serializers.IntegerField(min_value=0)
    conversion_rate = serializers.FloatField(min_value=0)
    contribution_percent = serializers.FloatField(min_value=0)
    revenue = serializers.DecimalField(max_digits=18, decimal_places=2, allow_null=True)


class DashboardGraphClientSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()


class DashboardGraphSeriesSerializer(serializers.Serializer):
    range = DashboardRangeSerializer()
    client_id = serializers.IntegerField(allow_null=True)
    points = DashboardPerformancePointSerializer(many=True)


class DashboardRecentActivitySerializer(serializers.Serializer):
    rid = serializers.CharField()
    user_name = serializers.CharField()
    project_id = serializers.CharField()
    client_name = serializers.CharField()
    status = serializers.CharField()
    status_label = serializers.CharField()
    initiated_at = serializers.DateTimeField()


class DashboardResponseSerializer(serializers.Serializer):
    range = DashboardRangeSerializer()
    summary = DashboardSummarySerializer()
    comparison = DashboardComparisonSerializer(allow_null=True)
    financial_years = DashboardFinancialYearSerializer(many=True)
    traffic_chart = DashboardGraphSeriesSerializer(allow_null=True)
    finance_chart = DashboardGraphSeriesSerializer(allow_null=True)
    graph_clients = DashboardGraphClientSerializer(many=True)
    client_distribution = DashboardClientShareSerializer(many=True, allow_null=True)
    status_breakdown = DashboardStatusBreakdownSerializer(allow_null=True)
    device_breakdown = DashboardDeviceBreakdownSerializer(allow_null=True)
    device_performance = DashboardDevicePerformanceSerializer(allow_null=True)
    top_users = DashboardTopUserSerializer(many=True, allow_null=True)
    generated_at = serializers.DateTimeField()


class SurveyAttemptSerializer(serializers.ModelSerializer):
    prescreener_uid = serializers.SerializerMethodField()
    registered_profile_uid = serializers.CharField(source="prescreener_uid", read_only=True)
    profile_was_reused = serializers.SerializerMethodField()
    survey_local_id = serializers.CharField(source="survey.local_id", read_only=True)
    survey_source_id = serializers.SerializerMethodField()
    survey_name = serializers.CharField(source="survey.name", read_only=True)
    company_name = serializers.CharField(source="survey.company_name", read_only=True)
    country = serializers.CharField(source="survey.country", read_only=True)
    country_code = serializers.CharField(source="survey.country_code", read_only=True)
    language_code = serializers.CharField(source="survey.language_code", read_only=True)
    user_name = serializers.SerializerMethodField()
    username = serializers.CharField(source="platform_user.username", read_only=True, allow_null=True)
    user_email = serializers.EmailField(source="platform_user.email", read_only=True, allow_null=True)
    status_label = serializers.SerializerMethodField()
    final_status = serializers.SerializerMethodField()
    final_status_label = serializers.SerializerMethodField()
    final_status_month = serializers.SerializerMethodField()
    entry_ip = serializers.IPAddressField(source="initiation_ip", read_only=True, allow_null=True)
    exit_ip = serializers.IPAddressField(source="callback_ip", read_only=True, allow_null=True)
    client_name = serializers.SerializerMethodField()
    buyer_id = serializers.CharField(source="survey.buyer_id", read_only=True)
    vendor_name = serializers.SerializerMethodField()
    supplier = serializers.IntegerField(source="vendor_id", read_only=True, allow_null=True)
    supplier_name = serializers.SerializerMethodField()
    source_cpi_snapshot = serializers.SerializerMethodField()
    termination_reason = serializers.SerializerMethodField()
    termination_category = serializers.SerializerMethodField()

    class Meta:
        model = SurveyAttempt
        fields = [
            "rid", "prescreener_uid", "registered_profile_uid", "profile_was_reused", "survey_local_id", "survey_source_id", "survey_name", "company_name", "country", "country_code",
            "language_code", "platform_user", "user_id", "user_name", "username", "user_email", "supplier",
            "supplier_name", "vendor", "vendor_name", "client", "client_name", "client_allocation", "survey_allocation", "supplier_code",
            "buyer_id", "source_cpi_snapshot", "cpi_snapshot_source", "cpi_cut_percent_snapshot", "payable_cpi_snapshot", "cpi_currency_snapshot",
            "status_label", "final_status", "final_status_label", "final_status_month",
            "termination_reason", "termination_category",
            "status", "initiated_at", "submitted_at", "redirected_at", "callback_at", "last_callback_at",
            "loi_seconds", "entry_ip", "exit_ip", "initiation_ip", "callback_ip", "entry_user_agent",
            "exit_user_agent", "entry_browser", "exit_browser", "entry_device", "exit_device", "entry_os",
            "exit_os", "entry_referrer", "entry_accept_language", "entry_client_data", "exit_client_data",
            "status_source", "upstream_checked_at", "upstream_transaction_data", "answers", "outbound_url", "callback_count",
            "is_verified", "created_at", "updated_at",
        ]

    def get_user_name(self, obj) -> str:
        if not obj.platform_user:
            return "Deleted user"
        return obj.platform_user.get_full_name() or obj.platform_user.username

    def get_prescreener_uid(self, obj) -> str:
        return obj.provider_profile_uid or obj.prescreener_uid or ""

    def get_profile_was_reused(self, obj) -> bool:
        return bool(obj.provider_profile_uid)

    def get_survey_source_id(self, obj) -> str:
        return str(_display_source_identifier(obj.survey))

    def get_client_name(self, obj) -> str:
        client = obj.client or obj.survey.client
        return client.name if client else obj.survey.company_name

    def get_vendor_name(self, obj) -> str | None:
        if not obj.vendor:
            return None
        return obj.vendor.get_full_name() or obj.vendor.username

    def get_supplier_name(self, obj) -> str | None:
        return self.get_vendor_name(obj)

    @extend_schema_field(serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True))
    def get_source_cpi_snapshot(self, obj):
        request = self.context.get("request")
        return viewer_attempt_cpi(obj, request.user) if request else obj.source_cpi_snapshot

    def get_status_label(self, obj) -> str:
        final_status = getattr(obj, "final_id_status", None)
        if final_status is not None:
            return (
                "Client Accepted"
                if final_status.status == FinalIDUpload.Decision.ACCEPTED
                else "Client Rejected"
            )
        if obj.status in {SurveyAttempt.Status.INITIATED, SurveyAttempt.Status.REDIRECTED}:
            return "Initiated"
        return obj.get_status_display()

    def get_final_status(self, obj) -> str:
        final_status = getattr(obj, "final_id_status", None)
        return final_status.status if final_status else ""

    def get_final_status_label(self, obj) -> str:
        value = self.get_final_status(obj)
        return value.title() if value else ""

    def get_final_status_month(self, obj):
        final_status = getattr(obj, "final_id_status", None)
        return final_status.accounting_month if final_status else None

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        if request and "studies.column.final_status" not in effective_permission_codes(request.user):
            for name in ("final_status", "final_status_label", "final_status_month"):
                fields.pop(name, None)
        return fields

    def get_termination_reason(self, obj) -> str:
        if obj.status not in {
            SurveyAttempt.Status.TERMINATED,
            SurveyAttempt.Status.OVER_QUOTA,
            SurveyAttempt.Status.QUALITY_TERMINATED,
            SurveyAttempt.Status.SURVEY_NOT_AVAILABLE,
            SurveyAttempt.Status.NO_SURVEYS,
            SurveyAttempt.Status.NO_COOKIES,
            SurveyAttempt.Status.MAX_SURVEYS_REACHED,
            SurveyAttempt.Status.NOT_QUALIFIED,
            SurveyAttempt.Status.SURVEY_TAKEN,
        }:
            return ""
        return provider_outcome(obj).get("reason", "")

    def get_termination_category(self, obj) -> str:
        if obj.status not in {
            SurveyAttempt.Status.TERMINATED,
            SurveyAttempt.Status.OVER_QUOTA,
            SurveyAttempt.Status.QUALITY_TERMINATED,
            SurveyAttempt.Status.SURVEY_NOT_AVAILABLE,
            SurveyAttempt.Status.NO_SURVEYS,
            SurveyAttempt.Status.NO_COOKIES,
            SurveyAttempt.Status.MAX_SURVEYS_REACHED,
            SurveyAttempt.Status.NOT_QUALIFIED,
            SurveyAttempt.Status.SURVEY_TAKEN,
        }:
            return ""
        return provider_outcome(obj).get("category", "")


class SurveyAttemptCompletedDeviceSummarySerializer(serializers.Serializer):
    desktop = serializers.IntegerField(allow_null=True)
    mobile = serializers.IntegerField(allow_null=True)
    tablet = serializers.IntegerField(allow_null=True)
    unclassified = serializers.IntegerField()


class SurveyAttemptSummarySerializer(serializers.Serializer):
    total = serializers.IntegerField(allow_null=True)
    initiated = serializers.IntegerField(allow_null=True)
    completed = serializers.IntegerField(allow_null=True)
    terminated = serializers.IntegerField(allow_null=True)
    over_quota = serializers.IntegerField(allow_null=True)
    security_terminated = serializers.IntegerField(allow_null=True)
    conversion_rate = serializers.FloatField(allow_null=True)
    incidence_rate = serializers.FloatField(allow_null=True)
    total_revenue = serializers.DecimalField(max_digits=18, decimal_places=2, allow_null=True)
    invoiced_revenue = serializers.DecimalField(max_digits=18, decimal_places=2, allow_null=True)
    revenue_currency = serializers.CharField(allow_null=True)
    completed_devices = SurveyAttemptCompletedDeviceSummarySerializer()


class SurveyAttemptListResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    results = SurveyAttemptSerializer(many=True)
    summary = SurveyAttemptSummarySerializer()
