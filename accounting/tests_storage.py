"""
Database Health (storage page, safe cleanup, quota watchdog) — tests.

Chalane ka tareeka:
    python manage.py test accounting.tests_storage -v 2

Golden rule under test: cleanup may delete ONLY bloat-only rows (visitor
telemetry, read notifications, expired OTPs/sessions/cache). Invoices,
ledger, repairs and contacts must survive every cleanup category.
"""
from __future__ import annotations

import io
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.sessions.models import Session as DbSession
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .models import (
    Contact, EmailOTP, Notification, PageView, RepairJob, StorageSnapshot,
    VisitorSession,
)
from .views import storage as storage_views

HEALTH_URL = '/db-health/'
CLEAN_URL = '/db-health/cleanup/'


def _mk_visitor(days_ago=0):
    s = VisitorSession.objects.create(
        visitor_id=uuid.uuid4(), last_seen_at=timezone.now(),
        entry_path='/', device_type='desktop',
    )
    if days_ago:
        ts = timezone.now() - timedelta(days=days_ago)
        VisitorSession.objects.filter(pk=s.pk).update(
            started_at=ts, last_seen_at=ts)
        s.refresh_from_db()
    return s


class StoragePageAccessTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_user(
            username='dbhealth_staff', password='TestPass123!',
            is_staff=True, is_active=True)
        self.customer = User.objects.create_user(
            username='dbhealth_cust', password='TestPass123!', is_active=True)

    def test_anonymous_redirected_to_login(self):
        resp = self.client.get(HEALTH_URL)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('login', resp['Location'])

    def test_non_staff_blocked(self):
        self.client.force_login(self.customer)
        resp = self.client.get(HEALTH_URL)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('customer', resp['Location'])

    def test_staff_sees_health_page(self):
        self.client.force_login(self.staff)
        resp = self.client.get(HEALTH_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Database Health')
        self.assertContains(resp, 'Safe Cleanup')
        self.assertContains(resp, 'Growth forecast')
        # every safe category is listed
        for key in ('visitor_data', 'read_notifications', 'expired_otps',
                    'expired_sessions', 'expired_cache'):
            self.assertContains(resp, '"%s"' % key)

    def test_cleanup_rejects_get(self):
        self.client.force_login(self.staff)
        self.assertEqual(
            self.client.get(CLEAN_URL, {'category': 'expired_otps'}).status_code,
            405)

    def test_cleanup_requires_confirm(self):
        old = _mk_visitor(days_ago=120)
        self.client.force_login(self.staff)
        resp = self.client.post(CLEAN_URL, {'category': 'visitor_data'})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(VisitorSession.objects.filter(pk=old.pk).exists())

    def test_cleanup_unknown_category_deletes_nothing(self):
        old = _mk_visitor(days_ago=120)
        self.client.force_login(self.staff)
        resp = self.client.post(CLEAN_URL,
                                {'category': 'invoices_please',
                                 'confirm': 'DELETE'})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(VisitorSession.objects.filter(pk=old.pk).exists())


class CleanupVisitorDataTests(TestCase):
    def setUp(self):
        self.old = _mk_visitor(days_ago=120)
        self.recent = _mk_visitor(days_ago=10)
        PageView.objects.create(session=self.old, path='/')
        PageView.objects.create(session=self.recent, path='/repairs/')
        # Accounting truth that must never be touched by cleanup
        self.contact = Contact.objects.create(
            contact_type='customer', name='Keep Me', phone='9899000111')
        self.job = RepairJob.objects.create(
            customer=self.contact, device_model='Dell XPS',
            issue_description='No display', status='repairing')

    def test_old_sessions_and_their_pageviews_removed(self):
        deleted, label, unit = storage_views.run_cleanup('visitor_data')
        self.assertGreaterEqual(deleted, 2)  # session + cascaded pageview
        self.assertFalse(VisitorSession.objects.filter(pk=self.old.pk).exists())
        self.assertFalse(PageView.objects.filter(session=self.old).exists())

    def test_recent_data_and_books_kept(self):
        storage_views.run_cleanup('visitor_data')
        self.assertTrue(VisitorSession.objects.filter(pk=self.recent.pk).exists())
        self.assertTrue(PageView.objects.filter(session=self.recent).exists())
        self.assertTrue(Contact.objects.filter(pk=self.contact.pk).exists())
        self.assertTrue(RepairJob.objects.filter(pk=self.job.pk).exists())


class CleanupNotificationTests(TestCase):
    def setUp(self):
        self.recipient = User.objects.create_user(
            username='notif_recipient', password='TestPass123!')
        self.old_read = Notification.objects.create(
            recipient=self.recipient, title='Old read', message='m',
            is_read=True)
        self.old_unread = Notification.objects.create(
            recipient=self.recipient, title='Old unread', message='m',
            is_read=False)
        self.recent_read = Notification.objects.create(
            recipient=self.recipient, title='Fresh read', message='m',
            is_read=True)
        now = timezone.now()
        for n in (self.old_read, self.old_unread):
            Notification.all_objects.filter(pk=n.pk).update(
                created_at=now - timedelta(days=90))

    def test_only_old_read_hard_deleted(self):
        deleted, _label, _unit = storage_views.run_cleanup('read_notifications')
        self.assertEqual(deleted, 1)
        ids = set(Notification.all_objects.values_list('pk', flat=True))
        self.assertIn(self.old_unread.pk, ids)   # unread NEVER deleted
        self.assertIn(self.recent_read.pk, ids)  # < 60 days kept
        self.assertNotIn(self.old_read.pk, ids)  # gone, not soft-deleted


class CleanupOtpTests(TestCase):
    def setUp(self):
        now = timezone.now()
        self.expired = EmailOTP.objects.create(
            email='a@b.com', otp='123456', purpose='reset_password',
            expires_at=now - timedelta(hours=2))
        self.valid = EmailOTP.objects.create(
            email='a@b.com', otp='654321', purpose='reset_password',
            expires_at=now + timedelta(hours=2))

    def test_expired_gone_valid_kept(self):
        deleted, _label, _unit = storage_views.run_cleanup('expired_otps')
        self.assertEqual(deleted, 1)
        self.assertFalse(EmailOTP.objects.filter(pk=self.expired.pk).exists())
        self.assertTrue(EmailOTP.objects.filter(pk=self.valid.pk).exists())


class CleanupSessionTests(TestCase):
    def setUp(self):
        now = timezone.now()
        self.expired = DbSession.objects.create(
            session_key='a' + 'x' * 39,
            session_data='eA==', expire_date=now - timedelta(days=1))
        self.active = DbSession.objects.create(
            session_key='b' + 'y' * 39,
            session_data='eA==', expire_date=now + timedelta(days=7))

    def test_only_expired_sessions_deleted(self):
        deleted, _label, _unit = storage_views.run_cleanup('expired_sessions')
        self.assertEqual(deleted, 1)
        self.assertFalse(DbSession.objects.filter(pk=self.expired.pk).exists())
        self.assertTrue(DbSession.objects.filter(pk=self.active.pk).exists())


class CleanupCacheTests(TestCase):
    def setUp(self):
        # createcachetable is part of build.sh; make sure it exists here too
        call_command('createcachetable', verbosity=0)
        self.assertIn('django_cache_table', connection.introspection.table_names())

    def test_expired_rows_purged_live_rows_survive(self):
        cache.set('live_key', 'still here', 3600)
        cache.set('dead_key', 'expire me', 3600)
        past = (timezone.now() - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
        with connection.cursor() as c:
            c.execute('SELECT cache_key, expires FROM django_cache_table')
            rows = c.fetchall()
            c.execute('UPDATE django_cache_table SET expires = %s '
                      'WHERE cache_key = %s',
                      [past, cache.make_key('dead_key')])
            updated = c.rowcount
            c.execute('SELECT count(*) FROM django_cache_table '
                      'WHERE expires < %s', [past])
            notional = c.fetchone()[0]
        self.assertEqual(
            updated, 1,
            'UPDATE matched no row; keys stored were: %r, notional=%s'
            % (rows, notional))
        deleted, _label, _unit = storage_views.run_cleanup('expired_cache')
        self.assertGreaterEqual(deleted, 1)
        self.assertEqual(cache.get('live_key'), 'still here')
        self.assertIsNone(cache.get('dead_key'))


class WatchdogTests(TestCase):
    def setUp(self):
        call_command('createcachetable', verbosity=0)

    def test_record_snapshot_creates_row(self):
        snap = storage_views.record_snapshot()
        self.assertGreater(snap.db_bytes, 0)
        self.assertEqual(StorageSnapshot.objects.count(), 1)

    def test_record_snapshot_prunes_ancient_rows(self):
        old = StorageSnapshot.objects.create(db_bytes=1)
        StorageSnapshot.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=400))
        storage_views.record_snapshot()
        self.assertFalse(StorageSnapshot.objects.filter(pk=old.pk).exists())
        self.assertEqual(StorageSnapshot.objects.count(), 1)

    @override_settings(ADMIN_EMAIL='owner@example.com')
    def test_alert_at_threshold_and_throttled(self):
        big = int(storage_views.FREE_QUOTA_BYTES * 0.85)
        cache.delete(storage_views.ALERT_THROTTLE_KEY)
        with patch.object(storage_views, 'get_db_size_bytes', return_value=big):
            db_bytes, pct, sent = storage_views.check_quota_and_alert()
            self.assertTrue(sent)
            self.assertEqual(len(mail.outbox), 1)
            message = mail.outbox[0]
            self.assertIn('owner@example.com', message.to)
            self.assertIn('Database storage', message.subject)
            # second run inside throttle window sends nothing more
            _b, _p, sent2 = storage_views.check_quota_and_alert()
            self.assertFalse(sent2)
            self.assertEqual(len(mail.outbox), 1)

    def test_no_alert_below_threshold(self):
        small = int(storage_views.FREE_QUOTA_BYTES * 0.10)
        with patch.object(storage_views, 'get_db_size_bytes', return_value=small):
            _b, _p, sent = storage_views.check_quota_and_alert()
        self.assertFalse(sent)
        self.assertEqual(len(mail.outbox), 0)

    def test_emails_go_to_active_superusers_deduped(self):
        User.objects.create_superuser(
            username='root1', password='X1!', email='root1@example.com')
        User.objects.create_superuser(
            username='root2', password='X2!', email='root1@example.com')  # dup email
        inactive = User.objects.create_superuser(
            username='gone', password='X3!', email='gone@example.com')
        inactive.is_active = False
        inactive.save()
        big = int(storage_views.FREE_QUOTA_BYTES * 0.95)
        cache.delete(storage_views.ALERT_THROTTLE_KEY)
        with patch.object(storage_views, 'get_db_size_bytes', return_value=big):
            storage_views.send_quota_alert(pct=95.0, db_bytes=big)
        to = mail.outbox[0].to
        self.assertIn('root1@example.com', to)
        self.assertNotIn('gone@example.com', to)
        self.assertEqual(len(to), len(set(to)))

    def test_smtp_outage_does_not_break_heartbeat(self):
        big = int(storage_views.FREE_QUOTA_BYTES * 0.9)
        cache.delete(storage_views.ALERT_THROTTLE_KEY)
        with override_settings(ADMIN_EMAIL='owner@example.com'), \
                patch.object(storage_views, 'get_db_size_bytes', return_value=big), \
                patch('django.core.mail.send_mail',
                      side_effect=ConnectionError('smtp down')):
            db_bytes, pct, sent = storage_views.check_quota_and_alert()
        self.assertFalse(sent)  # no crash, no false claim

    def test_watchdog_command_runs(self):
        out = io.StringIO()
        call_command('storage_watchdog', stdout=out)
        text = out.getvalue()
        self.assertIn('storage_watchdog', text)
        self.assertEqual(StorageSnapshot.objects.count(), 1)


class KeepaliveViewTests(TestCase):
    """Public /keepalive/ — free replacement for Render Cron Jobs."""

    URL = '/keepalive/'

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_anonymous_get_runs_full_watchdog(self):
        r = self.client.get(self.URL)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'watchdog ok', r.content)
        self.assertEqual(StorageSnapshot.objects.count(), 1)

    def test_second_hit_within_20h_is_throttled(self):
        self.client.get(self.URL)
        r = self.client.get(self.URL)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'already ran', r.content)
        self.assertEqual(StorageSnapshot.objects.count(), 1)

    def test_post_not_allowed(self):
        self.assertEqual(self.client.post(self.URL).status_code, 405)


class FormatTests(TestCase):
    def test_format_bytes_units(self):
        self.assertEqual(storage_views.format_bytes(512), '512 B')
        self.assertEqual(storage_views.format_bytes(2048), '2.0 KB')
        self.assertEqual(storage_views.format_bytes(500 * 1024 * 1024),
                         '500.0 MB')

    def test_run_cleanup_unknown_raises(self):
        with self.assertRaises(ValueError):
            storage_views.run_cleanup('ledger_lines')
