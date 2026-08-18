from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from surveys.models import ProfileReuseEvent, Survey, SurveyAttempt, TargetingQuestion
from surveys.serializers import SurveyAttemptSerializer
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
            profile_rereuse_enabled=True, profile_rereuse_percentage=50,
            profile_rereuse_cooldown_days=30,
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

    def candidate(self, uid, rid, *, submitted_days=90, usage_count=1,
                  source_client_code=None, last_reused_days=None):
        return PrescreenerSubmission.objects.using(DATABASE_ALIAS).create(
            uid=uid, rid=rid,
            source_client_code=source_client_code or self.integration.client.code,
            country="United States", country_code="US",
            language="English", language_code="EN", respondent_age=23,
            respondent_age_group="18-24", respondent_gender="male",
            usage_count=usage_count,
            last_reused_at=(timezone.now() - timedelta(days=last_reused_days)
                            if last_reused_days is not None else None),
            submitted_at=timezone.now() - timedelta(days=submitted_days),
        )

    def test_monthly_target_is_split_between_first_and_returning_pools(self):
        fresh = self.candidate("Aa11-Bb22-Cc33-Dd44", "OldRid0001", submitted_days=100)
        returning = self.candidate(
            "Ee55-Ff66-Gg77-Hh88", "OldRid0002", submitted_days=120,
            usage_count=2, last_reused_days=60,
        )
        first_attempt = create_attempt(self.survey, self.user, "8.8.8.8")
        first_event = maybe_assign_reusable_profile(first_attempt, self.answers())
        second_attempt = create_attempt(self.survey, self.user, "8.8.8.8")
        second_event = maybe_assign_reusable_profile(second_attempt, self.answers())
        self.assertEqual((first_event.reuse_pool, first_event.reused_uid), ("first", fresh.uid))
        self.assertEqual((second_event.reuse_pool, second_event.reused_uid), ("returning", returning.uid))
        status = profile_reuse_month_status(self.integration)
        self.assertEqual((status["first_reuse_target"], status["repeat_reuse_target"]), (5, 5))
        self.assertEqual((status["first_reuse_used"], status["repeat_reuse_used"]), (1, 1))

    def test_profile_never_crosses_client_boundary(self):
        other_client = Client.objects.create(code="other-client", name="Other client")
        self.candidate(
            "Uv11-Wx22-Yz33-Ab44", "OldRid0010", submitted_days=100,
            source_client_code=other_client.code,
        )
        attempt = create_attempt(self.survey, self.user, "8.8.8.8")
        self.assertIsNone(maybe_assign_reusable_profile(attempt, self.answers()))

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

    def test_reused_profile_has_a_new_traffic_journey_row(self):
        candidate = self.candidate("Tr11-Af22-Fi33-Cu44", "OldRid0042", submitted_days=90)
        attempt = create_attempt(self.survey, self.user, "8.8.8.8")
        registered_uid = attempt.prescreener_uid
        event = maybe_assign_reusable_profile(attempt, self.answers())
        attempt.refresh_from_db()
        traffic_row = SurveyAttemptSerializer(attempt).data
        self.assertNotEqual(attempt.rid, candidate.rid)
        self.assertEqual(event.reused_rid, candidate.rid)
        self.assertEqual(traffic_row["rid"], attempt.rid)
        self.assertEqual(traffic_row["prescreener_uid"], candidate.uid)
        self.assertEqual(traffic_row["registered_profile_uid"], registered_uid)
        self.assertTrue(traffic_row["profile_was_reused"])

        retry_event = maybe_assign_reusable_profile(attempt, self.answers())
        candidate.refresh_from_db(using=DATABASE_ALIAS)
        self.assertEqual(retry_event.pk, event.pk)
        self.assertEqual(candidate.usage_count, 2)

    def test_days_demographics_and_monthly_budget_are_enforced(self):
        self.candidate("Ii99-Jj00-Kk11-Ll22", "OldRid0003", submitted_days=5)
        attempt = create_attempt(self.survey, self.user, "8.8.8.8")
        self.assertIsNone(maybe_assign_reusable_profile(attempt, self.answers()))
        self.candidate("Mm33-Nn44-Oo55-Pp66", "OldRid0004", submitted_days=90)
        self.integration.profile_reuse_monthly_percentage = 10
        self.integration.profile_rereuse_enabled = False
        self.integration.save(update_fields=["profile_reuse_monthly_percentage", "profile_rereuse_enabled"])
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
        self.candidate(
            "Zz11-Yy22-Xx33-Ww44", "OldRid0098", submitted_days=90,
            source_client_code=client.code,
        )
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
