from datetime import timedelta

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.utils import timezone


class LocalIdSequence(models.Model):
    """Monthly counter used to issue 14-digit IDs such as 20260800000001."""

    year_month = models.CharField(max_length=6, primary_key=True)
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "local ID sequence"

    @classmethod
    def next_id(cls) -> str:
        prefix = timezone.localdate().strftime("%Y%m")
        with transaction.atomic():
            sequence, _ = cls.objects.select_for_update().get_or_create(year_month=prefix)
            sequence.last_value += 1
            sequence.save(update_fields=["last_value"])
            return f"{prefix}{sequence.last_value:08d}"


class SyncLease(models.Model):
    """Database-backed single-flight lease for recurring jobs."""

    name = models.CharField(max_length=80, primary_key=True)
    locked_until = models.DateTimeField(null=True, blank=True)

    @classmethod
    def acquire(cls, name: str, seconds: int = 300) -> bool:
        with transaction.atomic():
            cls.objects.get_or_create(name=name)
            lease = cls.objects.select_for_update().get(name=name)
            now = timezone.now()
            if lease.locked_until and lease.locked_until > now:
                return False
            lease.locked_until = now + timedelta(seconds=seconds)
            lease.save(update_fields=["locked_until"])
            return True

    @classmethod
    def release(cls, name: str) -> None:
        cls.objects.filter(name=name).update(locked_until=None)


class Survey(models.Model):
    class Status(models.TextChoices):
        LIVE = "live", "Live"
        CLOSED = "closed", "Closed"

    local_id = models.CharField(max_length=14, unique=True, editable=False, db_index=True)
    client = models.ForeignKey(
        "vendors.Client",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="surveys",
    )
    integration = models.ForeignKey(
        "vendors.ClientIntegration",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="surveys",
    )
    source_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Legacy numeric upstream survey ID when the provider uses one.",
    )
    source_key = models.CharField(
        max_length=160,
        blank=True,
        db_index=True,
        help_text="Provider survey identifier, including non-numeric IDs.",
    )
    company_name = models.CharField(max_length=160, default="InnovateMR", db_index=True)
    name = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.LIVE, db_index=True)
    sample_size = models.PositiveIntegerField(default=0)
    completes = models.PositiveIntegerField(default=0)
    remaining = models.PositiveIntegerField(default=0)
    starts = models.PositiveIntegerField(default=0)
    cpi = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    loi = models.PositiveIntegerField(null=True, blank=True)
    incidence_rate = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    country = models.CharField(max_length=120, blank=True, db_index=True)
    country_code = models.CharField(max_length=8, blank=True, db_index=True)
    language = models.CharField(max_length=80, blank=True)
    language_code = models.CharField(max_length=8, blank=True)
    group_type = models.CharField(max_length=80, blank=True)
    buyer_id = models.CharField(
        max_length=160,
        blank=True,
        db_index=True,
        help_text="Provider buyer/sub-client identifier.",
    )
    survey_type = models.CharField(
        max_length=20,
        blank=True,
        db_index=True,
        help_text="Normalized survey audience type, such as B2B or B2C.",
    )
    device_type = models.CharField(max_length=80, blank=True)
    entry_link = models.URLField(max_length=2000, blank=True)
    test_entry_link = models.URLField(max_length=2000, blank=True)
    job_category = models.CharField(max_length=180, blank=True)
    has_quota = models.BooleanField(default=False)
    is_pii_required = models.BooleanField(default=False)
    is_recontact = models.BooleanField(default=False)
    source_created_at = models.DateTimeField(null=True, blank=True, db_index=True)
    source_modified_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_seen_at = models.DateTimeField(default=timezone.now, db_index=True)
    detail_synced_at = models.DateTimeField(null=True, blank=True)
    quota_synced_at = models.DateTimeField(null=True, blank=True)
    targeting_synced_at = models.DateTimeField(null=True, blank=True)
    raw_data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ["-source_modified_at", "-created_at"]
        indexes = [
            models.Index(fields=["status", "country_code"]),
            models.Index(fields=["client", "cpi"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["integration", "source_id"], name="unique_integration_survey_source"),
            models.UniqueConstraint(fields=["integration", "source_key"], name="unique_integration_survey_key"),
        ]

    def save(self, *args, **kwargs):
        if not self.local_id:
            self.local_id = LocalIdSequence.next_id()
        if not self.source_key and self.source_id is not None:
            self.source_key = str(self.source_id)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.local_id} · {self.name or self.source_id}"


    @property
    def source_identifier(self):
        if self.source_id is not None and self.source_key in {"", str(self.source_id)}:
            return self.source_id
        return self.source_key or self.source_id


