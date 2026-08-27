import copy
import hashlib
import hmac
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from vendors.models import Client, ClientIntegration
from vendors.serializers import ClientIntegrationSerializer
from prescreener_vault.constants import DATABASE_ALIAS
from prescreener_vault.models import PrescreenerSubmission

from .models import (
    Survey,
    SurveyAttempt,
    SurveyQuota,
    TargetingQuestion,
    TolunaMember,
    TolunaNotification,
    TolunaReferenceQuestion,
)
from .outcomes import provider_outcome
from .provider_services import sync_client_integration
from .providers import ProviderError
from .providers.toluna import TolunaInviteRejected, TolunaProvider
from .serializers import SurveyListSerializer, SurveyQuotaSerializer
from .views import SurveyViewSet, _collect_prescreener_answers, _prescreener_questions


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self.payload = payload
        self.status_code = status_code
        self.content = b"" if payload is None else b"json"
        self.text = text

    def json(self):
        return self.payload


class RecordingSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


CULTURES = [{"CultureID": 1, "Name": "en-us", "Description": "United States English"}]
REFERENCE = [
    {
        "IsRoutable": False,
        "InternalName": "Gender",
        "TranslatedQuestion": {"QuestionID": 1001007, "CultureID": 1, "DisplayNameTranslation": "What is your gender?"},
        "TranslatedAnswers": [
            {"AnswerID": 2000246, "Translation": "Female", "AnswerInternalName": "Female"},
            {"AnswerID": 2000247, "Translation": "Male", "AnswerInternalName": "Male"},
        ],
        "AnswerType": "SingleSelect",
    },
]
QUOTAS = {
    "CountryID": 1,
    "CacheExpires": "2026-08-18T10:00:00Z",
    "Surveys": [{
        "SurveyID": 71,
        "SurveyName": "Toluna test survey",
        "WaveID": 72,
        "LOI": 8,
        "IR": 45,
        "StudyTypeID": 1,
        "DeviceTypeIDs": [1, 2],
        "CompletesRequired": 20,
        "EstimatedCompletesRemaining": 12,
        "Price": {"Amount": 2.75, "CurrencyID": 1},
        "Quotas": [{
            "QuotaID": 900,
            "CompletesRequired": 20,
            "EstimatedCompletesRemaining": 12,
            "Layers": [
                {"LayerID": 1, "SubQuotas": [{"SubQuotaID": 10, "QuestionsAndAnswers": [{"QuestionID": 1001538, "AnswerIDs": [2006353], "AnswerValues": ["25-29"]}]}]},
                {"LayerID": 2, "SubQuotas": [{"SubQuotaID": 20, "QuestionsAndAnswers": [{"QuestionID": 1001007, "AnswerIDs": [2000247], "AnswerValues": []}]}]},
            ],
        }],
    }],
}


