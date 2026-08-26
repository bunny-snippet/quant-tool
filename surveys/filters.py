import django_filters
from django.db.models import Q

from .models import Survey, SurveyAttempt


class CharInFilter(django_filters.BaseInFilter, django_filters.CharFilter):
    """Accept comma-separated values, e.g. ?country=US,IN."""


class SurveyFilter(django_filters.FilterSet):
    client = django_filters.NumberFilter(field_name="client_id", help_text="Internal client record ID")
    client_name = CharInFilter(field_name="client__name", lookup_expr="in", help_text="Comma-separated allocated client names")
    country = CharInFilter(field_name="country_code", lookup_expr="in", help_text="Comma-separated country codes, e.g. US,IN")
    language = CharInFilter(field_name="language_code", lookup_expr="in", help_text="Comma-separated language codes, e.g. EN,HI")
    status = CharInFilter(field_name="status", lookup_expr="in", help_text="Comma-separated statuses: live,closed")
    company = CharInFilter(field_name="company_name", lookup_expr="in", help_text="Comma-separated supplier company names")
    buyer_id = CharInFilter(field_name="buyer_id", lookup_expr="in", help_text="Comma-separated provider buyer/sub-client IDs")
    survey_type = CharInFilter(method="filter_survey_type", help_text="Comma-separated normalized types, e.g. B2B,B2C")
    created_from = django_filters.IsoDateTimeFilter(field_name="source_created_at", lookup_expr="gte")
    created_to = django_filters.IsoDateTimeFilter(field_name="source_created_at", lookup_expr="lte")
    modified_from = django_filters.IsoDateTimeFilter(field_name="source_modified_at", lookup_expr="gte")
    modified_to = django_filters.IsoDateTimeFilter(field_name="source_modified_at", lookup_expr="lte")
    min_cpi = django_filters.NumberFilter(field_name="visible_cpi", lookup_expr="gte")
    max_cpi = django_filters.NumberFilter(field_name="visible_cpi", lookup_expr="lte")

    def filter_survey_type(self, queryset, _name, value):
        values = value if isinstance(value, (list, tuple, set)) else str(value or "").split(",")
        values = {str(item).strip().upper() for item in values if str(item).strip()}
        if not values:
            return queryset
        query = Q(pk__in=[])
        for item in values:
            query |= Q(survey_type__iexact=item) | Q(group_type__iexact=item)
        return queryset.filter(query)

    class Meta:
        model = Survey
        fields = ["client", "client_name", "country", "language", "status", "company", "buyer_id", "survey_type", "created_from", "created_to", "modified_from", "modified_to", "min_cpi", "max_cpi"]


