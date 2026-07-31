import re
import logging
from django.core.management.base import BaseCommand
from django.contrib.contenttypes.models import ContentType
from accounting.models import Notification, Invoice, RepairJob, Purchase, Contact, Payment

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = "Backfill content_type and object_id for existing notifications (Safe: does NOT delete anything)."

    def handle(self, *args, **options):
        qs = Notification.objects.filter(content_type__isnull=True)
        total = qs.count()
        self.stdout.write(f"📊 Found {total} notifications to backfill.")

        updated = 0
        skipped = 0

        for notif in qs.iterator():
            link = notif.link or ""
            model_name = None
            obj_id = None

            patterns = [
                (r'/sales/(\d+)/', 'invoice'),
                (r'/repair[s]?/(\d+)/', 'repairjob'),
                (r'/purchases/(\d+)/', 'purchase'),
                (r'/customer/repair[s]?/(\d+)/', 'repairjob'),
                (r'/customer/invoices/(\d+)/', 'invoice'),
            ]
            
            for pattern, model_str in patterns:
                match = re.search(pattern, link)
                if match:
                    model_name = model_str
                    obj_id = int(match.group(1))
                    break

            if model_name and obj_id:
                model_map = {
                    'invoice': Invoice,
                    'repairjob': RepairJob,
                    'purchase': Purchase,
                    'contact': Contact,
                    'payment': Payment,
                }
                model_class = model_map.get(model_name)
                if model_class and model_class.objects.filter(pk=obj_id).exists():
                    content_type = ContentType.objects.get_for_model(model_class)
                    Notification.objects.filter(pk=notif.pk).update(
                        content_type=content_type,
                        object_id=obj_id
                    )
                    updated += 1
                else:
                    skipped += 1
            else:
                skipped += 1

        self.stdout.write(self.style.SUCCESS(
            f"✅ Backfill complete: {updated} updated, {skipped} skipped (orphans or invalid links)."
        ))