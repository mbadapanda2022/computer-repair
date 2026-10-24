"""
Advance → Invoice settlement tests.

Run:
    python manage.py test accounting.tests_advance_settlement -v 2

Production scenario (Suvam Giri, 13 Oct 2026): the customer paid a
₹1000 advance at intake (Payment is_advance=True, no invoice), then a
₹7000 repair invoice was created and ₹6000 cash was paid as the balance.
The advance stayed floating in the pool and the invoice always showed
₹1000 due. Now apply_available_advance() settles the advance against the
invoice — paid_amount must NOT double-count and un-applying must return
the amount to the pool.
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounting.models import (
    AdvanceAdjustment, Contact, Invoice, Payment, PaymentAllocation,
)

User = get_user_model()


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class AdvanceSettlementTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='settle_staff', password='TestPass123!',
            is_staff=True, is_active=True,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)
        suffix = timezone.now().strftime('%H%M%S%f')[:8]
        self.customer = Contact.objects.create(
            contact_type='customer', name=f'Settle Cust {suffix}',
            phone='9' + suffix,
        )

    # ---------- factories ----------

    def advance(self, amount):
        return Payment.objects.create(
            direction='received', contact=self.customer,
            amount=Decimal(amount), is_advance=True,
        )

    def invoice(self, total):
        inv = Invoice.objects.create(
            customer=self.customer,
            subtotal=Decimal(total),
            grand_total=Decimal(total),
            balance_due=Decimal(total),
        )
        return inv

    def pay_balance(self, inv, amount):
        pay = Payment.objects.create(
            direction='received', contact=self.customer,
            amount=Decimal(amount), is_advance=False,
        )
        PaymentAllocation.objects.create(
            payment=pay, invoice=inv, amount=Decimal(amount),
        )
        return pay

    def refresh(self, obj):
        obj.refresh_from_db()
        return obj

    # ---------- core settlement ----------

    def test_suvam_full_flow_settles_to_zero(self):
        """₹1000 advance + ₹7000 invoice → apply → ₹6000 due → cash pay → paid."""
        self.advance('1000.00')
        inv = self.invoice('7000.00')

        applied = inv.apply_available_advance(note='test')
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0][1], Decimal('1000.00'))

        inv = self.refresh(inv)
        self.assertEqual(inv.paid_amount, Decimal('1000.00'))
        self.assertEqual(inv.balance_due, Decimal('6000.00'))
        self.assertEqual(inv.payment_status, 'partial')

        cust = self.refresh(self.customer)
        self.assertEqual(cust.advance_balance, Decimal('0.00'))

        # Staff now takes the exact ₹6000 cash
        self.pay_balance(inv, '6000.00')
        inv = self.refresh(inv)
        self.assertEqual(inv.paid_amount, Decimal('7000.00'))
        self.assertEqual(inv.balance_due, Decimal('0.00'))
        self.assertEqual(inv.payment_status, 'paid')

    def test_apply_capped_at_balance_due(self):
        """Large advance, small invoice — leftover credit must remain in the pool."""
        self.advance('7500.00')
        inv = self.invoice('7000.00')

        applied = inv.apply_available_advance()
        self.assertEqual(sum(a for _, a in applied), Decimal('7000.00'))

        inv = self.refresh(inv)
        self.assertEqual(inv.balance_due, Decimal('0.00'))
        self.assertEqual(inv.payment_status, 'paid')

        cust = self.refresh(self.customer)
        self.assertEqual(cust.advance_balance, Decimal('500.00'))

    def test_fifo_split_across_two_invoices(self):
        """When one advance splits across two invoices, both must stay correctly accounted."""
        self.advance('1500.00')
        inv_a = self.invoice('1000.00')
        inv_b = self.invoice('1000.00')

        a1 = inv_a.apply_available_advance()
        self.assertEqual(sum(a for _, a in a1), Decimal('1000.00'))
        b1 = inv_b.apply_available_advance()
        self.assertEqual(sum(a for _, a in b1), Decimal('500.00'))

        inv_a, inv_b = self.refresh(inv_a), self.refresh(inv_b)
        self.assertEqual(inv_a.balance_due, Decimal('0.00'))
        self.assertEqual(inv_b.balance_due, Decimal('500.00'))
        self.assertEqual(inv_b.paid_amount, Decimal('500.00'))

    def test_only_unsettled_portion_of_advance_is_applied(self):
        """Part of the advance was already applied to another invoice."""
        pay = self.advance('1000.00')
        other = self.invoice('2000.00')
        AdvanceAdjustment.objects.create(
            payment=pay, invoice=other, amount=Decimal('400.00'),
        )
        inv = self.invoice('7000.00')

        applied = inv.apply_available_advance()
        self.assertEqual(sum(a for _, a in applied), Decimal('600.00'))

    def test_double_apply_is_idempotent(self):
        """Applying again must NOT double-count."""
        self.advance('1000.00')
        inv = self.invoice('7000.00')
        inv.apply_available_advance()
        second = inv.apply_available_advance()
        self.assertEqual(second, [])
        inv = self.refresh(inv)
        self.assertEqual(inv.paid_amount, Decimal('1000.00'))
        self.assertEqual(inv.balance_due, Decimal('6000.00'))

    def test_unapply_returns_credit_to_pool(self):
        self.advance('1000.00')
        inv = self.invoice('7000.00')
        inv.apply_available_advance()

        removed = inv.unapply_advance()
        self.assertEqual(len(removed), 1)

        inv = self.refresh(inv)
        self.assertEqual(inv.paid_amount, Decimal('0.00'))
        self.assertEqual(inv.balance_due, Decimal('7000.00'))
        self.assertEqual(inv.payment_status, 'unpaid')

        cust = self.refresh(self.customer)
        self.assertEqual(cust.advance_balance, Decimal('1000.00'))

    def test_settled_advance_survives_later_normal_payment(self):
        """Settle + partial cash → paid_amount = 7000, no double count."""
        self.advance('1000.00')
        inv = self.invoice('7000.00')
        inv.apply_available_advance()
        self.pay_balance(inv, '3000.00')

        inv = self.refresh(inv)
        self.assertEqual(inv.paid_amount, Decimal('4000.00'))
        self.assertEqual(inv.balance_due, Decimal('3000.00'))

    def test_deleting_advance_payment_unsettles_invoice(self):
        pay = self.advance('1000.00')
        inv = self.invoice('7000.00')
        inv.apply_available_advance()
        self.assertEqual(self.refresh(inv).paid_amount, Decimal('1000.00'))

        pay.delete()
        inv = self.refresh(inv)
        self.assertEqual(inv.paid_amount, Decimal('0.00'))
        self.assertEqual(inv.balance_due, Decimal('7000.00'))

    # ---------- staff views ----------

    def test_apply_view_post_settles_and_redirects(self):
        self.advance('1000.00')
        inv = self.invoice('7000.00')
        url = reverse('accounting:invoice_apply_advance', args=[inv.pk])

        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)

        inv = self.refresh(inv)
        self.assertEqual(inv.balance_due, Decimal('6000.00'))
        self.assertEqual(inv.payment_status, 'partial')

    def test_apply_view_rejects_get(self):
        inv = self.invoice('7000.00')
        resp = self.client.get(
            reverse('accounting:invoice_apply_advance', args=[inv.pk])
        )
        self.assertEqual(resp.status_code, 405)

    def test_unapply_view_post(self):
        self.advance('1000.00')
        inv = self.invoice('7000.00')
        inv.apply_available_advance()

        resp = self.client.post(
            reverse('accounting:invoice_unapply_advance', args=[inv.pk])
        )
        self.assertEqual(resp.status_code, 302)
        inv = self.refresh(inv)
        self.assertEqual(inv.balance_due, Decimal('7000.00'))

    def test_invoice_detail_shows_apply_banner(self):
        self.advance('1000.00')
        inv = self.invoice('7000.00')
        page = self.client.get(
            reverse('accounting:invoice_detail', args=[inv.pk])
        )
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'unapplied advance')
        self.assertContains(page, 'Apply ₹1,000.00 to this invoice')