@patch.dict("os.environ", {
    "TOLUNA_API_AUTH_KEY": "api-key",
    "TOLUNA_PARTNER_AUTH_KEY": "reference-key",
    "TOLUNA_HMAC_KEY": "hmac-secret",
    "TOLUNA_PANEL_EN_US": "panel-guid",
}, clear=False)
class TolunaProviderTests(TestCase):
    databases = {"default", DATABASE_ALIAS}
    def setUp(self):
        client = Client.objects.create(code="toluna", name="Toluna", provider_code="toluna")
        self.integration = ClientIntegration.objects.create(
            client=client,
            name="Toluna production",
            provider_code="toluna",
            base_url="https://tws.toluna.com",
            credential_env_keys={
                "api_auth_key": "TOLUNA_API_AUTH_KEY",
                "partner_auth_key": "TOLUNA_PARTNER_AUTH_KEY",
                "hmac_key": "TOLUNA_HMAC_KEY",
                "panel_en_us": "TOLUNA_PANEL_EN_US",
            },
            config={"environment": "production", "callback_hash_required": True},
        )

    def _complete_member_contract_case(self):
        reference = copy.deepcopy(REFERENCE)
        reference.extend([
            {
                "IsRoutable": False,
                "InternalName": "Annual Household Income",
                "TranslatedQuestion": {
                    "QuestionID": 1001107,
                    "CultureID": 1,
                    "DisplayNameTranslation": "What is your household income?",
                },
                "TranslatedAnswers": [
                    {
                        "AnswerID": 2002333,
                        "Translation": "$100,000-$199,999",
                        "AnswerInternalName": "income-mid",
                    },
                    {
                        "AnswerID": 2002334,
                        "Translation": "$200,000+",
                        "AnswerInternalName": "income-high",
                    },
                ],
                "AnswerType": "SingleSelect",
            },
            {
                "IsRoutable": False,
                "InternalName": "Owned Devices",
                "TranslatedQuestion": {
                    "QuestionID": 1002001,
                    "CultureID": 1,
                    "DisplayNameTranslation": "Which devices do you own?",
                },
                "TranslatedAnswers": [
                    {
                        "AnswerID": 3002001,
                        "Translation": "Phone",
                        "AnswerInternalName": "phone",
                    },
                    {
                        "AnswerID": 3002002,
                        "Translation": "Tablet",
                        "AnswerInternalName": "tablet",
                    },
                ],
                "AnswerType": "MultiSelect",
            },
            {
                "IsRoutable": False,
                "InternalName": "Favorite Color",
                "TranslatedQuestion": {
                    "QuestionID": 1003001,
                    "CultureID": 1,
                    "DisplayNameTranslation": "What is your favorite color?",
                },
                "TranslatedAnswers": [{
                    "AnswerID": 3003001,
                    "Translation": "Open answer",
                    "AnswerInternalName": "open-answer",
                }],
                "AnswerType": "OpenEnd",
            },
            {
                "IsRoutable": False,
                "InternalName": "Postal Code",
                "TranslatedQuestion": {
                    "QuestionID": 1001042,
                    "CultureID": 1,
                    "DisplayNameTranslation": "What is your postal code?",
                },
                "TranslatedAnswers": [{
                    "AnswerID": 2224508,
                    "Translation": "Postal code",
                    "AnswerInternalName": "postal",
                }],
                "AnswerType": "OpenEnd",
            },
            {
                "IsRoutable": True,
                "InternalName": "Toluna preliminary attribute",
                "TranslatedQuestion": {
                    "QuestionID": 2910077,
                    "CultureID": 1,
                    "DisplayNameTranslation": "Toluna preliminary attribute",
                },
                "TranslatedAnswers": [{
                    "AnswerID": 5312785,
                    "Translation": "Yes",
                    "AnswerInternalName": "Yes",
                }],
                "AnswerType": "SingleSelect",
            },
        ])
        quotas = copy.deepcopy(QUOTAS)
        quotas["Surveys"][0]["Quotas"][0]["Layers"].extend([
            {
                "LayerID": 3,
                "SubQuotas": [{
                    "SubQuotaID": 30,
                    "QuestionsAndAnswers": [{
                        "QuestionID": 1001107,
                        "AnswerIDs": [2002334],
                        "AnswerValues": [],
                        "IsRoutable": False,
                    }],
                }],
            },
            {
                "LayerID": 4,
                "SubQuotas": [{
                    "SubQuotaID": 40,
                    "QuestionsAndAnswers": [{
                        "QuestionID": 1002001,
                        "AnswerIDs": [3002001, 3002002],
                        "AnswerValues": [],
                        "IsRoutable": False,
                    }],
                }],
            },
            {
                "LayerID": 5,
                "SubQuotas": [{
                    "SubQuotaID": 50,
                    "QuestionsAndAnswers": [{
                        "QuestionID": 1003001,
                        "AnswerIDs": [3003001],
                        "AnswerValues": ["blue"],
                        "IsRoutable": False,
                    }],
                }],
            },
            {
                "LayerID": 6,
                "SubQuotas": [{
                    "SubQuotaID": 60,
                    "QuestionsAndAnswers": [{
                        "QuestionID": 1001042,
                        "AnswerIDs": [2224508],
                        "AnswerValues": ["100"],
                        "IsRoutable": False,
                    }],
                }],
            },
            {
                "LayerID": 7,
                "SubQuotas": [{
                    "SubQuotaID": 70,
                    "QuestionsAndAnswers": [{
                        "QuestionID": 2910077,
                        "AnswerIDs": [5312785],
                        "AnswerValues": [],
                        "IsRoutable": True,
                    }],
                }],
            },
        ])
        bootstrap = TolunaProvider(
            self.integration,
            session=RecordingSession(
                FakeResponse(CULTURES), FakeResponse(reference), FakeResponse(quotas)
            ),
        )
        normalized = bootstrap.normalize_inventory_item(
            bootstrap.inventory()[0], timezone.now()
        )
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key=normalized.source_key,
            **normalized.values,
        )
        bootstrap.refresh_details(survey)
        questions = {
            row.question_id: row for row in survey.targeting_questions.all()
        }
        request = RequestFactory().post("/survey/prescreener/", {
            f"question_{questions[1001538].pk}": "27",
            f"question_{questions[1001007].pk}": "2000247",
            f"question_{questions[1001107].pk}": "2002334",
            f"question_{questions[1002001].pk}": ["3002001", "3002002"],
            f"question_{questions[1003001].pk}": "blue",
            f"question_{questions[1001042].pk}": "10023",
            f"question_{questions[2910077].pk}": "5312785",
        })
        answers, errors = _collect_prescreener_answers(request, survey)
        self.assertEqual(errors, [])
        self.assertEqual(bootstrap._matching_quota(survey, answers).quota_id, 900)
        return survey, questions, answers

    def _assert_complete_member_contract_payload(
        self, survey, questions, payload, member_code
    ):
        self.assertEqual(payload["PartnerGUID"], "panel-guid")
        self.assertEqual(payload["MemberCode"], member_code)
        self.assertEqual(
            payload["BirthDate"], TolunaProvider._birth_date(27, member_code)
        )
        self.assertEqual(payload["PostalCode"], "10023")
        registration = {
            item["QuestionID"]: item["Answers"]
            for item in payload["RegistrationAnswers"]
        }
        self.assertEqual(registration[1001007], [{"AnswerID": 2000247}])
        self.assertEqual(registration[1001107], [{"AnswerID": 2002334}])
        self.assertCountEqual(
            registration[1002001],
            [{"AnswerID": 3002001}, {"AnswerID": 3002002}],
        )
        self.assertEqual(registration[1003001], [{
            "AnswerID": 3003001,
            "AnswerValue": "blue",
        }])
        self.assertEqual(registration[2910077], [{"AnswerID": 5312785}])

        required_for_member = {
            question.question_id
            for question in questions.values()
            if (question.raw_data or {}).get("required_for_member")
        }
        represented_in_payload = set(registration) | {1001538, 1001042}
        self.assertEqual(represented_in_payload, required_for_member | {2910077})
        self.assertTrue(questions[2910077].raw_data["required_by_provider"])
        self.assertFalse(questions[2910077].raw_data["required_for_member"])

    def test_inventory_uses_reference_and_quota_apis_without_persisting_panel_guid(self):
        session = RecordingSession(FakeResponse(CULTURES), FakeResponse(REFERENCE), FakeResponse(QUOTAS))
        provider = TolunaProvider(self.integration, session=session)
        rows = provider.inventory()
        normalized = provider.normalize_inventory_item(rows[0], timezone.now())

        self.assertEqual(normalized.source_key, "71:72")
        self.assertIsNone(normalized.numeric_source_id)
        self.assertEqual(normalized.values["cpi"], Decimal("2.75"))
        self.assertEqual(normalized.values["country_code"], "US")
        self.assertEqual(
            provider.inventory_cache_expires_at.isoformat(),
            "2026-08-18T10:00:00+00:00",
        )
        self.assertEqual(TolunaReferenceQuestion.objects.filter(integration=self.integration).count(), 2)
        self.assertNotIn("panel-guid", str(rows))
        self.assertEqual(session.calls[2][2]["headers"]["API_AUTH_KEY"], "api-key")

    def test_generic_typed_question_with_options_is_selectable(self):
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key="dummy-options",
            company_name="Toluna",
            name="Option rendering test",
            status=Survey.Status.LIVE,
        )
        TargetingQuestion.objects.create(
            survey=survey,
            question_id=919191,
            key="REGION",
            text="What is your region?",
            question_type="Dummy",
            category="Provider qualification",
            options=[
                {"OptionId": "north", "OptionText": "North"},
                {"OptionId": "south", "OptionText": "South"},
            ],
        )

        question = _prescreener_questions(survey)[0]

        self.assertEqual(question["input_kind"], "radio")
        self.assertEqual(
            [(item["value"], item["label"]) for item in question["options"]],
            [("north", "North"), ("south", "South")],
        )

    @patch("surveys.provider_services.get_provider")
    def test_inventory_sync_persists_stable_fallback_timestamps(self, get_provider_mock):
        get_provider_mock.return_value = TolunaProvider(
            self.integration,
            session=RecordingSession(FakeResponse(CULTURES), FakeResponse(REFERENCE), FakeResponse(QUOTAS)),
        )
        first = sync_client_integration(self.integration)
        survey = Survey.objects.get(integration=self.integration, source_key="71:72")
        created_at = survey.source_created_at
        modified_at = survey.source_modified_at

        self.assertEqual(first.created, 1)
        self.assertIsNotNone(created_at)
        self.assertEqual(modified_at, created_at)

        get_provider_mock.return_value = TolunaProvider(
            self.integration,
            session=RecordingSession(FakeResponse(CULTURES), FakeResponse(QUOTAS)),
        )
        second = sync_client_integration(self.integration)
        survey.refresh_from_db()

        self.assertEqual(second.unchanged, 1)
        self.assertEqual(survey.source_created_at, created_at)
        self.assertEqual(survey.source_modified_at, modified_at)

    @patch("surveys.provider_services.get_provider")
    def test_inventory_sync_marks_toluna_details_stale_when_quota_targeting_changes(
        self, get_provider_mock
    ):
        get_provider_mock.return_value = TolunaProvider(
            self.integration,
            session=RecordingSession(
                FakeResponse(CULTURES), FakeResponse(REFERENCE), FakeResponse(QUOTAS)
            ),
        )
        sync_client_integration(self.integration)
        survey = Survey.objects.get(
            integration=self.integration, source_key="71:72"
        )
        previous_detail_sync = timezone.now()
        survey.detail_synced_at = previous_detail_sync
        survey.save(update_fields=["detail_synced_at", "updated_at"])

        changed_quotas = copy.deepcopy(QUOTAS)
        targeting = changed_quotas["Surveys"][0]["Quotas"][0]["Layers"][0][
            "SubQuotas"
        ][0]["QuestionsAndAnswers"][0]
        targeting["AnswerIDs"] = [2006354]
        targeting["AnswerValues"] = ["30-34"]
        get_provider_mock.return_value = TolunaProvider(
            self.integration,
            session=RecordingSession(
                FakeResponse(CULTURES), FakeResponse(changed_quotas)
            ),
        )

        result = sync_client_integration(self.integration)
        survey.refresh_from_db()

        self.assertEqual(result.updated, 1)
        self.assertIsNone(survey.detail_synced_at)

    @patch("surveys.provider_services.get_provider")
    def test_inventory_capacity_change_updates_quota_without_invalidating_questions(
        self, get_provider_mock
    ):
        provider = TolunaProvider(
            self.integration,
            session=RecordingSession(
                FakeResponse(CULTURES), FakeResponse(REFERENCE), FakeResponse(QUOTAS)
            ),
        )
        get_provider_mock.return_value = provider
        sync_client_integration(self.integration)
        survey = Survey.objects.get(
            integration=self.integration, source_key="71:72"
        )
        provider.refresh_details(survey)
        survey.refresh_from_db()
        previous_detail_sync = survey.detail_synced_at
        quota = survey.quotas.get(quota_id=900)
        quota.status = "Full"
        quota.remaining = 0
        quota.save(update_fields=["status", "remaining", "updated_at"])
        old_notification = TolunaNotification.objects.create(
            event_type=TolunaNotification.EventType.QUOTA_STATUS,
            payload_hash="old-capacity-notification",
            integration=self.integration,
            survey=survey,
            provider_survey_id=71,
            wave_id=72,
            quota_id=900,
            provider_status="Unavailable",
            is_live=False,
            applied=True,
            raw_payload={
                "SurveyID": 71,
                "WaveID": 72,
                "QuotaID": 900,
                "IsLive": False,
            },
        )
        TolunaNotification.objects.filter(pk=old_notification.pk).update(
            received_at=timezone.now() - timedelta(minutes=5),
        )

        changed_quotas = copy.deepcopy(QUOTAS)
        changed_quota = changed_quotas["Surveys"][0]["Quotas"][0]
        changed_quota["EstimatedCompletesRemaining"] = 7
        changed_quota["Layers"][0]["SubQuotas"][0]["CurrentCompletes"] = 3
        get_provider_mock.return_value = TolunaProvider(
            self.integration,
            session=RecordingSession(FakeResponse(CULTURES), FakeResponse(changed_quotas)),
        )

        result = sync_client_integration(self.integration)

        survey.refresh_from_db()
        quota = survey.quotas.get(quota_id=900)
        self.assertEqual(result.updated, 1)
        self.assertEqual(survey.detail_synced_at, previous_detail_sync)
        self.assertEqual(quota.status, "Open")
        self.assertEqual(quota.remaining, 7)
        self.assertEqual(quota.raw_data["EstimatedCompletesRemaining"], 7)

    @patch("surveys.serializers.has_function_access", return_value=True)
    def test_blank_upstream_link_still_returns_toluna_platform_copy_link(self, _access):
        user = get_user_model().objects.create_user(
            username="toluna-copy", email="toluna-copy@example.test", password="test-password"
        )
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key="71:72",
            company_name="Toluna",
            name="Toluna test survey",
            status=Survey.Status.LIVE,
            entry_link="",
        )
        request = RequestFactory().get("/api/surveys/")
        request.user = user

        data = SurveyListSerializer(survey, context={"request": request}).data

        self.assertEqual(data["source_id"], "71:72")
        self.assertEqual(data["display_source_id"], "71")
        self.assertEqual(data["survey_id"], "71:72")
        self.assertIn("/survey/start?", data["start_link"])
        self.assertIn("surveyId=71%3A72", data["start_link"])

        survey.status = Survey.Status.CLOSED
        survey.save(update_fields=["status", "updated_at"])
        closed_data = SurveyListSerializer(
            survey, context={"request": request}
        ).data
        self.assertIsNone(closed_data["start_link"])

    def test_member_registration_quota_match_and_invite_build(self):
        bootstrap = TolunaProvider(
            self.integration,
            session=RecordingSession(FakeResponse(CULTURES), FakeResponse(REFERENCE), FakeResponse(QUOTAS)),
        )
        payload = bootstrap.inventory()[0]
        normalized = bootstrap.normalize_inventory_item(payload, timezone.now())
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key=normalized.source_key,
            **normalized.values,
        )
        bootstrap.refresh_details(survey)
        attempt = SurveyAttempt.objects.create(
            rid="Abc123XyZ9",
            prescreener_uid="Ab1c-De2f-Gh3i-Jk4l",
            survey=survey,
            user_id="1",
        )
        questions = {row.question_id: row for row in survey.targeting_questions.all()}
        self.assertEqual(questions[1001538].question_type, "numeric")
        self.assertEqual(questions[1001538].text, "What is your age?")
        targeting_only = TargetingQuestion.objects.create(
            survey=survey,
            question_id=2910077,
            key="toluna_2910077",
            text="Toluna preliminary attribute",
            question_type="single",
            category="Toluna targeting",
            options=[{"OptionId": 5312785, "OptionText": "Yes"}],
            raw_data={"toluna_kind": "profile"},
        )
        answers = {
            str(questions[1001538].pk): {
                "question_id": 1001538, "question_key": questions[1001538].key,
                "values": ["27"], "upstream_values": ["2006353"],
            },
            str(questions[1001007].pk): {
                "question_id": 1001007, "question_key": questions[1001007].key,
                "values": ["2000247"], "upstream_values": ["2000247"],
            },
            str(targeting_only.pk): {
                "question_id": 2910077, "question_key": targeting_only.key,
                "values": ["5312785"], "upstream_values": ["5312785"],
            },
        }
        invite = {
            "SurveyId": 71, "WaveID": 72, "QuotaID": 900,
            "MemberAmount": 0, "PartnerAmount": 3.25,
            "URL": "https://router.toluna.test/invite?token=abc", "LOI": 7, "IR": 40,
        }
        session = RecordingSession(FakeResponse(None, 201), FakeResponse(invite))
        provider = TolunaProvider(self.integration, session=session)
        outbound = provider.build_outbound_url(survey, attempt, answers)

        self.assertEqual([call[0] for call in session.calls], ["POST", "GET"])
        member_body = session.calls[0][2]["json"]
        self.assertEqual(member_body["PartnerGUID"], "panel-guid")
        self.assertEqual(member_body["MemberCode"], attempt.prescreener_uid)
        self.assertNotIn("PostalCode", member_body)
        born = datetime.strptime(member_body["BirthDate"], "%m/%d/%Y").date()
        calculated_age = date.today().year - born.year - (
            (date.today().month, date.today().day) < (born.month, born.day)
        )
        self.assertEqual(calculated_age, 27)
        self.assertEqual(
            member_body["BirthDate"],
            TolunaProvider._birth_date(27, attempt.prescreener_uid),
        )
        self.assertEqual(provider.last_member_summary, {
            "member_id": attempt.prescreener_uid,
            "birth_date": member_body["BirthDate"],
        })
        self.assertEqual(len(member_body["RegistrationAnswers"]), 1)
        self.assertEqual(member_body["RegistrationAnswers"][0]["QuestionID"], 1001007)
        self.assertEqual(parse_qs(urlsplit(outbound).query)["rid"], [attempt.rid])
        self.assertEqual(attempt.source_cpi_snapshot, Decimal("3.25"))
        self.assertTrue(TolunaMember.objects.get(member_code=attempt.prescreener_uid).is_registered)

        # The same vault UID and unchanged profile must not be registered a
        # second time. Only a fresh invite is requested for the new journey.
        repeat_session = RecordingSession(FakeResponse(invite))
        repeat_outbound = TolunaProvider(
            self.integration, session=repeat_session
        ).build_outbound_url(survey, attempt, answers)
        self.assertEqual([call[0] for call in repeat_session.calls], ["GET"])
        self.assertEqual(parse_qs(urlsplit(repeat_outbound).query)["rid"], [attempt.rid])

    def test_invite_quota_must_exactly_match_selected_local_quota(self):
        survey, _questions, answers = self._complete_member_contract_case()
        attempt = SurveyAttempt.objects.create(
            rid="Qta123AbC9",
            prescreener_uid="Qt1a-Mi2s-Ma3t-Ch4x",
            survey=survey,
            user_id="invite-quota-mismatch-user",
        )
        mismatched_invite = {
            "SurveyId": 71,
            "WaveID": 72,
            "QuotaID": 901,
            "MemberAmount": 0,
            "PartnerAmount": 3.25,
            "URL": "https://router.toluna.test/invite?token=wrong-quota",
            "LOI": 7,
            "IR": 40,
        }
        session = RecordingSession(
            FakeResponse(None, 201), FakeResponse(mismatched_invite)
        )
        provider = TolunaProvider(self.integration, session=session)

        with self.assertRaises(ProviderError):
            provider.build_outbound_url(survey, attempt, answers)

        self.assertEqual([call[0] for call in session.calls], ["POST", "GET"])
        self.assertEqual(attempt.outbound_url, "")
        self.assertIsNone(attempt.source_cpi_snapshot)
        self.assertEqual(attempt.upstream_transaction_data, {})

    def test_invite_url_query_is_preserved_byte_for_byte_when_rid_is_appended(self):
        survey, _questions, answers = self._complete_member_contract_case()
        attempt = SurveyAttempt.objects.create(
            rid="Url123AbC9",
            prescreener_uid="Ur1l-By2t-Es3a-Fe4x",
            survey=survey,
            user_id="opaque-invite-url-user",
        )
        provider_url = (
            "https://router.toluna.test/invite?"
            "token=a%2Fb%20c&sig=AbC%2B123%3D%3D&blank=&dup=one&dup=two"
            "#provider-fragment&rid=FragmentOnly"
        )
        invite = {
            "SurveyId": 71,
            "WaveID": 72,
            "QuotaID": 900,
            "MemberAmount": 0,
            "PartnerAmount": 3.25,
            "URL": provider_url,
            "LOI": 7,
            "IR": 40,
        }
        session = RecordingSession(FakeResponse(None, 201), FakeResponse(invite))
        provider = TolunaProvider(self.integration, session=session)

        outbound = provider.build_outbound_url(survey, attempt, answers)

        self.assertEqual(
            outbound,
            "https://router.toluna.test/invite?"
            "token=a%2Fb%20c&sig=AbC%2B123%3D%3D&blank=&dup=one&dup=two"
            f"&rid={attempt.rid}#provider-fragment&rid=FragmentOnly",
        )

    def test_member_create_posts_complete_required_profile_before_invite(self):
        survey, questions, answers = self._complete_member_contract_case()
        attempt = SurveyAttempt.objects.create(
            rid="Crt123AbC9",
            prescreener_uid="Cr1e-At2e-Uid3-Test",
            survey=survey,
            user_id="complete-create-user",
        )
        invite = {
            "SurveyId": 71,
            "WaveID": 72,
            "QuotaID": 900,
            "MemberAmount": 0,
            "PartnerAmount": 3.25,
            "URL": "https://router.toluna.test/invite?token=complete-create",
            "LOI": 7,
            "IR": 40,
        }
        session = RecordingSession(FakeResponse(None, 201), FakeResponse(invite))
        provider = TolunaProvider(self.integration, session=session)

        outbound = provider.build_outbound_url(survey, attempt, answers)

        self.assertEqual([call[0] for call in session.calls], ["POST", "GET"])
        member_url = (
            "https://ip.surveyrouter.com/IntegratedPanelService/api/Respondent"
        )
        self.assertEqual(session.calls[0][1], member_url)
        member_body = session.calls[0][2]["json"]
        self._assert_complete_member_contract_payload(
            survey, questions, member_body, attempt.prescreener_uid
        )
        invite_url = (
            "https://tws.toluna.com/IPExternalSamplingService/ExternalSample/"
            f"panel-guid/{member_body['MemberCode']}/Invite/900"
        )
        self.assertEqual(session.calls[1][1], invite_url)
        self.assertEqual(
            provider.last_member_summary["member_id"], member_body["MemberCode"]
        )
        self.assertEqual(parse_qs(urlsplit(outbound).query)["rid"], [attempt.rid])

    def test_reused_member_update_puts_complete_profile_before_same_member_invite(self):
        survey, questions, answers = self._complete_member_contract_case()
        reused_member_code = "Old1-Mem2-Ber3-Code"
        attempt = SurveyAttempt.objects.create(
            rid="Upd123AbC9",
            prescreener_uid="Ne1w-Jou2-Rne3-Yuid",
            provider_profile_uid=reused_member_code,
            survey=survey,
            user_id="complete-update-user",
        )
        member = TolunaMember.objects.create(
            integration=self.integration,
            member_code=reused_member_code,
            culture_code="en-us",
            profile_hash="stale-profile-hash",
            is_registered=True,
        )
        invite = {
            "SurveyId": 71,
            "WaveID": 72,
            "QuotaID": 900,
            "MemberAmount": 0,
            "PartnerAmount": 3.25,
            "URL": "https://router.toluna.test/invite?token=complete-update",
            "LOI": 7,
            "IR": 40,
        }
        session = RecordingSession(
            FakeResponse(None, status_code=200), FakeResponse(invite)
        )
        provider = TolunaProvider(self.integration, session=session)

        outbound = provider.build_outbound_url(survey, attempt, answers)

        self.assertEqual([call[0] for call in session.calls], ["PUT", "GET"])
        member_url = (
            "https://ip.surveyrouter.com/IntegratedPanelService/api/Respondent"
        )
        self.assertEqual(session.calls[0][1], member_url)
        member_body = session.calls[0][2]["json"]
        self._assert_complete_member_contract_payload(
            survey, questions, member_body, reused_member_code
        )
        self.assertNotEqual(member_body["MemberCode"], attempt.prescreener_uid)
        invite_url = (
            "https://tws.toluna.com/IPExternalSamplingService/ExternalSample/"
            f"panel-guid/{reused_member_code}/Invite/900"
        )
        self.assertEqual(session.calls[1][1], invite_url)
        self.assertNotIn(attempt.prescreener_uid, session.calls[1][1])
        self.assertEqual(
            provider.last_member_summary["member_id"], reused_member_code
        )
        member.refresh_from_db()
        self.assertNotEqual(member.profile_hash, "stale-profile-hash")
        self.assertIsNotNone(member.last_synced_at)
        self.assertEqual(parse_qs(urlsplit(outbound).query)["rid"], [attempt.rid])

    def test_required_member_answers_fail_closed_before_member_http(self):
        survey, questions, answers = self._complete_member_contract_case()
        attempt = SurveyAttempt.objects.create(
            rid="Req123Fail",
            prescreener_uid="Re1q-Fa2i-Lu3r-Test",
            survey=survey,
            user_id="required-member-answer-user",
        )
        session = RecordingSession()
        provider = TolunaProvider(self.integration, session=session)

        missing_postal = copy.deepcopy(answers)
        missing_postal.pop(str(questions[1001042].pk))
        with self.assertRaisesRegex(ProviderError, "required question 1001042"):
            provider._register_member(survey, attempt, missing_postal)

        open_question = questions[1003001]
        open_question.options = []
        open_question.raw_data = {
            **open_question.raw_data,
            "allowed_answer_ids": [],
        }
        open_question.save(update_fields=["options", "raw_data"])
        with self.assertRaisesRegex(ProviderError, "no unambiguous open-answer mapping"):
            provider._register_member(survey, attempt, answers)

        self.assertEqual(session.calls, [])
        self.assertFalse(TolunaMember.objects.filter(
            integration=self.integration,
            member_code=attempt.prescreener_uid,
        ).exists())

    def test_numeric_open_text_is_sent_as_answer_value_not_answer_id(self):
        survey, questions, answers = self._complete_member_contract_case()
        open_answer = answers[str(questions[1003001].pk)]
        open_answer["values"] = ["12345"]
        open_answer["upstream_values"] = ["12345"]
        attempt = SurveyAttempt.objects.create(
            rid="Txt123Open",
            prescreener_uid="Te1x-Va2l-Ue3s-Test",
            survey=survey,
            user_id="numeric-open-answer-user",
        )

        payload = TolunaProvider(self.integration)._member_payload(
            survey, attempt, answers
        )
        registration = {
            item["QuestionID"]: item["Answers"]
            for item in payload["RegistrationAnswers"]
        }
        self.assertEqual(registration[1003001], [{
            "AnswerID": 3003001,
            "AnswerValue": "12345",
        }])

    def test_routable_postal_is_still_sent_as_core_member_property(self):
        survey, questions, answers = self._complete_member_contract_case()
        postal = questions[1001042]
        postal.raw_data = {
            **postal.raw_data,
            "toluna_is_routable": True,
            "required_for_member": False,
        }
        postal.save(update_fields=["raw_data"])
        TolunaReferenceQuestion.objects.filter(
            integration=self.integration,
            culture_code="en-us",
            question_id=1001042,
        ).update(is_routable=True)
        attempt = SurveyAttempt.objects.create(
            rid="Zip123Core",
            prescreener_uid="Zi1p-Co2r-Ep3r-Test",
            survey=survey,
            user_id="routable-postal-user",
        )

        payload = TolunaProvider(self.integration)._member_payload(
            survey, attempt, answers
        )

        self.assertEqual(payload["PostalCode"], "10023")
        self.assertNotIn(
            1001042,
            {item["QuestionID"] for item in payload["RegistrationAnswers"]},
        )

    def test_quota_serializer_uses_readable_scope_and_targeting_instead_of_id(self):
        provider = TolunaProvider(
            self.integration,
            session=RecordingSession(FakeResponse(CULTURES), FakeResponse(REFERENCE), FakeResponse(QUOTAS)),
        )
        normalized = provider.normalize_inventory_item(provider.inventory()[0], timezone.now())
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key=normalized.source_key,
            **normalized.values,
        )
        provider.refresh_details(survey)

        data = SurveyQuotaSerializer(survey.quotas.get()).data

        self.assertEqual(data["display_name"], "Targeted respondent quota")
        self.assertEqual(data["scope_label"], "Targeted respondent quota")
        self.assertNotIn(str(data["quota_id"]), data["display_name"])
        self.assertEqual(
            {row["name"] for row in data["targeting_details"]},
            {"What is your age?", "What is your gender?"},
        )

    def test_quota_serializer_preserves_layers_and_resolves_routable_reference_labels(self):
        reference = copy.deepcopy(REFERENCE)
        reference.append({
            "IsRoutable": True,
            "InternalName": "Annual Household Income",
            "TranslatedQuestion": {
                "QuestionID": 1001107,
                "CultureID": 1,
                "DisplayNameTranslation": "Household Yearly Income (Gross):",
            },
            "TranslatedAnswers": [{
                "AnswerID": 2002334,
                "Translation": "$200,000+",
                "AnswerInternalName": "$200,000+",
            }],
            "AnswerType": "SingleSelect",
        })
        quotas = copy.deepcopy(QUOTAS)
        quotas["Surveys"][0]["Quotas"][0]["Layers"] = [
            {
                "LayerID": 101,
                "SubQuotas": [
                    {
                        "SubQuotaID": 1001,
                        "CurrentCompletes": 4,
                        "MaxTargetCompletes": 10,
                        "QuestionsAndAnswers": [{
                            "QuestionID": 1001107,
                            "AnswerIDs": [2002334],
                            "AnswerValues": [],
                            "IsRoutable": True,
                        }],
                    },
                    {
                        "SubQuotaID": 1002,
                        "CurrentCompletes": 8,
                        "MaxTargetCompletes": 8,
                        "QuestionsAndAnswers": [{
                            "QuestionID": 1001538,
                            "AnswerIDs": [2006361],
                            "AnswerValues": [],
                            "IsRoutable": False,
                        }],
                    },
                ],
            },
            {
                "LayerID": 102,
                "SubQuotas": [{
                    "SubQuotaID": 1003,
                    "QuestionsAndAnswers": [{
                        "QuestionID": 1001007,
                        "AnswerIDs": [2000246, 2000247],
                        "AnswerValues": [],
                        "IsRoutable": False,
                    }],
                }],
            },
        ]
        provider = TolunaProvider(
            self.integration,
            session=RecordingSession(
                FakeResponse(CULTURES), FakeResponse(reference), FakeResponse(quotas)
            ),
        )
        normalized = provider.normalize_inventory_item(provider.inventory()[0], timezone.now())
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key=normalized.source_key,
            **normalized.values,
        )
        provider.refresh_details(survey)

        self.assertTrue(survey.targeting_questions.filter(question_id=1001107).exists())
        data = SurveyQuotaSerializer(survey.quotas.get()).data
        self.assertEqual(len(data["toluna_layers"]), 2)
        first_layer = data["toluna_layers"][0]
        self.assertEqual(len(first_layer["subquotas"]), 2)
        income_segment, age_segment = first_layer["subquotas"]
        self.assertEqual(income_segment["target"], 10)
        self.assertEqual(income_segment["completed"], 4)
        self.assertEqual(income_segment["remaining"], 6)
        self.assertEqual(income_segment["status"], "Open")
        self.assertEqual(income_segment["targeting_details"], [{
            "question_id": "1001107",
            "name": "Household Yearly Income (Gross):",
            "values": ["$200,000+"],
            "is_routable": True,
        }])
        self.assertEqual(age_segment["status"], "Full")
        self.assertEqual(age_segment["targeting_details"][0]["values"], ["65\u201399"])
        no_capacity = data["toluna_layers"][1]["subquotas"][0]
        self.assertFalse(no_capacity["target_known"])
        self.assertFalse(no_capacity["completed_known"])
        self.assertFalse(no_capacity["remaining_known"])
        self.assertIsNone(no_capacity["remaining"])

    def test_quota_serializer_hides_unmapped_provider_ids(self):
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key="unmapped-reference",
            raw_data={"_toluna": {"culture_code": "en-us"}},
        )
        quota = SurveyQuota.objects.create(
            survey=survey,
            source_key="404",
            quota_id=404,
            raw_data={
                "Layers": [{
                    "LayerID": 1,
                    "SubQuotas": [{
                        "SubQuotaID": 2,
                        "QuestionsAndAnswers": [{
                            "QuestionID": 9999999,
                            "AnswerIDs": [8888888],
                            "AnswerValues": [],
                            "IsRoutable": True,
                        }],
                    }],
                }],
            },
        )

        detail = SurveyQuotaSerializer(quota).data["toluna_layers"][0]["subquotas"][0][
            "targeting_details"
        ][0]

        self.assertEqual(detail["name"], "Toluna qualification")
        self.assertEqual(detail["values"], ["Provider-defined answer"])

    def test_routable_quota_attribute_is_required_locally_and_registered(self):
        reference = copy.deepcopy(REFERENCE)
        reference.append({
            "IsRoutable": True,
            "InternalName": "Toluna preliminary attribute",
            "TranslatedQuestion": {
                "QuestionID": 2910077,
                "CultureID": 1,
                "DisplayNameTranslation": "Toluna preliminary attribute",
            },
            "TranslatedAnswers": [
                {"AnswerID": 5312785, "Translation": "Yes", "AnswerInternalName": "Yes"},
            ],
            "AnswerType": "SingleSelect",
        })
        quotas = copy.deepcopy(QUOTAS)
        quotas["Surveys"][0]["Quotas"][0]["Layers"].append({
            "LayerID": 3,
            "SubQuotas": [{
                "SubQuotaID": 30,
                "QuestionsAndAnswers": [{
                    "QuestionID": 2910077,
                    "AnswerIDs": [5312785],
                    "AnswerValues": [],
                    "IsRoutable": True,
                }],
            }],
        })
        provider = TolunaProvider(
            self.integration,
            session=RecordingSession(FakeResponse(CULTURES), FakeResponse(reference), FakeResponse(quotas)),
        )
        normalized = provider.normalize_inventory_item(provider.inventory()[0], timezone.now())
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key=normalized.source_key,
            **normalized.values,
        )
        provider.refresh_details(survey)

        self.assertTrue(survey.targeting_questions.filter(question_id=2910077).exists())
        self.assertTrue(
            survey.targeting_questions.filter(raw_data__adapter_version=7).exists()
        )

        questions = {row.question_id: row for row in survey.targeting_questions.all()}
        routable = questions[2910077]
        self.assertTrue(routable.raw_data["required_by_provider"])
        self.assertTrue(routable.raw_data["toluna_is_routable"])
        self.assertFalse(routable.raw_data["required_for_member"])
        prepared = {
            item["model"].question_id: item for item in _prescreener_questions(survey)
        }
        self.assertEqual(prepared[2910077]["input_kind"], "radio")
        self.assertEqual(
            [(item["value"], item["label"]) for item in prepared[2910077]["options"]],
            [("5312785", "Yes")],
        )
        answers = {
            str(questions[1001538].pk): {
                "question_id": 1001538, "values": ["27"], "upstream_values": ["2006353"],
            },
            str(questions[1001007].pk): {
                "question_id": 1001007, "values": ["2000247"], "upstream_values": ["2000247"],
            },
            str(routable.pk): {
                "question_id": 2910077, "values": ["5312785"], "upstream_values": ["5312785"],
            },
        }
        matched = provider._matching_quota(survey, answers)
        self.assertEqual(matched.quota_id, 900)

        attempt = SurveyAttempt.objects.create(
            rid="Rou123Tab9",
            prescreener_uid="Ro1u-Ta2b-Le3q-Ue4s",
            survey=survey,
            user_id="routable-user",
        )
        member = provider._member_payload(survey, attempt, answers)
        registered_question_ids = {
            item["QuestionID"] for item in member["RegistrationAnswers"]
        }
        self.assertIn(1001007, registered_question_ids)
        self.assertIn(2910077, registered_question_ids)
        routable_answer = next(
            item for item in member["RegistrationAnswers"]
            if item["QuestionID"] == 2910077
        )
        self.assertEqual(routable_answer["Answers"], [{"AnswerID": 5312785}])

        routable.options = []
        routable.raw_data = {
            **routable.raw_data,
            "allowed_answer_ids": [],
        }
        routable.save(update_fields=["options", "raw_data"])
        unmapped_member = provider._member_payload(survey, attempt, answers)
        self.assertNotIn(
            2910077,
            {
                item["QuestionID"]
                for item in unmapped_member["RegistrationAnswers"]
            },
        )

    def test_non_routable_quota_questions_are_required_and_show_only_provider_values(self):
        reference = copy.deepcopy(REFERENCE)
        reference.extend([
            {
                "IsRoutable": False,
                "InternalName": "Annual Household Income",
                "TranslatedQuestion": {
                    "QuestionID": 1001107,
                    "CultureID": 1,
                    "DisplayNameTranslation": "What is your household income?",
                },
                "TranslatedAnswers": [
                    {
                        "AnswerID": 2002333,
                        "Translation": "$100,000-$199,999",
                        "AnswerInternalName": "income-mid",
                    },
                    {
                        "AnswerID": 2002334,
                        "Translation": "$200,000+",
                        "AnswerInternalName": "income-high",
                    },
                ],
                "AnswerType": "SingleSelect",
            },
            {
                "IsRoutable": False,
                "InternalName": "Postal Code",
                "TranslatedQuestion": {
                    "QuestionID": 1001042,
                    "CultureID": 1,
                    "DisplayNameTranslation": "What is your postal code?",
                },
                "TranslatedAnswers": [{
                    "AnswerID": 2224508,
                    "Translation": "Postal code",
                    "AnswerInternalName": "postal",
                }],
                "AnswerType": "OpenEnd",
            },
        ])
        quotas = copy.deepcopy(QUOTAS)
        quotas["Surveys"][0]["Quotas"][0]["Layers"].extend([
            {
                "LayerID": 3,
                "SubQuotas": [{
                    "SubQuotaID": 30,
                    "QuestionsAndAnswers": [{
                        "QuestionID": 1001107,
                        "AnswerIDs": [2002334],
                        "AnswerValues": [],
                        "IsRoutable": False,
                    }],
                }],
            },
            {
                "LayerID": 4,
                "SubQuotas": [{
                    "SubQuotaID": 40,
                    "QuestionsAndAnswers": [{
                        "QuestionID": 1001042,
                        "AnswerIDs": [2224508],
                        "AnswerValues": ["100, 90210"],
                        "IsRoutable": False,
                    }],
                }],
            },
        ])
        provider = TolunaProvider(
            self.integration,
            session=RecordingSession(
                FakeResponse(CULTURES), FakeResponse(reference), FakeResponse(quotas)
            ),
        )
        normalized = provider.normalize_inventory_item(provider.inventory()[0], timezone.now())
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key=normalized.source_key,
            **normalized.values,
        )

        provider.refresh_details(survey)

        questions = {row.question_id: row for row in survey.targeting_questions.all()}
        income = questions[1001107]
        postal = questions[1001042]
        self.assertTrue(income.raw_data["required_by_provider"])
        self.assertTrue(postal.raw_data["required_by_provider"])
        self.assertEqual(
            [(option["OptionId"], option["OptionText"]) for option in income.options],
            [(2002334, "$200,000+")],
        )

        prepared = {
            item["model"].question_id: item for item in _prescreener_questions(survey)
        }
        self.assertEqual(prepared[1001107]["input_kind"], "radio")
        self.assertEqual(
            [item["value"] for item in prepared[1001107]["options"]],
            ["2002334"],
        )
        self.assertEqual(
            prepared[1001107]["targeting_note"],
            "Only answers accepted by this survey are shown.",
        )
        self.assertEqual(prepared[1001042]["input_kind"], "text")
        self.assertEqual(prepared[1001042]["allowed_values"], ["100", "90210"])
        self.assertTrue(prepared[1001042]["postal_prefix_match"])
        self.assertEqual(
            prepared[1001042]["targeting_note"],
            "Required postal codes or prefixes: 100, 90210",
        )

        age = questions[1001538]
        gender = questions[1001007]
        incomplete_request = RequestFactory().post("/survey/prescreener/", {
            f"question_{age.pk}": "27",
            f"question_{gender.pk}": "2000247",
        })
        _, errors = _collect_prescreener_answers(incomplete_request, survey)
        self.assertIn("Please answer: What is your household income?", errors)
        self.assertIn("Please answer: What is your postal code?", errors)

        accepted_request = RequestFactory().post("/survey/prescreener/", {
            f"question_{age.pk}": "27",
            f"question_{gender.pk}": "2000247",
            f"question_{income.pk}": "2002334",
            # 100 is a provider-returned prefix; retain the respondent's full
            # ZIP for the Toluna member payload and quota matching.
            f"question_{postal.pk}": "10023",
        })
        collected, errors = _collect_prescreener_answers(accepted_request, survey)
        self.assertEqual(errors, [])
        self.assertEqual(collected[str(postal.pk)]["values"], ["10023"])
        self.assertEqual(collected[str(postal.pk)]["upstream_values"], ["10023"])

        rejected_request = RequestFactory().post("/survey/prescreener/", {
            f"question_{age.pk}": "27",
            f"question_{gender.pk}": "2000247",
            f"question_{income.pk}": "2002334",
            f"question_{postal.pk}": "77777",
        })
        _, errors = _collect_prescreener_answers(rejected_request, survey)
        self.assertIn(
            "Enter a ZIP/postal code accepted by this survey for: What is your postal code?",
            errors,
        )

        attempt = SurveyAttempt.objects.create(
            rid="Req123AbC9",
            prescreener_uid="Rq1a-Bc2d-Ef3g-Hi4j",
            survey=survey,
            user_id="required-user",
        )
        answers = {
            str(age.pk): {
                "question_id": age.question_id,
                "values": ["27"],
                "upstream_values": ["27"],
            },
            str(gender.pk): {
                "question_id": gender.question_id,
                "values": ["2000247"],
                "upstream_values": ["2000247"],
            },
            str(income.pk): {
                "question_id": income.question_id,
                "values": ["2002334"],
                "upstream_values": ["2002334"],
            },
            str(postal.pk): {
                "question_id": postal.question_id,
                "values": ["10023"],
                "upstream_values": ["10023"],
            },
        }
        payload = provider._member_payload(survey, attempt, answers)
        registration = {
            item["QuestionID"]: item["Answers"]
            for item in payload["RegistrationAnswers"]
        }
        self.assertEqual(registration[1001007], [{"AnswerID": 2000247}])
        self.assertEqual(registration[1001107], [{"AnswerID": 2002334}])
        self.assertEqual(payload["PostalCode"], "10023")
        postal_requirement = quotas["Surveys"][0]["Quotas"][0]["Layers"][3][
            "SubQuotas"
        ][0]["QuestionsAndAnswers"][0]
        self.assertTrue(provider._answer_matches(
            postal_requirement, answers[str(postal.pk)], postal
        ))
        rejected_postal = {
            **answers[str(postal.pk)],
            "values": ["77777"],
            "upstream_values": ["77777"],
        }
        self.assertFalse(provider._answer_matches(
            postal_requirement, rejected_postal, postal
        ))

        # Some Toluna open-ended questions supply only a synthetic AnswerID.
        # It is provider metadata, not a value the respondent should type.
        postal.raw_data = {
            **postal.raw_data,
            "allowed_answer_values": [],
        }
        postal.save(update_fields=["raw_data"])
        open_postal = next(
            item for item in _prescreener_questions(survey)
            if item["model"].question_id == 1001042
        )
        self.assertEqual(open_postal["allowed_values"], [])
        self.assertFalse(open_postal["postal_prefix_match"])
        unrestricted_request = RequestFactory().post("/survey/prescreener/", {
            f"question_{age.pk}": "27",
            f"question_{gender.pk}": "2000247",
            f"question_{income.pk}": "2002334",
            f"question_{postal.pk}": "60601",
        })
        unrestricted_answers, errors = _collect_prescreener_answers(
            unrestricted_request, survey
        )
        self.assertEqual(errors, [])
        synthetic_only_requirement = {
            **postal_requirement,
            "AnswerValues": [],
        }
        self.assertTrue(provider._answer_matches(
            synthetic_only_requirement,
            unrestricted_answers[str(postal.pk)],
            postal,
        ))

        # Malformed provider values must never normalize to an empty prefix,
        # because every submitted postal code would otherwise match it.
        postal.raw_data = {
            **postal.raw_data,
            "allowed_answer_values": ["-"],
        }
        postal.save(update_fields=["raw_data"])
        malformed_postal = next(
            item for item in _prescreener_questions(survey)
            if item["model"].question_id == 1001042
        )
        self.assertEqual(malformed_postal["allowed_values"], [])
        self.assertTrue(malformed_postal["postal_prefix_match"])
        _, errors = _collect_prescreener_answers(unrestricted_request, survey)
        self.assertIn(
            "Enter a ZIP/postal code accepted by this survey for: What is your postal code?",
            errors,
        )
        malformed_requirement = {
            **postal_requirement,
            "AnswerValues": ["-"],
        }
        self.assertFalse(provider._answer_matches(
            malformed_requirement,
            unrestricted_answers[str(postal.pk)],
            postal,
        ))

    def test_prescreener_lists_every_open_postal_value_without_layer_names(self):
        first_values = [f"10{index:03d}" for index in range(15)]
        second_values = [f"20{index:03d}" for index in range(15)]

        def quota(quota_id, values, *, remaining=5, current=0, maximum=5):
            return {
                "QuotaID": quota_id,
                "EstimatedCompletesRemaining": remaining,
                "Layers": [{
                    "LayerID": quota_id * 10,
                    "SubQuotas": [{
                        "SubQuotaID": quota_id * 100,
                        "CurrentCompletes": current,
                        "MaxTargetCompletes": maximum,
                        "QuestionsAndAnswers": [{
                            "QuestionID": 1001042,
                            "AnswerIDs": [2224508],
                            "AnswerValues": values,
                            "IsRoutable": False,
                        }],
                    }],
                }],
            }

        open_quotas = [
            quota(1, first_values),
            quota(2, second_values),
        ]
        blocked_by_full_layer = quota(5, ["77777"])
        blocked_by_full_layer["Layers"].append({
            "LayerID": 51,
            "SubQuotas": [{
                "SubQuotaID": 501,
                "CurrentCompletes": 5,
                "MaxTargetCompletes": 5,
                "QuestionsAndAnswers": [{
                    "QuestionID": 1001538,
                    "AnswerValues": ["25-29"],
                }],
            }],
        })
        requirements = TolunaProvider._quota_question_rows([
            *open_quotas,
            quota(3, ["99999"], remaining=0),
            quota(4, ["88888"], current=5, maximum=5),
            blocked_by_full_layer,
        ])
        merged_values = requirements[1001042]["answer_values"]
        self.assertEqual(merged_values, first_values + second_values)
        self.assertNotIn("99999", merged_values)
        self.assertNotIn("88888", merged_values)
        self.assertNotIn("77777", merged_values)

        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key="toluna-all-postal-values",
            name="Toluna all postal values",
            status=Survey.Status.LIVE,
            raw_data={"_toluna": {"culture_code": "en-us"}},
        )
        postal = TargetingQuestion.objects.create(
            survey=survey,
            question_id=1001042,
            key="TOLUNA_1001042",
            text="What is your postal code?",
            question_type="text",
            category="Required profile",
            options=[],
            raw_data={
                "adapter_version": 7,
                "toluna_kind": "postal",
                "required_by_provider": True,
                "required_for_member": True,
                "reference_answer_type": "OpenEnd",
                "allowed_answer_ids": [2224508],
                "allowed_answer_values": merged_values,
            },
        )
        for raw_quota in open_quotas:
            SurveyQuota.objects.create(
                survey=survey,
                source_key=str(raw_quota["QuotaID"]),
                quota_id=raw_quota["QuotaID"],
                remaining=5,
                raw_data=raw_quota,
            )

        prepared = _prescreener_questions(survey)
        field = prepared[0]
        displayed_values = [
            value for group in field["targeting_value_groups"] for value in group
        ]
        self.assertEqual(field["targeting_value_count"], 30)
        self.assertEqual(displayed_values, first_values + second_values)
        self.assertEqual(
            field["targeting_note"],
            "Required postal codes or prefixes: all 30 accepted values are listed below.",
        )
        html = render_to_string("surveys/prescreener.html", {
            "attempt": type("Attempt", (), {"rid": "ZipLayer01"})(),
            "questions": prepared,
            "is_rfg": False,
        })
        self.assertIn(first_values[0], html)
        self.assertIn(second_values[-1], html)
        self.assertIn("All 30 accepted ZIP / postal values", html)
        self.assertNotIn("LayerID", html)
        self.assertNotIn("(+10 more)", html)

        request = RequestFactory().post("/survey/prescreener/", {
            f"question_{postal.pk}": second_values[-1],
        })
        answers, errors = _collect_prescreener_answers(request, survey)
        self.assertEqual(errors, [])
        self.assertEqual(
            TolunaProvider(self.integration, session=RecordingSession())
            ._matching_quota(survey, answers).quota_id,
            2,
        )

    def test_multiple_ranges_for_same_question_are_or_conditions(self):
        quotas = copy.deepcopy(QUOTAS)
        quotas["Surveys"][0]["Quotas"][0]["Layers"][0]["SubQuotas"][0][
            "QuestionsAndAnswers"
        ] = [
            {
                "QuestionID": 1001538,
                "AnswerIDs": [],
                "AnswerValues": ["13-17"],
                "IsRoutable": False,
            },
            {
                "QuestionID": 1001538,
                "AnswerIDs": [],
                "AnswerValues": ["18-24"],
                "IsRoutable": False,
            },
            {
                "QuestionID": 1001538,
                "AnswerIDs": [],
                "AnswerValues": ["25-29"],
                "IsRoutable": False,
            },
        ]
        provider = TolunaProvider(
            self.integration,
            session=RecordingSession(FakeResponse(CULTURES), FakeResponse(REFERENCE), FakeResponse(quotas)),
        )
        normalized = provider.normalize_inventory_item(provider.inventory()[0], timezone.now())
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key=normalized.source_key,
            **normalized.values,
        )
        provider.refresh_details(survey)
        questions = {row.question_id: row for row in survey.targeting_questions.all()}
        answers = {
            str(questions[1001538].pk): {
                "question_id": 1001538,
                "values": ["23"],
                "upstream_values": ["23"],
            },
            str(questions[1001007].pk): {
                "question_id": 1001007,
                "values": ["2000247"],
                "upstream_values": ["2000247"],
            },
        }

        matched = provider._matching_quota(survey, answers)

        self.assertEqual(matched.quota_id, 900)

    def test_matching_quota_skips_full_subquota_segments(self):
        quotas = copy.deepcopy(QUOTAS)
        age_layer = quotas["Surveys"][0]["Quotas"][0]["Layers"][0]
        age_layer["SubQuotas"] = [
            {
                "SubQuotaID": 10,
                "CurrentCompletes": 10,
                "MaxTargetCompletes": 10,
                "QuestionsAndAnswers": [{
                    "QuestionID": 1001538,
                    "AnswerIDs": [],
                    "AnswerValues": ["25-29"],
                }],
            },
            {
                "SubQuotaID": 11,
                "CurrentCompletes": 0,
                "MaxTargetCompletes": 10,
                "QuestionsAndAnswers": [{
                    "QuestionID": 1001538,
                    "AnswerIDs": [],
                    "AnswerValues": ["30-45"],
                }],
            },
        ]
        provider = TolunaProvider(
            self.integration,
            session=RecordingSession(
                FakeResponse(CULTURES), FakeResponse(REFERENCE), FakeResponse(quotas)
            ),
        )
        normalized = provider.normalize_inventory_item(provider.inventory()[0], timezone.now())
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key=normalized.source_key,
            **normalized.values,
        )
        provider.refresh_details(survey)
        questions = {row.question_id: row for row in survey.targeting_questions.all()}
        answers = {
            str(questions[1001538].pk): {
                "question_id": 1001538,
                "values": ["27"],
                "upstream_values": ["27"],
            },
            str(questions[1001007].pk): {
                "question_id": 1001007,
                "values": ["2000247"],
                "upstream_values": ["2000247"],
            },
        }

        with self.assertRaisesRegex(ProviderError, "does not match an open Toluna quota"):
            provider._matching_quota(survey, answers)

    def test_text_and_answer_id_age_ranges_are_merged_for_prescreener(self):
        quotas = copy.deepcopy(QUOTAS)
        age_rows = [
            {
                "QuestionID": 1001538,
                "AnswerIDs": [],
                "AnswerValues": [value],
                "IsRoutable": False,
            }
            for value in ["21-29", "30-45", "46-64"]
        ]
        age_rows.append({
            "QuestionID": 1001538,
            "AnswerIDs": [2006361],
            "AnswerValues": [],
            "IsRoutable": False,
        })
        quotas["Surveys"][0]["Quotas"][0]["Layers"][0]["SubQuotas"][0][
            "QuestionsAndAnswers"
        ] = age_rows
        provider = TolunaProvider(
            self.integration,
            session=RecordingSession(FakeResponse(CULTURES), FakeResponse(REFERENCE), FakeResponse(quotas)),
        )
        normalized = provider.normalize_inventory_item(provider.inventory()[0], timezone.now())
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key=normalized.source_key,
            **normalized.values,
        )

        provider.refresh_details(survey)

        age_question = survey.targeting_questions.get(question_id=1001538)
        self.assertEqual(age_question.options, [])
        self.assertEqual(age_question.raw_data["adapter_version"], 7)
        self.assertEqual(age_question.raw_data["targeting_age_ranges"], [
            {"min": 21, "max": 29},
            {"min": 30, "max": 45},
            {"min": 46, "max": 64},
            {"min": 65, "max": 99},
        ])
        prepared = _prescreener_questions(survey)
        age_field = next(item for item in prepared if item["model"].question_id == 1001538)
        self.assertEqual(age_field["input_kind"], "number")
        self.assertEqual(age_field["min_value"], 21)
        self.assertEqual(age_field["max_value"], 99)
        self.assertEqual(
            age_field["targeting_note"],
            "Qualifying age: 21\u201399",
        )

        questions = {row.question_id: row for row in survey.targeting_questions.all()}
        answers = {
            str(questions[1001538].pk): {
                "question_id": 1001538,
                "values": ["66"],
                "upstream_values": ["66"],
            },
            str(questions[1001007].pk): {
                "question_id": 1001007,
                "values": ["2000247"],
                "upstream_values": ["2000247"],
            },
        }
        self.assertEqual(provider._matching_quota(survey, answers).quota_id, 900)
        answers[str(questions[1001538].pk)] = {
            "question_id": 1001538,
            "values": ["100"],
            "upstream_values": ["100"],
        }
        with self.assertRaisesRegex(ProviderError, "does not match an open Toluna quota"):
            provider._matching_quota(survey, answers)

    def test_toluna_age_ranges_normalize_unsupported_upper_bounds(self):
        self.assertEqual(TolunaProvider._age_range("65-130"), (65, 99))
        self.assertEqual(TolunaProvider._age_range("18-125"), (18, 99))
        self.assertEqual(TolunaProvider._age_range("100-120"), None)
        self.assertEqual(TolunaProvider._age_range("25+"), (25, 99))
        self.assertEqual(TolunaProvider._age_range("65 and older"), (65, 99))

    @patch("surveys.views.get_provider")
    def test_targeting_details_refreshes_previous_adapter_rows(self, get_provider_mock):
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key="toluna-adapter-v2",
            targeting_synced_at=timezone.now(),
        )
        TargetingQuestion.objects.create(
            survey=survey,
            question_id=1001538,
            key="TOLUNA_1001538",
            text="What is your age?",
            question_type="numeric",
            raw_data={"adapter_version": 5, "toluna_kind": "birth_date"},
        )

        SurveyViewSet._refresh_if_stale(survey, "targeting")

        get_provider_mock.assert_called_once_with(self.integration)
        get_provider_mock.return_value.refresh_details.assert_called_once_with(survey)

    def test_legacy_member_ready_releases_invite_without_rendering_identity(self):
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key="71:72",
            company_name="Toluna",
            name="Toluna test survey",
            status=Survey.Status.LIVE,
        )
        attempt = SurveyAttempt.objects.create(
            rid="Rdy123AbC9",
            prescreener_uid="Ab1c-De2f-Gh3i-Jk4l",
            survey=survey,
            user_id="1",
            submitted_at=timezone.now(),
            outbound_url="https://router.toluna.test/invite?token=abc",
        )
        session = self.client.session
        session[f"toluna_member_ready_{attempt.rid}"] = {
            "member_id": attempt.prescreener_uid,
            "birth_date": "08/12/1999",
        }
        session.save()
        url = reverse("toluna-member-ready")

        response = self.client.get(url, {"rid": attempt.rid})
        self.assertRedirects(
            response,
            attempt.outbound_url,
            fetch_redirect_response=False,
        )
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.REDIRECTED)
        self.assertIsNotNone(attempt.redirected_at)

        replay = self.client.post(url, {"rid": attempt.rid})
        self.assertEqual(replay.status_code, 409)

    @override_settings(PRESCREENER_VAULT_ENABLED=True)
    @patch("surveys.views.get_provider")
    def test_complete_prescreener_vault_redirects_directly_to_toluna(self, get_provider_mock):
        bootstrap = TolunaProvider(
            self.integration,
            session=RecordingSession(FakeResponse(CULTURES), FakeResponse(REFERENCE), FakeResponse(QUOTAS)),
        )
        normalized = bootstrap.normalize_inventory_item(bootstrap.inventory()[0], timezone.now())
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key=normalized.source_key,
            **normalized.values,
        )
        bootstrap.refresh_details(survey)
        user = get_user_model().objects.create_user(
            username="toluna-flow", email="toluna-flow@example.test", password="test-password"
        )
        attempt = SurveyAttempt.objects.create(
            rid="Flw123AbC9",
            prescreener_uid="Lm1n-Op2q-Rs3t-Uv4w",
            survey=survey,
            platform_user=user,
            user_id=str(user.pk),
        )
        questions = {row.question_id: row for row in survey.targeting_questions.all()}
        invite = {
            "SurveyId": 71, "WaveID": 72, "QuotaID": 900,
            "MemberAmount": 0, "PartnerAmount": 3.25,
            "URL": "https://router.toluna.test/invite?token=full-flow", "LOI": 7, "IR": 40,
        }
        provider = TolunaProvider(
            self.integration,
            session=RecordingSession(FakeResponse(None, 201), FakeResponse(invite)),
        )
        get_provider_mock.return_value = provider

        response = self.client.post(reverse("survey-start"), {
            "rid": attempt.rid,
            f"question_{questions[1001538].pk}": "27",
            f"question_{questions[1001007].pk}": "2000247",
        })

        self.assertRedirects(
            response,
            f"{invite['URL']}&rid={attempt.rid}",
            fetch_redirect_response=False,
        )
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.REDIRECTED)
        self.assertIsNotNone(attempt.submitted_at)
        self.assertIsNotNone(attempt.redirected_at)
        self.assertTrue(attempt.outbound_url)
        self.assertTrue(
            PrescreenerSubmission.objects.using(DATABASE_ALIAS).filter(
                rid=attempt.rid,
                uid=attempt.prescreener_uid,
                respondent_age=27,
                respondent_gender="male",
            ).exists()
        )

        replay = self.client.get(reverse("toluna-member-ready"), {"rid": attempt.rid})
        self.assertEqual(replay.status_code, 409)

    @override_settings(PRESCREENER_VAULT_ENABLED=True)
    @patch("surveys.views.get_provider")
    def test_stale_toluna_detail_post_refreshes_and_requires_updated_targeting_review(
        self, get_provider_mock
    ):
        bootstrap = TolunaProvider(
            self.integration,
            session=RecordingSession(
                FakeResponse(CULTURES), FakeResponse(REFERENCE), FakeResponse(QUOTAS)
            ),
        )
        normalized = bootstrap.normalize_inventory_item(
            bootstrap.inventory()[0], timezone.now()
        )
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key=normalized.source_key,
            **normalized.values,
        )
        bootstrap.refresh_details(survey)
        survey.detail_synced_at = None
        survey.save(update_fields=["detail_synced_at", "updated_at"])
        user = get_user_model().objects.create_user(
            username="toluna-stale-post",
            email="toluna-stale-post@example.test",
            password="test-password",
        )
        attempt = SurveyAttempt.objects.create(
            rid="Stl123AbC9",
            prescreener_uid="St1a-Le2d-Et3a-Il4s",
            survey=survey,
            platform_user=user,
            user_id=str(user.pk),
        )
        questions = {
            row.question_id: row for row in survey.targeting_questions.all()
        }
        provider = get_provider_mock.return_value

        response = self.client.post(reverse("survey-start"), {
            "rid": attempt.rid,
            f"question_{questions[1001538].pk}": "27",
            f"question_{questions[1001007].pk}": "2000247",
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Toluna targeting changed while this page was open. "
            "Please review the updated questions and submit again.",
        )
        get_provider_mock.assert_called_once_with(self.integration)
        provider.refresh_details.assert_called_once()
        refreshed_survey = provider.refresh_details.call_args.args[0]
        self.assertEqual(refreshed_survey.pk, survey.pk)
        provider.build_outbound_url.assert_not_called()
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.INITIATED)
        self.assertIsNone(attempt.submitted_at)
        self.assertIsNone(attempt.redirected_at)
        self.assertEqual(attempt.outbound_url, "")
        self.assertEqual(attempt.answers, {})

    @override_settings(PRESCREENER_VAULT_ENABLED=False)
    @patch("surveys.views.get_provider")
    def test_prescreener_records_invite_business_rejection_as_status_page(self, get_provider_mock):
        bootstrap = TolunaProvider(
            self.integration,
            session=RecordingSession(
                FakeResponse(CULTURES), FakeResponse(REFERENCE), FakeResponse(QUOTAS)
            ),
        )
        normalized = bootstrap.normalize_inventory_item(bootstrap.inventory()[0], timezone.now())
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key=normalized.source_key,
            **normalized.values,
        )
        bootstrap.refresh_details(survey)
        user = get_user_model().objects.create_user(
            username="toluna-rejection",
            email="toluna-rejection@example.test",
            password="test-password",
        )
        attempt = SurveyAttempt.objects.create(
            rid="Rej123AbC9",
            prescreener_uid="Qr1s-Tu2v-Wx3y-Za4b",
            survey=survey,
            platform_user=user,
            user_id=str(user.pk),
        )
        questions = {row.question_id: row for row in survey.targeting_questions.all()}
        provider = TolunaProvider(
            self.integration,
            session=RecordingSession(
                FakeResponse(None, 201),
                FakeResponse({
                    "Result": "SURVEY_NOT_ENABLED_FOR_IP_ES",
                    "ResultCode": 15,
                }, status_code=400),
            ),
        )
        get_provider_mock.return_value = provider

        response = self.client.post(reverse("survey-start"), {
            "rid": attempt.rid,
            f"question_{questions[1001538].pk}": "27",
            f"question_{questions[1001007].pk}": "2000247",
        })

        self.assertRedirects(
            response,
            f"{reverse('survey-status')}?status=7&rid={attempt.rid}",
            fetch_redirect_response=False,
        )
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.SURVEY_NOT_AVAILABLE)
        self.assertEqual(attempt.status_source, "toluna_invite_rejection")
        self.assertEqual(
            attempt.upstream_transaction_data["toluna_invite_rejection"]["result_code"],
            15,
        )
        landing = self.client.get(response.url)
        self.assertEqual(landing.status_code, 200)
        self.assertContains(landing, "Survey not available")
        self.assertNotContains(landing, "Invalid Toluna callback")

        tampered = self.client.get(
            reverse("survey-status"),
            {"status": "1", "rid": attempt.rid},
        )
        self.assertEqual(tampered.status_code, 403)
        self.assertContains(tampered, "Invalid Toluna callback", status_code=403)

    def test_toluna_validation_error_exposes_only_safe_diagnostic_fields(self):
        provider = TolunaProvider(
            self.integration,
            session=RecordingSession(FakeResponse({
                "Result": "INVALID_PROPERTY_DATA",
                "ResultCode": 7,
                "Message": "PostalCode is invalid",
                "PartnerGUID": "must-not-be-echoed",
                "MemberCode": "must-not-be-echoed",
            }, status_code=400)),
        )

        with self.assertRaisesRegex(ProviderError, "INVALID_PROPERTY_DATA") as raised:
            provider._request("POST", "https://toluna.test/member")

        self.assertNotIn("must-not-be-echoed", str(raised.exception))

    def test_member_update_accepts_documented_empty_http_200(self):
        provider = TolunaProvider(
            self.integration,
            session=RecordingSession(FakeResponse(None, status_code=200)),
        )

        payload, status_code = provider._request(
            "PUT",
            "https://toluna.test/member",
            expected=(200,),
            allow_empty=True,
        )

        self.assertIsNone(payload)
        self.assertEqual(status_code, 200)

    def test_invite_result_code_15_is_a_terminal_survey_unavailable_outcome(self):
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key="71:72",
            company_name="Toluna",
            name="Toluna unavailable survey",
            status=Survey.Status.LIVE,
            raw_data={"SurveyID": 71, "WaveID": 72, "_toluna": {"culture_code": "en-us"}},
        )
        attempt = SurveyAttempt.objects.create(
            rid="Unv123AbC9",
            prescreener_uid="Wx1y-Za2b-Cd3e-Fg4h",
            survey=survey,
            user_id="1",
        )
        provider = TolunaProvider(
            self.integration,
            session=RecordingSession(FakeResponse({
                "Result": "SURVEY_NOT_ENABLED_FOR_IP_ES",
                "ResultCode": 15,
            }, status_code=400)),
        )
        provider._matching_quota = lambda _survey, _answers: type("Quota", (), {"quota_id": 900})()
        provider._register_member = lambda _survey, _attempt, _answers: {
            "member_id": attempt.prescreener_uid,
            "birth_date": "01/01/2000",
        }

        with self.assertRaises(TolunaInviteRejected) as raised:
            provider.build_outbound_url(survey, attempt, {})

        self.assertEqual(raised.exception.status_code, "7")
        self.assertEqual(raised.exception.result_code, 15)
        self.assertNotIn("HTTP 400", str(raised.exception))

    def test_member_validation_error_never_echoes_profile_payload(self):
        provider = TolunaProvider(
            self.integration,
            session=RecordingSession(FakeResponse({
                "Message": (
                    'Cannot Register MemberCode:secret-member. An attribute is invalid: '
                    '{"PartnerGUID":"secret-guid","BirthDate":"09/07/2002",'
                    '"RegistrationAnswers":[{"QuestionID":2910077,'
                    '"Answers":[{"AnswerID":5312785}]}]}'
                ),
            }, status_code=400)),
        )

        with self.assertRaisesRegex(
            ProviderError,
            r"rejected one or more member profile attributes.*2910077",
        ) as raised:
            provider._request("POST", "https://toluna.test/member")

        rendered = str(raised.exception)
        self.assertNotIn("secret-member", rendered)
        self.assertNotIn("secret-guid", rendered)
        self.assertNotIn("09/07/2002", rendered)
        self.assertNotIn("5312785", rendered)

    def test_callback_hmac_verifies_exact_url_with_trailing_ampersand(self):
        unsigned = "http://testserver/survey?status=1&rid=Abc123XyZ9&"
        signature = hmac.new(b"hmac-secret", unsigned.encode(), hashlib.sha256).hexdigest()
        request = RequestFactory().get(f"/survey?status=1&rid=Abc123XyZ9&hash={signature}")
        provider = TolunaProvider(self.integration, session=RecordingSession())
        self.assertTrue(provider.verify_callback(request))

    def test_extended_toluna_status_pages_are_verified_and_recorded(self):
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key="toluna-status-pages",
            name="Toluna status test",
            status=Survey.Status.LIVE,
        )
        cases = [
            ("1", "TolSt10001", SurveyAttempt.Status.COMPLETED, "Qualified"),
            ("2", "TolSt20001", SurveyAttempt.Status.TERMINATED, "Terminated"),
            ("3", "TolSt30001", SurveyAttempt.Status.OVER_QUOTA, "Quota full"),
            ("4", "TolSt40001", SurveyAttempt.Status.QUALITY_TERMINATED, "Fraud terminated"),
            ("7", "TolSt70001", SurveyAttempt.Status.SURVEY_NOT_AVAILABLE, "Survey not available"),
            ("8", "TolSt80001", SurveyAttempt.Status.NO_SURVEYS, "No surveys"),
            ("9", "TolSt90001", SurveyAttempt.Status.NO_COOKIES, "No cookies"),
            ("10", "TolS100001", SurveyAttempt.Status.MAX_SURVEYS_REACHED, "Maximum surveys reached"),
            ("11", "TolS110001", SurveyAttempt.Status.NOT_QUALIFIED, "Not qualified"),
            ("12", "TolS120001", SurveyAttempt.Status.SURVEY_TAKEN, "Survey already taken"),
        ]
        for code, rid, expected_status, label in cases:
            with self.subTest(code=code):
                attempt = SurveyAttempt.objects.create(
                    rid=rid,
                    survey=survey,
                    user_id="1",
                    status=SurveyAttempt.Status.REDIRECTED,
                )
                unsigned = f"http://testserver/survey?status={code}&rid={rid}&"
                signature = hmac.new(
                    b"hmac-secret", unsigned.encode(), hashlib.sha256
                ).hexdigest()

                response = self.client.get(
                    f"/survey?status={code}&rid={rid}&hash={signature}",
                    follow=True,
                )

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, label)
                attempt.refresh_from_db()
                self.assertEqual(attempt.status, expected_status)
                self.assertEqual(attempt.status_source, "toluna_callback")
                self.assertTrue(attempt.is_verified)
                self.assertEqual(
                    attempt.upstream_transaction_data["toluna_outcome"]["code"], code
                )

    def test_signed_not_qualified_callback_reports_same_internet_identity_rejection(self):
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key="toluna-internet-identifier-attempted",
            name="Toluna duplicate internet identity test",
            status=Survey.Status.LIVE,
        )
        attempt = SurveyAttempt.objects.create(
            rid="TolS1173A1",
            survey=survey,
            user_id="1",
            status=SurveyAttempt.Status.REDIRECTED,
        )
        unsigned = (
            f"http://testserver/survey?status=11&rid={attempt.rid}"
            "&rejectionID=73&"
        )
        signature = hmac.new(
            b"hmac-secret", unsigned.encode(), hashlib.sha256
        ).hexdigest()

        response = self.client.get(
            f"/survey?status=11&rid={attempt.rid}&rejectionID=73&hash={signature}",
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Survey already attempted")
        self.assertContains(
            response,
            "same internet identity has already attempted this survey",
        )
        self.assertContains(response, "Provider reason")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.NOT_QUALIFIED)
        self.assertEqual(attempt.status_source, "toluna_callback")
        self.assertTrue(attempt.is_verified)
        callback = attempt.upstream_transaction_data["toluna_callback"]
        self.assertEqual(callback["rejectionID"], "73")
        self.assertEqual(callback["hash"], "[redacted]")
        outcome = attempt.upstream_transaction_data["toluna_outcome"]
        self.assertEqual(outcome["code"], "11")
        self.assertEqual(outcome["rejection_id"], "73")
        self.assertEqual(outcome["category"], "Duplicate survey attempt")
        self.assertIn("same internet identity", outcome["reason"])
        self.assertEqual(provider_outcome(attempt)["reason"], outcome["reason"])

    def test_unsigned_rejection_id_cannot_change_toluna_attempt_or_audit(self):
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key="toluna-unverified-rejection-id",
            name="Toluna unverified rejection test",
            status=Survey.Status.LIVE,
        )
        attempt = SurveyAttempt.objects.create(
            rid="TolBad73A1",
            survey=survey,
            user_id="1",
            status=SurveyAttempt.Status.REDIRECTED,
        )

        response = self.client.get(
            f"/survey?status=11&rid={attempt.rid}&rejectionID=73&hash=invalid"
        )

        self.assertEqual(response.status_code, 403)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.REDIRECTED)
        self.assertEqual(attempt.callback_count, 0)
        self.assertEqual(attempt.upstream_transaction_data, {})

    def test_reused_member_code_callback_updates_and_displays_canonical_rid(self):
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key="toluna-reused-member",
            name="Toluna reused member test",
            status=Survey.Status.LIVE,
        )
        attempt = SurveyAttempt.objects.create(
            rid="OwnRid1001",
            prescreener_uid="New1-Uid2-For3-Test",
            provider_profile_uid="Old1-Uid2-For3-Test",
            survey=survey,
            user_id="1",
            status=SurveyAttempt.Status.REDIRECTED,
        )
        echoed_uid = attempt.provider_profile_uid
        unsigned = f"http://testserver/survey?status=1&rid={echoed_uid}&"
        signature = hmac.new(
            b"hmac-secret", unsigned.encode(), hashlib.sha256
        ).hexdigest()

        response = self.client.get(
            f"/survey?status=1&rid={echoed_uid}&hash={signature}",
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, attempt.rid)
        self.assertNotContains(response, echoed_uid)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.COMPLETED)
        self.assertEqual(attempt.status_source, "toluna_callback")

    def test_signed_toluna_callback_uses_read_only_post_redirect_get(self):
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key="toluna-callback-prg",
            name="Toluna callback PRG test",
            status=Survey.Status.LIVE,
        )
        attempt = SurveyAttempt.objects.create(
            rid="Prg123AbC9",
            survey=survey,
            user_id="1",
            status=SurveyAttempt.Status.REDIRECTED,
        )
        unsigned = f"http://testserver/survey?status=1&rid={attempt.rid}&"
        signature = hmac.new(
            b"hmac-secret", unsigned.encode(), hashlib.sha256
        ).hexdigest()
        signed_url = f"/survey?status=1&rid={attempt.rid}&hash={signature}"

        first = self.client.get(signed_url)
        self.assertRedirects(
            first,
            f"/survey?status=1&rid={attempt.rid}",
            fetch_redirect_response=False,
        )
        attempt.refresh_from_db()
        first_count = attempt.callback_count
        first_callback_at = attempt.last_callback_at

        replay = self.client.get(signed_url)
        self.assertRedirects(
            replay,
            f"/survey?status=1&rid={attempt.rid}",
            fetch_redirect_response=False,
        )
        clean = self.client.get(f"/survey?status=1&rid={attempt.rid}")
        self.assertEqual(clean.status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(first_count, 1)
        self.assertEqual(attempt.callback_count, first_count)
        self.assertEqual(attempt.last_callback_at, first_callback_at)

    def test_toluna_callback_fails_closed_when_hmac_is_disabled(self):
        self.integration.config = {
            **(self.integration.config or {}),
            "callback_hash_required": False,
        }
        self.integration.save(update_fields=["config", "updated_at"])
        survey = Survey.objects.create(
            client=self.integration.client,
            integration=self.integration,
            source_key="toluna-disabled-hmac",
            name="Toluna disabled HMAC test",
            status=Survey.Status.LIVE,
        )
        attempt = SurveyAttempt.objects.create(
            rid="Hmc123AbC9",
            survey=survey,
            user_id="1",
            status=SurveyAttempt.Status.REDIRECTED,
        )

        response = self.client.get(f"/survey?status=1&rid={attempt.rid}")

        self.assertEqual(response.status_code, 403)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.REDIRECTED)
        self.assertEqual(attempt.callback_count, 0)

    def test_serializer_rejects_non_numeric_interval_and_resets_verification_after_edit(self):
        invalid = ClientIntegrationSerializer(
            self.integration,
            data={"sync_interval_seconds": "soon"},
            partial=True,
        )
        self.assertFalse(invalid.is_valid())
        self.assertIn("sync_interval_seconds", invalid.errors)

        self.integration.last_test_status = "success"
        self.integration.scheduled_sync_enabled = True
        self.integration.save(update_fields=["last_test_status", "scheduled_sync_enabled", "updated_at"])
        serializer = ClientIntegrationSerializer(
            self.integration,
            data={"config": {"environment": "sandbox", "callback_hash_required": True}},
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        updated = serializer.save()
        self.assertEqual(updated.last_test_status, "")
        self.assertFalse(updated.scheduled_sync_enabled)

    def test_serializer_requires_hmac_reference_when_callback_verification_is_enabled(self):
        credential_refs = dict(self.integration.credential_env_keys)
        credential_refs.pop("hmac_key")
        serializer = ClientIntegrationSerializer(
            self.integration,
            data={
                "credential_env_keys": credential_refs,
                "config": {"environment": "production", "callback_hash_required": True},
            },
            partial=True,
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("credential_env_keys", serializer.errors)