class SurveyQuota(models.Model):
    survey = models.ForeignKey(Survey, related_name="quotas", on_delete=models.CASCADE)
    source_key = models.CharField(max_length=120)
    quota_id = models.BigIntegerField(null=True, blank=True)
    title = models.TextField(blank=True)
    name = models.CharField(max_length=500, blank=True)
    sample_size = models.PositiveIntegerField(default=0)
    remaining = models.PositiveIntegerField(default=0)
    completes = models.PositiveIntegerField(default=0)
    clicks = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=80, blank=True)
    targeting = models.JSONField(default=dict, blank=True)
    raw_data = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["survey", "source_key"], name="unique_survey_quota")]
        ordering = ["id"]


class TargetingQuestion(models.Model):
    survey = models.ForeignKey(Survey, related_name="targeting_questions", on_delete=models.CASCADE)
    question_id = models.BigIntegerField()
    key = models.CharField(max_length=180, blank=True)
    text = models.TextField(blank=True)
    question_type = models.CharField(max_length=120, blank=True)
    category = models.CharField(max_length=120, blank=True)
    options = models.JSONField(default=list, blank=True)
    raw_data = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["survey", "question_id"], name="unique_survey_question")]
        ordering = ["question_id"]


class TolunaReferenceQuestion(models.Model):
    """Culture-specific Toluna question/answer mapping used by quota matching.

    Credentials and Panel GUIDs deliberately do not live in this table.  The
    row is only a local, provider-neutral display/mapping cache and can be
    rebuilt from Toluna's Reference Data API at any time.
    """

    integration = models.ForeignKey(
        "vendors.ClientIntegration",
        related_name="toluna_reference_questions",
        on_delete=models.CASCADE,
    )
    culture_code = models.CharField(max_length=12, db_index=True)
    culture_id = models.PositiveIntegerField(db_index=True)
    question_id = models.BigIntegerField(db_index=True)
    internal_name = models.CharField(max_length=300, blank=True)
    display_name = models.TextField(blank=True)
    answer_type = models.CharField(max_length=80, blank=True)
    is_routable = models.BooleanField(default=False)
    options = models.JSONField(default=list, blank=True)
    raw_data = models.JSONField(default=dict, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["integration", "culture_code", "question_id"],
                name="unique_toluna_reference_question",
            )
        ]
        indexes = [models.Index(fields=["integration", "culture_code"])]
        ordering = ["culture_code", "question_id"]


class TolunaMember(models.Model):
    """Audit/cache of a member profile registered with Toluna.

    ``member_code`` is our stable prescreener UID.  ``profile_hash`` makes a
    repeated attempt idempotent while still issuing a PUT when the profile has
    genuinely changed.
    """

    integration = models.ForeignKey(
        "vendors.ClientIntegration",
        related_name="toluna_members",
        on_delete=models.CASCADE,
    )
    member_code = models.CharField(max_length=80, db_index=True)
    culture_code = models.CharField(max_length=12, blank=True, db_index=True)
    profile_hash = models.CharField(max_length=64, blank=True)
    is_registered = models.BooleanField(default=False)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["integration", "member_code"],
                name="unique_toluna_member_code",
            )
        ]
        ordering = ["-updated_at"]


