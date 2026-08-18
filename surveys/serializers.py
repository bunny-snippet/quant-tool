from urllib.parse import urlencode
from django.conf import settings
from django.urls import reverse
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from accounts.access import has_function_access
from vendors.access import vendor_scope_user_id
from vendors.services import organization_client_ids_for_user, survey_pricing_for_user

from .models import Survey, SurveyAttempt, SurveyQuota, SyncRun, TargetingQuestion
from .outcomes import provider_outcome
from .report_pricing import viewer_attempt_cpi
from .rfg_text import clean_rfg_display_text, clean_rfg_options


class SurveyQuotaSerializer(serializers.ModelSerializer):
    display_name = serializers.SerializerMethodField(help_text="Readable quota title without exposing provider-internal quota IDs.")
    status = serializers.SerializerMethodField(help_text="Current quota state; RFG zero-remaining quotas are reported as Full.")
    target_known = serializers.SerializerMethodField(help_text="True only when the provider supplied a target total.")
    completed_known = serializers.SerializerMethodField(help_text="True only when the provider supplied a completed total.")
    limit_type = serializers.SerializerMethodField(help_text="Provider quota unit, such as Completes or Starts.")
    scope_label = serializers.SerializerMethodField(help_text="Human-readable overall or targeted quota scope.")
    targeting_details = serializers.SerializerMethodField(help_text="Quota targeting decoded into readable datapoint names and answer labels.")

    class Meta:
        model = SurveyQuota
        fields = [
            "id", "quota_id", "title", "name", "display_name", "sample_size", "remaining", "completes",
            "clicks", "status", "targeting", "target_known", "completed_known", "limit_type",
            "scope_label", "targeting_details", "updated_at",
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
        if not self._is_rfg(obj):
            return True
        raw = obj.raw_data or {}
        return obj.sample_size > 0 or any(
            raw.get(key) is not None for key in ("limit", "quotaTarget", "sampleSize")
        )

    def get_completed_known(self, obj) -> bool:
        if not self._is_rfg(obj):
            return True
        raw = obj.raw_data or {}
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
        return "Targeted respondent quota" if self._quota_datapoints(obj) else "Overall survey quota"

    @staticmethod
    def _range_label(value) -> str:
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
        questions = list(obj.survey.targeting_questions.all())
        if self._is_toluna(obj):
            question_map = {str(item.question_id): item for item in questions}
            grouped = {}
            for row in self._toluna_question_rows(obj):
                question_id = str(row.get("QuestionID") or "").strip()
                if not question_id:
                    continue
                question = question_map.get(question_id)
                detail = grouped.setdefault(question_id, {
                    "name": question.text if question else f"Qualification {question_id}",
                    "values": [],
                })
                option_labels = {
                    str(option.get("OptionId")): str(option.get("OptionText") or option.get("Translation") or option.get("OptionId"))
                    for option in (question.options if question else [])
                    if isinstance(option, dict)
                }
                values = row.get("AnswerValues") or []
                if isinstance(values, str):
                    values = [part.strip() for part in values.split(",") if part.strip()]
                readable = [str(value) for value in values]
                readable.extend(
                    option_labels.get(str(value), str(value))
                    for value in (row.get("AnswerIDs") or [])
                )
                for value in readable:
                    if value and value not in detail["values"]:
                        detail["values"].append(value)
            return list(grouped.values())
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
                range_label = self._range_label(value)
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
        ranges = raw.get("targeting_age_ranges") or []
        if ranges:
            labels = [SurveyQuotaSerializer._range_label(item) for item in ranges if isinstance(item, dict)]
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
            "id", "local_id", "client", "client_name", "display_company_name", "source_id", "provider_code", "company_name", "name", "status", "sample_size", "completes", "remaining",
            "starts", "cpi", "cpi_cut_percent", "vendor_pricing", "loi", "incidence_rate", "country", "country_code", "country_label",
            "language", "language_code", "group_type", "buyer_id", "survey_type", "device_type", "entry_link", "start_link", "has_quota",
            "source_created_at", "source_modified_at", "source_created_display", "source_modified_display",
            "detail_synced_at", "quota_synced_at", "targeting_synced_at", "created_at", "updated_at",
            "progress_percent",
        ]

    def get_country_label(self, obj) -> str:
        return " ".join(part for part in [obj.country_code, obj.language_code] if part) or obj.country

    @extend_schema_field({"oneOf": [{"type": "integer"}, {"type": "string"}]})
    def get_source_id(self, obj):
        return obj.source_identifier

    def get_provider_code(self, obj) -> str:
        return obj.integration.provider_code if obj.integration_id else getattr(obj.client, "provider_code", "innovatemr")

    def get_display_company_name(self, obj) -> str:
        request = self.context.get("request")
        if request and (
            vendor_scope_user_id(request.user) or organization_client_ids_for_user(request.user) is not None
        ) and obj.client:
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
        request = self.context.get("request")
        return survey_pricing_for_user(request.user, obj) if request and request.user.is_authenticated else (obj.cpi, None)

    @extend_schema_field(serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True))
    def get_cpi(self, obj):
        return self._pricing(obj)[0]

    @extend_schema_field(serializers.DecimalField(max_digits=5, decimal_places=2, allow_null=True))
    def get_cpi_cut_percent(self, obj):
        return self._pricing(obj)[1]

    def get_vendor_pricing(self, obj) -> bool:
        request = self.context.get("request")
        return bool(request and vendor_scope_user_id(request.user))

    def get_start_link(self, obj) -> str | None:
        """Return the shareable platform pre-screener URL, never the supplier entry URL."""
        request = self.context.get("request")
        if not request or not request.user.is_authenticated or not has_function_access(request.user, "survey_links.copy"):
            return None
        is_dynamic_provider = bool(
            obj.integration_id and obj.integration.provider_code in {"rfg", "toluna"}
        )
        if not obj.entry_link and not is_dynamic_provider:
            return None
        query = urlencode({
            "surveyId": obj.source_identifier,
            "supplierCode": obj.integration.supplier_code if obj.integration_id else settings.PUBLIC_SUPPLIER_CODE,
            "userId": request.user.pk,
            "code": obj.local_id,
        })
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
    completes = serializers.IntegerField(min_value=0)
    share_percent = serializers.FloatField(min_value=0, max_value=100)


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


