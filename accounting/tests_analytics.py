# accounting/tests_analytics.py
"""Tests for the built-in visitor analytics (middleware + ping + staff page)."""

import uuid
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.utils import timezone

from .models import VisitorSession, PageView
from .analytics_middleware import VISITOR_COOKIE, parse_user_agent

REAL_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
           '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
MOBILE_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
             'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1')
BOT_UA = 'Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GoogleBot/1.0)'


class AnalyticsRecordingTests(TestCase):
    def setUp(self):
        self.client = Client()

    def _get(self, path='/', **extra):
        extra.setdefault('HTTP_USER_AGENT', REAL_UA)
        return self.client.get(path, **extra)

    def test_page_view_recorded_and_cookie_set(self):
        resp = self._get('/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(VisitorSession.objects.count(), 1)
        self.assertEqual(PageView.objects.count(), 1)
        session = VisitorSession.objects.get()
        self.assertEqual(session.entry_path, '/')
        self.assertEqual(session.device_type, 'desktop')
        self.assertEqual(session.browser, 'Chrome')
        self.assertIn(VISITOR_COOKIE, resp.cookies)

    def test_same_visitor_reuses_session_within_gap(self):
        self._get('/')
        self._get('/privacy-policy/')
        self.assertEqual(VisitorSession.objects.count(), 1)
        self.assertEqual(PageView.objects.count(), 2)
        session = VisitorSession.objects.get()
        self.assertEqual(session.exit_path, '/privacy-policy/')
        self.assertEqual(session.pageviews.count(), 2)

    def test_new_session_after_gap(self):
        self._get('/')
        session = VisitorSession.objects.get()
        VisitorSession.objects.filter(pk=session.pk).update(
            last_seen_at=timezone.now() - timedelta(minutes=31))
        self._get('/')
        self.assertEqual(VisitorSession.objects.count(), 2)

    def test_bot_user_agent_not_recorded(self):
        self._get('/', HTTP_USER_AGENT=BOT_UA)
        self.assertEqual(VisitorSession.objects.count(), 0)

    def test_post_not_recorded(self):
        self.client.post('/contact-message/',
                         HTTP_USER_AGENT=REAL_UA)
        self.assertEqual(VisitorSession.objects.count(), 0)

    def test_htmx_fragment_not_recorded(self):
        self.client.force_login(User.objects.create_user(
            username='htmx_staff', password='X1!', is_staff=True))
        self.client.get('/dashboard/refresh-stats/',
                        HTTP_USER_AGENT=REAL_UA, HTTP_HX_REQUEST='true')
        self.assertEqual(VisitorSession.objects.count(), 0)

    def test_static_and_ping_paths_skipped(self):
        self._get('/analytics/ping/')
        self.assertEqual(VisitorSession.objects.count(), 0)

    def test_mobile_ua_parsed(self):
        self._get('/', HTTP_USER_AGENT=MOBILE_UA)
        session = VisitorSession.objects.get()
        self.assertEqual(session.device_type, 'mobile')
        self.assertEqual(session.os_name, 'iOS')

    def test_utm_source_captured(self):
        self._get('/?utm_source=whatsapp')
        self.assertEqual(VisitorSession.objects.get().utm_source, 'whatsapp')


class VisitorPingTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.get('/', HTTP_USER_AGENT=REAL_UA)
        self.session = VisitorSession.objects.get()

    def _ping(self, referer='http://testserver'):
        return self.client.post('/analytics/ping/',
                                HTTP_REFERER=referer,
                                HTTP_USER_AGENT=REAL_UA)

    def test_ping_updates_last_seen(self):
        VisitorSession.objects.filter(pk=self.session.pk).update(
            last_seen_at=timezone.now() - timedelta(minutes=10))
        resp = self._ping()
        self.assertEqual(resp.status_code, 200)
        self.session.refresh_from_db()
        self.assertLess(
            (timezone.now() - self.session.last_seen_at).total_seconds(), 60)

    def test_ping_rejects_cross_origin(self):
        VisitorSession.objects.filter(pk=self.session.pk).update(
            last_seen_at=timezone.now() - timedelta(minutes=10))
        resp = self._ping(referer='https://evil.example.com')
        self.assertEqual(resp.status_code, 403)
        self.session.refresh_from_db()
        self.assertGreater(
            (timezone.now() - self.session.last_seen_at).total_seconds(), 300)

    def test_ping_get_not_allowed(self):
        self.assertEqual(self.client.get('/analytics/ping/').status_code, 405)

    def test_ping_without_session_404(self):
        VisitorSession.objects.all().delete()
        self.assertEqual(self._ping().status_code, 404)


class VisitorAnalyticsPageTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_user(
            username='analytics_staff', password='TestPass123!',
            is_staff=True, is_active=True)
        self.customer = User.objects.create_user(
            username='analytics_cust', password='TestPass123!',
            is_active=True)

    def test_anonymous_redirected_to_login(self):
        resp = self.client.get('/analytics/visitors/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('login', resp['Location'])

    def test_non_staff_blocked(self):
        self.client.force_login(self.customer)
        resp = self.client.get('/analytics/visitors/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('customer', resp['Location'])

    def test_staff_sees_data(self):
        self.client.get('/', HTTP_USER_AGENT=REAL_UA)
        self.client.get('/privacy-policy/', HTTP_USER_AGENT=REAL_UA)
        self.client.force_login(self.staff)
        resp = self.client.get('/analytics/visitors/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Website Visitors')
        self.assertContains(resp, 'People on the site right now')
        session = resp.context['session_rows'][0]['s']
        self.assertEqual(session.num_pages, 2)
        self.assertContains(resp, 'How to read the table below')
        self.assertContains(resp, 'Website home')

    def test_days_filter_param(self):
        self.client.force_login(self.staff)
        resp = self.client.get('/analytics/visitors/?days=7')
        self.assertEqual(resp.context['days'], 7)
        resp = self.client.get('/analytics/visitors/?days=bogus')
        self.assertEqual(resp.context['days'], 30)


class SourceDisplayTests(TestCase):
    def _session(self, **kw):
        defaults = dict(visitor_id=uuid.uuid4(),
                        last_seen_at=timezone.now(), entry_path='/')
        defaults.update(kw)
        return VisitorSession.objects.create(**defaults)

    def test_google_referrer(self):
        s = self._session(referrer='https://www.google.com/search?q=repair')
        self.assertEqual(s.source_display, 'Google Search')

    def test_utm_whatsapp(self):
        s = self._session(utm_source='whatsapp')
        self.assertEqual(s.source_display, 'WhatsApp')

    def test_direct(self):
        s = self._session()
        self.assertEqual(s.source_display, 'Typed the address / direct')

    def test_friendly_path(self):
        from .views.analytics import friendly_path
        self.assertEqual(friendly_path('/'), 'Website home')
        self.assertEqual(friendly_path('/privacy-policy/'), 'Privacy Policy')
        self.assertEqual(friendly_path('/blog/my-first-post/'),
                         'Blog › My First Post')


class UserAgentParserTests(TestCase):
    def test_bots_return_none(self):
        for ua in ['', BOT_UA, 'curl/8.0', 'python-requests/2.31']:
            self.assertIsNone(parse_user_agent(ua))

    def test_desktop_firefox(self):
        result = parse_user_agent(
            'Mozilla/5.0 (Windows NT 10.0; rv:121.0) Gecko/20100101 Firefox/121.0')
        self.assertEqual(result, ('desktop', 'Firefox', 'Windows'))

    def test_edge(self):
        result = parse_user_agent(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0')
        self.assertEqual(result[1], 'Edge')

    def test_android_tablet(self):
        result = parse_user_agent(
            'Mozilla/5.0 (Linux; Android 13; SM-T733) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36')
        self.assertEqual(result[0], 'tablet')
        self.assertEqual(result[2], 'Android')