class TolunaNotification(models.Model):
    """Immutable, idempotent audit of one Toluna server notification.

    Toluna can resend member-completion notifications when a respondent
    refreshes its end page.  ``payload_hash`` therefore identifies an exact
    provider event while ``duplicate_count`` records repeated deliveries
    without applying the same status transition twice.
    """

    class EventType(models.TextChoices):
        MEMBER_COMPLETE = "member_complete", "Member completion"
        MEMBER_TERMINATE = "member_terminate", "Member termination"
        ENHANCED_TERMINATION = "enhanced_termination", "Enhanced termination"
        QUOTA_STATUS = "quota_status", "Quota status"
        SURVEY_CLOSED = "survey_closed", "Survey closed"
        RECONCILIATION = "reconciliation", "Reconciliation"

    event_type = models.CharField(max_length=32, choices=EventType.choices, db_index=True)
    payload_hash = models.CharField(max_length=64)
    integration = models.ForeignKey(
        "vendors.ClientIntegration",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="toluna_notifications",
    )
    survey = models.ForeignKey(
        Survey,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="toluna_notifications",
    )
    attempt = models.ForeignKey(
        "SurveyAttempt",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="toluna_notifications",
    )
    unique_code = models.CharField(max_length=160, blank=True, db_index=True)
    provider_survey_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    survey_ref = models.CharField(max_length=300, blank=True)
    wave_id = models.BigIntegerField(null=True, blank=True)
    quota_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    provider_status = models.CharField(max_length=80, blank=True, db_index=True)
    reason = models.CharField(max_length=160, blank=True, db_index=True)
    rejection_id = models.BigIntegerField(null=True, blank=True)
    rejection_name = models.CharField(max_length=200, blank=True, db_index=True)
    reconciliation_id = models.BigIntegerField(null=True, blank=True)
    revenue_cents = models.IntegerField(null=True, blank=True)
    is_live = models.BooleanField(null=True, blank=True)
    occurred_at = models.DateTimeField(null=True, blank=True, db_index=True)
    raw_payload = models.JSONField(default=dict, blank=True, editable=False)
    applied = models.BooleanField(default=False, db_index=True)
    processing_message = models.CharField(max_length=300, blank=True)
    duplicate_count = models.PositiveIntegerField(default=0)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    last_received_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["event_type", "payload_hash"],
                name="unique_toluna_notification_payload",
            )
        ]
        indexes = [
            models.Index(fields=["event_type", "-received_at"]),
            models.Index(fields=["integration", "event_type", "-received_at"]),
            models.Index(fields=["attempt", "-received_at"]),
        ]
        ordering = ["-received_at"]


class SyncRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"

    integration = models.ForeignKey(
        "vendors.ClientIntegration",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="sync_runs",
    )
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.RUNNING)
    fetched_full = models.PositiveIntegerField(default=0)
    fetched_paged = models.PositiveIntegerField(default=0)
    unique_surveys = models.PositiveIntegerField(default=0)
    created = models.PositiveIntegerField(default=0)
    updated = models.PositiveIntegerField(default=0)
    unchanged = models.PositiveIntegerField(default=0)
    closed = models.PositiveIntegerField(default=0)
    detail_failures = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]


