# accounting/tests_branding.py
"""Handover-readiness: no company-specific hardcode may survive anywhere.

A new owner must only fill CompanyProfile in Staff Settings and the whole
site (pages, emails, WhatsApp messages, admin, alerts) rebrands. The
developer credit footer is the ONE permanent exception."""

from decimal import Decimal
from pathlib import Path

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase, override_settings

from .models import (
    CompanyProfile, Contact, Invoice, Payment, company_message_name,
)

# Split so this file's own literals cannot trip the source scan.
FORBIDDEN = ['A1 ' + 'Computer', 'a1computers' + 'olutions']

BRAND = 'Test Traders Pvt'


def _company(**kw):
    CompanyProfile.objects.all().delete()
    base = dict(
        name=BRAND,
        address='Test Complex, Bhubaneswar-751000',
        phone='9876543210',
        email='shop@test.example',
        city='Bhubaneswar',
        state='Odisha',
        website_url='https://test.example',
        whatsapp_number='919876543210',
    )
    base.update(kw)
    return CompanyProfile.objects.create(**base)


class SourceScanTests(TestCase):
    """No brand literal may remain in shipped templates/static/code."""

    def _files(self):
        root = Path(__file__).resolve().parents[1]
        roots = [(root / 'templates', {'.html'}),
                 (root / 'static' / 'css', {'.css'}),
                 (root / 'static' / 'js', {'.js'})]
        for d, exts in roots:
            for p in d.rglob('*'):
                if p.suffix in exts:
                    yield p
        acc = root / 'accounting'
        for p in acc.rglob('*.py'):
            if 'migrations' in p.parts or p.name.startswith('tests_'):
                continue
            yield p

    def test_no_hardcoded_brand_in_source(self):
        hits = []
        for path in self._files():
            try:
                text = path.read_text(encoding='utf-8', errors='ignore').lower()
            except OSError:
                continue
            for needle in FORBIDDEN:
                if needle.lower() in text:
                    hits.append(f'{path}: {needle!r}')
        self.assertEqual(hits, [], 'Hardcoded brand found:\n' + '\n'.join(hits))


class PublicPageBrandingTests(TestCase):
    def setUp(self):
        _company()

    def _assert_branched(self, url, extra=None):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200, url)
        html = resp.content.decode()
        self.assertIn(BRAND, html, url)
        for needle in FORBIDDEN:
            self.assertNotIn(needle.lower(), html.lower(), url)
        if extra:
            for token in extra:
                self.assertIn(token, html, f'{url} missing {token}')
        return html

    def test_landing(self):
        # float widget prefills the company name into the WhatsApp text
        self._assert_branched('/', extra=['Hello%20Test%20Traders%20Pvt'])

    def test_legal_pages(self):
        self._assert_branched('/privacy-policy/', extra=[
            'tel:9876543210', 'wa.me/919876543210', 'mailto:shop@test.example'])
        self._assert_branched('/terms-of-service/')
        self._assert_branched('/return-policy/')
        self._assert_branched('/cookie-policy/')

    def test_login_page_title(self):
        self._assert_branched('/login/')

    def test_landing_eyebrow_uses_city(self):
        html = self.client.get('/').content.decode()
        self.assertIn('Bhubaneswar', html)


class StaffPageBrandingTests(TestCase):
    def setUp(self):
        _company()
        self.staff = User.objects.create_user(
            'brand_staff', password='TestPass123!', is_staff=True)
        self.client = Client()
        self.client.force_login(self.staff)

    def test_db_health_page_branched(self):
        resp = self.client.get('/db-health/')
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn(BRAND, html)
        for needle in FORBIDDEN:
            self.assertNotIn(needle.lower(), html.lower())

    def test_quota_alert_subject_uses_brand(self):
        from .views.storage import send_quota_alert
        User.objects.create_superuser(
            'brand_root', email='root@test.example', password='x' * 10)
        sent = send_quota_alert(91.0, 480 * 1024 * 1024)
        self.assertTrue(sent)
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(mail.outbox[0].subject.startswith(f'[{BRAND}]'))
        self.assertIn(BRAND, mail.outbox[0].body)


