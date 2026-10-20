# accounting/tests_login_backend.py
"""Regression tests for login failures with correct credentials.

Bugs fixed in EmailOrPhoneBackend + unified_login_view:
  - User.objects.get(email__iexact=...) raised MultipleObjectsReturned on
    duplicate emails and the login failed despite a correct password.
  - A Contact row matching the phone but having no portal account shadowed
    authentication (.first() picked it blindly).
  - The view called authenticate() a second time after AuthenticationForm
    had already authenticated — two lookups could disagree, showing
    "Please enter a correct username and password" inconsistently.
"""

from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.test import Client, TestCase

from .models import Contact


class PhoneLoginTests(TestCase):
    PASSWORD = 'GoodPass#2026'

    def setUp(self):
        self.phone = '9812345678'
        self.user = User.objects.create_user(
            'phone_cust', password=self.PASSWORD, is_active=True)
        self.contact = Contact.objects.create(
            contact_type='customer', name='Linked Row',
            phone=self.phone, user=self.user)

    def _other_phone_contact(self):
        # A contact row for the same number WITHOUT a portal account is
        # impossible (phone is unique) — but an unlinked contact must never
        # make the linked lookup crash or shadow it.
        Contact.objects.filter(pk=self.contact.pk).update(user=None)

    def test_backend_finds_user_via_phone(self):
        got = authenticate(None, username=self.phone, password=self.PASSWORD)
        self.assertEqual(got, self.user)

    def test_backend_accepts_plus_prefixed_input(self):
        # Contact.save() normalises stored phones to 10 digits; user input
        # like +91… must still authenticate.
        got = authenticate(None, username=f'+91{self.phone}',
                           password=self.PASSWORD)
        self.assertEqual(got, self.user)

    def test_unlinked_contact_does_not_crash(self):
        self._other_phone_contact()
        self.assertIsNone(
            authenticate(None, username=self.phone, password=self.PASSWORD))

    def test_backend_wrong_password_still_fails(self):
        self.assertIsNone(
            authenticate(None, username=self.phone, password='nope'))

    def test_login_view_succeeds(self):
        client = Client()
        resp = client.post('/login/', {
            'username': self.phone, 'password': self.PASSWORD,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/customer/', resp['Location'])


class EmailLoginDuplicateRowsTests(TestCase):
    def test_password_decides_between_two_accounts_sharing_email(self):
        u1 = User.objects.create_user('alice1', password='First#2026',
                                      email='shared@example.com')
        u2 = User.objects.create_user('alice2', password='Second#2026',
                                      email='shared@example.com')
        # Old code: User.objects.get(email__iexact=...) raised
        # MultipleObjectsReturned and the login failed with correct creds.
        got = authenticate(None, username='Shared@Example.com',
                           password='Second#2026')
        self.assertEqual(got, u2)


class StaffLoginStillWorksTests(TestCase):
    def test_staff_username_password(self):
        staff = User.objects.create_user(
            'qa_staff_x', password='Staff#2026x',
            is_staff=True, is_active=True)
        client = Client()
        resp = client.post('/login/', {
            'username': 'qa_staff_x', 'password': 'Staff#2026x',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/dashboard/', resp['Location'])

    def test_wrong_password_no_login(self):
        User.objects.create_user('qa_staff_y', password='Staff#2026y',
                                 is_staff=True, is_active=True)
        client = Client()
        resp = client.post('/login/', {
            'username': 'qa_staff_y', 'password': 'wrong',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('sessionid', client.cookies)


class FriendlyLoginErrorsTests(TestCase):
    """No more generic 'enter a correct username and password' —
    the page must say exactly what went wrong."""

    def test_wrong_password_says_password_mismatch(self):
        User.objects.create_user('friendly_user', password='Right#2026',
                                 is_active=True)
        resp = Client().post('/login/', {
            'username': 'friendly_user', 'password': 'nope',
        })
        self.assertContains(resp, 'The password does not match')

    def test_unknown_account_says_not_found(self):
        resp = Client().post('/login/', {
            'username': '9998887776', 'password': 'whatever1',
        })
        self.assertContains(resp, 'could not find an account')


class UsernameCaseInsensitiveTests(TestCase):
    def test_username_casing_does_not_break_login(self):
        User.objects.create_user('ShopAdmin', password='Case#2026',
                                 is_staff=True, is_active=True)
        got = authenticate(None, username='shopadmin', password='Case#2026')
        self.assertIsNotNone(got)
        self.assertEqual(got.username, 'ShopAdmin')


class HtmxQueueIsolationTests(TestCase):
    """The username field's live-validation request must NOT share the
    form's hx-sync queue: htmx inherits hx-sync from ancestors, so a
    delayed validation request (keyup delay:500ms) firing after the login
    POST started would ABORT the in-flight login — intermittent 'correct
    password rejected' for fast clickers."""

    def test_validation_input_has_own_sync_queue(self):
        html = Client().get('/login/').content.decode()
        # The form keeps its double-submit guard...
        self.assertIn('hx-sync="this:abort"', html)
        # ...but the validation input drops onto its own queue.
        input_tag = html[html.index('id_username'):html.index('username-errors')]
        self.assertIn('hx-sync="this:drop"', input_tag)
