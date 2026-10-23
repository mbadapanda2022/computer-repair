"""Daily storage watchdog (Render Cron Job).

Three jobs:
1. Heartbeat — opening a DB connection keeps the Supabase FREE project
   from ever going idle (idle projects pause after 7 days and are
   DELETED after 30 days paused).
2. Record a StorageSnapshot so the Database Health page shows a trend.
3. Email staff when the DB passes the free-quota alert threshold.

Usage (also the cron command):
    python manage.py storage_watchdog
"""
from django.core.management.base import BaseCommand

from accounting.views.storage import check_quota_and_alert, record_snapshot


class Command(BaseCommand):
    help = ('Touch the database, record its size, and alert staff if the '
            'free-tier quota is running out. Safe to run every day.')

    def handle(self, *args, **options):
        snap = record_snapshot()
        db_bytes, pct, alert_sent = check_quota_and_alert()
        self.stdout.write(self.style.SUCCESS(
            'storage_watchdog: DB is %.1f MB (%.1f%% of free quota); '
            'snapshot id=%s; alert_email_sent=%s'
            % (db_bytes / 1048576, pct, snap.pk, alert_sent)))
