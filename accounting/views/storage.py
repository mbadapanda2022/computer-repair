"""Staff Database Health: Supabase free-tier usage, safe cleanup of
bloat-only data, and quota watchdog alerts.

Nothing here may touch accounting truth (invoices, payments, ledger,
repairs, contacts). Cleanup categories are limited to telemetry-style
rows that no report reads: visitor analytics, read notifications older
than 60 days, expired OTPs, expired cache rows, expired sessions.
"""
from __future__ import annotations

import os
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.db import connection
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..models import EmailOTP, Notification, PageView, StorageSnapshot, VisitorSession

# Supabase free project disk quota
FREE_QUOTA_BYTES = 500 * 1024 * 1024
ALERT_PCT = 80.0
ALERT_THROTTLE_KEY = 'storage_alert_throttle'
ALERT_THROTTLE_SECONDS = 7 * 24 * 3600


def format_bytes(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if abs(n) < 1024 or unit == 'GB':
            return f'{n:.1f} {unit}' if unit != 'B' else f'{int(n)} B'
        n /= 1024


def get_db_size_bytes():
    """Whole-database size, or (dev/tests) the sqlite file size."""
    if connection.vendor == 'postgresql':
        with connection.cursor() as c:
            c.execute('SELECT pg_database_size(current_database())')
            return int(c.fetchone()[0])
    base = str(settings.DATABASES['default']['NAME'])
    total = 0
    for path in (base, base + '-wal', base + '-shm'):
        try:
            total += os.path.getsize(path)
        except OSError:
            pass
    if not total:
        # In-memory sqlite (tests): measure via pages instead.
        with connection.cursor() as c:
            c.execute('PRAGMA page_count')
            pages = int(c.fetchone()[0])
            c.execute('PRAGMA page_size')
            page_size = int(c.fetchone()[0])
        total = pages * page_size
    return total


def _table_sizes(limit=12):
    if connection.vendor != 'postgresql':
        return []
    with connection.cursor() as c:
        c.execute("""
            SELECT relname, pg_total_relation_size(relid)::bigint AS bytes
            FROM pg_stat_user_tables
            ORDER BY bytes DESC
            LIMIT %s
        """, [limit])
        return c.fetchall()


# ────────────────────────────────────────────────────────────────
#  Safe cleanup categories
# ────────────────────────────────────────────────────────────────

VISITOR_DATA_DAYS = 90
READ_NOTIF_DAYS = 60


def _cache_now_str():
    # DatabaseCache stores expires as 'YYYY-MM-DD HH:MM:SS' text on sqlite
    # and timestamptz on Postgres — this string works for both.
    return timezone.now().strftime('%Y-%m-%d %H:%M:%S')


def _cache_expired_count():
    try:
        with connection.cursor() as c:
            c.execute('SELECT count(*) FROM django_cache_table WHERE expires < %s',
                      [_cache_now_str()])
            return c.fetchone()[0]
    except Exception:
        return 0  # table not created yet


def _delete_cache_expired():
    with connection.cursor() as c:
        c.execute('DELETE FROM django_cache_table WHERE expires < %s',
                  [_cache_now_str()])
        return c.rowcount if c.rowcount and c.rowcount > 0 else 0


def _old_sessions():
    cutoff = timezone.now() - timedelta(days=VISITOR_DATA_DAYS)
    return VisitorSession.objects.filter(started_at__lt=cutoff)


def _old_read_notifications():
    cutoff = timezone.now() - timedelta(days=READ_NOTIF_DAYS)
    # all_objects: hard-delete, including rows already soft-deleted —
    # read notifications older than 60 days are not part of any report.
    return Notification.all_objects.filter(is_read=True, created_at__lt=cutoff)


def _expired_otps():
    return EmailOTP.objects.filter(expires_at__lt=timezone.now())


def _expired_sessions():
    return Session.objects.filter(expire_date__lt=timezone.now())


CLEANUP_CATEGORIES = [
    {
        'key': 'visitor_data',
        'label': 'Website visitor history (90+ days old)',
        'desc': ('Old anonymous visits + their page views on the public '
                 'website. Reports only show the last 90 days, so older '
                 'rows are dead weight. No customer record is touched.'),
        'count': lambda: (_old_sessions().count(),
                          'sessions'),
        'extra': lambda: f'{PageView.objects.filter(session__started_at__lt=timezone.now() - timedelta(days=VISITOR_DATA_DAYS)).count()} page views',
        'run': lambda: _old_sessions().delete()[0],
    },
    {
        'key': 'read_notifications',
        'label': 'Read notifications (60+ days old)',
        'desc': ('Bell-icon notifications the staff has already read. '
                 'Unread notifications are NEVER deleted. Books and '
                 'records are unaffected.'),
        'count': lambda: (_old_read_notifications().count(), 'notifications'),
        'extra': lambda: '',
        'run': lambda: _old_read_notifications().delete()[0],
    },
    {
        'key': 'expired_otps',
        'label': 'Expired login/reset OTPs',
        'desc': ('One-time codes already past their expiry — unusable '
                 'by design, so deleting them is always safe.'),
        'count': lambda: (_expired_otps().count(), 'OTPs'),
        'extra': lambda: '',
        'run': lambda: _expired_otps().delete()[0],
    },
    {
        'key': 'expired_sessions',
        'label': 'Expired browser sign-in sessions',
        'desc': ('Sessions whose expiry date has passed — those devices '
                 'must log in again anyway. Active sessions are kept.'),
        'count': lambda: (_expired_sessions().count(), 'sessions'),
        'extra': lambda: '',
        'run': lambda: _expired_sessions().delete()[0],
    },
    {
        'key': 'expired_cache',
        'label': 'Expired cache rows',
        'desc': ('Internal key-value cache (rate limits, counters). '
                 'Expired entries are recreated automatically.'),
        'count': lambda: (_cache_expired_count(), 'keys'),
        'extra': lambda: '',
        'run': lambda: _delete_cache_expired(),
    },
]


def cleanup_previews():
    out = []
    for cat in CLEANUP_CATEGORIES:
        n, unit = cat['count']()
        out.append({
            'key': cat['key'],
            'label': cat['label'],
            'desc': cat['desc'],
            'count': n,
            'unit': unit,
            'extra': cat['extra'](),
        })
    return out


def run_cleanup(key):
    for cat in CLEANUP_CATEGORIES:
        if cat['key'] == key:
            _n, unit = cat['count']()
            return cat['run'](), cat['label'], unit
    raise ValueError(f'Unknown cleanup category: {key}')


# ────────────────────────────────────────────────────────────────
#  Watchdog: snapshots + alert email
# ────────────────────────────────────────────────────────────────

def record_snapshot():
    snap = StorageSnapshot.objects.create(db_bytes=get_db_size_bytes())
    # Keep this table itself tiny: drop snapshots older than a year.
    StorageSnapshot.objects.filter(
        created_at__lt=timezone.now() - timedelta(days=365)
    ).delete()
    return snap


def send_quota_alert(pct, db_bytes):
    from django.core.mail import send_mail
    from django.contrib.auth import get_user_model

    recipients = []
    admin_email = getattr(settings, 'ADMIN_EMAIL', '')
    if admin_email:
        recipients.append(admin_email)
    User = get_user_model()
    recipients += list(
        User.objects.filter(is_superuser=True, is_active=True)
        .exclude(email='').values_list('email', flat=True))
    recipients = list(dict.fromkeys(recipients))
    if not recipients:
        return False

    used_mb = db_bytes / 1048576
    free_mb = FREE_QUOTA_BYTES / 1048576
    body = (
        f'Database storage check at {timezone.now():%Y-%m-%d %H:%M} UTC\n\n'
        f'Used: {used_mb:.1f} MB of {free_mb:.0f} MB ({pct:.1f}%) on the '
        'Supabase FREE plan.\n\n'
        'WHAT TO DO (in order):\n'
        '1. Open Staff Portal → Database Health → Safe Cleanup and run the '
        'listed cleanups (visitor history, read notifications, expired OTPs, '
        'expired sessions, expired cache). None of them affect the books.\n'
        '2. Take a backup: Supabase dashboard → Database → Backups, or run '
        '"python manage.py backup_restore backup" and keep the JSON safe.\n'
        '3. If still growing toward the limit, upgrade to Supabase Pro '
        '(about $25/month, 8 GB) at https://supabase.com/pricing — the '
        'free plan can also PAUSE a project after 7 days without traffic '
        'and DELETE it after 30 days paused, so never ignore this.\n\n'
        '— A1 Computer Solutions storage watchdog'
    )
    send_mail(
        subject=f'[A1] Database storage {pct:.0f}% used — clean up / backup now',
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=recipients,
        fail_silently=False,
    )
    return True


def check_quota_and_alert():
    """Returns (db_bytes, pct, alert_sent)."""
    db_bytes = get_db_size_bytes()
    pct = db_bytes / FREE_QUOTA_BYTES * 100
    alert_sent = False
    if pct >= ALERT_PCT and not cache.get(ALERT_THROTTLE_KEY):
        try:
            alert_sent = send_quota_alert(pct, db_bytes)
            cache.set(ALERT_THROTTLE_KEY, 1, ALERT_THROTTLE_SECONDS)
        except Exception:
            pass  # SMTP outage must not break the cron heartbeat
    return db_bytes, pct, alert_sent


# ────────────────────────────────────────────────────────────────
#  Staff page
# ────────────────────────────────────────────────────────────────

def _estimated_daily_growth_bytes():
    """Bytes/day estimate from rows created in the last 30 days across
    the tables that actually grow, at ~1 KB/row (measured average on
    production incl. indexes)."""
    since = timezone.now() - timedelta(days=30)
    from ..models import AuditLog, RepairStatusLog
    counters = [
        VisitorSession.objects.filter(started_at__gte=since).count(),
        PageView.objects.filter(visited_at__gte=since).count(),
        Notification.objects.filter(created_at__gte=since).count(),
        RepairStatusLog.objects.filter(changed_at__gte=since).count(),
        AuditLog.objects.filter(timestamp__gte=since).count(),
        EmailOTP.objects.filter(created_at__gte=since).count(),
    ]
    return sum(counters) * 1024 // 30


@login_required
def storage_health(request):
    """Staff Database Health page (AccessControlMiddleware enforces staff)."""
    db_bytes = get_db_size_bytes()
    pct = min(db_bytes / FREE_QUOTA_BYTES * 100, 100)
    band = ('success' if pct < 60 else
            'warning' if pct < ALERT_PCT else 'danger')
    free_bytes = max(FREE_QUOTA_BYTES - db_bytes, 0)

    daily_growth = _estimated_daily_growth_bytes()
    days_to_full = (free_bytes // daily_growth) if daily_growth else None

    last_check = StorageSnapshot.objects.order_by('-created_at').first()
    snapshots = list(StorageSnapshot.objects.order_by('-created_at')[:30])
    for s in snapshots:
        s.size_display = format_bytes(s.db_bytes)

    context = {
        'db_bytes': db_bytes,
        'db_size_display': format_bytes(db_bytes),
        'quota_display': format_bytes(FREE_QUOTA_BYTES),
        'pct': pct,
        'band': band,
        'free_display': format_bytes(free_bytes),
        'daily_growth_display': format_bytes(daily_growth),
        'days_to_full': days_to_full,
        'years_to_full': round(days_to_full / 365, 1) if days_to_full else None,
        'tables': [(name, format_bytes(size)) for name, size in _table_sizes()],
        'previews': cleanup_previews(),
        'snapshots': snapshots,
        'last_check': last_check,
        'alert_pct': ALERT_PCT,
    }
    return render(request, 'dashboard/storage_health.html', context)


@login_required
@require_POST
def storage_cleanup(request):
    key = request.POST.get('category', '')
    if request.POST.get('confirm') != 'DELETE':
        messages.error(request, 'Cleanup was not confirmed — nothing deleted.')
        return redirect('accounting:storage_health')
    try:
        n, label, unit = run_cleanup(key)
    except ValueError:
        messages.error(request, 'Unknown cleanup category.')
        return redirect('accounting:storage_health')
    messages.success(
        request,
        f'Cleaned up {n} {unit} from “{label}”. Books and records were not '
        'affected.',
    )
    return redirect('accounting:storage_health')
