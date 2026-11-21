"""Tests for staff-approved linking of portal signups to shop contacts.

Covers: registration matching (email/phone), the pending-approval gate,
staff approve/reject, manual linking, and data preservation.
"""
from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Contact, Invoice, PortalClaimRequest
from .utils.portal_linking import (
    attach_portal_identity,
    link_contact_to_user,
)

REG_DATA = {
    'username': 'ramesh1',
    'full_name': 'Ramesh Kumar',
    'email': 'ramesh@example.com',
    'phone': '9876543210',
    'password1': 'TestPass123!',
    'password2': 'TestPass123!',
    'website': '',
}


class PortalLinkingTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_user(
            username='link_staff', password='TestPass123!', is_staff=True)
        self.existing = Contact.objects.create(
            name='Ramesh Kumar', phone='9876543210', contact_type='customer')
        self.invoice = Invoice.objects.create(
            customer=self.existing, date=timezone.localdate())

    # ── helpers ──────────────────────────────────────────────
    def _register(self, **overrides):
        data = dict(REG_DATA)
        data.update(overrides)
        return self.client.post(reverse('accounting:register'), data)

    def _signup_user(self, username='ramesh1'):
        return User.objects.get(username=username)

    def _claim(self):
        return PortalClaimRequest.objects.get()

    def _activate(self, user):
        # Simulates the OTP-verified state before login.
        user.is_active = True
        user.save(update_fields=['is_active'])

    def _resolve(self, claim, action, **extra):
        data = {'action': action}
        data.update(extra)
        return self.client.post(
            reverse('accounting:portal_claim_resolve', args=[claim.pk]), data)

    # ── registration matching ────────────────────────────────
    def test_phone_match_creates_claim_not_duplicate_contact(self):
        resp = self._register()
        self.assertEqual(resp.status_code, 302)

        user = self._signup_user()
        self.assertFalse(user.is_active)
        self.assertEqual(Contact.objects.count(), 1)

        claim = self._claim()
        self.assertEqual(claim.user_id, user.pk)
        self.assertEqual(claim.contact_id, self.existing.pk)
        self.assertEqual(claim.matched_by, 'phone')
        self.assertEqual(claim.status, 'pending')

    def test_email_match_with_new_phone_still_creates_claim(self):
        self.existing.email = 'ramesh@example.com'
        self.existing.phone = None
        self.existing.save()

        self._register()
        claim = self._claim()
        self.assertEqual(claim.contact_id, self.existing.pk)
        self.assertEqual(claim.matched_by, 'email')

    def test_phone_linked_to_another_account_blocks_signup(self):
        other = User.objects.create_user(
            username='someone_else', email='someone@example.com')
        self.existing.user = other
        self.existing.save()

        resp = self._register()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'linked to an existing account')
        self.assertFalse(
            User.objects.filter(username='ramesh1').exists())
        self.assertEqual(PortalClaimRequest.objects.count(), 0)

    def test_no_match_creates_linkable_contact_immediately(self):
        resp = self._register(
            username='fresh_user', email='fresh@example.com',
            phone='9123456789', full_name='Fresh User')
        self.assertEqual(resp.status_code, 302)

        user = self._signup_user('fresh_user')
        contact = Contact.objects.get(user=user)
        self.assertEqual(contact.name, 'Fresh User')
        self.assertEqual(contact.phone, '9123456789')
        self.assertEqual(PortalClaimRequest.objects.count(), 0)

    # ── pending gate ─────────────────────────────────────────
    def test_pending_claim_gates_customer_portal(self):
        user = User.objects.create_user(
            username='gated_user', email='gated@example.com',
            is_active=True)
        attach_portal_identity(
            user, 'Gated User', 'gated@example.com', '9876543210')
        self.assertEqual(PortalClaimRequest.objects.count(), 1)

        self.client.force_login(user)
        resp = self.client.get(reverse('customer:customer_dashboard'))
        self.assertRedirects(
            resp, reverse('customer:account_pending'),
            fetch_redirect_response=False)

        resp = self.client.get(reverse('customer:customer_invoices'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('customer:account_pending'), resp['Location'])

        resp = self.client.get(reverse('customer:account_pending'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'waiting for shop approval')

    # ── staff approval / rejection ───────────────────────────
    def test_staff_approve_links_contact_and_preserves_old_data(self):
        self._register()
        user = self._signup_user()
        claim = self._claim()

        self.client.force_login(self.staff)
        resp = self._resolve(claim, 'approve')
        self.assertEqual(resp.status_code, 302)

        self.existing.refresh_from_db()
        claim.refresh_from_db()
        user.refresh_from_db()

        self.assertEqual(self.existing.user_id, user.pk)
        self.assertEqual(self.existing.phone, '9876543210')
        self.assertEqual(self.existing.name, 'Ramesh Kumar')
        self.assertEqual(claim.status, 'approved')
        self.assertEqual(claim.resolved_by_id, self.staff.pk)

        self._activate(user)
        self.client.force_login(user)
        resp = self.client.get(reverse('customer:customer_invoices'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.invoice.invoice_number)

    def test_approve_fills_only_blank_fields(self):
        self.existing.email = ''
        self.existing.save()
        self._register()
        claim = self._claim()

        self.client.force_login(self.staff)
        self._resolve(claim, 'approve')

        self.existing.refresh_from_db()
        self.assertEqual(self.existing.email, 'ramesh@example.com')
        self.assertEqual(self.existing.name, 'Ramesh Kumar')
        self.assertEqual(self.existing.phone, '9876543210')

    def test_reject_creates_separate_profile_and_opens_portal(self):
        self._register()
        user = self._signup_user()
        claim = self._claim()

        self.client.force_login(self.staff)
        self._resolve(claim, 'reject')

        claim.refresh_from_db()
        self.existing.refresh_from_db()
        self.assertEqual(claim.status, 'rejected')
        self.assertIsNone(self.existing.user_id)

        new_contact = Contact.objects.get(user=user)
        self.assertNotEqual(new_contact.pk, self.existing.pk)
        self.assertFalse(new_contact.phone)  # number stays on the old record

        self._activate(user)
        self.client.force_login(user)
        resp = self.client.get(reverse('customer:customer_invoices'))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, self.invoice.invoice_number)

    def test_alternate_match_can_be_chosen(self):
        email_contact = Contact.objects.create(
            name='Ramesh By Email', email='ramesh@example.com',
            contact_type='customer')
        self._register()
        claim = self._claim()
        self.assertEqual(claim.contact_id, email_contact.pk)
        self.assertEqual(claim.alternate_contact_id, self.existing.pk)
        self.assertEqual(claim.matched_by, 'both')

        self.client.force_login(self.staff)
        self._resolve(claim, 'approve', contact_id=str(self.existing.pk))

        self.existing.refresh_from_db()
        email_contact.refresh_from_db()
        self.assertIsNotNone(self.existing.user_id)
        self.assertIsNone(email_contact.user_id)
        self.assertEqual(self.existing.email, 'ramesh@example.com')

    def test_vendor_contact_becomes_both_on_approval(self):
        self.existing.contact_type = 'vendor'
        self.existing.save()
        self._register()
        claim = self._claim()

        self.client.force_login(self.staff)
        self._resolve(claim, 'approve')

        self.existing.refresh_from_db()
        self.assertEqual(self.existing.contact_type, 'both')

    def test_staff_can_reject_with_note(self):
        self._register()
        claim = self._claim()
        self.client.force_login(self.staff)
        self._resolve(claim, 'reject', note='Different person — old number')
        claim.refresh_from_db()
        self.assertIn('Different person', claim.note)

    # ── staff-only access ────────────────────────────────────
    def test_claims_page_is_staff_only(self):
        self._register()
        claim = self._claim()
        customer = self._signup_user()
        self._activate(customer)
        self.client.force_login(customer)

        resp = self.client.get(reverse('accounting:portal_claims'))
        self.assertEqual(resp.status_code, 302)

        resp = self._resolve(claim, 'approve')
        self.assertEqual(resp.status_code, 302)
        claim.refresh_from_db()
        self.assertEqual(claim.status, 'pending')

        self.client.force_login(self.staff)
        resp = self.client.get(reverse('accounting:portal_claims'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Portal Linking Requests')
        self.assertContains(resp, 'Ramesh Kumar')

    def test_contact_list_shows_pending_claim_count(self):
        self._register()
        self.client.force_login(self.staff)
        resp = self.client.get(reverse('accounting:contact_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['pending_claim_count'], 1)

    # ── manual link tool ─────────────────────────────────────
    def test_manual_link_attaches_existing_portal_user(self):
        user = User.objects.create_user(
            username='manual_user', email='manual@example.com',
            is_active=True)

        self.client.force_login(self.staff)
        resp = self.client.post(
            reverse('accounting:contact_portal_link', args=[self.existing.pk]),
            {'identifier': 'manual@example.com'})
        self.assertEqual(resp.status_code, 302)

        self.existing.refresh_from_db()
        self.assertEqual(self.existing.user_id, user.pk)
        self.assertEqual(self.existing.email, 'manual@example.com')

    def test_manual_link_view_shows_error_for_busy_profile(self):
        user = User.objects.create_user(
            username='taken_user', email='taken@example.com',
            is_active=True)
        other = Contact.objects.create(
            user=user, name='Taken User', email='taken@example.com',
            contact_type='customer')
        Invoice.objects.create(customer=other, date=timezone.localdate())

        self.client.force_login(self.staff)
        resp = self.client.post(
            reverse('accounting:contact_portal_link', args=[self.existing.pk]),
            {'identifier': 'taken_user'}, follow=True)

        self.assertContains(resp, 'Merge the two contacts first.')
        self.existing.refresh_from_db()
        self.assertIsNone(self.existing.user_id)
        self.assertTrue(Contact.objects.filter(pk=other.pk).exists())

    def test_manual_link_resolves_pending_claim(self):
        user = User.objects.create_user(
            username='claim_user', email='claim@example.com',
            is_active=True)
        claim = PortalClaimRequest.objects.create(
            user=user, contact=self.existing, matched_by='phone',
            submitted_name='Claim User', submitted_email='claim@example.com',
            submitted_phone='9876543210')

        contact = link_contact_to_user(self.existing, user, self.staff)
        claim.refresh_from_db()
        self.assertEqual(contact.user_id, user.pk)
        self.assertEqual(claim.status, 'approved')

    def test_manual_link_drops_empty_auto_created_profile(self):
        user = User.objects.create_user(
            username='auto_user', email='auto@example.com', is_active=True)
        placeholder = Contact.objects.create(
            user=user, name='Auto User', email='auto@example.com',
            contact_type='customer')

        link_contact_to_user(self.existing, user, self.staff)

        self.existing.refresh_from_db()
        self.assertEqual(self.existing.user_id, user.pk)
        self.assertFalse(
            Contact.all_objects.filter(pk=placeholder.pk).exists())

    def test_manual_link_refuses_profile_with_transactions(self):
        user = User.objects.create_user(
            username='busy_user', email='busy@example.com', is_active=True)
        placeholder = Contact.objects.create(
            user=user, name='Busy User', email='busy@example.com',
            contact_type='customer')
        Invoice.objects.create(customer=placeholder, date=timezone.localdate())

        with self.assertRaises(ValueError):
            link_contact_to_user(self.existing, user, self.staff)

        self.existing.refresh_from_db()
        self.assertIsNone(self.existing.user_id)
        self.assertTrue(Contact.objects.filter(pk=placeholder.pk).exists())
