from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from surveys.models import ProfileReuseEvent, Survey, SurveyAttempt, TargetingQuestion
from surveys.survey_flow import create_attempt
from vendors.models import Client, ClientIntegration

from .constants import DATABASE_ALIAS
from .models import PrescreenerSubmission
from .reuse import maybe_assign_reusable_profile, profile_reuse_month_status


@override_settings(PRESCREENER_VAULT_ENABLED=True)
class ReusableProfileQueueTests(TestCase):
    databases = {"default", DATABASE_ALIAS}

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="reuse-user")
        client = Client.objects.create(code="reuse-client", name="Reuse client")
        self.integration = ClientIntegration.objects.create(
            client=client, name="Reuse production", provider_code="toluna",
            base_url="https://provider.example/api", profile_reuse_enabled=True,
            profile_reuse_eligible_after_days=30, profile_reuse_monthly_percentage=100,
            profile_reuse_country_codes=["US"], profile_reuse_age_groups=["18-24"],
            profile_reuse_genders=["male"],
        )
        self.survey = Survey.objects.create(
            integration=self.integration, source_id=99101, name="Reuse survey",
            status=Survey.Status.LIVE, company_name="Reuse client", country="United States",
            country_code="US", language="English", language_code="EN",
            entry_link="https://provider.example/start",
        )
        self.age = TargetingQuestion.objects.create(
            survey=self.survey, question_id=1, key="AGE", text="What is your age?",
            question_type="Numeric", category="Demographic",
        )
        self.gender = TargetingQuestion.objects.create(
            survey=self.survey, question_id=2, key="GENDER", text="What is your gender?",
            question_type="Single Punch", category="Demographic",
            options=[{"OptionId": 1, "OptionText": "Male"}, {"OptionId": 2, "OptionText": "Female"}],
        )
        current_month = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        previous_month = current_month - timedelta(days=1)
        for _ in range(10):
            baseline = create_attempt(self.survey, self.user, "8.8.8.8")
            SurveyAttempt.objects.filter(pk=baseline.pk).update(initiated_at=previous_month)

    def answers(self, age="23", gender="1"):
        return {
            str(self.age.pk): {"values": [age], "upstream_values": [age]},
            str(self.gender.pk): {"values": [gender], "upstream_values": [gender]},
        }

    def candidate(self, uid, rid, *, submitted_days=90, usage_count=1):
        return PrescreenerSubmission.objects.using(DATABASE_ALIAS).create(
            uid=uid, rid=rid, country="United States", country_code="US",
            language="English", language_code="EN", respondent_age=23,
            respondent_age_group="18-24", respondent_gender="male",
            usage_count=usage_count, submitted_at=timezone.now() - timedelta(days=submitted_days),
        )

    def test_lowest_usage_round_is_exhausted_before_a_uid_repeats(self):
        first = self.candidate("Aa11-Bb22-Cc33-Dd44", "OldRid0001", submitted_days=100)
        second = self.candidate("Ee55-Ff66-Gg77-Hh88", "OldRid0002", submitted_days=90)
        selected = []
        for _ in range(3):
            attempt = create_attempt(self.survey, self.user, "8.8.8.8")
            event = maybe_assign_reusable_profile(attempt, self.answers())
            self.assertIsNotNone(event)
            attempt.refresh_from_db()
            selected.append(attempt.provider_profile_uid)
            self.assertNotEqual(attempt.prescreener_uid, attempt.provider_profile_uid)
            self.assertEqual(event.reused_rid, first.rid if len(selected) != 2 else second.rid)
        self.assertEqual(selected, [first.uid, second.uid, first.uid])
        first.refresh_from_db(using=DATABASE_ALIAS)
        second.refresh_from_db(using=DATABASE_ALIAS)
        self.assertEqual((first.usage_count, second.usage_count), (3, 2))
        self.assertEqual(ProfileReuseEvent.objects.count(), 3)

    def test_reuse_keeps_original_vault_pair_and_only_increments_visits(self):
        candidate = self.candidate(
            "Qq11-Rr22-Ss33-Tt44", "OldRid0099", submitted_days=90
        )
        attempt = create_attempt(self.survey, self.user, "8.8.8.8")
        original_count = PrescreenerSubmission.objects.using(DATABASE_ALIAS).count()

        event = maybe_assign_reusable_profile(attempt, self.answers())
        self.assertIsNotNone(event)
        self.assertEqual(
            PrescreenerSubmission.objects.using(DATABASE_ALIAS).count(), original_count
        )
        candidate.refresh_from_db(using=DATABASE_ALIAS)
        self.assertEqual((event.reused_rid, event.reused_uid), (candidate.rid, candidate.uid))
        self.assertEqual(candidate.usage_count, 2)

        retry_event = maybe_assign_reusable_profile(attempt, self.answers())
        candidate.refresh_from_db(using=DATABASE_ALIAS)
        self.assertEqual(retry_event.pk, event.pk)
        self.assertEqual(candidate.usage_count, 2)

    def test_days_demographics_and_monthly_budget_are_enforced(self):
        self.candidate("Ii99-Jj00-Kk11-Ll22", "OldRid0003", submitted_days=5)
        attempt = create_attempt(self.survey, self.user, "8.8.8.8")
        self.assertIsNone(maybe_assign_reusable_profile(attempt, self.answers()))
        self.candidate("Mm33-Nn44-Oo55-Pp66", "OldRid0004", submitted_days=90)
        self.integration.profile_reuse_country_codes = ["IN"]
        self.integration.save(update_fields=["profile_reuse_country_codes"])
        blocked = create_attempt(self.survey, self.user, "8.8.8.8")
        self.assertIsNone(maybe_assign_reusable_profile(blocked, self.answers()))
        self.integration.profile_reuse_country_codes = ["US"]
        self.integration.profile_reuse_monthly_percentage = 10
        self.integration.save(update_fields=["profile_reuse_country_codes", "profile_reuse_monthly_percentage"])
        allowed = create_attempt(self.survey, self.user, "8.8.8.8")
        self.assertIsNotNone(maybe_assign_reusable_profile(allowed, self.answers()))
        exhausted = create_attempt(self.survey, self.user, "8.8.8.8")
        self.assertIsNone(maybe_assign_reusable_profile(exhausted, self.answers()))
        self.assertEqual(profile_reuse_month_status(self.integration)["target_reuses"], 1)

    def test_first_month_uses_current_attempts_as_rolling_bootstrap(self):
        client = Client.objects.create(code="new-client", name="New client")
        integration = ClientIntegration.objects.create(
            client=client, name="New production", provider_code="toluna",
            base_url="https://new-provider.example/api", profile_reuse_enabled=True,
            profile_reuse_eligible_after_days=1, profile_reuse_monthly_percentage=100,
            profile_reuse_country_codes=["US"], profile_reuse_age_groups=["18-24"],
            profile_reuse_genders=["male"],
        )
        survey = Survey.objects.create(
            integration=integration, source_id=99102, name="First month survey",
            status=Survey.Status.LIVE, company_name="New client", country="United States",
            country_code="US", language="English", language_code="EN",
            entry_link="https://new-provider.example/start",
        )
        age = TargetingQuestion.objects.create(
            survey=survey, question_id=1, key="AGE", text="What is your age?",
            question_type="Numeric", category="Demographic",
        )
        gender = TargetingQuestion.objects.create(
            survey=survey, question_id=2, key="GENDER", text="What is your gender?",
            question_type="Single Punch", category="Demographic",
            options=[{"OptionId": 1, "OptionText": "Male"}],
        )
        self.candidate("Zz11-Yy22-Xx33-Ww44", "OldRid0098", submitted_days=90)
        attempt = create_attempt(survey, self.user, "8.8.8.8")
        answers = {
            str(age.pk): {"values": ["23"], "upstream_values": ["23"]},
            str(gender.pk): {"values": ["1"], "upstream_values": ["1"]},
        }

        self.assertIsNotNone(maybe_assign_reusable_profile(attempt, answers))
        status = profile_reuse_month_status(integration)
        self.assertEqual(status["previous_month_attempts"], 0)
        self.assertEqual(status["baseline_source"], "current_month_bootstrap")
        self.assertEqual(status["baseline_attempts"], 1)
        self.assertEqual(status["target_reuses"], 1)
        self.assertEqual(status["used_reuses"], 1)
