import os
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.test import SimpleTestCase
from django.utils import timezone

from surveys.models import Survey
from surveys.providers.zamplia import ZampliaProvider
from surveys.serializers import SurveyQuotaSerializer


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class CapturingSession:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.payloads.pop(0))


def integration(**overrides):
    values = {
        "pk": 91,
        "provider_code": "zamplia",
        "base_url": "https://surveysupply.zamplia.com/api/v1",
        "credential_env_key": "",
        "credential_env_keys": {
            "client_id": "TEST_ZAMPLIA_CLIENT_ID",
            "token": "TEST_ZAMPLIA_ZAMP_KEY",
        },
        "config": {},
        "client": SimpleNamespace(name="Zamplia"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@patch.dict(
    os.environ,
    {
        "TEST_ZAMPLIA_CLIENT_ID": "2",
        "TEST_ZAMPLIA_ZAMP_KEY": "test-zamp-key",
    },
)
class ZampliaProviderTests(SimpleTestCase):
    def test_legacy_list_targeting_does_not_break_quota_serial(self):
        quota = SimpleNamespace(
            raw_data={},
            targeting=[],
            survey=SimpleNamespace(integration_id=None),
        )

        self.assertEqual(
            SurveyQuotaSerializer().get_scope_label(quota),
            "Overall survey quota",
        )

    def test_inventory_uses_documented_header(self):
        session = CapturingSession(
            {"result": {"success": True, "data": [{"SurveyId": 42}]}}
        )
        rows = ZampliaProvider(integration(), session=session).inventory()

        self.assertEqual(rows, [{"SurveyId": 42}])
        url, request = session.calls[0]
        self.assertEqual(
            url,
            "https://surveysupply.zamplia.com/api/v1/Surveys/GetAllocatedSurveys",
        )
        self.assertEqual(request["headers"]["ZAMP-KEY"], "test-zamp-key")

    def test_inventory_status_and_end_date_control_local_close_state(self):
        language_payload = {
            "result": {
                "success": True,
                "data": [
                    {
                        "LanguageId": 4,
                        "LanguageCode": "en-US",
                        "CountryCode": "US",
                        "Country": "United States",
                    }
                ],
            }
        }
        now = timezone.now()
        live = {
            "SurveyId": 42,
            "Name": "Live survey",
            "LanguageId": 4,
            "status": "Live",
            "SurveyEndDate": (now + timedelta(days=1)).isoformat(),
        }
        expired = {
            **live,
            "SurveyId": 43,
            "SurveyEndDate": (now - timedelta(seconds=1)).isoformat(),
        }
        stopped = {
            **live,
            "SurveyId": 44,
            "status": "Closed",
        }
        provider = ZampliaProvider(
            integration(pk=92),
            session=CapturingSession(language_payload),
        )

        self.assertEqual(
            provider.normalize_inventory_item(live, now).values["status"],
            Survey.Status.LIVE,
        )
        self.assertEqual(
            provider.normalize_inventory_item(expired, now).values["status"],
            Survey.Status.CLOSED,
        )
        self.assertEqual(
            provider.normalize_inventory_item(stopped, now).values["status"],
            Survey.Status.CLOSED,
        )

    def test_entry_link_carries_identity_and_targeting_values(self):
        provider = ZampliaProvider(integration())
        survey = SimpleNamespace(
            source_key="9001",
            buyer_id="17",
            raw_data={"ApiClientId": 17},
        )
        attempt = SimpleNamespace(rid="opaque-rid")
        answers = {
            "age": {"question_id": 29, "upstream_values": ["25"]},
            "gender": {"question_id": 1, "upstream_values": ["2"]},
        }

        with patch(
            "surveys.providers.zamplia.effective_profile_uid",
            return_value="profile-uid",
        ):
            result = provider.build_outbound_url(survey, attempt, answers)

        parsed = urlsplit(result)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.hostname, "zampparticipant.zamplia.com")
        self.assertEqual(query["vid"], ["2"])
        self.assertEqual(query["sid"], ["9001"])
        self.assertEqual(query["cid"], ["17"])
        self.assertEqual(query["UID"], ["opaque-rid"])
        self.assertEqual(query["umid"], ["profile-uid"])
        self.assertEqual(query["q29"], ["25"])
        self.assertEqual(query["q1"], ["2"])
