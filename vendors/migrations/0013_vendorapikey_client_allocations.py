from django.db import migrations, models


def preserve_existing_key_scope(apps, schema_editor):
    VendorAPIKey = apps.get_model("vendors", "VendorAPIKey")
    VendorClientAllocation = apps.get_model("vendors", "VendorClientAllocation")
    for api_key in VendorAPIKey.objects.all().iterator(chunk_size=200):
        allocation_ids = list(
            VendorClientAllocation.objects.filter(vendor_id=api_key.vendor_id)
            .values_list("pk", flat=True)
        )
        if allocation_ids:
            api_key.client_allocations.add(*allocation_ids)


class Migration(migrations.Migration):
    dependencies = [("vendors", "0012_supplier_product_terminology")]

    operations = [
        migrations.AddField(
            model_name="vendorapikey",
            name="client_allocations",
            field=models.ManyToManyField(
                blank=True,
                help_text=(
                    "Client grants this key may expose. Project visibility and caps still follow "
                    "the live allocation rules."
                ),
                related_name="api_keys",
                to="vendors.vendorclientallocation",
            ),
        ),
        migrations.RunPython(preserve_existing_key_scope, migrations.RunPython.noop),
    ]