class SurveyAttempt(models.Model):
    class Status(models.TextChoices):
        INITIATED = "initiated", "Initiated"
        REDIRECTED = "redirected", "Redirected to survey"
        COMPLETED = "1", "Completed"
        TERMINATED = "2", "Terminated"
        OVER_QUOTA = "3", "Over quota"
        QUALITY_TERMINATED = "4", "Quality terminated"
        SURVEY_NOT_AVAILABLE = "7", "Survey not available"
        NO_SURVEYS = "8", "No surveys"
        NO_COOKIES = "9", "No cookies"
        MAX_SURVEYS_REACHED = "10", "Maximum surveys reached"
        NOT_QUALIFIED = "11", "Not qualified"
        SURVEY_TAKEN = "12", "Survey already taken"

    rid = models.CharField(max_length=10, unique=True, db_index=True)
    prescreener_uid = models.CharField(
        max_length=19,
        unique=True,
        null=True,
        blank=True,
        editable=False,
        help_text="Stable XXXX-XXXX-XXXX-XXXX identity for the isolated prescreener vault.",
    )
    provider_profile_uid = models.CharField(
        max_length=19,
        blank=True,
        db_index=True,
        editable=False,
        help_text="Reusable UID actually sent to the provider; blank means prescreener_uid was used.",
    )
    survey = models.ForeignKey(Survey, related_name="attempts", on_delete=models.PROTECT)
    platform_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, related_name="survey_attempts", on_delete=models.SET_NULL
    )
    vendor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="vendor_owned_attempts",
        on_delete=models.SET_NULL,
    )
    client = models.ForeignKey(
        "vendors.Client",
        null=True,
        blank=True,
        related_name="attempts",
        on_delete=models.PROTECT,
    )
    client_allocation = models.ForeignKey(
        "vendors.VendorClientAllocation",
        null=True,
        blank=True,
        related_name="attempts",
        on_delete=models.PROTECT,
    )
    survey_allocation = models.ForeignKey(
        "vendors.VendorSurveyAllocation",
        null=True,
        blank=True,
        related_name="attempts",
        on_delete=models.PROTECT,
    )
    supplier_api_key_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        db_index=True,
        editable=False,
        help_text="API key that issued this respondent entry link, stored without coupling attempt history to key deletion.",
    )
    supplier_delivery_config = models.JSONField(
        default=dict,
        blank=True,
        editable=False,
        help_text="Immutable non-secret external-supplier delivery snapshot for this journey.",
    )
    user_id = models.CharField(max_length=160, db_index=True)
    supplier_code = models.CharField(max_length=40, blank=True)
    source_cpi_snapshot = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    cpi_snapshot_source = models.CharField(
        max_length=24,
        blank=True,
        help_text="How the immutable source CPI snapshot was captured or recovered.",
    )
    cpi_cut_percent_snapshot = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    payable_cpi_snapshot = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    cpi_currency_snapshot = models.CharField(max_length=3, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.INITIATED, db_index=True)
    initiated_at = models.DateTimeField(default=timezone.now, db_index=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    redirected_at = models.DateTimeField(null=True, blank=True)
    callback_at = models.DateTimeField(null=True, blank=True)
    last_callback_at = models.DateTimeField(null=True, blank=True)
    loi_seconds = models.PositiveIntegerField(null=True, blank=True)
    initiation_ip = models.GenericIPAddressField(null=True, blank=True)
    callback_ip = models.GenericIPAddressField(null=True, blank=True)
    entry_user_agent = models.TextField(blank=True)
    exit_user_agent = models.TextField(blank=True)
    entry_browser = models.CharField(max_length=160, blank=True)
    exit_browser = models.CharField(max_length=160, blank=True)
    entry_device = models.CharField(max_length=80, blank=True)
    exit_device = models.CharField(max_length=80, blank=True)
    entry_os = models.CharField(max_length=160, blank=True)
    exit_os = models.CharField(max_length=160, blank=True)
    entry_referrer = models.TextField(blank=True)
    entry_accept_language = models.CharField(max_length=500, blank=True)
    entry_client_data = models.JSONField(default=dict, blank=True)
    exit_client_data = models.JSONField(default=dict, blank=True)
    status_source = models.CharField(max_length=40, blank=True)
    upstream_checked_at = models.DateTimeField(null=True, blank=True, db_index=True)
    upstream_transaction_data = models.JSONField(default=dict, blank=True)
    answers = models.JSONField(default=dict, blank=True)
    outbound_url = models.URLField(max_length=3000, blank=True)
    callback_count = models.PositiveIntegerField(default=0)
    is_verified = models.BooleanField(default=False, help_text="True only after a trusted S2S notification/hash verification.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-initiated_at"]
        indexes = [
            models.Index(fields=["survey", "user_id", "-initiated_at"]),
            models.Index(fields=["initiation_ip"], name="attempt_entry_ip_idx"),
        ]

    def __str__(self):
        return f"{self.rid} · {self.survey.source_id} · {self.user_id}"

    @property
    def loi_started_at(self):
        """Measure the full respondent journey, including our pre-screener."""

        return self.initiated_at

    def calculate_loi_seconds(self, ended_at) -> int:
        return max(0, int((ended_at - self.loi_started_at).total_seconds()))


class SurveyEntryIPClaim(models.Model):
    """Permanent, race-safe claim for one public respondent entry IP.

    The unique IP is the actual enforcement primitive. ``first_attempt`` keeps
    the decision auditable without putting a uniqueness constraint on the
    historical attempt table (which legitimately also stores rejected tries).
    """

    ip_address = models.GenericIPAddressField(unique=True)
    first_attempt = models.OneToOneField(
        SurveyAttempt,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="entry_ip_claim",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]


class ProfileReuseMonthlyCounter(models.Model):
    """Concurrency-safe monthly client budget for previously registered UIDs."""

    integration = models.ForeignKey(
        "vendors.ClientIntegration",
        on_delete=models.PROTECT,
        related_name="profile_reuse_months",
    )
    period_start = models.DateField(db_index=True)
    baseline_attempts = models.PositiveIntegerField(default=0)
    target_reuses = models.PositiveIntegerField(default=0)
    allocated_reuses = models.PositiveIntegerField(default=0)
    first_reuse_allocated = models.PositiveIntegerField(default=0)
    repeat_reuse_allocated = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["integration", "period_start"],
                name="unique_profile_reuse_month",
            ),
        ]
        indexes = [models.Index(fields=["integration", "period_start"])]


