# accounting/tests_contact_portal_login.py
"""Admin-created portal logins from a contact record."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Contact, Invoice

User = get_user_model()


class ContactPortalLoginTests(TestCase):

    def setUp(self):
        self.staff = User.objects.create_user(
            username='counter', password='StaffPass123!', is_staff=True)
        self.contact = Contact.objects.create(
            name='Ramesh Kumar', phone='9876543210',
            contact_type='customer')
        self.invoice = Invoice.objects.create(
            customer=self.contact, date=timezone.localdate())
        self.url = reverse(
            'accounting:contact_create_portal_login', args=[self.contact.pk])

    def _payload(self, **overrides):
        payload = {
            'username': 'ramesh_portal',
            'email': '',
            'password1': 'PortalPass123!',
            'password2': 'PortalPass123!',
        }
        payload.update(overrides)
        return payload

    def test_staff_creates_login_linked_to_contact(self):
        self.client.force_login(self.staff)
        resp = self.client.post(self.url, self._payload())
        self.assertEqual(resp.status_code, 302)

        user = User.objects.get(username='ramesh_portal')
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertTrue(user.check_password('PortalPass123!'))

        self.contact.refresh_from_db()
        self.assertEqual(self.contact.user_id, user.pk)
        self.assertEqual(self.contact.name, 'Ramesh Kumar')
        self.assertEqual(self.contact.phone, '9876543210')
        self.assertFalse(Contact.objects.filter(
            user=user).exclude(pk=self.contact.pk).exists())

    def test_customer_login_sees_old_invoice(self):
        self.client.force_login(self.staff)
        self.client.post(self.url, self._payload())

        user = User.objects.get(username='ramesh_portal')
        self.client.force_login(user)
        resp = self.client.get(reverse('customer:customer_invoices'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.invoice.invoice_number)

    def test_duplicate_username_rejected(self):
        User.objects.create_user(username='taken', email='taken@example.com')
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.url, self._payload(username='taken'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'This username is already taken.')
        self.contact.refresh_from_db()
        self.assertIsNone(self.contact.user_id)

    def test_duplicate_email_rejected(self):
        User.objects.create_user(
            username='other', email='used@example.com')
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.url, self._payload(email='used@example.com'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'already used by another login')
        self.assertFalse(
            User.objects.filter(username='ramesh_portal').exists())

    def test_already_linked_contact_refused(self):
        linked = User.objects.create_user(
            username='already', email='already@example.com')
        self.contact.user = linked
        self.contact.save()

        self.client.force_login(self.staff)
        resp = self.client.post(self.url, self._payload())
        self.assertFalse(
            User.objects.filter(username='ramesh_portal').exists())
        self.assertEqual(resp.status_code, 302)
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.user_id, linked.pk)

    def test_vendor_contact_becomes_both(self):
        vendor = Contact.objects.create(
            name='Supplier', phone='9000000002', contact_type='vendor')
        url = reverse(
            'accounting:contact_create_portal_login', args=[vendor.pk])

        self.client.force_login(self.staff)
        self.client.post(url, self._payload(username='supplier_login'))

        vendor.refresh_from_db()
        self.assertEqual(vendor.contact_type, 'both')
        self.assertIsNotNone(vendor.user_id)

    def test_non_staff_cannot_create_login(self):
        customer = User.objects.create_user(
            username='someone', email='someone@example.com',
            is_active=True)
        self.client.force_login(customer)
        resp = self.client.post(self.url, self._payload())
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(
            User.objects.filter(username='ramesh_portal').exists())
