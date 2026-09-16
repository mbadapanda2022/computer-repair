# accounting/management/commands/cleanup_orphans.py
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count

from accounting.models import Contact

User = get_user_model()


class Command(BaseCommand):
    help = "Anonymize orphan users from soft-deleted contacts"

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would change without saving',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN\n"))

        self.stdout.write("Checking for orphan users...")

        deleted_contacts = (
            Contact.all_objects
            .filter(is_deleted=True, user__isnull=False)
            .select_related('user')
        )

        orphan_count = 0

        for contact in deleted_contacts:
            user = contact.user
            if not user or user.is_staff:
                continue
            if user.username.startswith('deleted_user_'):
                continue

            if dry_run:
                self.stdout.write(
                    f"  [DRY] Would anonymize User#{user.id} ({user.username})"
                )
            else:
                with transaction.atomic():
                    user.is_active = False
                    user.email = f'deleted_{user.id}@deleted.local'
                    user.username = f'deleted_user_{user.id}'
                    user.set_unusable_password()
                    user.save(update_fields=['is_active', 'email', 'username', 'password'])
            orphan_count += 1

        self.stdout.write(
            self.style.SUCCESS(f"  {orphan_count} user(s) anonymized\n")
        )

        self.stdout.write("Checking for duplicate phone numbers...")

        phone_conflicts = (
            Contact.all_objects
            .filter(phone__isnull=False)
            .values('phone')
            .annotate(total=Count('id'))
            .filter(total__gt=1)
        )

        blocking = 0
        for row in phone_conflicts:
            phone = row['phone']
            contacts = Contact.all_objects.filter(phone=phone)
            active = [c for c in contacts if not c.is_deleted]

            if len(active) > 1:
                self.stdout.write(
                    self.style.ERROR(
                        f"  X Phone {phone}: {len(active)} ACTIVE contacts!"
                    )
                )
                blocking += 1

        if blocking:
            self.stdout.write(
                self.style.ERROR(f"\n  {blocking} blocking conflict(s)\n")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS("  No blocking conflicts\n")
            )

        self.stdout.write(self.style.SUCCESS("Done."))
