"""Discount limits — a discount can never create money out of nothing.

Every discount path must obey the same rule: the concession is applied to a
bill (or, on an advance, to an open balance), never on top of it. Before these
guards a ₹5,000 discount on a ₹3,900 invoice pushed the customer receivable to
−₹1,100, a ₹5,000 waiver on a ₹100 receipt pushed it to −₹1,200, and a ₹4,000
discount on a ₹3,000 vendor bill left the vendor payable at −₹1,000 — all with
a perfectly balanced ledger, which is exactly what made it dangerous.

These tests POST through the real views: a rejected entry must roll back
completely (no invoice/payment row, no ledger line, no contact balance change)
and keep the typed cart / allocation boxes on screen.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounting.models import (
    BankAccount, Contact, Invoice, InvoiceItem, LedgerLine, Payment,
    Product, Purchase, PurchaseItem, RepairJob,
)

User = get_user_model()


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class DiscountGuardBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='disc_guard_staff', password='TestPass123!',
            is_staff=True, is_active=True,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)
        stamp = timezone.now().strftime('%H%M%S%f')[:10]
        self.customer = Contact.objects.create(
            contact_type='customer', name=f'Guard Cust {stamp}',
            phone='96' + stamp[:8])
        self.vendor = Contact.objects.create(
            contact_type='vendor', name=f'Guard Vendor {stamp}',
            phone='95' + stamp[:8])
        self.product = Product.objects.create(
            name=f'Guard Svc {stamp}', selling_price=Decimal('3900.00'),
            purchase_price=Decimal('3000.00'), tax_rate=Decimal('0'),
            current_stock=Decimal('0'), is_service=True, is_active=True)
        self.bank = BankAccount.objects.create(
            name=f'Guard Bank {stamp}', bank_name='Axis Bank',
            account_type='savings', opening_balance=Decimal('0.00'))

    # ── helpers ──

    def _assert_ledger_balanced(self):
        lines = LedgerLine.objects.filter(ledger_entry__is_deleted=False)
        debit = sum((line.debit for line in lines), Decimal('0'))
        credit = sum((line.credit for line in lines), Decimal('0'))
        self.assertEqual(debit, credit, 'ledger must stay balanced')

    def _invoice(self, amount='3900.00'):
        inv = Invoice.objects.create(
            customer=self.customer, date=timezone.localdate())
        InvoiceItem.objects.create(
            invoice=inv, product=self.product, quantity=Decimal('1'),
            unit_price=Decimal(amount), tax_rate=Decimal('0'))
        inv.refresh_from_db()
        return inv

    def _invoice_post(self, discount, **overrides):
        data = {
            'customer': str(self.customer.pk),
            'date': str(timezone.localdate()),
            'due_date': '',
            'gst_type': 'non_gst',
            'discount_amount': discount,
            'discount_type': '',
            'discount_note': '',
            'discount_date': '',
            'notes': '',
        }
        data.update(overrides)
        return data

    def _cart(self, key, price, **extra):
        item = {
            'product_id': self.product.pk,
            'product_name': self.product.name,
            'quantity': '1',
            'unit_price': price,
            'tax_rate': '0',
            'line_total': price,
            'description': '',
        }
        item.update(extra)
        session = self.client.session
        session[key] = [item]
        session.save()

    def _purchase(self, amount='3000.00'):
        purchase = Purchase.objects.create(
            vendor=self.vendor, date=timezone.localdate(),
            subtotal=Decimal(amount), grand_total=Decimal(amount))
        PurchaseItem.objects.create(
            purchase=purchase, product=self.product, quantity=Decimal('1'),
            unit_price=Decimal(amount), tax_rate=Decimal('0'))
        purchase.refresh_from_db()
        return purchase

    def _purchase_post(self, discount, **overrides):
        data = {
            'vendor': str(self.vendor.pk),
            'date': str(timezone.localdate()),
            'gst_type': 'non_gst',
            'discount_amount': discount,
            'freight_charge': '0',
            'discount_type': '',
            'discount_note': '',
            'discount_date': '',
            'notes': '',
        }
        data.update(overrides)
        return data


class InvoiceDiscountGuardTests(DiscountGuardBase):
    def test_create_rejects_discount_above_items_subtotal(self):
        self._cart('temp_invoice_items_new', '3900.00')

        resp = self.client.post(
            reverse('accounting:invoice_create'),
            self._invoice_post('5000.00'))

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Invoice.objects.exists())
        errors = resp.context['form'].non_field_errors()
        self.assertTrue(errors)
        self.assertIn('more than the items subtotal', errors[0])
        # The cart survives so the user only fixes the discount.
        self.assertEqual(
            len(self.client.session['temp_invoice_items_new']), 1)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.receivable_balance, Decimal('0.00'))

    def test_create_accepts_discount_equal_to_subtotal(self):
        self._cart('temp_invoice_items_new', '3900.00')

        resp = self.client.post(
            reverse('accounting:invoice_create'),
            self._invoice_post('3900.00'))

        self.assertEqual(resp.status_code, 302)
        invoice = Invoice.objects.get()
        self.assertEqual(invoice.grand_total, Decimal('0.00'))
        self.assertEqual(invoice.discount_amount, Decimal('3900.00'))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.receivable_balance, Decimal('0.00'))
        self._assert_ledger_balanced()

    def test_update_rejects_discount_above_items_subtotal(self):
        invoice = self._invoice('3900.00')
        self.client.get(reverse('accounting:invoice_update',
                                args=[invoice.pk]))

        resp = self.client.post(
            reverse('accounting:invoice_update', args=[invoice.pk]),
            self._invoice_post('5000.00'))

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context['form'].non_field_errors())
        invoice.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(invoice.discount_amount, Decimal('0.00'))
        self.assertEqual(invoice.grand_total, Decimal('3900.00'))
        self.assertEqual(self.customer.receivable_balance, Decimal('3900.00'))
        self._assert_ledger_balanced()

    def test_update_accepts_discount_within_subtotal(self):
        invoice = self._invoice('3900.00')
        self.client.get(reverse('accounting:invoice_update',
                                args=[invoice.pk]))

        resp = self.client.post(
            reverse('accounting:invoice_update', args=[invoice.pk]),
            self._invoice_post('400.00'))

        self.assertEqual(resp.status_code, 302)
        invoice.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(invoice.grand_total, Decimal('3500.00'))
        self.assertEqual(self.customer.receivable_balance, Decimal('3500.00'))
        self._assert_ledger_balanced()

    def test_edit_form_opens_discount_section_when_discount_exists(self):
        invoice = self._invoice('3900.00')
        invoice.discount_amount = Decimal('400.00')
        invoice.save()

        body = self.client.get(
            reverse('accounting:invoice_update', args=[invoice.pk])
        ).content.decode()

        self.assertIn('collapse mt-2 show', body)


class PurchaseDiscountGuardTests(DiscountGuardBase):
    def test_create_rejects_discount_above_items_subtotal(self):
        self._cart('temp_purchase_items_new', '3000.00')

        resp = self.client.post(
            reverse('accounting:purchase_create'),
            self._purchase_post('4000.00'))

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Purchase.objects.exists())
        self.assertTrue(resp.context['form'].non_field_errors())
        self.assertIn(
            'more than the items subtotal',
            resp.context['form'].non_field_errors()[0])
        self.assertEqual(
            len(self.client.session['temp_purchase_items_new']), 1)
        self.vendor.refresh_from_db()
        self.assertEqual(self.vendor.payable_balance, Decimal('0.00'))

    def test_create_accepts_discount_within_subtotal(self):
        self._cart('temp_purchase_items_new', '3000.00')

        resp = self.client.post(
            reverse('accounting:purchase_create'),
            self._purchase_post('300.00'))

        self.assertEqual(resp.status_code, 302)
        purchase = Purchase.objects.get()
        self.assertEqual(purchase.grand_total, Decimal('2700.00'))
        self.vendor.refresh_from_db()
        self.assertEqual(self.vendor.payable_balance, Decimal('2700.00'))
        self._assert_ledger_balanced()

    def test_update_rejects_discount_above_items_subtotal(self):
        purchase = self._purchase('3000.00')
        self.client.get(reverse('accounting:purchase_update',
                                args=[purchase.pk]))

        resp = self.client.post(
            reverse('accounting:purchase_update', args=[purchase.pk]),
            self._purchase_post('4000.00'))

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context['form'].non_field_errors())
        purchase.refresh_from_db()
        self.vendor.refresh_from_db()
        self.assertEqual(purchase.grand_total, Decimal('3000.00'))
        self.assertEqual(purchase.discount_amount, Decimal('0.00'))
        self.assertEqual(self.vendor.payable_balance, Decimal('3000.00'))
        self._assert_ledger_balanced()


class PaymentDiscountGuardTests(DiscountGuardBase):
    def _pay_post(self, invoice, amount, discount, allocation=None, **extra):
        data = {
            'direction': 'received',
            'contact': str(self.customer.pk),
            'amount': amount,
            'date': str(timezone.localdate()),
            'method': 'cash',
            'bank_account': '',
            'upi_ref': '',
            'reference': '',
            'description': '',
            'discount_amount': discount,
            'discount_type': '',
            'discount_note': '',
        }
        if allocation is not None:
            data[f'allocation_amount_{invoice.pk}'] = allocation
        data.update(extra)
        return self.client.post(
            reverse('accounting:payment_create'), data)

    def test_create_rejects_discount_above_allocated_bills(self):
        invoice = self._invoice('3900.00')

        resp = self._pay_post(invoice, '100.00', '5000.00', allocation='3900')

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Payment.objects.exists())
        self.assertTrue(resp.context['form'].non_field_errors())
        self.assertIn(
            'applied to bills',
            resp.context['form'].non_field_errors()[0])
        invoice.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(invoice.balance_due, Decimal('3900.00'))
        self.assertEqual(invoice.payment_status, 'unpaid')
        self.assertEqual(self.customer.receivable_balance, Decimal('3900.00'))

    def test_create_rejects_discount_without_any_allocation(self):
        self._invoice('3900.00')

        resp = self._pay_post(None, '1000.00', '100.00')

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Payment.objects.exists())
        self.assertTrue(resp.context['form'].non_field_errors())

    def test_create_rejects_discount_on_an_advance(self):
        resp = self._pay_post(
            None, '1000.00', '100.00', is_advance='on')

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Payment.objects.exists())
        self.assertTrue(resp.context['form'].non_field_errors())
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.receivable_balance, Decimal('0.00'))

    def test_create_accepts_cash_plus_discount_that_closes_the_bill(self):
        invoice = self._invoice('850.00')

        resp = self._pay_post(invoice, '800.00', '50.00', allocation='850.00')

        self.assertEqual(resp.status_code, 302)
        payment = Payment.objects.get()
        self.assertEqual(payment.settled_amount, Decimal('850.00'))
        invoice.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(invoice.balance_due, Decimal('0.00'))
        self.assertEqual(invoice.payment_status, 'paid')
        self.assertEqual(self.customer.receivable_balance, Decimal('0.00'))
        self._assert_ledger_balanced()

    def test_create_still_allows_cash_overpayment_to_become_credit(self):
        """A cash overpayment is a real customer credit; only an unbacked
        discount is rejected."""
        invoice = self._invoice('850.00')

        resp = self._pay_post(invoice, '900.00', '0.00', allocation='850.00')

        self.assertEqual(resp.status_code, 302)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.receivable_balance, Decimal('-50.00'))
        self._assert_ledger_balanced()

    def test_update_rejects_discount_above_allocated_bills(self):
        invoice = self._invoice('850.00')
        self._pay_post(invoice, '500.00', '0.00', allocation='500.00')
        payment = Payment.objects.get()

        resp = self.client.post(
            reverse('accounting:payment_update', args=[payment.pk]), {
                'direction': 'received',
                'contact': str(self.customer.pk),
                'amount': '500.00',
                'date': str(timezone.localdate()),
                'method': 'cash',
                'bank_account': '',
                'upi_ref': '',
                'reference': '',
                'description': '',
                'discount_amount': '900.00',
                'discount_type': '',
                'discount_note': '',
                'reset_allocations': '1',
                f'allocation_amount_{invoice.pk}': '500.00',
            })

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context['form'].non_field_errors())
        payment.refresh_from_db()
        invoice.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(payment.discount_amount, Decimal('0.00'))
        self.assertEqual(payment.amount, Decimal('500.00'))
        self.assertEqual(invoice.balance_due, Decimal('350.00'))
        self.assertEqual(self.customer.receivable_balance, Decimal('350.00'))
        self._assert_ledger_balanced()


class RepairInvoiceDiscountGuardTests(DiscountGuardBase):
    def setUp(self):
        super().setUp()
        self.job = RepairJob.objects.create(
            customer=self.customer, device_model='Guard Laptop',
            issue_description='Discount guard', status='ready')
        self._cart('temp_repair_invoice_items', '3900.00', source='service')

    def _post(self, discount):
        return self.client.post(
            reverse('accounting:create_invoice_from_repair',
                    args=[self.job.pk]), {
                'invoice_date': str(timezone.localdate()),
                'due_date': '',
                'gst_type': 'non_gst',
                'notes': '',
                'discount_amount': discount,
                'discount_type': '',
                'discount_note': '',
            })

    def test_repair_invoice_rejects_discount_above_subtotal(self):
        resp = self._post('5000.00')

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Invoice.objects.exists())
        self.job.refresh_from_db()
        self.assertIsNone(self.job.invoice_id)
        self.assertIn(
            'more than the items subtotal',
            resp.content.decode())
        # The typed discount and the cart are still on screen.
        self.assertIn('value="5000.00"', resp.content.decode())

    def test_repair_invoice_accepts_discount_within_subtotal(self):
        resp = self._post('400.00')

        self.assertEqual(resp.status_code, 302)
        invoice = Invoice.objects.get()
        self.assertEqual(invoice.grand_total, Decimal('3500.00'))
        self.job.refresh_from_db()
        self.assertEqual(self.job.invoice_id, invoice.pk)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.receivable_balance, Decimal('3500.00'))
        self._assert_ledger_balanced()
