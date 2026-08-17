from django.contrib import admin

from .models import (
    Survey,
    SurveyAttempt,
    SurveyQuota,
    SyncRun,
    TargetingQuestion,
    TolunaMember,
    TolunaReferenceQuestion,
)


class SurveyQuotaInline(admin.TabularInline):
    model = SurveyQuota
    extra = 0
    readonly_fields = ["source_key", "quota_id", "title", "sample_size", "remaining", "status", "updated_at"]


class TargetingQuestionInline(admin.TabularInline):
    model = TargetingQuestion
    extra = 0
    readonly_fields = ["question_id", "key", "text", "question_type", "category", "updated_at"]


@admin.register(Survey)
class SurveyAdmin(admin.ModelAdmin):
    list_display = ["local_id", "client", "integration", "source_key", "company_name", "name", "country_code", "language_code", "completes", "sample_size", "status", "source_modified_at"]
    search_fields = ["local_id", "source_key", "source_id", "company_name", "name"]
    list_filter = ["status", "client", "integration", "company_name", "country_code", "language_code", "has_quota"]
    readonly_fields = ["local_id", "source_id", "source_key", "raw_data", "created_at", "updated_at", "last_seen_at", "detail_synced_at"]
    inlines = [SurveyQuotaInline, TargetingQuestionInline]


@admin.register(SyncRun)
class SyncRunAdmin(admin.ModelAdmin):
    list_display = ["id", "integration", "started_at", "status", "unique_surveys", "created", "updated", "closed", "detail_failures"]
    readonly_fields = [field.name for field in SyncRun._meta.fields]


@admin.register(SurveyAttempt)
class SurveyAttemptAdmin(admin.ModelAdmin):
    list_display = [
        "rid", "prescreener_uid", "survey", "platform_user", "vendor", "client", "status", "status_source", "initiated_at", "loi_seconds", "initiation_ip",
        "callback_ip", "entry_browser", "entry_device", "is_verified",
    ]
    search_fields = [
        "rid", "prescreener_uid", "user_id", "platform_user__username", "platform_user__email", "survey__local_id",
        "survey__source_key", "survey__source_id", "initiation_ip", "callback_ip",
    ]
    list_filter = ["status", "status_source", "supplier_code", "entry_device", "entry_browser", "is_verified", "initiated_at"]
    readonly_fields = [field.name for field in SurveyAttempt._meta.fields]


@admin.register(TolunaReferenceQuestion)
class TolunaReferenceQuestionAdmin(admin.ModelAdmin):
    list_display = ["integration", "culture_code", "question_id", "internal_name", "answer_type", "is_routable", "updated_at"]
    list_filter = ["integration", "culture_code", "answer_type", "is_routable"]
    search_fields = ["question_id", "internal_name", "display_name"]
    readonly_fields = ["raw_data", "created_at", "updated_at"]


@admin.register(TolunaMember)
class TolunaMemberAdmin(admin.ModelAdmin):
    list_display = ["integration", "member_code", "culture_code", "is_registered", "last_synced_at", "updated_at"]
    list_filter = ["integration", "culture_code", "is_registered"]
    search_fields = ["member_code"]
    readonly_fields = ["created_at", "updated_at"]
