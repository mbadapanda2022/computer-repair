"""
Payment discount semantics — one real job, walked end to end.

Run:
    venv/Scripts/python.exe manage.py test accounting.tests_payment_discount -v 2

An ₹850 monitor repair was paid with ₹800 cash and a ₹50 waiver. The shop
entered 800 and 50 because 800 is what the drawer holds, and the code then read
the discount as a subtraction: the till was booked ₹750 (₹50 short of the cash
in hand) and the bill only took ₹800, leaving a phantom ₹50 due that no edit
could clear.

So the rule these tests pin down is: ``amount`` is the money that actually
moved, a discount is granted on top of it, and the two together
(``settled_amount``) are what closes a bill. Every layer has to agree — ledger,
bank transaction, allocations, invoice status, contact balance and the screen
text — because a shop owner reconciles by hand and any one of them being out by
the discount is what makes him stop trusting the books.
"""
from __future__ import annotations

from decimal import Decimal
from urllib.parse import unquote

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounting.models import (
    Account, BankAccount, Contact, Invoice, InvoiceItem, LedgerEntry,
    Payment, PaymentAllocation, Product, Purchase,
)

User = get_user_model()


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class PaymentDiscountTests(TestCase):
    """₹800 received + ₹50 discount = an ₹850 bill that is fully paid."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='disc_staff', password='TestPass123!',
            is_staff=True, is_active=True,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)
        suffix = timezone.now().strftime('%H%M%S%f')[:8]
        self.customer = Contact.objects.create(
            contact_type='customer', name=f'Tusar Cust {suffix}',
            phone='97' + suffix,
        )
        self.vendor = Contact.objects.create(
            contact_type='vendor', name=f'Disc Vendor {suffix}',
            phone='98' + suffix,
        )
        self.bank = BankAccount.objects.create(
            name=f'Axis {suffix}', bank_name='Axis Bank',
            account_type='savings', opening_balance=Decimal('0.00'),
        )

    # ── helpers ──

    def invoice(self, amount='850.00'):
        """A real invoice carries a line — Invoice.save() derives grand_total
        from its items, so a header-only fixture would collapse to ₹0."""
        inv = Invoice.objects.create(
            customer=self.customer, date=timezone.localdate(),
        )
        product = Product.objects.create(
            name=f'Disc Svc {timezone.now().strftime("%H%M%S%f")[:12]}',
            selling_price=Decimal(amount), purchase_price=Decimal(amount),
            tax_rate=Decimal('0'), current_stock=Decimal('0'),
            is_service=True, is_active=True,
        )
        InvoiceItem.objects.create(
            invoice=inv, product=product, quantity=Decimal('1'),
            unit_price=Decimal(amount), tax_rate=Decimal('0'),
        )
        inv.refresh_from_db()
        return inv

    def purchase(self, amount='1000.00'):
        return Purchase.objects.create(
            vendor=self.vendor,
            date=timezone.localdate(),
            subtotal=Decimal(amount),
            grand_total=Decimal(amount),
        )

    def pay_from_invoice(self, inv, amount, discount='0', **kw):
        """POST the same form the payments page submits, with ?invoice= set."""
        data = {
            'direction': 'received',
            'contact': inv.customer_id,
            'amount': amount,
            'date': str(timezone.localdate()),
            'method': kw.pop('method', 'cash'),
            'bank_account': kw.pop('bank_account', ''),
            'discount_amount': discount,
            'discount_type': '',
            'discount_note': '',
            'description': '',
            'reference': '', 'upi_ref': '',
        }
        data.update(kw)
        return self.client.post(
            f'/payments/create/?invoice={inv.pk}', data)

    def lines(self, payment):
        entry = LedgerEntry.objects.get(
            reference_id=payment.pk,
            entry_type__in=['payment', 'advance_received', 'advance_paid'])
        return list(entry.lines.all())

    def total(self, payment, code, side):
        account = Account.objects.get(code=code)
        rows = [l for l in self.lines(payment) if l.account_id == account.pk]
        return sum((getattr(l, side) for l in rows), Decimal('0'))

    # ════════════════════════════════════════════════════════
    # The bill must close, and the cash must be the cash
    # ════════════════════════════════════════════════════════

    def test_cash_plus_discount_settles_the_whole_bill(self):
        inv = self.invoice()
        self.pay_from_invoice(inv, '800.00', '50.00')

        inv.refresh_from_db()
        pay = Payment.objects.get(contact=self.customer)
        self.assertEqual(pay.amount, Decimal('800.00'))
        self.assertEqual(pay.settled_amount, Decimal('850.00'))
        self.assertEqual(inv.paid_amount, Decimal('850.00'))
        self.assertEqual(inv.balance_due, Decimal('0.00'))
        self.assertEqual(inv.payment_status, 'paid')

    def test_the_till_holds_the_cash_not_the_bill(self):
        """Dr Cash = money in hand; the waiver goes to Discount Allowed."""
        inv = self.invoice()
        self.pay_from_invoice(inv, '800.00', '50.00')
        pay = Payment.objects.get(contact=self.customer)

        self.assertEqual(self.total(pay, '1001', 'debit'), Decimal('800.00'))
        self.assertEqual(self.total(pay, '5010', 'debit'), Decimal('50.00'))
        self.assertEqual(self.total(pay, '1011', 'credit'), Decimal('850.00'))

    def test_the_entry_still_balances(self):
        inv = self.invoice()
        self.pay_from_invoice(inv, '800.00', '50.00')
        pay = Payment.objects.get(contact=self.customer)
        rows = self.lines(pay)
        self.assertEqual(sum(l.debit for l in rows),
                         sum(l.credit for l in rows))

    def test_the_customer_owes_fifty_rupees_less_than_before(self):
        """Measured against the balance the customer had just before the
        payment: what the payment must do is clear the whole ₹850 bill out of
        the receivable, cash and waiver together."""
        inv = self.invoice()
        self.customer.refresh_from_db()
        before = self.customer.receivable_balance
        self.pay_from_invoice(inv, '800.00', '50.00')
        self.customer.refresh_from_db()
        self.assertEqual(before - self.customer.receivable_balance,
                         Decimal('850.00'))

    def test_the_allocation_carries_the_discount(self):
        """The bill is settled by the allocation row, so it must hold 850 —
        800 there would leave the invoice at Partial forever."""
        inv = self.invoice()
        self.pay_from_invoice(inv, '800.00', '50.00')
        pay = Payment.objects.get(contact=self.customer)
        alloc = PaymentAllocation.objects.get(payment=pay, invoice=inv)
        self.assertEqual(alloc.amount, Decimal('850.00'))

    def test_a_bank_or_upi_payment_moves_the_cash_amount(self):
        inv = self.invoice('1,000.00'.replace(',', ''))
        self.pay_from_invoice(inv, '950.00', '50.00',
                              method='upi', bank_account=self.bank.pk)
        pay = Payment.objects.get(contact=self.customer)
        pay.refresh_from_db()
        self.assertEqual(pay.bank_transaction.amount, Decimal('950.00'))

    # ════════════════════════════════════════════════════════
    # Editing has to be able to repair the record
    # ════════════════════════════════════════════════════════

    def post_update(self, pay, data, inv=None):
        body = {
            'direction': pay.direction,
            'contact': pay.contact_id,
            'amount': str(data.get('amount', pay.amount)),
            'date': str(data.get('date', pay.date)),
            'method': data.get('method', pay.method),
            'bank_account': data.get('bank_account', ''),
            'discount_amount': str(data.get('discount_amount',
                                           pay.discount_amount)),
            'discount_type': '', 'discount_note': '',
            'description': '', 'reference': '', 'upi_ref': '',
            'reset_allocations': '1',
        }
        if inv is not None and 'alloc' in data:
            body[f'allocation_amount_{inv.pk}'] = data['alloc']
        return self.client.post(f'/payments/{pay.pk}/update/', body)

    def test_edit_adding_a_discount_finishes_the_bill(self):
        inv = self.invoice()
        self.pay_from_invoice(inv, '800.00')
        pay = Payment.objects.get(contact=self.customer)
        inv.refresh_from_db()
        self.assertEqual(inv.payment_status, 'partial')
        self.assertEqual(inv.balance_due, Decimal('50.00'))

        self.post_update(pay, {'amount': '800.00', 'discount_amount': '50.00',
                               'alloc': '850.00'}, inv=inv)

        inv.refresh_from_db()
        self.assertEqual(inv.paid_amount, Decimal('850.00'))
        self.assertEqual(inv.balance_due, Decimal('0.00'))
        self.assertEqual(inv.payment_status, 'paid')

    def test_an_edit_cannot_allocate_more_than_the_payment_settles(self):
        """Stale stored dues used to clamp the fresh allocation to the old
        balance; the row must still stop at amount + discount."""
        inv = self.invoice()
        self.pay_from_invoice(inv, '800.00')
        pay = Payment.objects.get(contact=self.customer)

        self.post_update(pay, {'amount': '800.00',
                               'discount_amount': '50.00',
                               'alloc': '999.00'}, inv=inv)

        inv.refresh_from_db()
        self.assertEqual(inv.paid_amount, Decimal('850.00'))

    def test_removing_the_discount_trims_the_bill_back(self):
        inv = self.invoice()
        self.pay_from_invoice(inv, '800.00', '50.00')
        pay = Payment.objects.get(contact=self.customer)

        pay.discount_amount = Decimal('0.00')
        pay.save()
        pay.refresh_from_db()
        inv.refresh_from_db()

        self.assertEqual(
            PaymentAllocation.objects.get(payment=pay).amount,
            Decimal('800.00'))
        self.assertEqual(inv.paid_amount, Decimal('800.00'))
        self.assertEqual(inv.balance_due, Decimal('50.00'))
        self.assertEqual(inv.payment_status, 'partial')

    def test_a_bigger_cash_amount_does_not_need_trimming(self):
        inv = self.invoice()
        self.pay_from_invoice(inv, '500.00', '50.00')
        pay = Payment.objects.get(contact=self.customer)

        pay.amount = Decimal('850.00')
        pay.discount_amount = Decimal('0.00')
        pay.save()

        self.assertEqual(
            PaymentAllocation.objects.get(payment=pay).amount,
            Decimal('550.00'))

    # ════════════════════════════════════════════════════════
    # The vendor side mirrors it
    # ════════════════════════════════════════════════════════

    def test_cash_plus_waiver_settles_a_vendor_bill(self):
        bill = self.purchase('1000.00')
        resp = self.client.post(
            f'/payments/create/?purchase={bill.pk}', {
                'direction': 'paid', 'contact': self.vendor.pk,
                'amount': '950.00', 'discount_amount': '50.00',
                'date': str(timezone.localdate()), 'method': 'cash',
                'bank_account': '', 'discount_type': '', 'discount_note': '',
                'description': '', 'reference': '', 'upi_ref': '',
            })
        self.assertEqual(resp.status_code, 302, resp.content[:400])

        bill.refresh_from_db()
        pay = Payment.objects.get(contact=self.vendor)
        self.assertEqual(bill.paid_amount, Decimal('1000.00'))
        self.assertEqual(bill.balance_due, Decimal('0.00'))
        self.assertTrue(bill.paid)
        # Paid to a supplier: cash out is the money, income is the waiver.
        self.assertEqual(self.total(pay, '1001', 'credit'), Decimal('950.00'))
        self.assertEqual(self.total(pay, '4011', 'credit'), Decimal('50.00'))

    # ════════════════════════════════════════════════════════
    # An advance is still only the money banked
    # ════════════════════════════════════════════════════════

    def test_an_advance_pools_only_its_cash(self):
        pay = Payment.objects.create(
            direction='received', contact=self.customer,
            amount=Decimal('1000.00'), discount_amount=Decimal('100.00'),
            method='cash', is_advance=True, date=timezone.localdate())

        self.assertEqual(pay.allocation_pool, Decimal('1000.00'))
        self.assertEqual(self.total(pay, '1012', 'credit'), Decimal('1000.00'))
        # The waiver still reduces what the customer owes.
        self.assertEqual(self.total(pay, '1011', 'credit'), Decimal('100.00'))
        self.assertEqual(self.total(pay, '5010', 'debit'), Decimal('100.00'))

    # ════════════════════════════════════════════════════════
    # Screens must say the same thing the ledger says
    # ════════════════════════════════════════════════════════

    def test_the_list_row_reads_settles_not_net(self):
        inv = self.invoice()
        self.pay_from_invoice(inv, '800.00', '50.00')
        body = self.client.get('/payments/').content.decode()
        self.assertIn('Settles', body)
        self.assertNotIn('Net ₹750', body)

    def test_the_detail_modal_shows_the_three_numbers(self):
        inv = self.invoice()
        self.pay_from_invoice(inv, '800.00', '50.00')
        pay = Payment.objects.get(contact=self.customer)
        body = self.client.get(reverse('accounting:payment_detail_modal',
                                       args=[pay.pk])).content.decode()
        self.assertIn('Settles on bills', body)
        self.assertIn('850', body)
        self.assertNotIn('Net Amount', body)

    def test_the_whatsapp_receipt_tells_the_customer_the_waiver(self):
        inv = self.invoice()
        self.pay_from_invoice(inv, '800.00', '50.00')
        pay = Payment.objects.get(contact=self.customer)
        msg = unquote(pay.whatsapp_share_url)
        self.assertIn('Amount: Rs.800.00', msg)
        self.assertIn('Discount: Rs.50.00', msg)
        self.assertIn('Total settled: Rs.850.00', msg)
        self.assertNotIn('Net:', msg)

    def test_the_form_offers_no_way_to_enter_it_wrong(self):
        """The amount box is labelled for cash in hand, and the third box
        shows what it settles — the old 'Net Amount' reading invited the very
        mistake this file exists to prevent."""
        body = self.client.get('/payments/create/').content.decode()
        self.assertIn('Amount received / paid', body)
        self.assertIn('settled-amount-display', body)
        self.assertNotIn('net-amount-display', body)
