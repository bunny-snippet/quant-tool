from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("surveys", "0026_final_id_reconciliation")]

    operations = [
        migrations.AddField(
            model_name="finalidupload",
            name="invalid_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="finalidupload",
            name="auto_rejected_count",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
