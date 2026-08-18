from decimal import Decimal

import django.core.validators
from django.db import migrations, models


def clear_legacy_country_selection(apps, schema_editor):
    Integration = apps.get_model("vendors", "ClientIntegration")
    Integration.objects.exclude(profile_reuse_country_codes=[]).update(
        profile_reuse_country_codes=[]
    )


class Migration(migrations.Migration):
    dependencies = [("vendors", "0014_clientintegration_profile_reuse_age_groups_and_more")]

    operations = [
        migrations.AddField(
            model_name="clientintegration",
            name="profile_rereuse_enabled",
            field=models.BooleanField(
                default=False,
                help_text="Allow profiles that already completed one reuse round to enter a separate queue.",
            ),
        ),
        migrations.AddField(
            model_name="clientintegration",
            name="profile_rereuse_percentage",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("50.00"),
                help_text="Share of the monthly reuse target reserved for already-reused profiles.",
                max_digits=5,
                validators=[
                    django.core.validators.MinValueValidator(Decimal("0")),
                    django.core.validators.MaxValueValidator(Decimal("100")),
                ],
            ),
        ),
        migrations.AddField(
            model_name="clientintegration",
            name="profile_rereuse_cooldown_days",
            field=models.PositiveSmallIntegerField(
                default=30,
                help_text="Minimum cooldown after a profile reuse before it can be used again.",
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(730),
                ],
            ),
        ),
        migrations.RunPython(clear_legacy_country_selection, migrations.RunPython.noop),
    ]
