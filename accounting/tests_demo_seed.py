# accounting/tests_demo_seed.py
"""The demo dataset must showcase the current feature set offline.

A buyer demos this package to shop owners, so the seeded data is the product
tour: every headline flow has to have a live example. These tests lock the
invariants the launcher's first-run walkthrough depends on.

Run: venv/Scripts/python.exe -u manage.py test accounting.tests_demo_seed
"""
from decimal import Decimal

from django.test import TestCase, override_settings

from accounting.models import (
    AdvanceAdjustment, CompanyProfile, Contact, Invoice, RepairJob,
    UpiPaymentRequest,
)
from accounting.utils.tracking import generate_tracking_token


@override_settings(DEMO_MODE=True)
class DemoSeedTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from django.core.management import call_command
        call_command('demo_seed', '--fresh', verbosity=0)

    # ── branding / login ───────────────────────────────────────
    def test_company_carries_demo_upi_id(self):
        """Without a VPA every QR in the demo would be blank."""
        company = CompanyProfile.get_instance()
        self.assertEqual(company.upi_id, 'democomputercare@ybl')
        self.assertTrue(company.upi_holder_name)

    def test_refuses_to_run_outside_demo_mode(self):
        """The guard is what keeps demo fixtures away from a real database."""
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with override_settings(DEMO_MODE=False):
            with self.assertRaises(CommandError):
                call_command('demo_seed', '--fresh', verbosity=0)
        self.assertTrue(Invoice.objects.exists(),
                        'refusal must not wipe the seeded demo first')

    def test_second_run_is_skipped_not_duplicated(self):
        from django.core.management import call_command
        before = Invoice.objects.count()
        self.assertEqual(call_command('demo_seed', verbosity=0), 0)
        self.assertEqual(Invoice.objects.count(), before)

    # ── the money story a buyer needs to see ───────────────────
    def test_portal_customer_has_an_open_bill(self):
        """demo_customer logs in and must find something to pay."""
        portal = Contact.objects.get(user__username='demo_customer')
        open_bills = Invoice.objects.filter(
            customer=portal, balance_due__gt=Decimal('0.00'))
        self.assertTrue(open_bills.exists())

    def test_one_pending_upi_claim_exists_to_verify(self):
        claim = UpiPaymentRequest.objects.get(upi_reference='463012345678901')
        self.assertEqual(claim.status, 'pending')
        self.assertGreater(claim.invoice.balance_due, Decimal('0.00'))

    def test_an_advance_is_settled_against_a_bill(self):
        self.assertTrue(AdvanceAdjustment.objects.exists())

    def test_demo_has_a_pending_on_site_booking(self):
        """On-site repairs (engineer visits the customer) are the newest
        booking option — the demo must show one waiting for dispatch.
        The value has to be exactly 'site'; every template branch that draws
        the location block compares against it."""
        job = RepairJob.objects.get(service_mode='site')
        self.assertEqual(job.service_mode, 'site')
        self.assertEqual(job.status, 'pending')
        self.assertTrue(job.service_location)
        self.assertTrue(job.site_contact_name)
        self.assertTrue(job.site_contact_phone)

    # ── repair flows on the public tracking page ───────────────
    def test_every_repair_status_is_represented(self):
        states = set(RepairJob.objects.values_list('status', flat=True))
        for expected in ('pending', 'received', 'diagnosis', 'repairing',
                         'ready', 'delivered'):
            self.assertIn(expected, states, f'demo has no {expected} job')

    def test_a_ready_repair_has_an_unpaid_invoice(self):
        """The tracking link a shop WhatsApps to a customer is the UPI
        collection point, so at least one demo job must reach it with a
        balance due — otherwise the QR screen is invisible in the demo."""
        job = self._ready_unpaid_job()
        self.assertGreater(job.invoice.balance_due, Decimal('0.00'))

    def test_tracking_page_of_unpaid_repair_offers_both_upi_codes(self):
        job = self._ready_unpaid_job()
        token = generate_tracking_token(job)
        response = self.client.get(f'/tracking/repair/{token}/')
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        self.assertIn('by UPI', page)
        # A phone cannot scan its own screen, so both codes need tap links.
        self.assertGreaterEqual(page.count('upi://pay?'), 2)
        self.assertIn('Pay any amount', page)
        self.assertIn('democomputercare@ybl', page)

    def _ready_unpaid_job(self):
        return RepairJob.objects.select_related('invoice').get(
            status='ready', invoice__isnull=False)
