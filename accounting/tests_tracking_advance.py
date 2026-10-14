"""
Tracking page — advance payment visibility tests.

Chalane ka tareeka:
    python manage.py test accounting.tests_tracking_advance -v 2

Rule: customer ne is repair ke liye advance diya (invoice par applied
AdvanceAdjustment, ya intake par recorded unapplied advance Payment)
toh tracking par "Advance" section dikhna chahiye; advance nahi hai toh
bilkul nahi dikhna chahiye. Invoice maujood na hone par bhi advance
dikhna chahiye.
"""
from __future__ import annotations

from datetime import timedelta
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
        self.job = RepairJob.objects.create(
            customer=self.customer, device_model='HP Test',
            issue_description='Boot loop', status='received',
        )

    def advance_payment(self, amount='1000.00', days_ago=0, is_advance=True, **kwargs):
        return Payment.objects.create(
            direction='received', contact=self.customer,
            amount=Decimal(amount), is_advance=is_advance,
            date=timezone.now().date() - timedelta(days=days_ago),
            **kwargs,
        )

    def track_page(self):
        token = generate_tracking_token(self.job)
        return self.client.get(f'/tracking/repair/{token}/')

    def make_invoice(self, total='1000.00'):
        invoice = Invoice.objects.create(
            customer=self.customer, grand_total=Decimal(total),
            subtotal=Decimal(total),
        )
        self.job.invoice = invoice
        self.job.final_amount = Decimal(total)
        self.job.save()
        return invoice

    # ---------- hidden cases ----------

    def test_no_advance_means_no_section(self):
        page = self.track_page()
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'Advance Payment')

    def test_normal_non_advance_payment_not_shown(self):
        self.advance_payment(is_advance=False)
        page = self.track_page()
        self.assertNotContains(page, 'Advance Payment')

    def test_old_advance_before_job_not_shown(self):
        self.advance_payment(days_ago=30)
        page = self.track_page()
        self.assertNotContains(page, 'Advance Payment')

    def test_advance_fully_settled_on_other_invoice_not_shown(self):
        other_invoice = Invoice.objects.create(
            customer=self.customer, grand_total=Decimal('500.00'),
        )
        pay = self.advance_payment()
        AdvanceAdjustment.objects.create(
            payment=pay, invoice=other_invoice, amount=Decimal('1000.00'),
        )
        page = self.track_page()
        self.assertNotContains(page, 'Advance Payment')

    def test_soft_deleted_advance_payment_not_shown(self):
        pay = self.advance_payment()
        pay.soft_delete()
        page = self.track_page()
        self.assertNotContains(page, 'Advance Payment')

    # ---------- visible cases ----------

    def test_unapplied_advance_at_intake_shown_without_invoice(self):
        # Suvam Giri jaisa case: advance Payment recorded, invoice abhi nahi
        self.advance_payment('1000.00')
        page = self.track_page()
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Advance Payment')
        self.assertContains(page, '₹1000.00')

    def test_advance_applied_to_invoice_shown(self):
        invoice = self.make_invoice()
        pay = self.advance_payment('300.00')
        AdvanceAdjustment.objects.create(
            payment=pay, invoice=invoice, amount=Decimal('300.00'),
        )
        page = self.track_page()
        self.assertContains(page, 'Advance Payment')
        self.assertContains(page, '₹300.00')

    def test_applied_plus_remaining_both_counted(self):
        invoice = self.make_invoice(total='2000.00')
        pay = self.advance_payment('1000.00')
        AdvanceAdjustment.objects.create(
            payment=pay, invoice=invoice, amount=Decimal('400.00'),
        )
        page = self.track_page()
        # applied 400 (invoice) + remaining 600 (unapplied) = 1000
        self.assertContains(page, '₹1000.00')

    def test_soft_deleted_adjustment_falls_back_to_unapplied(self):
        invoice = self.make_invoice()
        pay = self.advance_payment('500.00')
        adj = AdvanceAdjustment.objects.create(
            payment=pay, invoice=invoice, amount=Decimal('500.00'),
        )
        adj.soft_delete()
        page = self.track_page()
        # adjustment cancelled -> payment ka pura 500 unapplied advance hai
        self.assertContains(page, 'Advance Payment')
        self.assertContains(page, '₹500.00')

    def test_overpaid_invoice_balance_shown_as_zero_not_negative(self):
        # advance 1000 bill 800 par -> balance_due model me -200 store hota hai
        invoice = self.make_invoice(total='800.00')
        pay = self.advance_payment('1000.00')
        AdvanceAdjustment.objects.create(
            payment=pay, invoice=invoice, amount=Decimal('1000.00'),
        )
        invoice.paid_amount = Decimal('1000.00')
        invoice.balance_due = Decimal('-200.00')
        invoice.save()
        page = self.track_page()
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, '₹-200.00')
        self.assertNotContains(page, '-200.00')
