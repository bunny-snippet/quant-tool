from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("surveys", "0015_toluna_models"),
    ]

    operations = [
        migrations.AlterField(
            model_name="surveyattempt",
            name="status",
            field=models.CharField(
                choices=[
                    ("initiated", "Initiated"),
                    ("redirected", "Redirected to survey"),
                    ("1", "Completed"),
                    ("2", "Terminated"),
                    ("3", "Over quota"),
                    ("4", "Quality terminated"),
                    ("7", "Survey not available"),
                    ("8", "No surveys"),
                    ("9", "No cookies"),
                    ("10", "Maximum surveys reached"),
                    ("11", "Not qualified"),
                    ("12", "Survey already taken"),
                ],
                db_index=True,
                default="initiated",
                max_length=20,
            ),
        ),
    ]
