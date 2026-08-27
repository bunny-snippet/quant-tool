import copy
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from vendors.models import Client, ClientIntegration

from .models import Survey, SurveyQuota, SyncRun, TolunaNotification
from .provider_services import sync_client_integration
from .providers.base import NormalizedSurvey


def _quota_payload(quota_id, remaining, *, answer_ids=(10,)):
    return {
        "QuotaID": quota_id,
        "CompletesRequired": 25,
        "EstimatedCompletesRemaining": remaining,
        "Layers": [
            {
                "LayerID": 1,
                "SubQuotas": [
                    {
                        "SubQuotaID": quota_id * 10,
                        "QuestionsAndAnswers": [
                            {
                                "QuestionID": 1001,
                                "IsRoutable": True,
                                "AnswerIDs": list(answer_ids),
                                "AnswerValues": [str(value) for value in answer_ids],
                            }
                        ],
                    }
                ],
            }
        ],
    }


class _InventoryProvider:
    minimum_sync_interval_seconds = 60
    inventory_cache_expires_at = None

    def __init__(self, integration, payloads):
        self.integration = integration
        self._payloads = payloads

    def inventory(self):
        return copy.deepcopy(self._payloads)

    def normalize_inventory_item(self, payload, seen_at):
        raw_data = copy.deepcopy(payload["raw_data"])
        source_key = payload["source_key"]
        remaining = int(payload.get("remaining", 5))
        return NormalizedSurvey(
            source_key=source_key,
            numeric_source_id=None,
            modified_at=None,
            raw_data=raw_data,
            values={
                "company_name": self.integration.client.name,
                "name": payload.get("name", f"Toluna {source_key}"),
                "status": payload.get("status", Survey.Status.LIVE),
                "sample_size": 25,
                "completes": 25 - remaining,
                "remaining": remaining,
                "cpi": Decimal("1.25"),
                "loi": 12,
                "incidence_rate": Decimal("35.00"),
                "country": "United States",
                "country_code": "US",
                "language": "English",
                "language_code": "EN",
                "group_type": "1",
                "buyer_id": source_key.split(":", 1)[-1],
                "survey_type": "Standard",
                "device_type": "1, 2",
                "has_quota": bool(raw_data.get("Quotas")),
                "is_recontact": False,
                "source_created_at": None,
                "source_modified_at": None,
                "last_seen_at": seen_at,
                "entry_link": "",
                "raw_data": raw_data,
            },
        )


