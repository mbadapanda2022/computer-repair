# accounting/management/commands/sync_google_reviews.py
from django.core.management.base import BaseCommand

from accounting.google_reviews import sync_google_reviews, is_configured


class Command(BaseCommand):
    help = "Fetch Google Business Profile reviews (Places API) into Testimonials"

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would change without touching the database',
        )

    def handle(self, *args, **options):
        if not is_configured():
            self.stderr_error = (
                "GOOGLE_PLACES_API_KEY / GOOGLE_PLACE_ID not set in environment. "
                "Sync skipped."
            )
            self.stdout.write(self.style.WARNING(self.stderr_error))
            return

        stats = sync_google_reviews(dry_run=options['dry_run'])
        prefix = "[dry-run] " if options['dry_run'] else ""
        if stats['error']:
            self.stdout.write(self.style.ERROR(
                f"{prefix}Sync failed: {stats['error']} (existing reviews kept)"
            ))
            return
        self.stdout.write(self.style.SUCCESS(
            f"{prefix}Fetched {stats['fetched']} | created {stats['created']} | "
            f"updated {stats['updated']} | deactivated {stats['deactivated']} | "
            f"skipped {stats['skipped']}"
        ))
