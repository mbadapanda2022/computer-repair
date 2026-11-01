"""
Contact → Repairs link tests (Django TestCase).

Covers the "View Repairs" entry points added to the Contacts actions
dropdown and the contact detail modal, which jump to the staff repair
list pre-filtered to that contact's jobs.

Run:
    venv\\Scripts\\python.exe manage.py test accounting.tests_contact_repairs_link -v 2
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounting.models import Contact, RepairJob

User = get_user_model()


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class ContactViewRepairsLinkTests(TestCase):
    """Staff sees a per-contact 'View Repairs' link that filters the list."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='link_staff', password='TestPass123!',
            is_staff=True, is_active=True,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)

        suffix = timezone.now().strftime('%H%M%S%f')[:8]
        self.customer = Contact.objects.create(
            contact_type='customer', name=f'Link Customer {suffix}',
            phone=f'9{suffix}'.ljust(10, '0')[:10],
        )
        self.vendor = Contact.objects.create(
            contact_type='vendor', name=f'Link Vendor {suffix}',
            phone=f'8{suffix}'.ljust(10, '0')[:10],
        )
        self.other_customer = Contact.objects.create(
            contact_type='customer', name=f'Link Other {suffix}',
            phone=f'7{suffix}'.ljust(10, '0')[:10],
        )
        self.job = RepairJob.objects.create(
            customer=self.customer, device_model='Linked Laptop',
            issue_description='Link smoke test',
        )
        self.other_job = RepairJob.objects.create(
            customer=self.other_customer, device_model='Other Laptop',
            issue_description='Should not appear',
        )

    # ── contacts page ────────────────────────────────────────────
    def _contact_list_html(self, contact):
        response = self.client.get(
            reverse('accounting:contact_list'), {'search': contact.name}
        )
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_dropdown_has_view_repairs_for_customer(self):
        html = self._contact_list_html(self.customer)
        self.assertIn('View Repairs', html)
        self.assertIn(
            f'{reverse("accounting:repair_list")}?customer={self.customer.pk}',
            html,
        )

    def test_dropdown_hides_view_repairs_for_vendor(self):
        html = self._contact_list_html(self.vendor)
        self.assertIn(self.vendor.name, html)
        self.assertNotIn('View Repairs', html)

    def test_detail_modal_has_view_repairs_for_customer(self):
        response = self.client.get(
            reverse('accounting:contact_detail_modal', args=[self.customer.pk])
        )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('View Repairs', html)
        self.assertIn(
            f'{reverse("accounting:repair_list")}?customer={self.customer.pk}',
            html,
        )

    # ── filtered repair list ─────────────────────────────────────
    def test_repair_list_filtered_by_customer_shows_only_their_jobs(self):
        response = self.client.get(
            reverse('accounting:repair_list'), {'customer': self.customer.pk}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.job.job_number)
        self.assertNotContains(response, self.other_job.job_number)

    def test_filtered_repair_list_offers_customer_scoped_print_and_export(self):
        """Print/Export buttons forward the live query string, so the
        filtered view exports exactly the customer's repairs."""
        response = self.client.get(
            reverse('accounting:repair_list'), {'customer': self.customer.pk}
        )
        html = response.content.decode()
        self.assertIn(reverse('accounting:repair_list_print'), html)
        self.assertIn(reverse('accounting:export_repairs_excel'), html)
        self.assertIn('new URLSearchParams(window.location.search)', html)
