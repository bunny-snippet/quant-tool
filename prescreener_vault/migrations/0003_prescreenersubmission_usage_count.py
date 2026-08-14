from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("prescreener_vault", "0002_minimize_submission_identity"),
    ]

    operations = [
        migrations.AddField(
            model_name="prescreenersubmission",
            name="usage_count",
            field=models.PositiveIntegerField(
                db_index=True,
                default=1,
                help_text="Total visits: one original submission plus approved profile reuses.",
            ),
        ),
    ]
