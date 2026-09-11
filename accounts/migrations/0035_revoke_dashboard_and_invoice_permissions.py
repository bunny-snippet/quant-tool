from django.db import migrations


def revoke_permissions(apps, schema_editor):
    AccessFunction = apps.get_model("accounts", "AccessFunction")
    RoleFunctionPermission = apps.get_model("accounts", "RoleFunctionPermission")
    UserFunctionOverride = apps.get_model("accounts", "UserFunctionOverride")

    functions = AccessFunction.objects.filter(code__startswith="dashboard.") | AccessFunction.objects.filter(
        code="studies.card.invoiced_revenue"
    )
    function_ids = list(functions.values_list("id", flat=True))
    RoleFunctionPermission.objects.filter(function_id__in=function_ids).delete()
    UserFunctionOverride.objects.filter(
        function_id__in=function_ids,
        effect="allow",
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0034_seed_shared_quest_permissions")]
    operations = [migrations.RunPython(revoke_permissions, migrations.RunPython.noop)]
