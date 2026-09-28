from django.db import migrations
from decimal import Decimal


def migrate_labour_to_services(apps, schema_editor):
    """
    Data migration: convert existing RepairJob.labour_charge values
    into RepairService entries, then zero out labour_charge.

    Products: Uses / creates a 'Repair Labour' Product with is_service=True.
    """
    RepairJob = apps.get_model('accounting', 'RepairJob')
    RepairService = apps.get_model('accounting', 'RepairService')
    Product = apps.get_model('accounting', 'Product')

    # Get or create the default service product
    labour_product, _ = Product.objects.get_or_create(
        name="Repair Labour",
        defaults={
            'is_service': True,
            'selling_price': Decimal('0'),
            'tax_rate': Decimal('0'),
            'hsn_code': '998446',
            'is_active': True,
        },
    )

    jobs_with_labour = RepairJob.objects.filter(labour_charge__gt=0)
    count = 0
    for job in jobs_with_labour:
        # Skip if a service already exists (idempotent)
        if RepairService.objects.filter(
            repair_job=job,
            product=labour_product,
        ).exists():
            continue

        RepairService.objects.create(
            repair_job=job,
            product=labour_product,
            amount=job.labour_charge,
            description="Migrated from labour_charge field",
            line_total=job.labour_charge,
        )
        job.labour_charge = Decimal('0')
        job.save(update_fields=['labour_charge'])
        count += 1

    print(f"\n[migration] Migrated {count} repair labour charges to RepairService.")


def reverse_migration(apps, schema_editor):
    """Reverse: restore labour_charge from RepairService entries."""
    RepairService = apps.get_model('accounting', 'RepairService')
    Product = apps.get_model('accounting', 'Product')

    try:
        labour_product = Product.objects.get(name="Repair Labour")
    except Product.DoesNotExist:
        return

    services = RepairService.objects.filter(product=labour_product)
    for svc in services:
        job = svc.repair_job
        job.labour_charge = svc.amount
        job.save(update_fields=['labour_charge'])
        svc.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0033_repairservice'),
    ]

    operations = [
        migrations.RunPython(migrate_labour_to_services, reverse_migration),
    ]