import re
from django.core.management.base import BaseCommand
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from accounting.models import Notification

class Command(BaseCommand):
    help = 'Delete notifications whose linked object no longer exists (Hybrid: Uses new fields first, then falls back to link parsing).'

    def handle(self, *args, **kwargs):
        deleted = 0
        model_names = ['repairjob', 'invoice', 'contact', 'purchase', 'payment']

        self.stdout.write("🔍 Checking orphans using new GenericForeignKey fields...")
        for model_name in model_names:
            try:
                model = apps.get_model('accounting', model_name)
            except LookupError:
                continue

            content_type = ContentType.objects.get_for_model(model)

            notif_ids = Notification.objects.filter(content_type=content_type).values_list('id', 'object_id')
            
            for notif_id, obj_id in notif_ids:
                if obj_id is None:
                    continue
                if not model.objects.filter(pk=obj_id).exists():
                    Notification.objects.filter(pk=notif_id).delete()
                    deleted += 1
                    self.stdout.write(f'🗑️ Deleted notification {notif_id} (orphan via GFK)')

        self.stdout.write("🔍 Checking remaining orphans using legacy link parsing (for backward compatibility)...")
        
        legacy_notifs = Notification.objects.filter(content_type__isnull=True)
        
        for notif in legacy_notifs:
            link = notif.link or ""
            deleted_this = False
            
            for model_name in model_names:
                pattern = rf'/{model_name}/(\d+)/'
                match = re.search(pattern, link)
                if match:
                    pk = int(match.group(1))
                    try:
                        model = apps.get_model('accounting', model_name)
                        model.objects.get(pk=pk)
                    except model.DoesNotExist:
                        notif.delete()
                        deleted += 1
                        deleted_this = True
                        self.stdout.write(f'🗑️ Deleted notification {notif.id} (orphan via legacy link)')
                        break 
                    except LookupError:
                        pass

        self.stdout.write(self.style.SUCCESS(f'✅ Cleanup complete: Deleted {deleted} orphan notifications.'))