class TolunaBulkInventorySyncTests(TestCase):
    def setUp(self):
        self.client = Client.objects.create(
            code="toluna-bulk",
            name="Toluna",
            provider_code="toluna",
        )
        self.integration = ClientIntegration.objects.create(
            client=self.client,
            name="Toluna bulk sync",
            provider_code="toluna",
            base_url="https://example.test",
        )

    def _survey(self, source_key, raw_data, **overrides):
        values = {
            "client": self.client,
            "integration": self.integration,
            "source_key": source_key,
            "company_name": "Toluna",
            "name": f"Old {source_key}",
            "status": Survey.Status.LIVE,
            "detail_synced_at": timezone.now() - timedelta(hours=1),
            "quota_synced_at": timezone.now() - timedelta(hours=1),
            "raw_data": copy.deepcopy(raw_data),
        }
        values.update(overrides)
        return Survey.objects.create(**values)

    @staticmethod
    def _payload(source_key, quotas, **values):
        return {
            "source_key": source_key,
            "raw_data": {"Quotas": copy.deepcopy(quotas)},
            **values,
        }

    def _query_count_for_changed_rows(self, count, *, source_offset=0):
        payloads = []
        quota_rows = []
        for index in range(count):
            source_key = (
                f"{7000 + source_offset + index}:"
                f"{8000 + source_offset + index}"
            )
            old_quota = _quota_payload(9000 + index, 10)
            survey = self._survey(
                source_key,
                {"Quotas": [old_quota]},
                name=f"Old {index}",
            )
            quota_rows.append(
                SurveyQuota(
                    survey=survey,
                    source_key=f"quota:{9000 + index}",
                    quota_id=9000 + index,
                    remaining=10,
                    raw_data=old_quota,
                )
            )
            payloads.append(
                self._payload(
                    source_key,
                    [_quota_payload(9000 + index, 9)],
                    name=f"Changed {index}",
                    remaining=9,
                )
            )
        SurveyQuota.objects.bulk_create(quota_rows)
        provider = _InventoryProvider(self.integration, payloads)
        with (
            patch("surveys.provider_services.get_provider", return_value=provider),
            patch(
                "surveys.toluna_notifications."
                "reconcile_toluna_operational_notifications_for_surveys"
            ),
            CaptureQueriesContext(connection) as queries,
        ):
            sync_client_integration(self.integration)
        return len(queries)

    def test_changed_row_query_count_does_not_scale_per_survey(self):
        one_row_queries = self._query_count_for_changed_rows(1)
        twenty_row_queries = self._query_count_for_changed_rows(
            20, source_offset=100
        )

        self.assertLessEqual(
            twenty_row_queries,
            one_row_queries + 4,
            f"one row used {one_row_queries} queries; 20 rows used {twenty_row_queries}",
        )

    @patch(
        "surveys.toluna_notifications."
        "reconcile_toluna_operational_notifications_for_surveys"
    )
    @patch("surveys.provider_services.get_provider")
    def test_bulk_merge_preserves_targeting_capacity_and_missing_quota_semantics(
        self, get_provider_mock, reconcile_mock
    ):
        unchanged_contract = _quota_payload(101, 10)
        capacity_survey = self._survey(
            "11:21", {"Quotas": [unchanged_contract]}, remaining=10
        )
        capacity_detail_sync = capacity_survey.detail_synced_at
        capacity_previous_update = capacity_survey.updated_at
        capacity_quota = SurveyQuota.objects.create(
            survey=capacity_survey,
            source_key="quota:101",
            quota_id=101,
            sample_size=25,
            completes=15,
            remaining=10,
            status="Open",
            raw_data=unchanged_contract,
        )

        missing_first = _quota_payload(201, 10)
        missing_second = _quota_payload(202, 10)
        missing_survey = self._survey(
            "12:22", {"Quotas": [missing_first, missing_second]}
        )
        missing_quota_sync = missing_survey.quota_synced_at
        missing_quota = SurveyQuota.objects.create(
            survey=missing_survey,
            source_key="quota:201",
            quota_id=201,
            remaining=10,
            raw_data=missing_first,
        )

        targeting_old = _quota_payload(301, 10, answer_ids=(10,))
        targeting_survey = self._survey("13:23", {"Quotas": [targeting_old]})
        targeting_quota = SurveyQuota.objects.create(
            survey=targeting_survey,
            source_key="quota:301",
            quota_id=301,
            remaining=10,
            raw_data=targeting_old,
        )

        closed_survey = self._survey(
            "14:24", {"Quotas": []}, status=Survey.Status.LIVE
        )
        closed_previous_update = closed_survey.updated_at

        payloads = [
            self._payload("11:21", [_quota_payload(101, 7)], remaining=7),
            self._payload(
                "12:22",
                [_quota_payload(201, 6), _quota_payload(202, 4)],
                remaining=4,
            ),
            self._payload(
                "13:23",
                [_quota_payload(301, 8, answer_ids=(99,))],
                remaining=8,
            ),
            self._payload("15:25", [_quota_payload(401, 3)], remaining=3),
        ]
        get_provider_mock.return_value = _InventoryProvider(
            self.integration, payloads
        )
        started = timezone.now()

        with patch.object(
            Survey.objects,
            "bulk_update",
            wraps=Survey.objects.bulk_update,
        ) as survey_bulk_update:
            run = sync_client_integration(self.integration)

        capacity_survey.refresh_from_db()
        capacity_quota.refresh_from_db()
        missing_survey.refresh_from_db()
        missing_quota.refresh_from_db()
        targeting_survey.refresh_from_db()
        targeting_quota.refresh_from_db()
        closed_survey.refresh_from_db()
        created_survey = Survey.objects.get(
            integration=self.integration, source_key="15:25"
        )

        self.assertEqual((run.created, run.updated, run.unchanged, run.closed), (1, 3, 0, 1))
        self.assertTrue(created_survey.local_id)
        self.assertEqual(len(created_survey.local_id), 14)

        self.assertEqual(capacity_survey.detail_synced_at, capacity_detail_sync)
        self.assertGreaterEqual(capacity_survey.quota_synced_at, started)
        self.assertGreater(capacity_survey.updated_at, capacity_previous_update)
        self.assertEqual(capacity_quota.remaining, 7)
        self.assertEqual(capacity_quota.completes, 18)
        self.assertEqual(capacity_quota.status, "Open")
        self.assertEqual(capacity_quota.raw_data["EstimatedCompletesRemaining"], 7)

        self.assertIsNone(missing_survey.detail_synced_at)
        self.assertEqual(missing_survey.quota_synced_at, missing_quota_sync)
        self.assertEqual(missing_quota.remaining, 6)

        self.assertIsNone(targeting_survey.detail_synced_at)
        self.assertEqual(targeting_quota.remaining, 10)
        self.assertEqual(
            targeting_quota.raw_data["EstimatedCompletesRemaining"], 10
        )

        self.assertEqual(closed_survey.status, Survey.Status.CLOSED)
        self.assertGreaterEqual(closed_survey.updated_at, closed_previous_update)
        self.assertEqual(run.status, SyncRun.Status.SUCCESS)
        reconcile_mock.assert_called_once()
        args, kwargs = reconcile_mock.call_args
        self.assertCountEqual(
            [survey.source_key for survey in args[0]],
            ["11:21", "12:22", "13:23", "15:25"],
        )
        self.assertEqual(
            kwargs["include_applied_event_types"],
            {
                TolunaNotification.EventType.QUOTA_STATUS,
                TolunaNotification.EventType.SURVEY_CLOSED,
            },
        )
        self.assertGreaterEqual(kwargs["applied_since"], started)
        concrete_fields = {
            field.name
            for field in Survey._meta.concrete_fields
            if not field.primary_key
        }
        self.assertTrue(survey_bulk_update.called)
        for call in survey_bulk_update.call_args_list:
            self.assertTrue(set(call.args[1]).issubset(concrete_fields))

    @patch(
        "surveys.toluna_notifications."
        "reconcile_toluna_operational_notifications_for_surveys"
    )
    @patch("surveys.provider_services.get_provider")
    def test_bulk_quota_failure_rolls_back_inventory_transaction_and_marks_run_failed(
        self, get_provider_mock, _reconcile_mock
    ):
        old_quota = _quota_payload(501, 10)
        survey = self._survey("31:41", {"Quotas": [old_quota]}, name="Before")
        SurveyQuota.objects.create(
            survey=survey,
            source_key="quota:501",
            quota_id=501,
            remaining=10,
            raw_data=old_quota,
        )
        get_provider_mock.return_value = _InventoryProvider(
            self.integration,
            [
                self._payload(
                    "31:41",
                    [_quota_payload(501, 2)],
                    name="After",
                    remaining=2,
                )
            ],
        )

        with patch.object(
            SurveyQuota.objects, "bulk_update", side_effect=RuntimeError("quota write failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "quota write failed"):
                sync_client_integration(self.integration)

        survey.refresh_from_db()
        quota = survey.quotas.get(quota_id=501)
        run = SyncRun.objects.latest("pk")
        self.integration.refresh_from_db()
        self.assertEqual(survey.name, "Before")
        self.assertEqual(survey.remaining, 0)
        self.assertEqual(quota.remaining, 10)
        self.assertEqual(run.status, SyncRun.Status.FAILED)
        self.assertEqual(self.integration.last_sync_status, "failed")
        self.assertIn("quota write failed", run.error)

    @patch(
        "surveys.toluna_notifications."
        "reconcile_toluna_operational_notifications_for_surveys"
    )
    @patch("surveys.provider_services.get_provider")
    def test_snapshot_marker_keeps_current_and_newer_rows_and_closes_only_missing_live(
        self, get_provider_mock, _reconcile_mock
    ):
        payloads = [
            self._payload("61:71", [], name="Current"),
            self._payload(
                "62:72",
                [],
                name="Disabled",
                status=Survey.Status.CLOSED,
            ),
        ]
        get_provider_mock.return_value = _InventoryProvider(
            self.integration, payloads
        )

        create_run = sync_client_integration(self.integration)
        current = Survey.objects.get(
            integration=self.integration, source_key="61:71"
        )
        provider_disabled = Survey.objects.get(
            integration=self.integration, source_key="62:72"
        )
        self.assertEqual(create_run.created, 2)
        self.assertTrue(current.local_id)
        self.assertTrue(provider_disabled.local_id)

        old_time = timezone.now() - timedelta(days=1)
        Survey.objects.filter(
            pk__in=[current.pk, provider_disabled.pk]
        ).update(last_seen_at=old_time)
        current.last_seen_at = old_time
        provider_disabled.last_seen_at = old_time
        missing = self._survey(
            "63:73", {"Quotas": []}, last_seen_at=old_time
        )
        already_closed = self._survey(
            "64:74",
            {"Quotas": []},
            status=Survey.Status.CLOSED,
            last_seen_at=old_time,
        )
        newer_snapshot = self._survey(
            "65:75",
            {"Quotas": []},
            last_seen_at=timezone.now() + timedelta(days=1),
        )
        equal_boundary = self._survey(
            "66:76",
            {"Quotas": []},
        )
        get_provider_mock.return_value = _InventoryProvider(
            self.integration, payloads
        )
        started = timezone.now()
        Survey.objects.filter(pk=equal_boundary.pk).update(last_seen_at=started)
        equal_boundary.last_seen_at = started

        with (
            patch("surveys.provider_services.timezone.now", return_value=started),
            CaptureQueriesContext(connection) as queries,
        ):
            run = sync_client_integration(self.integration)

        for survey in (
            current,
            provider_disabled,
            missing,
            already_closed,
            newer_snapshot,
            equal_boundary,
        ):
            survey.refresh_from_db()

        self.assertEqual(current.status, Survey.Status.LIVE)
        self.assertGreaterEqual(current.last_seen_at, started)
        self.assertEqual(provider_disabled.status, Survey.Status.CLOSED)
        self.assertGreaterEqual(provider_disabled.last_seen_at, started)
        self.assertEqual(missing.status, Survey.Status.CLOSED)
        self.assertEqual(already_closed.status, Survey.Status.CLOSED)
        self.assertEqual(newer_snapshot.status, Survey.Status.LIVE)
        self.assertEqual(equal_boundary.status, Survey.Status.CLOSED)
        self.assertEqual(run.created, 0)
        self.assertEqual(run.updated, 0)
        self.assertEqual(run.unchanged, 2)
        self.assertEqual(run.closed, 2)

        close_queries = [
            query["sql"]
            for query in queries.captured_queries
            if 'UPDATE "surveys_survey"' in query["sql"]
            and '"last_seen_at" <=' in query["sql"]
        ]
        self.assertEqual(len(close_queries), 1)
        self.assertNotIn("NOT IN", close_queries[0].upper())
