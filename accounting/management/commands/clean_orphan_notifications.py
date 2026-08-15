# accounting/management/commands/clean_orphan_notifications.py

import re
import logging
from django.core.management.base import BaseCommand
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth import get_user_model
from accounting.models import Notification

logger = logging.getLogger(__name__)
User = get_user_model()


class Command(BaseCommand):
    help = """
    Delete notifications whose linked object no longer exists.
    Uses GenericForeignKey fields (content_type, object_id) first,
    then falls back to legacy link parsing for backward compatibility.
    """

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show which notifications would be deleted without actually deleting.',
        )
        parser.add_argument(
            '--model',
            type=str,
            help='Only clean notifications for a specific model (e.g., repairjob, invoice)',
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Show detailed output for each notification checked.',
        )

    def handle(self, *args, **options):
        dry_run = options.get('dry_run', False)
        verbose = options.get('verbose', False)
        specific_model = options.get('model')

        # Models to check – accounting models + User (for notifications with recipient)
        model_names = ['repairjob', 'invoice', 'contact', 'purchase', 'payment']
        if specific_model:
            if specific_model.lower() not in model_names and specific_model.lower() != 'user':
                self.stderr.write(self.style.ERROR(f"Invalid model '{specific_model}'. Allowed: {model_names + ['user']}"))
                return
            if specific_model.lower() == 'user':
                model_names = []  # we'll handle user separately
            else:
                model_names = [specific_model.lower()]

        total_deleted = 0

        # --- 1. Clean using GenericForeignKey (content_type, object_id) ---
        self.stdout.write("🔍 Checking orphans using GenericForeignKey fields...")
        for model_name in model_names:
            try:
                model = apps.get_model('accounting', model_name)
            except LookupError:
                self.stderr.write(self.style.WARNING(f"Model '{model_name}' not found, skipping."))
                continue

            content_type = ContentType.objects.get_for_model(model)

            # Get all notifications for this content type
            notif_qs = Notification.objects.filter(content_type=content_type)

            if not notif_qs.exists():
                if verbose:
                    self.stdout.write(f"  No notifications for {model_name}")
                continue

            # Collect orphan IDs
            orphan_ids = []
            existing_ids = set(model.objects.values_list('pk', flat=True))

            for notif in notif_qs.only('id', 'object_id'):
                obj_id = notif.object_id
                if obj_id is None or obj_id not in existing_ids:
                    orphan_ids.append(notif.id)

            if orphan_ids:
                if not dry_run:
                    # Bulk delete
                    deleted_count = Notification.objects.filter(id__in=orphan_ids).delete()[0]
                    total_deleted += deleted_count
                    self.stdout.write(self.style.SUCCESS(f"  Deleted {deleted_count} orphan notifications for {model_name}"))
                else:
                    total_deleted += len(orphan_ids)
                    self.stdout.write(f"  Would delete {len(orphan_ids)} orphan notifications for {model_name}")
                if verbose:
                    for nid in orphan_ids[:10]:  # Show first 10
                        self.stdout.write(f"    - Notification ID {nid}")
                    if len(orphan_ids) > 10:
                        self.stdout.write(f"    ... and {len(orphan_ids)-10} more")
            else:
                if verbose:
                    self.stdout.write(f"  No orphan notifications for {model_name}")

        # --- 2. Clean User notifications (recipient deleted) ---
        if specific_model is None or specific_model.lower() == 'user':
            self.stdout.write("🔍 Checking User notifications (recipient deleted)...")
            notif_qs = Notification.objects.all()
            # Get all valid user IDs
            valid_user_ids = set(User.objects.values_list('pk', flat=True))
            orphan_user_notif_ids = []

            for notif in notif_qs.only('id', 'recipient_id'):
                if notif.recipient_id and notif.recipient_id not in valid_user_ids:
                    orphan_user_notif_ids.append(notif.id)

            if orphan_user_notif_ids:
                if not dry_run:
                    deleted_count = Notification.objects.filter(id__in=orphan_user_notif_ids).delete()[0]
                    total_deleted += deleted_count
                    self.stdout.write(self.style.SUCCESS(f"  Deleted {deleted_count} notifications with deleted recipient"))
                else:
                    total_deleted += len(orphan_user_notif_ids)
                    self.stdout.write(f"  Would delete {len(orphan_user_notif_ids)} notifications with deleted recipient")
            else:
                if verbose:
                    self.stdout.write("  No orphan notifications for deleted recipients")

        # --- 3. Legacy link parsing (for notifications without content_type) ---
        self.stdout.write("🔍 Checking remaining orphans using legacy link parsing...")
        # Get notifications that have no content_type (legacy) or link is present
        legacy_notifs = Notification.objects.filter(content_type__isnull=True)

        if legacy_notifs.exists():
            orphan_ids = []
            # Build regex patterns for each model
            model_patterns = {}
            for model_name in model_names:
                try:
                    model = apps.get_model('accounting', model_name)
                except LookupError:
                    continue
                # Match URLs like /repair/123/ or /invoice/45/
                pattern = rf'/{model_name}/(\d+)/'
                model_patterns[model_name] = (model, re.compile(pattern))

            for notif in legacy_notifs.only('id', 'link'):
                link = notif.link or ""
                found = False
                for model_name, (model, pattern) in model_patterns.items():
                    match = pattern.search(link)
                    if match:
                        pk = int(match.group(1))
                        try:
                            model.objects.get(pk=pk)
                            found = True  # exists, so not orphan
                            break
                        except model.DoesNotExist:
                            # orphan – will delete
                            orphan_ids.append(notif.id)
                            found = True  # we found a match, but it's orphan
                            break
                # If no pattern matched, but link exists and content_type is null, we might consider it orphan? 
                # But we skip – we only delete if we found a matching pattern and the object is missing.

            if orphan_ids:
                if not dry_run:
                    deleted_count = Notification.objects.filter(id__in=orphan_ids).delete()[0]
                    total_deleted += deleted_count
                    self.stdout.write(self.style.SUCCESS(f"  Deleted {deleted_count} orphan notifications via legacy links"))
                else:
                    total_deleted += len(orphan_ids)
                    self.stdout.write(f"  Would delete {len(orphan_ids)} orphan notifications via legacy links")
                if verbose:
                    for nid in orphan_ids[:10]:
                        self.stdout.write(f"    - Notification ID {nid}")
            else:
                if verbose:
                    self.stdout.write("  No orphan notifications found via legacy links")

        # --- Final Summary ---
        if dry_run:
            self.stdout.write(self.style.WARNING(f"✅ Dry-run complete: Would delete {total_deleted} orphan notifications."))
        else:
            self.stdout.write(self.style.SUCCESS(f"✅ Cleanup complete: Deleted {total_deleted} orphan notifications."))