class SurveyAttemptFilter(django_filters.FilterSet):
    status = CharInFilter(field_name="status", lookup_expr="in", help_text="Comma-separated attempt statuses")
    user = CharInFilter(field_name="platform_user_id", lookup_expr="in", help_text="Comma-separated platform user IDs")
    country = CharInFilter(field_name="survey__country_code", lookup_expr="in", help_text="Comma-separated survey country codes")
    company = CharInFilter(field_name="survey__company_name", lookup_expr="in")
    client = CharInFilter(field_name="survey__client_id", lookup_expr="in", help_text="Comma-separated internal client IDs")
    buyer_id = CharInFilter(field_name="survey__buyer_id", lookup_expr="in", help_text="Comma-separated buyer/sub-client IDs")
    branch = CharInFilter(method="filter_branch", help_text="Comma-separated organization Branch IDs or legacy labels")
    sub_branch = CharInFilter(method="filter_sub_branch", help_text="Comma-separated organization Sub-branch IDs or legacy labels")
    shift = CharInFilter(method="filter_shift", help_text="Comma-separated organization Shift IDs or legacy labels")
    survey_id = django_filters.CharFilter(
        method="filter_survey_id",
        help_text="Exact provider survey key, Toluna SurveyID, or Toluna WaveID.",
    )
    internal_id = django_filters.CharFilter(field_name="survey__local_id", lookup_expr="iexact")
    initiated_from = django_filters.IsoDateTimeFilter(field_name="initiated_at", lookup_expr="gte")
    initiated_to = django_filters.IsoDateTimeFilter(field_name="initiated_at", lookup_expr="lte")
    callback_from = django_filters.IsoDateTimeFilter(field_name="callback_at", lookup_expr="gte")
    callback_to = django_filters.IsoDateTimeFilter(field_name="callback_at", lookup_expr="lte")
    entry_ip = django_filters.CharFilter(field_name="initiation_ip", lookup_expr="iexact")
    exit_ip = django_filters.CharFilter(field_name="callback_ip", lookup_expr="iexact")

    def filter_survey_id(self, queryset, _name, value):
        identifier = str(value or "").strip()
        if not identifier:
            return queryset
        return queryset.filter(
            Q(survey__source_key__iexact=identifier)
            | Q(survey__source_id__iexact=identifier)
            | Q(survey__buyer_id__iexact=identifier)
            | Q(
                survey__integration__provider_code="toluna",
                survey__source_key__istartswith=f"{identifier}:",
            )
        )

    @staticmethod
    def _split_hierarchy_values(value):
        values = value if isinstance(value, (list, tuple, set)) else str(value or "").split(",")
        values = {str(item).strip() for item in values if str(item).strip()}
        return {int(item) for item in values if item.isdigit()}, {item for item in values if not item.isdigit()}

    def filter_branch(self, queryset, _name, value):
        unit = "platform_user__employee_profile__organization_unit"
        ids, labels = self._split_hierarchy_values(value)
        query = None
        if ids:
            query = (
                Q(**{f"{unit}_id__in": ids, f"{unit}__unit_type": "branch"})
                | Q(**{f"{unit}__parent_id__in": ids, f"{unit}__parent__unit_type": "branch"})
                | Q(**{f"{unit}__parent__parent_id__in": ids, f"{unit}__parent__parent__unit_type": "branch"})
            )
        if labels:
            label_query = (
                Q(**{f"{unit}__name__in": labels, f"{unit}__unit_type": "branch"})
                | Q(**{f"{unit}__parent__name__in": labels, f"{unit}__parent__unit_type": "branch"})
                | Q(**{f"{unit}__parent__parent__name__in": labels, f"{unit}__parent__parent__unit_type": "branch"})
                | Q(platform_user__employee_profile__company_name__in=labels)
            )
            query = label_query if query is None else query | label_query
        return queryset.filter(query).distinct() if query is not None else queryset

    def filter_sub_branch(self, queryset, _name, value):
        unit = "platform_user__employee_profile__organization_unit"
        ids, labels = self._split_hierarchy_values(value)
        query = None
        if ids:
            query = (
                Q(**{f"{unit}_id__in": ids, f"{unit}__unit_type": "sub_branch"})
                | Q(**{f"{unit}__parent_id__in": ids, f"{unit}__parent__unit_type": "sub_branch"})
            )
        if labels:
            label_query = (
                Q(**{f"{unit}__name__in": labels, f"{unit}__unit_type": "sub_branch"})
                | Q(**{f"{unit}__parent__name__in": labels, f"{unit}__parent__unit_type": "sub_branch"})
                | Q(platform_user__employee_profile__department__in=labels)
            )
            query = label_query if query is None else query | label_query
        return queryset.filter(query).distinct() if query is not None else queryset

    def filter_shift(self, queryset, _name, value):
        unit = "platform_user__employee_profile__organization_unit"
        ids, labels = self._split_hierarchy_values(value)
        query = None
        if ids:
            query = Q(**{f"{unit}_id__in": ids, f"{unit}__unit_type": "shift"})
        if labels:
            label_query = Q(**{f"{unit}__name__in": labels, f"{unit}__unit_type": "shift"})
            query = label_query if query is None else query | label_query
        return queryset.filter(query).distinct() if query is not None else queryset

    class Meta:
        model = SurveyAttempt
        fields = [
            "status", "user", "country", "company", "client", "buyer_id", "branch", "sub_branch", "shift",
            "survey_id", "internal_id", "initiated_from", "initiated_to",
            "callback_from", "callback_to", "entry_ip", "exit_ip",
        ]