class ProfileReuseEvent(models.Model):
    """Audit one journey that reused an existing vault RID/UID profile pair."""

    integration = models.ForeignKey(
        "vendors.ClientIntegration",
        on_delete=models.PROTECT,
        related_name="profile_reuse_events",
    )
    attempt = models.OneToOneField(
        SurveyAttempt,
        on_delete=models.PROTECT,
        related_name="profile_reuse_event",
    )
    survey = models.ForeignKey(
        Survey,
        on_delete=models.PROTECT,
        related_name="profile_reuse_events",
        help_text="Denormalized project identity used for permanent same-project exclusion.",
    )
    registered_uid = models.CharField(max_length=19, db_index=True)
    reused_rid = models.CharField(max_length=10, db_index=True)
    reused_uid = models.CharField(max_length=19, db_index=True)
    source_registered_at = models.DateTimeField()
    source_usage_number = models.PositiveIntegerField()
    reuse_pool = models.CharField(
        max_length=16,
        choices=(("first", "First reuse"), ("returning", "Returning profile")),
        default="first",
        db_index=True,
    )
    country_code = models.CharField(max_length=8, db_index=True)
    age_group = models.CharField(max_length=20, db_index=True)
    gender = models.CharField(max_length=20, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["integration", "created_at"]),
            models.Index(fields=["integration", "country_code", "age_group", "gender"]),
            models.Index(fields=["integration", "reused_uid", "created_at"]),
        ]


class ProfileReuseState(models.Model):
    """Operational lock row that serializes concurrent reuse of one UID."""

    integration = models.ForeignKey(
        "vendors.ClientIntegration",
        on_delete=models.PROTECT,
        related_name="profile_reuse_states",
    )
    reused_uid = models.CharField(max_length=19)
    last_reused_at = models.DateTimeField(null=True, blank=True, db_index=True)
    total_reuses = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["integration", "reused_uid"],
                name="unique_profile_reuse_state",
            ),
        ]
        indexes = [models.Index(fields=["integration", "last_reused_at"])]


class ProfileReuseProjectUsage(models.Model):
    """Permanent no-repeat lock for one client UID on one survey project."""

    integration = models.ForeignKey(
        "vendors.ClientIntegration",
        on_delete=models.PROTECT,
        related_name="profile_reuse_project_usages",
    )
    survey = models.ForeignKey(
        Survey,
        on_delete=models.PROTECT,
        related_name="profile_reuse_project_usages",
    )
    reused_uid = models.CharField(max_length=19)
    first_attempt = models.ForeignKey(
        SurveyAttempt,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="profile_reuse_project_locks",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["integration", "survey", "reused_uid"],
                name="unique_profile_uid_per_project",
            ),
        ]
        indexes = [models.Index(fields=["integration", "survey"])]
