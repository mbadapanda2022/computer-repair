from django.db import migrations
import re


def sync_counters(apps, schema_editor):
    """Sync Repair/Invoice/Purchase counters with existing records."""
    InvoiceCounter = apps.get_model('accounting', 'InvoiceCounter')
    RepairJob = apps.get_model('accounting', 'RepairJob')
    Invoice = apps.get_model('accounting', 'Invoice')
    Purchase = apps.get_model('accounting', 'Purchase')

    # ---- REP counter ----
    rep_nums = []
    for jn in RepairJob.objects.filter(job_number__startswith='REP-').values_list('job_number', flat=True):
        m = re.search(r'REP-(\d+)', jn)
        if m:
            rep_nums.append(int(m.group(1)))
    max_rep = max(rep_nums) if rep_nums else 0
    counter, _ = InvoiceCounter.objects.get_or_create(prefix='REP')
    counter.last_number = max_rep
    counter.save()

    # ---- INV counter ----
    inv_nums = []
    for inv in Invoice.objects.all().values_list('invoice_number', flat=True):
        if not inv:
            continue
        m = re.search(r'(\d+)\s*$', inv)
        if m:
            inv_nums.append(int(m.group(1)))
    max_inv = max(inv_nums) if inv_nums else 0
    sample = Invoice.objects.first()
    if sample and sample.invoice_number:
        prefix = sample.invoice_number.rsplit('-', 1)[0]
    else:
        prefix = 'INV'
    counter, _ = InvoiceCounter.objects.get_or_create(prefix=prefix)
    counter.last_number = max_inv
    counter.save()

    # ---- PUR counter ----
    pur_nums = []
    for pn in Purchase.objects.filter(purchase_number__startswith='PUR-').values_list('purchase_number', flat=True):
        m = re.search(r'PUR-(\d+)', pn)
        if m:
            pur_nums.append(int(m.group(1)))
    max_pur = max(pur_nums) if pur_nums else 0
    counter, _ = InvoiceCounter.objects.get_or_create(prefix='PUR')
    counter.last_number = max_pur
    counter.save()


def reverse_sync(apps, schema_editor):
    """Rollback — kuch nahi."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0022_backfill_repair_timeline'),
    ]

    operations = [
        migrations.RunPython(sync_counters, reverse_sync),
    ]