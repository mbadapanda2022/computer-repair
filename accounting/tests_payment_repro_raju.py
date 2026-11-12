"""
Repro: one ₹4,400 payment split over two invoices (₹3,900 + ₹500),
then deleted and re-entered (production incident, 12 Nov 2026).

The shop saw the invoices stay "unpaid" after the first save and the
contact detail showing ₹8,800 after re-entry. This walks the exact form
POST and HTMX DELETE through the real views to pin both symptoms.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounting.models import (
    BankAccount, Contact, Invoice, InvoiceItem, LedgerEntry, LedgerLine,
    Payment, PaymentAllocation, Product,
)

User = get_user_model()


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class RajuDuplicateDeleteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='raju_staff', password='TestPass123!',
            is_staff=True, is_active=True,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)
        self.customer = Contact.objects.create(
            contact_type='customer', name='Raju Ganesh',
            phone='9700000001',
        )
        self.bank = BankAccount.objects.create(
            name='Axis Raju', bank_name='Axis Bank',
            account_type='savings', opening_balance=Decimal('0.00'),
        )
        self.inv_a = self._invoice('3900.00')
        self.inv_b = self._invoice('500.00')

    def _invoice(self, amount):
        inv = Invoice.objects.create(
            customer=self.customer, date=timezone.localdate(),
        )
        product = Product.objects.create(
            name=f'Svc {amount}', selling_price=Decimal(amount),
            purchase_price=Decimal(amount), tax_rate=Decimal('0'),
            current_stock=Decimal('0'), is_service=True, is_active=True,
        )
        InvoiceItem.objects.create(
            invoice=inv, product=product, quantity=Decimal('1'),
            unit_price=Decimal(amount), tax_rate=Decimal('0'),
        )
        inv.refresh_from_db()
        return inv

    def _post_payment(self, amount='4400.00', allocations=None,
                      confirm_duplicate=None):
        data = {
            'direction': 'received',
            'contact': str(self.customer.pk),
            'amount': amount,
            'date': str(timezone.localdate()),
            'method': 'upi',
            'bank_account': str(self.bank.pk),
            'upi_ref': '',
            'reference': '',
            'description': '',
            'discount_amount': '0',
        }
        if confirm_duplicate:
            data['confirm_duplicate'] = confirm_duplicate
        for inv_id, amt in (allocations or {}).items():
            data[f'allocation_amount_{inv_id}'] = amt
        return self.client.post(
            reverse('accounting:payment_create'), data,
            HTTP_HX_REQUEST='true',
        )

    def test_full_cycle(self):
        resp = self._post_payment(allocations={
            self.inv_a.pk: '3900', self.inv_b.pk: '500',
        })
        self.assertEqual(resp.status_code, 200, resp.content[:2000])

        payment = Payment.objects.get(contact=self.customer)
        self.assertEqual(payment.amount, Decimal('4400.00'))
        allocs = PaymentAllocation.objects.filter(payment=payment)
        self.assertEqual(allocs.count(), 2)

        self.inv_a.refresh_from_db()
        self.inv_b.refresh_from_db()
        self.assertEqual(self.inv_a.payment_status, 'paid')
        self.assertEqual(self.inv_b.payment_status, 'paid')

        self.customer.refresh_from_db()
        self.assertEqual(self.customer.receivable_balance, Decimal('0.00'))
        self.assertEqual(self.customer.balance, Decimal('0.00'))

        # ── HTMX delete exactly as the table button fires it ──
        delete_resp = self.client.delete(
            reverse('accounting:payment_delete', args=[payment.pk]),
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(delete_resp.status_code, 200, delete_resp.content[:2000])
        self.assertTrue(
            Payment.all_objects.get(pk=payment.pk).is_deleted,
            'payment should be soft deleted by the DELETE endpoint',
        )

        self.inv_a.refresh_from_db()
        self.inv_b.refresh_from_db()
        self.assertEqual(self.inv_a.payment_status, 'unpaid')
        self.assertEqual(self.inv_b.payment_status, 'unpaid')
        self.assertEqual(self.inv_a.paid_amount, Decimal('0.00'))
        self.assertEqual(self.inv_b.paid_amount, Decimal('0.00'))

        self.customer.refresh_from_db()
        self.assertEqual(self.customer.receivable_balance, Decimal('4400.00'))

        # No ghost ledger rows / allocations left behind (invoice entries can
        # share the same reference_id, so scope by entry_type).
        self.assertFalse(
            LedgerEntry.objects.filter(
                reference_id=payment.pk,
                entry_type__in=['payment', 'advance_received', 'advance_paid'],
            ).exists()
        )
        self.assertFalse(
            LedgerLine.objects.filter(
                ledger_entry__reference_id=payment.pk,
                ledger_entry__entry_type__in=[
                    'payment', 'advance_received', 'advance_paid',
                ],
            ).exists()
        )
        self.assertFalse(
            PaymentAllocation.objects.filter(payment=payment).exists()
        )

    def test_duplicate_entry_detected(self):
        """Re-entering the same payment must be surfaced, not silently saved."""
        self._post_payment(allocations={
            self.inv_a.pk: '3900', self.inv_b.pk: '500',
        })
        second = self._post_payment(allocations={
            self.inv_a.pk: '3900', self.inv_b.pk: '500',
        })
        self.assertEqual(second.status_code, 200)
        self.assertContains(second, 'This looks like a duplicate entry')
        self.assertEqual(
            Payment.objects.filter(contact=self.customer).count(), 1,
            'duplicate must not be saved without explicit confirmation',
        )

        confirmed = self._post_payment(confirm_duplicate='1')
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(
            Payment.objects.filter(contact=self.customer).count(), 2,
            'explicit confirmation must allow a genuinely separate payment',
        )

    def test_allocations_survive_validation_error(self):
        """A validation error must not wipe the amounts the shop typed."""
        data = {
            'direction': 'received',
            'contact': str(self.customer.pk),
            'amount': '4400.00',
            'date': str(timezone.localdate()),
            'method': 'upi',
            'bank_account': '',  # required for UPI -> form invalid
            'discount_amount': '0',
            f'allocation_amount_{self.inv_a.pk}': '3900',
            f'allocation_amount_{self.inv_b.pk}': '500',
        }
        resp = self.client.post(
            reverse('accounting:payment_create'), data,
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'value="3900.00"')
        self.assertContains(resp, 'value="500.00"')
        self.assertEqual(
            Payment.objects.filter(contact=self.customer).count(), 0)
