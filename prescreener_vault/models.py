from django.db import models


class PrescreenerSubmission(models.Model):
    """Immutable submission snapshot stored outside the operational database."""

    uid = models.CharField(max_length=19, primary_key=True)
    rid = models.CharField(max_length=10, unique=True)
    source_client_code = models.CharField(
        max_length=80,
        blank=True,
        db_index=True,
        help_text="Stable client scope that prevents profiles crossing between clients.",
    )
    country = models.CharField(max_length=120, blank=True)
    country_code = models.CharField(max_length=8, blank=True, db_index=True)
    language = models.CharField(max_length=80, blank=True)
    language_code = models.CharField(max_length=8, blank=True, db_index=True)
    respondent_age = models.PositiveSmallIntegerField(null=True, blank=True, db_index=True)
    respondent_age_group = models.CharField(max_length=20, blank=True, db_index=True)
    respondent_gender = models.CharField(max_length=80, blank=True, db_index=True)
    respondent_ethnicity = models.CharField(max_length=160, blank=True, db_index=True)
    respondent_postal_code = models.CharField(max_length=40, blank=True, db_index=True)
    profile_dimensions = models.JSONField(default=dict, blank=True)
    raw_answers = models.JSONField(default=dict, blank=True)
    answer_count = models.PositiveSmallIntegerField(default=0)
    usage_count = models.PositiveIntegerField(
        default=1,
        db_index=True,
        help_text="Total visits: one original submission plus approved profile reuses.",
    )
    last_reused_at = models.DateTimeField(null=True, blank=True, db_index=True)
    submitted_at = models.DateTimeField(db_index=True)
    captured_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-submitted_at"]
        indexes = [
            models.Index(fields=["country_code", "respondent_age_group", "respondent_gender"], name="vault_country_profile_idx"),
            models.Index(
                fields=["country_code", "respondent_age_group", "respondent_gender", "usage_count", "submitted_at"],
                name="vault_reuse_queue_idx",
            ),
            models.Index(
                fields=["source_client_code", "country_code", "respondent_age_group", "respondent_gender", "usage_count"],
                name="vault_client_reuse_idx",
            ),
        ]


class PrescreenerAnswer(models.Model):
    submission = models.ForeignKey(
        PrescreenerSubmission,
        related_name="question_answers",
        on_delete=models.CASCADE,
    )
    position = models.PositiveSmallIntegerField()
    question_record_id = models.CharField(max_length=40, blank=True)
    question_id = models.CharField(max_length=160, blank=True)
    question_key = models.CharField(max_length=180, blank=True, db_index=True)
    question_text = models.TextField(blank=True)
    question_type = models.CharField(max_length=120, blank=True)
    question_category = models.CharField(max_length=120, blank=True)
    canonical_attribute = models.CharField(max_length=80, blank=True, db_index=True)
    answer_values = models.JSONField(default=list, blank=True)
    answer_labels = models.JSONField(default=list, blank=True)
    upstream_values = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["submission", "position"], name="vault_unique_answer_position"),
        ]
        indexes = [
            models.Index(fields=["canonical_attribute", "question_key"], name="vault_answer_attribute_idx"),
        ]


class PrescreenerAnswerValue(models.Model):
    answer = models.ForeignKey(PrescreenerAnswer, related_name="values", on_delete=models.CASCADE)
    position = models.PositiveSmallIntegerField()
    value = models.TextField(blank=True)
    label = models.TextField(blank=True)
    normalized_value = models.CharField(max_length=191, blank=True, db_index=True)
    canonical_attribute = models.CharField(max_length=80, blank=True, db_index=True)
    country_code = models.CharField(max_length=8, blank=True, db_index=True)

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["answer", "position"], name="vault_unique_value_position"),
        ]
        indexes = [
            models.Index(
                fields=["country_code", "canonical_attribute", "normalized_value"],
                name="vault_matching_value_idx",
            ),
        ]