class OtpEmailBrandingTests(TestCase):
    def setUp(self):
        _company()
        self.user = User.objects.create_user(
            'brand_user', email='u@test.example', password='x' * 10,
            first_name='User')

    def test_otp_html_has_brand_and_site_link(self):
        from .utils.otp_helpers import send_otp_email
        ok = send_otp_email(self.user, 'u@test.example', '123456',
                            'reset_password')
        self.assertTrue(ok)
        html = mail.outbox[0].alternatives[0][0]
        self.assertIn(BRAND, html)
        self.assertIn('https://test.example/verify-otp/', html)
        self.assertIn('shop@test.example', html)
        for needle in FORBIDDEN:
            self.assertNotIn(needle.lower(), html.lower())

    def test_no_website_url_still_renders(self):
        cp = CompanyProfile.objects.first()
        cp.website_url = ''
        cp.save()
        from .utils.otp_helpers import send_otp_email
        self.assertTrue(send_otp_email(self.user, 'u@test.example',
                                       '654321', 'reset_password'))


class NotificationEmailBrandingTests(TestCase):
    def test_subject_prefixed_with_company(self):
        _company()
        u = User.objects.create_user(
            'notif_brand', email='n@test.example', password='x' * 10)
        from .utils.notification_helpers import send_notification
        send_notification(u, title='Stock low', message='Printer ink low',
                          send_email=True)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, f'[{BRAND}] Stock low')
        self.assertIn(BRAND, mail.outbox[0].alternatives[0][0])


class WhatsappMessageBrandingTests(TestCase):
    def setUp(self):
        _company()
        self.customer = Contact.objects.create(
            contact_type='customer', name='Buyer Das', phone='9437000001')

    def test_invoice_share_names_company(self):
        inv = Invoice.objects.create(
            invoice_number='BR-INV-1', customer=self.customer,
            subtotal=Decimal('100.00'), grand_total=Decimal('100.00'),
            balance_due=Decimal('100.00'))
        from urllib.parse import unquote
        text = unquote(inv.whatsapp_share_url)
        self.assertIn(BRAND, text)
        for needle in FORBIDDEN:
            self.assertNotIn(needle.lower(), text.lower())

    def test_payment_share_names_company(self):
        pay = Payment.objects.create(
            direction='received', contact=self.customer,
            amount=Decimal('100.00'))
        from urllib.parse import unquote
        text = unquote(pay.whatsapp_share_url)
        self.assertIn(BRAND, text)

    def test_message_name_fallback_without_profile(self):
        CompanyProfile.objects.all().delete()
        self.assertEqual(company_message_name(), 'our store')


class FixUrlTests(TestCase):
    def test_repairs_single_slash_and_is_idempotent(self):
        from .context_processors import fix_url
        good = 'https://res.cloudinary.com/x/y.png'
        self.assertEqual(fix_url(good), good)          # must NOT become https:///
        self.assertEqual(fix_url('https:/x.co/a'), 'https://x.co/a')
        self.assertEqual(fix_url('https://x.co/a'), 'https://x.co/a')
        self.assertEqual(fix_url('http://x.co/a'), 'http://x.co/a')
        self.assertEqual(fix_url(''), '')
        self.assertIsNone(fix_url(None))


class FromEmailTests(TestCase):
    """Sender must come from env when set, else from Staff Settings email."""

    @override_settings(DEFAULT_FROM_EMAIL='')
    def test_falls_back_to_company_email(self):
        _company(email='owner@test.example')
        from .utils.company_email import get_from_email
        self.assertEqual(get_from_email(), 'owner@test.example')

    @override_settings(DEFAULT_FROM_EMAIL='env@test.example')
    def test_env_configured_sender_wins(self):
        _company(email='owner@test.example')
        from .utils.company_email import get_from_email
        self.assertEqual(get_from_email(), 'env@test.example')


class CompanyProfileFormTests(TestCase):
    def test_city_and_website_url_savable(self):
        from .forms import CompanyProfileForm
        profile = _company(name='Old Name')
        form = CompanyProfileForm(instance=profile)
        data = {k: v for k, v in form.initial.items() if v is not None}
        data.update({'name': 'New Real Owners Shop', 'city': 'Cuttack',
                     'website_url': 'https://newowner.example'})
        bound = CompanyProfileForm(data, instance=profile)
        self.assertTrue(bound.is_valid(), bound.errors)
        bound.save()
        profile.refresh_from_db()
        self.assertEqual(profile.city, 'Cuttack')
        self.assertEqual(profile.website_url, 'https://newowner.example')
