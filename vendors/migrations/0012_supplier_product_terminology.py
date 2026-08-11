from django.conf import settings
import django.core.validators
from django.db import migrations, models
import django.db.models.deletion
from decimal import Decimal


class Migration(migrations.Migration):
    dependencies = [("vendors", "0011_seed_hidden_biobrain_integration")]
    operations = [
        migrations.AlterField(
            model_name="organizationunit",
            name="workspace_owner",
            field=models.ForeignKey(
                help_text="The super-admin workspace or internal supplier that owns this hierarchy.",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="organization_units",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="vendorclientallocation",
            name="cpi_cut_override_percent",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Optional client-specific cut. Blank uses the supplier commercial default.",
                max_digits=5,
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(Decimal("0.00")),
                    django.core.validators.MaxValueValidator(Decimal("100.00")),
                ],
            ),
        ),
        migrations.AlterField(
            model_name="vendorcommercialprofile",
            name="delivery_mode",
            field=models.CharField(
                choices=[("panel", "Panel only"), ("api", "API only"), ("both", "Panel and API")],
                db_index=True,
                default="panel",
                help_text="Controls whether an external supplier can sign in to the panel, use API keys, or both.",
                max_length=8,
            ),
        ),
        migrations.AlterField(
            model_name="vendorsurveyallocation",
            name="cpi_cut_override_percent",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Optional survey-specific cut. Blank uses the client/supplier policy.",
                max_digits=5,
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(Decimal("0.00")),
                    django.core.validators.MaxValueValidator(Decimal("100.00")),
                ],
            ),
        ),
    ]
