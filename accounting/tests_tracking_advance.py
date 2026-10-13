"""
Tracking page — advance payment visibility tests.

Chalane ka tareeka:
    python manage.py test accounting.tests_tracking_advance -v 2

Rule: customer ne is repair ke liye advance diya hai (invoice par
AdvanceAdjustment applied) toh tracking page par "Advance Paid" dikhna
chahiye; advance nahi hai toh bilkul nahi dikhna chahiye.
"""
from __future__ import annotations

from decimal import Decimal

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from accounting.models import (
    AdvanceAdjustment, Contact, Invoice, Payment, RepairJob,
)
from accounting.utils.tracking import generate_tracking_token


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class TrackingAdvanceVisibilityTests(TestCase):

    def setUp(self):
        self.client = Client()
        suffix = timezone.now().strftime('%H%M%S%f')[:8]
        self.customer = Contact.objects.create(
            contact_type='customer', name=f'Adv Cust {suffix}',
            phone='95' + suffix,
        )
        self.invoice = Invoice.objects.create(
            customer=self.customer, grand_total=Decimal('1000.00'),
            subtotal=Decimal('1000.00'), paid_amount=Decimal('300.00'),
            balance_due=Decimal('700.00'), payment_status='partial',
        )
        self.job = RepairJob.objects.create(
            customer=self.customer, device_model='HP Test',
            issue_description='Boot loop', status='ready',
            final_amount=Decimal('1000.00'), invoice=self.invoice,
        )
        self.advance_payment = Payment.objects.create(
            direction='received', contact=self.customer,
            amount=Decimal('300.00'), is_advance=True,
        )

    def track_page(self):
        token = generate_tracking_token(self.job)
        return self.client.get(f'/tracking/repair/{token}/')

    def test_advance_not_shown_when_none_applied(self):
        page = self.track_page()
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'Advance Paid')
        # Normal payment tiles still render
        self.assertContains(page, 'Balance Due')

    def test_advance_shown_when_applied_to_invoice(self):
        AdvanceAdjustment.objects.create(
            payment=self.advance_payment, invoice=self.invoice,
            amount=Decimal('300.00'),
        )
        page = self.track_page()
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Advance Paid')
        self.assertContains(page, '₹300.00')

    def test_soft_deleted_advance_not_shown(self):
        adj = AdvanceAdjustment.objects.create(
            payment=self.advance_payment, invoice=self.invoice,
            amount=Decimal('300.00'),
        )
        adj.soft_delete()
        page = self.track_page()
        self.assertNotContains(page, 'Advance Paid')

    def test_no_invoice_means_no_advance(self):
        self.job.invoice = None
        self.job.save()
        page = self.track_page()
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'Advance Paid')
