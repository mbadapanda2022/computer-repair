# accounting/management/commands/clean_orphan_notifications.py

import re
import logging
from django.core.management.base import BaseCommand
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth import get_user_model
from django.db.models import Exists, OuterRef
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

        model_names = ['repairjob', 'invoice', 'contact', 'purchase', 'payment']
        if specific_model:
            if specific_model.lower() not in model_names and specific_model.lower() != 'user':
                self.stderr.write(self.style.ERROR(
                    f"Invalid model '{specific_model}'. Allowed: {model_names + ['user']}"
                ))
                return
            if specific_model.lower() == 'user':
                model_names = []
            else:
                model_names = [specific_model.lower()]

        total_deleted = 0

        # --- 1. GenericForeignKey orphans ---
        self.stdout.write("🔍 Checking orphans using GenericForeignKey fields...")
        for model_name in model_names:
            try:
                model = apps.get_model('accounting', model_name)
            except LookupError:
                self.stderr.write(self.style.WARNING(f"Model '{model_name}' not found, skipping."))
                continue

            content_type = ContentType.objects.get_for_model(model)
            notif_qs = Notification.objects.filter(content_type=content_type)

            if not notif_qs.exists():
                if verbose:
                    self.stdout.write(f"  No notifications for {model_name}")
                continue

            # Bulk Exists check – no in-memory set of all IDs
            valid_obj = model.objects.filter(pk=OuterRef('object_id'))
            orphans = notif_qs.annotate(obj_exists=Exists(valid_obj)).filter(obj_exists=False)

            # Also handle NULL object_id
            null_object = notif_qs.filter(object_id__isnull=True)

            orphan_ids = list(orphans.values_list('id', flat=True)) + \
                         list(null_object.values_list('id', flat=True))

            if orphan_ids:
                if not dry_run:
                    deleted_count = Notification.objects.filter(id__in=orphan_ids).delete()[0]
                    total_deleted += deleted_count
                    self.stdout.write(self.style.SUCCESS(
                        f"  Deleted {deleted_count} orphan notifications for {model_name}"
                    ))
                else:
                    total_deleted += len(orphan_ids)
                    self.stdout.write(f"  Would delete {len(orphan_ids)} orphan notifications for {model_name}")
                if verbose:
                    for nid in orphan_ids[:10]:
                        self.stdout.write(f"    - Notification ID {nid}")
                    if len(orphan_ids) > 10:
                        self.stdout.write(f"    ... and {len(orphan_ids)-10} more")
            else:
                if verbose:
                    self.stdout.write(f"  No orphan notifications for {model_name}")

        # --- 2. User notifications (recipient deleted) ---
        if specific_model is None or specific_model.lower() == 'user':
            self.stdout.write("🔍 Checking User notifications (recipient deleted)...")

            valid_user = User.objects.filter(pk=OuterRef('recipient_id'))
            orphan_qs = Notification.objects.annotate(
                user_exists=Exists(valid_user)
            ).filter(recipient_id__isnull=False, user_exists=False)

            orphan_user_ids = list(orphan_qs.values_list('id', flat=True))

            if orphan_user_ids:
                if not dry_run:
                    deleted_count = Notification.objects.filter(id__in=orphan_user_ids).delete()[0]
                    total_deleted += deleted_count
                    self.stdout.write(self.style.SUCCESS(
                        f"  Deleted {deleted_count} notifications with deleted recipient"
                    ))
                else:
                    total_deleted += len(orphan_user_ids)
                    self.stdout.write(f"  Would delete {len(orphan_user_ids)} notifications with deleted recipient")
            else:
                if verbose:
                    self.stdout.write("  No orphan notifications for deleted recipients")

        # --- 3. Legacy link parsing (no content_type) ---
        self.stdout.write("🔍 Checking remaining orphans using legacy link parsing...")
        legacy_notifs = Notification.objects.filter(content_type__isnull=True)

        if legacy_notifs.exists():
            orphan_ids = []

            # Plural/singular dono handle karo
            model_patterns = {
                'repairjob': re.compile(r'/repairs?/(\d+)/'),
                'invoice': re.compile(r'/(?:sales|invoices?)/(\d+)/'),
                'purchase': re.compile(r'/purchases?/(\d+)/'),
                'contact': re.compile(r'/contacts?/(\d+)/'),
                'payment': re.compile(r'/payments?/(\d+)/'),
            }

            for notif in legacy_notifs.only('id', 'link').iterator():
                link = notif.link or ""
                for model_name, pattern in model_patterns.items():
                    match = pattern.search(link)
                    if match:
                        pk = int(match.group(1))
                        try:
                            model = apps.get_model('accounting', model_name)
                            if not model.objects.filter(pk=pk).exists():
                                orphan_ids.append(notif.id)
                        except LookupError:
                            pass
                        break

            if orphan_ids:
                if not dry_run:
                    deleted_count = Notification.objects.filter(id__in=orphan_ids).delete()[0]
                    total_deleted += deleted_count
                    self.stdout.write(self.style.SUCCESS(
                        f"  Deleted {deleted_count} orphan notifications via legacy links"
                    ))
                else:
                    total_deleted += len(orphan_ids)
                    self.stdout.write(f"  Would delete {len(orphan_ids)} orphan notifications via legacy links")
                if verbose:
                    for nid in orphan_ids[:10]:
                        self.stdout.write(f"    - Notification ID {nid}")
            else:
                if verbose:
                    self.stdout.write("  No orphan notifications found via legacy links")

        # --- Summary ---
        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"✅ Dry-run complete: Would delete {total_deleted} orphan notifications."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"✅ Cleanup complete: Deleted {total_deleted} orphan notifications."
            ))