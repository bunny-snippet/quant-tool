from django.db import migrations, models


def classify_existing_allocations(apps, schema_editor):
    Counter = apps.get_model("surveys", "ProfileReuseMonthlyCounter")
    for counter in Counter.objects.exclude(allocated_reuses=0).iterator():
        counter.first_reuse_allocated = counter.allocated_reuses
        counter.save(update_fields=["first_reuse_allocated"])


class Migration(migrations.Migration):
    dependencies = [("surveys", "0018_profilereuseevent_reused_rid")]

    operations = [
        migrations.AddField(
            model_name="profilereusemonthlycounter",
            name="first_reuse_allocated",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="profilereusemonthlycounter",
            name="repeat_reuse_allocated",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="profilereuseevent",
            name="reuse_pool",
            field=models.CharField(
                choices=[("first", "First reuse"), ("returning", "Returning profile")],
                db_index=True,
                default="first",
                max_length=16,
            ),
        ),
        migrations.RunPython(classify_existing_allocations, migrations.RunPython.noop),
    ]
