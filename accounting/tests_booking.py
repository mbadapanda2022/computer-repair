"""Public repair booking (/book-repair/) + allauth signup closure — tests.

Run:  python manage.py test accounting.tests_booking -v 2
"""
import re

from django.contrib.auth.models import User
from django.core.cache import cache
from django.http import Http404, HttpRequest
from django.test import TestCase
from django.urls import reverse

from computer_repair.urls import _signup_closed
from .models import Contact, Notification, RepairJob


def _post(client, **over):
    data = {'name': 'Test Buyer', 'phone': '9876500011',
            'device_model': 'HP Pavilion', 'serial_number': 'SN-1',
            'issue_description': 'Laptop heating and random shutdown issue',
            'website': ''}
    data.update(over)
    return client.post(reverse('book_repair'), data)


class BookRepairViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            'book_staff', password='x', is_staff=True)

    def setUp(self):
        cache.clear()

    def test_anonymous_can_open_form(self):
        r = self.client.get(reverse('book_repair'))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Book a Repair')

    def test_booking_creates_contact_job_and_notification(self):
        r = _post(self.client)
        self.assertEqual(r.status_code, 200)
        job = RepairJob.objects.get()
        self.assertEqual(job.status, 'pending')
        self.assertTrue(job.job_number.startswith('REP-'))
        self.assertTrue(
            Contact.objects.filter(phone='9876500011',
                                   contact_type='customer').exists())
        self.assertContains(r, job.job_number)
        self.assertEqual(
            Notification.objects.filter(recipient=self.staff).count(), 1)

    def test_tracking_link_from_booking_opens_anonymously(self):
        r = _post(self.client)
        m = re.search(r'href="(http://testserver/tracking/[^"]+)"',
                      r.content.decode())
        self.assertIsNotNone(m, 'tracking link missing on thanks page')
        self.assertEqual(self.client.get(m.group(1)).status_code, 200)

    def test_existing_phone_reused_no_duplicate_contact(self):
        Contact.objects.create(name='Old Name', phone='9876500011',
                               contact_type='customer')
        _post(self.client)
        self.assertEqual(Contact.objects.filter(phone='9876500011').count(), 1)
        self.assertEqual(RepairJob.objects.count(), 1)

    def test_honeypot_drops_silently(self):
        r = _post(self.client, website='http://spam.example')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(RepairJob.objects.count(), 0)
        self.assertContains(r, 'Thank you')

    def test_invalid_phone_rejected(self):
        r = _post(self.client, phone='12345')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(RepairJob.objects.count(), 0)
        self.assertContains(r, 'valid 10-digit')

    def test_phone_normalization_plus91(self):
        r = _post(self.client, phone='+91 98765-00011')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(Contact.objects.filter(phone='9876500011').exists())

    def test_throttle_after_five_per_hour(self):
        for i in range(5):
            r = _post(self.client, phone=f'980000{i:04d}', name=f'B{i}')
            self.assertEqual(r.status_code, 200, f'post {i} failed')
        r = _post(self.client, phone='9811100000', name='Sixth')
        self.assertEqual(r.status_code, 429)
        self.assertEqual(RepairJob.objects.count(), 5)


class AllauthSignupClosedTests(TestCase):
    def test_accounts_signup_never_renders_form(self):
        # Shadowed by _signup_closed 404 route + AccessControl redirect —
        # the bina-OTP allauth signup must never render an account form.
        r = self.client.get('/accounts/signup/')
        self.assertEqual(r.status_code, 302)
        final = self.client.get('/accounts/signup/', follow=True)
        self.assertNotContains(final, 'password2')
        self.assertEqual(User.objects.count(), 0)

    def test_signup_route_gives_404_when_middleware_bypassed(self):
        # Path-authenticated probe: resolve+call the view directly.
        with self.assertRaises(Http404):
            _signup_closed(HttpRequest())

    def test_custom_register_flow_still_open(self):
        self.assertEqual(self.client.get('/register/').status_code, 200)
