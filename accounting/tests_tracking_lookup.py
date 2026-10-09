"""
Public repair tracking lookup (landing page widget) — tests.

Chalane ka tareeka:
    python manage.py test accounting.tests_tracking_lookup -v 2

Customer job number + registered mobile number daal ke apna repair track karta
hai — registered ho ya staff-created contact, dono case cover hote hain.
"""
from __future__ import annotations

from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from accounting.models import Contact, RepairJob
from accounting.utils.tracking import verify_tracking_token


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class RepairLookupTests(TestCase):

    URL = '/tracking/lookup/'

    def setUp(self):
        self.client = Client()
        suffix = timezone.now().strftime('%H%M%S%f')[:8]
        self.phone = '94' + suffix
        self.customer = Contact.objects.create(
            contact_type='customer', name=f'Track Cust {suffix}', phone=self.phone,
        )
        self.job = RepairJob.objects.create(
            customer=self.customer, device_model='Dell Test',
            issue_description='No display', status='repairing',
        )

    def lookup(self, job_number, phone):
        return self.client.post(self.URL, {'job_number': job_number, 'phone': phone})

    def params(self, response):
        return parse_qs(urlparse(response.url).query)

    def test_valid_lookup_returns_tracking_token(self):
        r = self.lookup(self.job.job_number, self.phone)
        self.assertEqual(r.status_code, 302)
        self.assertIn('#track-repair', r.url)
        token = self.params(r)['token'][0]
        self.assertEqual(verify_tracking_token(token), self.job.pk)
        # token actually opens the tracking page
        page = self.client.get('/tracking/repair/%s/' % token)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, self.job.job_number)

    def test_lowercase_job_number_works(self):
        r = self.lookup(self.job.job_number.lower(), self.phone)
        self.assertEqual(r.status_code, 302)
        self.assertIn('token', self.params(r))

    def test_digits_only_job_number_works(self):
        # customer may remember only the numeric part (e.g. 0001)
        numeric = self.job.job_number.split('-')[-1]
        r = self.lookup(numeric, self.phone)
        self.assertEqual(r.status_code, 302)
        self.assertIn('token', self.params(r))

    def test_phone_with_91_prefix_works(self):
        r = self.lookup(self.job.job_number, '91' + self.phone)
        self.assertEqual(r.status_code, 302)
        self.assertIn('token', self.params(r))

    def test_wrong_phone_rejected(self):
        other = '0' * 10 if self.phone != '0' * 10 else '1' * 10
        r = self.lookup(self.job.job_number, other)
        self.assertEqual(r.status_code, 302)
        p = self.params(r)
        self.assertIn('err', p)
        self.assertNotIn('token', p)

    def test_wrong_job_number_rejected(self):
        r = self.lookup('REP-999999', self.phone)
        self.assertEqual(r.status_code, 302)
        self.assertIn('err', self.params(r))

    def test_invalid_inputs_rejected(self):
        for job, phone in [('REP', '123'), ('', self.phone), (self.job.job_number, 'abcdefghij')]:
            r = self.lookup(job, phone)
            self.assertEqual(r.status_code, 302)
            p = self.params(r)
            self.assertIn('err', p)
            self.assertNotIn('token', p)

    def test_get_not_allowed(self):
        r = self.client.get(self.URL)
        self.assertEqual(r.status_code, 405)

    def test_rate_limit(self):
        for _ in range(8):
            self.lookup('REP-999999', self.phone)
        r = self.lookup(self.job.job_number, self.phone)
        self.assertEqual(r.status_code, 302)
        self.assertIn('Too many attempts', self.params(r).get('err', [''])[0])


class TrackingShareMessageTests(TestCase):
    """WhatsApp share bhejta hai info + link, sirf nanga link nahi."""

    def setUp(self):
        self.customer = Contact.objects.create(
            contact_type='customer', name='Share Test', phone='9876543210',
        )
        self.job = RepairJob.objects.create(
            customer=self.customer, device_model='Dell Inspiron',
            issue_description='Not powering on', status='repairing',
        )

    def test_message_contains_job_info_and_link(self):
        url, text = self.job.tracking_share_content('http://example.com/track/abc/')
        for expected in [self.job.job_number, 'Dell Inspiron', 'Under Repair',
                         'Share Test', 'http://example.com/track/abc/']:
            self.assertIn(expected, text)
        self.assertTrue(url.startswith('https://wa.me/919876543210?text='))

    def test_ready_job_includes_amount(self):
        self.job.status = 'ready'
        self.job.final_amount = Decimal('2450.00')
        _, text = self.job.tracking_share_content('http://example.com/track/abc/')
        self.assertIn('Rs.2450.00', text)

    def test_estimate_included_when_no_final_amount(self):
        self.job.estimated_cost = Decimal('1500.00')
        self.job.estimate_status = 'approved'
        _, text = self.job.tracking_share_content('http://x/')
        self.assertIn('Estimated Cost: Rs.1500.00 (Approved)', text)
