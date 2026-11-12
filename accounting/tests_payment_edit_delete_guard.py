"""User-facing payment/invoice edit + delete flows.

Covers clearing/moving allocations on an edit, amount reductions, double
delete, delete after edit, the invoice-delete guards that stop a paid bill
from being removed, and the edit guards that stop an invoice or vendor bill
being shrunk below the money already recorded against it.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounting.models import (
    BankAccount, Contact, Invoice, InvoiceItem, LedgerEntry,
    Payment, PaymentAllocation, Product, Purchase, PurchaseItem,
)

User = get_user_model()


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class EditDeleteVerifyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='ed_staff', password='TestPass123!',
            is_staff=True, is_active=True,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)
        self.customer = Contact.objects.create(
            contact_type='customer', name='Raju Ganesh', phone='9700000077')
        self.bank = BankAccount.objects.create(
            name='Axis ED', bank_name='Axis Bank', account_type='savings',
            opening_balance=Decimal('0.00'))
        self.inv_a = self._invoice('3900.00')
        self.inv_b = self._invoice('500.00')
        self.payment = self._create_payment()

    def _invoice(self, amount):
        inv = Invoice.objects.create(
            customer=self.customer, date=timezone.localdate())
        product = Product.objects.create(
            name=f'Svc {amount}', selling_price=Decimal(amount),
            purchase_price=Decimal(amount), tax_rate=Decimal('0'),
            current_stock=Decimal('0'), is_service=True, is_active=True)
        InvoiceItem.objects.create(
            invoice=inv, product=product, quantity=Decimal('1'),
            unit_price=Decimal(amount), tax_rate=Decimal('0'))
        inv.refresh_from_db()
        return inv

    def _base_data(self, amount='4400.00'):
        return {
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

    def _create_payment(self):
        data = self._base_data()
        data[f'allocation_amount_{self.inv_a.pk}'] = '3900'
        data[f'allocation_amount_{self.inv_b.pk}'] = '500'
        resp = self.client.post(
            reverse('accounting:payment_create'), data,
            HTTP_HX_REQUEST='true')
        self.assertEqual(resp.status_code, 200, resp.content[:2000])
        return Payment.objects.get(contact=self.customer)

    def _update_payment(self, **overrides):
        data = self._base_data(overrides.pop('amount', '4400.00'))
        data[f'allocation_amount_{self.inv_a.pk}'] = overrides.pop('a', '')
        data[f'allocation_amount_{self.inv_b.pk}'] = overrides.pop('b', '')
        data.update(overrides)
        return self.client.post(
            reverse('accounting:payment_update', args=[self.payment.pk]),
            data, HTTP_HX_REQUEST='true')

    def _assert_state(self, a_status, a_due, b_status, b_due, receivable):
        self.inv_a.refresh_from_db()
        self.inv_b.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(self.inv_a.payment_status, a_status)
        self.assertEqual(self.inv_b.payment_status, b_status)
        self.assertEqual(self.inv_a.balance_due, Decimal(a_due))
        self.assertEqual(self.inv_b.balance_due, Decimal(b_due))
        self.assertEqual(self.customer.receivable_balance, Decimal(receivable))

    def test_edit_form_prefills_existing_allocations(self):
        resp = self.client.get(
            reverse('accounting:payment_update', args=[self.payment.pk]),
            HTTP_HX_REQUEST='true')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'value="3900.00"')
        self.assertContains(resp, 'value="500.00"')

    def test_edit_clears_one_allocation_and_reopens_that_invoice(self):
        resp = self._update_payment(a='3900', b='')
        self.assertEqual(resp.status_code, 200, resp.content[:2000])
        # Cash still credits the customer ledger, so the net receivable is 0;
        # the ₹500 left on the payment is unapplied credit.
        self._assert_state('paid', '0.00', 'unpaid', '500.00', '0.00')
        self.assertEqual(
            PaymentAllocation.objects.filter(payment=self.payment).count(), 1)
        self.assertIn('500.00 is not applied to any bill',
                      resp['HX-Trigger'])

    def test_edit_moves_whole_payment_to_one_invoice(self):
        resp = self._update_payment(a='4400', b='')
        self.assertEqual(resp.status_code, 200, resp.content[:2000])
        self._assert_state('paid', '0.00', 'unpaid', '500.00', '0.00')
        # Server clamps an invoice allocation to its own due amount.
        self.assertEqual(
            PaymentAllocation.objects.get(payment=self.payment).amount,
            Decimal('3900.00'))
        self.assertIn('500.00 is not applied to any bill',
                      resp['HX-Trigger'])

    def test_edit_reduces_amount_and_trims_allocation(self):
        resp = self._update_payment(amount='3500.00', a='3500', b='')
        self.assertEqual(resp.status_code, 200, resp.content[:2000])
        self._assert_state('partial', '400.00', 'unpaid', '500.00', '900.00')
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.amount, Decimal('3500.00'))

    def test_edit_cannot_allocate_more_than_amount(self):
        """Cleared boxes are authoritative, but the pool still caps totals."""
        resp = self._update_payment(amount='1000.00', a='3900', b='500')
        self.assertEqual(resp.status_code, 200, resp.content[:2000])
        self.payment.refresh_from_db()
        total = sum(
            PaymentAllocation.objects.filter(payment=self.payment)
            .values_list('amount', flat=True))
        self.assertLessEqual(total, Decimal('1000.00'))

    def test_double_delete_is_safe(self):
        first = self.client.delete(
            reverse('accounting:payment_delete', args=[self.payment.pk]),
            HTTP_HX_REQUEST='true')
        self.assertEqual(first.status_code, 200)
        second = self.client.delete(
            reverse('accounting:payment_delete', args=[self.payment.pk]),
            HTTP_HX_REQUEST='true')
        self.assertEqual(second.status_code, 404)
        self._assert_state('unpaid', '3900.00', 'unpaid', '500.00', '4400.00')

    def test_delete_after_edit_restores_everything(self):
        self._update_payment(a='4400', b='')
        del_resp = self.client.delete(
            reverse('accounting:payment_delete', args=[self.payment.pk]),
            HTTP_HX_REQUEST='true')
        self.assertEqual(del_resp.status_code, 200)
        self.assertIn('1 bill reopened', del_resp['HX-Trigger'])
        self._assert_state('unpaid', '3900.00', 'unpaid', '500.00', '4400.00')
        self.assertFalse(LedgerEntry.objects.filter(
            reference_id=self.payment.pk,
            entry_type__in=['payment', 'advance_received', 'advance_paid'],
        ).exists())

    def test_invoice_delete_blocked_while_payment_linked(self):
        resp = self.client.delete(
            reverse('accounting:invoice_delete', args=[self.inv_a.pk]),
            HTTP_HX_REQUEST='true')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp['HX-Reswap'], 'none')
        self.assertIn('payments are linked', resp['HX-Trigger'])
        self.assertFalse(
            Invoice.all_objects.get(pk=self.inv_a.pk).is_deleted)

    def test_invoice_delete_works_after_payment_removed(self):
        self.client.delete(
            reverse('accounting:payment_delete', args=[self.payment.pk]),
            HTTP_HX_REQUEST='true')
        resp = self.client.delete(
            reverse('accounting:invoice_delete', args=[self.inv_a.pk]),
            HTTP_HX_REQUEST='true')
        self.assertEqual(resp.status_code, 200, resp.content[:1000])
        self.assertTrue(
            Invoice.all_objects.get(pk=self.inv_a.pk).is_deleted)

    def _edit_down(self, new_price):
        self.client.get(
            reverse('accounting:invoice_update', args=[self.inv_a.pk]))
        key = f'temp_invoice_items_{self.inv_a.pk}'
        session = self.client.session
        rows = session[key]
        rows[0]['unit_price'] = new_price
        rows[0]['line_total'] = new_price
        session[key] = rows
        session.save()
        return self.client.post(
            reverse('accounting:invoice_update', args=[self.inv_a.pk]),
            {
                'customer': str(self.customer.pk),
                'date': str(self.inv_a.date),
                'due_date': '',
                'gst_type': 'regular',
                'discount_amount': '0',
                'discount_type': '',
                'discount_note': '',
                'discount_date': '',
                'notes': '',
            })

    def test_paid_invoice_cannot_be_edited_below_paid_amount(self):
        resp = self._edit_down('3000.00')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context['form'].non_field_errors())
        self.assertIn(
            'already recorded against it',
            resp.context['form'].non_field_errors()[0])
        self.inv_a.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(self.inv_a.grand_total, Decimal('3900.00'))
        self.assertEqual(self.inv_a.payment_status, 'paid')
        self.assertEqual(self.customer.receivable_balance, Decimal('0.00'))

    def test_unpaid_invoice_can_still_be_edited_down(self):
        self.client.delete(
            reverse('accounting:payment_delete', args=[self.payment.pk]),
            HTTP_HX_REQUEST='true')
        resp = self._edit_down('3000.00')
        self.assertEqual(resp.status_code, 302)
        self.inv_a.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(self.inv_a.grand_total, Decimal('3000.00'))
        self.assertEqual(self.customer.receivable_balance, Decimal('3500.00'))


class PurchaseBelowPaidTests(TestCase):
    """A paid vendor bill cannot be shrunk under the payment on it."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='pg_staff', password='TestPass123!',
            is_staff=True, is_active=True)
        self.client = Client()
        self.client.force_login(self.staff)
        self.vendor = Contact.objects.create(
            contact_type='vendor', name='PG Vendor', phone='9800000001')
        self.product = Product.objects.create(
            name='PG Product', selling_price=Decimal('4000.00'),
            purchase_price=Decimal('3000.00'), current_stock=Decimal('0.00'),
            is_service=False, is_active=True, tax_rate=Decimal('0'))
        self.purchase = Purchase.objects.create(
            vendor=self.vendor, date=timezone.localdate())
        PurchaseItem.objects.create(
            purchase=self.purchase, product=self.product,
            quantity=Decimal('1'), unit_price=Decimal('3000.00'),
            tax_rate=Decimal('0'))
        self.purchase.calculate_totals()
        self.purchase.save()
        pay = self.client.post(reverse('accounting:payment_create'), {
            'direction': 'paid',
            'contact': str(self.vendor.pk),
            'amount': '3000.00',
            'date': str(timezone.localdate()),
            'method': 'cash',
            'bank_account': '',
            'reference': '',
            'description': '',
            'discount_amount': '0',
            f'purchase_alloc_{self.purchase.pk}': '3000',
        }, HTTP_HX_REQUEST='true')
        self.assertEqual(pay.status_code, 200, pay.content[:2000])
        self.payment = Payment.objects.get(contact=self.vendor)

    def _edit_down(self, price):
        self.client.get(
            reverse('accounting:purchase_update', args=[self.purchase.pk]))
        key = f'temp_purchase_items_{self.purchase.pk}'
        session = self.client.session
        rows = session[key]
        rows[0]['unit_price'] = price
        rows[0]['line_total'] = price
        session[key] = rows
        session.save()
        return self.client.post(
            reverse('accounting:purchase_update', args=[self.purchase.pk]), {
                'vendor': self.vendor.pk,
                'date': str(self.purchase.date),
                'gst_type': self.purchase.gst_type,
                'discount_amount': '0',
                'freight_charge': '0',
                'discount_type': '', 'discount_note': '',
                'discount_date': '', 'notes': '',
            })

    def test_edit_below_paid_amount_is_blocked(self):
        resp = self._edit_down('2000.00')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('already paid against it',
                      resp.context['form'].non_field_errors()[0])
        self.purchase.refresh_from_db()
        self.vendor.refresh_from_db()
        self.assertEqual(self.purchase.grand_total, Decimal('3000.00'))
        self.assertEqual(self.vendor.payable_balance, Decimal('0.00'))

    def test_edit_below_paid_works_after_payment_removed(self):
        self.client.delete(
            reverse('accounting:payment_delete', args=[self.payment.pk]),
            HTTP_HX_REQUEST='true')
        resp = self._edit_down('2000.00')
        self.assertEqual(resp.status_code, 302)
        self.purchase.refresh_from_db()
        self.vendor.refresh_from_db()
        self.assertEqual(self.purchase.grand_total, Decimal('2000.00'))
        self.assertEqual(self.vendor.payable_balance, Decimal('2000.00'))