class DashboardTopUserSerializer(serializers.Serializer):
    user_id = serializers.IntegerField()
    name = serializers.CharField()
    hits = serializers.IntegerField(min_value=0)
    completes = serializers.IntegerField(min_value=0)
    conversion_rate = serializers.FloatField(min_value=0)


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
    traffic_chart = DashboardGraphSeriesSerializer(allow_null=True)
    finance_chart = DashboardGraphSeriesSerializer(allow_null=True)
    graph_clients = DashboardGraphClientSerializer(many=True)
    client_distribution = DashboardClientShareSerializer(many=True, allow_null=True)
    status_breakdown = DashboardStatusBreakdownSerializer(allow_null=True)
    device_breakdown = DashboardDeviceBreakdownSerializer(allow_null=True)
    top_users = DashboardTopUserSerializer(many=True, allow_null=True)
    generated_at = serializers.DateTimeField()


class SurveyAttemptSerializer(serializers.ModelSerializer):
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
            "rid", "prescreener_uid", "survey_local_id", "survey_source_id", "survey_name", "company_name", "country", "country_code",
            "language_code", "platform_user", "user_id", "user_name", "username", "user_email", "supplier",
            "supplier_name", "vendor", "vendor_name", "client", "client_name", "client_allocation", "survey_allocation", "supplier_code",
            "buyer_id", "source_cpi_snapshot", "cpi_snapshot_source", "cpi_cut_percent_snapshot", "payable_cpi_snapshot", "cpi_currency_snapshot",
            "status_label", "termination_reason", "termination_category",
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

    def get_survey_source_id(self, obj) -> str:
        return str(obj.survey.source_identifier)

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
        if obj.status in {SurveyAttempt.Status.INITIATED, SurveyAttempt.Status.REDIRECTED}:
            return "Initiated"
        return obj.get_status_display()

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
    revenue_currency = serializers.CharField(allow_null=True)
    completed_devices = SurveyAttemptCompletedDeviceSummarySerializer()


class SurveyAttemptListResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    results = SurveyAttemptSerializer(many=True)
    summary = SurveyAttemptSummarySerializer()
