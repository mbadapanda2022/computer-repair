# accounting/tests_demo_trial.py
"""Trial engine + demo-mode guards. Production must never be affected."""

import tempfile
import time
from datetime import timedelta
from pathlib import Path

from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from .demo_trial import get_trial_state, TRIAL_CONTACT


def _demo_settings(dirpath, **over):
    base = dict(
        DEMO_MODE=True,
        DEMO_DATA_DIR=dirpath,
        DEMO_TRIAL_DAYS=180,
        DEMO_TRIAL_WARN_DAYS=30,
        DEMO_TRIAL_HARD_DEADLINE='',
    )
    base.update(over)
    return override_settings(**base)


class TrialStateTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        from django.core.management import call_command
        call_command('createcachetable')
        cache.clear()
        self.tmp = tempfile.mkdtemp()
        self.ctx = _demo_settings(self.tmp)
        self.ctx.enable()
        self.addCleanup(self.ctx.disable)

    def test_fresh_install_gets_full_window(self):
        state = get_trial_state()
        self.assertFalse(state['expired'])
        self.assertFalse(state['warn'])
        self.assertEqual(state['days_left'], 180)
        self.assertTrue(Path(self.tmp, 'trial_state.json').exists())

    def test_warning_inside_last_30_days(self):
        get_trial_state()  # stamp
        now = timezone.now() + timedelta(days=151)
        state = get_trial_state(now=now)
        self.assertTrue(state['warn'])
        self.assertFalse(state['expired'])
        self.assertEqual(state['days_left'], 29)

    def test_expired_after_six_months(self):
        get_trial_state()
        now = timezone.now() + timedelta(days=181)
        state = get_trial_state(now=now)
        self.assertTrue(state['expired'])

    def test_clock_rollback_cannot_extend(self):
        get_trial_state()  # stamps file + DB cache
        from django.core.cache import cache
        old_ts = time.time() - 90 * 86400
        # DB keeps the original honest stamp (90 days ago)...
        cache.set('demo_trial_install_ts', old_ts, None)
        # ...a tamperer rewrites only the JSON file to "now".
        p = Path(self.tmp, 'trial_state.json')
        p.write_text(__import__('json').dumps({'install_ts': time.time()}))
        state = get_trial_state()
        self.assertLessEqual(abs(state['install_ts'] - old_ts), 2)

    def test_hard_deadline_expires_old_build(self):
        deadline = (timezone.now() - timedelta(days=1)).isoformat()
        with _demo_settings(self.tmp, DEMO_TRIAL_HARD_DEADLINE=deadline):
            state = get_trial_state()
        self.assertTrue(state['expired'])
        self.assertEqual(state['days_left'], 0)


class TrialMiddlewareTests(TestCase):
    def setUp(self):
        self.rf = RequestFactory()
        import tempfile as _tf
        self.tmp = _tf.mkdtemp()
        self.ctx = _demo_settings(self.tmp)
        self.ctx.enable()
        self.addCleanup(self.ctx.disable)

    def _banner_mw(self, state):
        from .middleware import DemoTrialMiddleware
        mw = DemoTrialMiddleware(
            lambda r: self._html_response('<html><body>page</body></html>'))
        DemoTrialMiddleware._memo = state
        DemoTrialMiddleware._memo_at = time.time()
        return mw

    @staticmethod
    def _html_response(body):
        from django.http import HttpResponse
        return HttpResponse(body, content_type='text/html')

    def test_noop_in_production_mode(self):
        from .middleware import DemoTrialMiddleware
        called = {}

        def get_response(request):
            called['yes'] = True
            return self._html_response('<html><body>ok</body></html>')

        mw = DemoTrialMiddleware(get_response)
        with override_settings(DEMO_MODE=False):
            resp = mw(self.rf.get('/'))
        self.assertTrue(called.get('yes'))
        self.assertIn(b'ok', resp.content)
        self.assertNotIn(b'demo-trial-banner', resp.content)

    def test_banner_injected_during_warning_window(self):
        state = {'expired': False, 'warn': True, 'days_left': 12,
                 'expiry_display': '01 Jan 2027'}
        mw = self._banner_mw(state)
        resp = mw(self.rf.get('/dashboard/'))
        html = resp.content.decode()
        self.assertIn('demo-trial-banner', html)
        self.assertIn('12 days left', html)
        self.assertIn(TRIAL_CONTACT['whatsapp'], html)
        self.assertTrue(html.rstrip().endswith('</html>'))

    def test_static_paths_pass_through(self):
        mw = self._banner_mw({'expired': True, 'warn': False,
                              'days_left': 0})
        resp = mw(self.rf.get('/static/css/custom.css'))
        self.assertIn(b'page', resp.content)

    def test_expired_renders_contact_page(self):
        state = {'expired': True, 'warn': False, 'days_left': 0,
                 'expiry_display': '20 Oct 2026'}
        mw = self._banner_mw(state)
        resp = mw(self.rf.get('/repairs/'))
        html = resp.content.decode()
        self.assertIn('Trial Has Ended', html)
        self.assertIn(TRIAL_CONTACT['whatsapp'], html)
        self.assertIn(TRIAL_CONTACT['phone'], html)


class GuideViewTests(TestCase):
    def test_guide_404_outside_demo_mode(self):
        resp = self.client.get('/demo-guide/')
        self.assertEqual(resp.status_code, 404)

    def test_guide_renders_in_demo_mode(self):
        from django.core.cache import cache
        from django.core.management import call_command
        call_command('createcachetable')
        cache.clear()
        tmp = tempfile.mkdtemp()
        ctx = _demo_settings(tmp)
        ctx.enable()
        self.addCleanup(ctx.disable)
        from .middleware import DemoTrialMiddleware
        DemoTrialMiddleware._memo = None
        resp = self.client.get('/demo-guide/')
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn(TRIAL_CONTACT['phone'], html)
