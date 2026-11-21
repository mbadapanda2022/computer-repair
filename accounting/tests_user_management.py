# accounting/tests_user_management.py
"""Dashboard user management (Users & Staff) regression tests."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import Contact

User = get_user_model()


class UserManagementTests(TestCase):

    def setUp(self):
        self.superuser = User.objects.create_superuser(
            username='owner', email='owner@example.com',
            password='OwnerPass123!')
        self.staff = User.objects.create_user(
            username='counter', email='counter@example.com',
            password='StaffPass123!', is_staff=True)
        self.customer = User.objects.create_user(
            username='ramesh', email='ramesh@example.com',
            password='CustPass123!')
        self.contact = Contact.objects.create(
            user=self.customer, name='Ramesh Kumar',
            email='ramesh@example.com', phone='9876543210',
            contact_type='customer')
        self.list_url = reverse('accounting:user_list')

    def _staff_login(self):
        self.client.force_login(self.staff)

    def _super_login(self):
        self.client.force_login(self.superuser)

    def _create_payload(self, **overrides):
        payload = {
            'username': 'newstaff',
            'full_name': 'New Staff',
            'email': 'newstaff@example.com',
            'phone': '',
            'role': 'staff',
            'password1': 'StrongPass123!',
            'password2': 'StrongPass123!',
        }
        payload.update(overrides)
        return payload

    # ── access ───────────────────────────────────────────────
    def test_staff_can_open_users_page(self):
        self._staff_login()
        resp = self.client.get(self.list_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Users &amp; Staff')

    def test_customer_cannot_open_users_page(self):
        self.client.force_login(self.customer)
        resp = self.client.get(self.list_url)
        self.assertEqual(resp.status_code, 302)

    def test_staff_list_hides_superusers(self):
        self._staff_login()
        resp = self.client.get(self.list_url)
        usernames = [u.username for u in resp.context['users']]
        self.assertIn('ramesh', usernames)
        self.assertNotIn('owner', usernames)
        self.assertFalse(resp.context['allow_admin_role'])

    def test_superuser_sees_other_superusers(self):
        User.objects.create_superuser(
            username='owner2', email='owner2@example.com',
            password='OwnerPass123!')
        self._super_login()
        resp = self.client.get(self.list_url)
        usernames = [u.username for u in resp.context['users']]
        self.assertIn('owner2', usernames)

    # ── create ───────────────────────────────────────────────
    def test_create_staff_login(self):
        self._staff_login()
        resp = self.client.post(
            reverse('accounting:user_create'), self._create_payload())
        self.assertEqual(resp.status_code, 302)
        user = User.objects.get(username='newstaff')
        self.assertTrue(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.is_active)
        self.assertTrue(user.check_password('StrongPass123!'))
        self.assertFalse(Contact.objects.filter(user=user).exists())

    def test_create_customer_login_makes_contact(self):
        self._staff_login()
        resp = self.client.post(
            reverse('accounting:user_create'),
            self._create_payload(
                username='sita', full_name='Sita Devi',
                email='sita@example.com', phone='9123456780',
                role='customer'))
        self.assertEqual(resp.status_code, 302)
        user = User.objects.get(username='sita')
        self.assertFalse(user.is_staff)
        contact = Contact.objects.get(user=user)
        self.assertEqual(contact.name, 'Sita Devi')
        self.assertEqual(contact.phone, '9123456780')

    def test_create_rejects_duplicate_email(self):
        self._staff_login()
        resp = self.client.post(
            reverse('accounting:user_create'),
            self._create_payload(email='counter@example.com'))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username='newstaff').exists())
        self.assertContains(resp, 'already used by another login')

    def test_create_customer_rejects_phone_on_existing_contact(self):
        self._staff_login()
        resp = self.client.post(
            reverse('accounting:user_create'),
            self._create_payload(
                username='sita', email='sita@example.com',
                phone='9876543210', role='customer'))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username='sita').exists())
        self.assertContains(resp, 'already on contact')

    def test_non_superuser_cannot_create_admin(self):
        self._staff_login()
        resp = self.client.post(
            reverse('accounting:user_create'),
            self._create_payload(role='admin'))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username='newstaff').exists())

    # ── edit ─────────────────────────────────────────────────
    def test_edit_customer_updates_contact_phone(self):
        self._staff_login()
        resp = self.client.post(
            reverse('accounting:user_edit', args=[self.customer.pk]),
            {'username': 'ramesh', 'email': 'ramesh@example.com',
             'full_name': 'Ramesh Kumar', 'role': 'customer',
             'phone': '9000000001'})
        self.assertEqual(resp.status_code, 302)
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.phone, '9000000001')

    def test_non_superuser_cannot_edit_superuser(self):
        self._staff_login()
        resp = self.client.get(
            reverse('accounting:user_edit', args=[self.superuser.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_staff_cannot_demote_self(self):
        self._staff_login()
        resp = self.client.post(
            reverse('accounting:user_edit', args=[self.staff.pk]),
            {'username': 'counter', 'email': 'counter@example.com',
             'full_name': 'Counter Staff', 'role': 'customer',
             'phone': ''})
        self.assertEqual(resp.status_code, 200)
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.is_staff)

    # ── activation ───────────────────────────────────────────
    def test_toggle_deactivates_customer(self):
        self._staff_login()
        resp = self.client.post(
            reverse('accounting:user_toggle_active', args=[self.customer.pk]))
        self.assertEqual(resp.status_code, 302)
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.is_active)

    def test_cannot_deactivate_self(self):
        self._staff_login()
        resp = self.client.post(
            reverse('accounting:user_toggle_active', args=[self.staff.pk]))
        self.assertEqual(resp.status_code, 302)
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.is_active)

    # ── password ─────────────────────────────────────────────
    def test_set_password(self):
        self._staff_login()
        resp = self.client.post(
            reverse('accounting:user_set_password', args=[self.customer.pk]),
            {'new_password1': 'ChangedPass123!',
             'new_password2': 'ChangedPass123!'})
        self.assertEqual(resp.status_code, 302)
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.check_password('ChangedPass123!'))

    def test_non_superuser_cannot_reset_superuser_password(self):
        self._staff_login()
        resp = self.client.post(
            reverse('accounting:user_set_password', args=[self.superuser.pk]),
            {'new_password1': 'HackedPass123!',
             'new_password2': 'HackedPass123!'})
        self.assertEqual(resp.status_code, 403)
        self.superuser.refresh_from_db()
        self.assertTrue(self.superuser.check_password('OwnerPass123!'))
