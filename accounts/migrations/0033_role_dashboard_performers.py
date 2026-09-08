from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0032_seed_user_dashboard_permission")]
    operations = [migrations.AddField(model_name="role", name="dashboard_performers", field=models.JSONField(default=dict, blank=True))]
