from django.db import migrations
from django.utils import timezone
from datetime import datetime, time as dtime


def backfill_timeline(apps, schema_editor):
    """
    Existing RepairJob records ko naye timeline fields ke saath backfill karein.
    
    Rules:
    1. submitted_at = date_in (saare purane records ke liye)
    2. Agar status='pending' aur received_by bhara hai -> migrate to 'received'
    3. Status received/diagnosis/repairing/ready/delivered -> received_at = date_in
    4. Status='delivered' -> delivered_at = delivery_date (ya date_in)
    5. Status='ready' ya 'delivered' -> ready_at = delivery_date (ya date_in)
    """
    RepairJob = apps.get_model('accounting', 'RepairJob')

    for job in RepairJob.objects.all():
        changed = []

        # 1. submitted_at backfill (from date_in)
        if job.submitted_at is None and job.date_in:
            naive = datetime.combine(job.date_in, dtime.min)
            if timezone.is_naive(naive):
                naive = timezone.make_aware(naive)
            job.submitted_at = naive
            changed.append('submitted_at')

        # 2. Staff-created pending records -> received
        if job.status == 'pending' and job.received_by:
            job.status = 'received'
            changed.append('status')

        # 3. received_at for records already past pending
        if job.status in ('received', 'diagnosis', 'repairing', 'ready', 'delivered'):
            if job.received_at is None:
                job.received_at = job.date_in
                changed.append('received_at')

        # 4. ready_at for ready/delivered records
        if job.status in ('ready', 'delivered'):
            if job.ready_at is None:
                job.ready_at = job.delivery_date or job.date_in
                changed.append('ready_at')

        # 5. delivered_at for delivered records
        if job.status == 'delivered':
            if job.delivered_at is None:
                source_date = job.delivery_date or job.date_in
                naive = datetime.combine(source_date, dtime.min)
                if timezone.is_naive(naive):
                    naive = timezone.make_aware(naive)
                job.delivered_at = naive
                changed.append('delivered_at')

        if changed:
            job.save(update_fields=changed)


def reverse_backfill(apps, schema_editor):
    """
    Rollback: naye fields clear kar do.
    Status changes ko reverse nahi kar sakte (safe side).
    """
    RepairJob = apps.get_model('accounting', 'RepairJob')
    RepairJob.objects.update(
        submitted_at=None,
        received_at=None,
        ready_at=None,
        delivered_at=None,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0021_repairjob_delivered_at_repairjob_ready_at_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_timeline, reverse_backfill),
    ]