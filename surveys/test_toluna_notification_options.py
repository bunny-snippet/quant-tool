from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from vendors.models import Client, ClientIntegration

from .models import Survey, TolunaNotification
from .views import _toluna_notification_options


class TolunaNotificationOptionTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_superuser(
            username="toluna-options-owner",
            email="toluna-options-owner@example.test",
            password="test-password",
        )
        self.alpha_client = Client.objects.create(
            code="toluna-options-alpha",
            name="Alpha Toluna",
            provider_code="toluna",
        )
        self.beta_client = Client.objects.create(
            code="toluna-options-beta",
            name="beta Toluna",
            provider_code="toluna",
        )
        self.alpha_integration = ClientIntegration.objects.create(
            client=self.alpha_client,
            name="Alpha Toluna",
            provider_code="toluna",
            base_url="https://alpha-toluna.example.test",
        )
        self.beta_integration = ClientIntegration.objects.create(
            client=self.beta_client,
            name="beta Toluna",
            provider_code="toluna",
            base_url="https://beta-toluna.example.test",
        )
        self.alpha_survey = Survey.objects.create(
            client=self.alpha_client,
            integration=self.alpha_integration,
            source_id=771001,
            source_key="771001:881001",
            name="Alpha survey",
            country="United States",
            country_code="US",
        )
        for index in range(3):
            TolunaNotification.objects.create(
                event_type=TolunaNotification.EventType.QUOTA_STATUS,
                payload_hash=f"toluna-options-alpha-{index}",
                integration=self.alpha_integration,
                survey=self.alpha_survey,
                provider_survey_id=self.alpha_survey.source_id,
                wave_id=881001,
                quota_id=990000 + index,
                provider_status="Open",
            )
        TolunaNotification.objects.create(
            event_type=TolunaNotification.EventType.SURVEY_CLOSED,
            payload_hash="toluna-options-beta",
            integration=self.beta_integration,
            provider_survey_id=772001,
            wave_id=882001,
            provider_status="Closed",
        )

    def test_repeated_notifications_produce_unique_client_options_and_distinct_sql(self):
        with CaptureQueriesContext(connection) as queries:
            options = _toluna_notification_options(
                TolunaNotification.objects.all(), self.owner
            )

        self.assertEqual(options["clients"], [
            {"id": self.alpha_client.pk, "name": "Alpha Toluna"},
            {"id": self.beta_client.pk, "name": "beta Toluna"},
        ])
        client_distinct_queries = [
            query["sql"]
            for query in queries
            if "SELECT DISTINCT" in query["sql"].upper()
            and "vendors_client" in query["sql"]
            and '"vendors_client"."name"' in query["sql"]
            and "tolunanotification" in query["sql"]
        ]
        self.assertEqual(len(client_distinct_queries), 2)
        for sql in client_distinct_queries:
            self.assertNotIn("received_at", sql)
            self.assertNotIn("ORDER BY", sql.upper())
