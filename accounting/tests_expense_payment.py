"""
Direct-expense payment tests (Payment.expense_account).

Run:
    python manage.py test accounting.tests_expense_payment -v 2

Real shop scenario: staff must pay 3 months of shop rent (₹4500) via
cash/NEFT/UPI, but there is no vendor bill/invoice. The first route,
Payment → Paid to Supplier, only debited Vendor Payable (2011) — expense
accounts (5201 Rent…) were unreachable from the staff UI. Now Payment
(direction=paid) has an optional 'expense_account': it posts Dr Expense /
Cr Cash|Bank directly.
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Sum
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounting.forms import PaymentForm
from accounting.models import (
    Account, Contact, LedgerEntry, LedgerLine, Payment, get_cash_in_hand,
)

User = get_user_model()

CASH = '1001'
BANK = '1010'
VENDOR_PAYABLE = '2011'


def account_balance(code: str) -> Decimal:
    agg = LedgerLine.objects.filter(
        account__code=code,
        is_deleted=False,
        ledger_entry__is_deleted=False,
    ).aggregate(debit=Sum('debit'), credit=Sum('credit'))
    return (agg['debit'] or Decimal('0')) - (agg['credit'] or Decimal('0'))


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class ExpensePaymentTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='exp_staff', password='TestPass123!',
            is_staff=True, is_active=True,
        )
        cls.rent = Account.objects.get(code='5201')       # Indirect
        cls.purchases = Account.objects.get(code='5011')  # Direct

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)
        suffix = timezone.now().strftime('%H%M%S%f')[:8]
        self.vendor = Contact.objects.create(
            contact_type='vendor', name=f'Malkhan Landlord {suffix}',
            phone='7' + suffix,
        )

    def expense_payment(self, amount='4500.00', method='cash',
                        account=None, date=None, **kwargs):
        return Payment.objects.create(
            direction='paid', contact=self.vendor,
            amount=Decimal(amount), method=method,
            expense_account=account or self.rent,
            date=date or timezone.localdate(),
            **kwargs,
        )

    def entry_lines(self, payment):
        entry = LedgerEntry.objects.get(reference_id=payment.id)
        return {
            line.account.code: (line.debit, line.credit)
            for line in entry.lines.all()
        }

    # ---------- ledger posting ----------

    def test_cash_expense_posts_dr_expense_cr_cash(self):
        before = get_cash_in_hand()
        pay = self.expense_payment('4500.00')
        lines = self.entry_lines(pay)
        self.assertEqual(lines['5201'], (Decimal('4500.00'), Decimal('0.00')))
        self.assertEqual(lines['1001'], (Decimal('0.00'), Decimal('4500.00')))
        self.assertEqual(len(lines), 2)
        self.assertEqual(get_cash_in_hand(), before - Decimal('4500.00'))

    def test_upi_expense_credits_bank_not_cash(self):
        pay = self.expense_payment('4500.00', method='upi')
        lines = self.entry_lines(pay)
        self.assertEqual(lines['1010'], (Decimal('0.00'), Decimal('4500.00')))
        self.assertNotIn('1001', lines)

    def test_no_vendor_payable_and_no_contact_on_expense_lines(self):
        pay = self.expense_payment()
        lines = self.entry_lines(pay)
        self.assertNotIn(VENDOR_PAYABLE, lines)
        entry = LedgerEntry.objects.get(reference_id=pay.id)
        self.assertEqual(
            entry.lines.filter(contact=self.vendor).count(), 0)

    def test_entry_type_and_description(self):
        pay = self.expense_payment()
        entry = LedgerEntry.objects.get(reference_id=pay.id)
        self.assertEqual(entry.entry_type, 'payment')
        self.assertIn('Expense: Rent', entry.description)
        self.assertIn(self.vendor.name, entry.description)

    # ---------- edit / delete ----------

    def test_edit_expense_payment_reposts_ledger(self):
        pay = self.expense_payment('4500.00', account=self.rent)
        pay.expense_account = self.purchases
        pay.amount = Decimal('5000.00')
        pay.save()
        lines = self.entry_lines(pay)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines['5011'], (Decimal('5000.00'), Decimal('0.00')))
        self.assertNotIn('5201', lines)

    def test_soft_deleted_expense_leaves_ledger_deleted(self):
        pay = self.expense_payment()
        pay.delete()
        # Default manager (live rows) should no longer see the entry...
        self.assertIsNone(LedgerEntry.objects.filter(reference_id=pay.id).first())
        # ...and the stored row must carry the soft-delete flag (no ghost).
        raw = LedgerEntry.all_objects.filter(reference_id=pay.id).first()
        self.assertTrue(raw.is_deleted,
                        "Soft-deleted payment ka ledger entry bhi delete hona chahiye")
        self.assertEqual(account_balance('5201'), Decimal('0.00'))

    # ---------- P&L report ----------

    def test_indirect_expense_payment_in_pl(self):
        self.expense_payment('4500.00')
        resp = self.client.get(
            reverse('accounting:profit_loss')
            + f'?date_from={timezone.localdate()}&date_to={timezone.localdate()}'
        )
        self.assertEqual(
            resp.context['total_indirect_expenses'], Decimal('4500.00'))

    def test_direct_expense_payment_in_trading(self):
        # Verifies P&L direct query counts 'payment' entry debits too
        self.expense_payment('2000.00', account=self.purchases)
        resp = self.client.get(reverse('accounting:profit_loss'))
        self.assertEqual(
            resp.context['total_direct_expenses'], Decimal('2000.00'))

    def test_out_of_range_expense_excluded(self):
        import datetime
        self.expense_payment(
            '4500.00', date=datetime.date(2020, 1, 1))
        resp = self.client.get(
            reverse('accounting:profit_loss')
            + '?date_from=2026-01-01&date_to=2026-12-31'
        )
        self.assertEqual(
            resp.context['total_indirect_expenses'], Decimal('0.00'))

    # ---------- form validation ----------

    def base_form_data(self, **overrides):
        data = {
            'direction': 'paid', 'contact': self.vendor.pk,
            'amount': '4500.00', 'date': timezone.localdate().isoformat(),
            'method': 'cash', 'discount_amount': '0',
        }
        data.update(overrides)
        return data

    def test_form_accepts_paid_with_expense_account(self):
        form = PaymentForm(data=self.base_form_data(
            expense_account=self.rent.pk))
        self.assertTrue(form.is_valid(), form.errors.as_json())
        pay = form.save()
        self.assertEqual(pay.expense_account_id, self.rent.pk)

    def test_form_rejects_expense_on_received(self):
        form = PaymentForm(data=self.base_form_data(
            direction='received', expense_account=self.rent.pk))
        self.assertFalse(form.is_valid())
        self.assertIn('expense_account', form.errors)

    def test_form_rejects_expense_with_advance(self):
        form = PaymentForm(data=self.base_form_data(
            is_advance=True, expense_account=self.rent.pk))
        self.assertFalse(form.is_valid())
        self.assertIn('expense_account', form.errors)

    def test_form_rejects_expense_with_discount(self):
        form = PaymentForm(data=self.base_form_data(
            discount_amount='100', expense_account=self.rent.pk))
        self.assertFalse(form.is_valid())
        self.assertIn('expense_account', form.errors)

    def test_normal_vendor_payment_still_works_without_expense(self):
        form = PaymentForm(data=self.base_form_data())
        self.assertTrue(form.is_valid(), form.errors.as_json())
        pay = form.save()
        lines = self.entry_lines(pay)
        self.assertIn(VENDOR_PAYABLE, lines)

    # ---------- create view round-trip ----------

    def test_create_form_shows_expense_account_select(self):
        resp = self.client.get(reverse('accounting:payment_create'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id_expense_account')
        self.assertContains(resp, 'Rent - Shop/Office')

    def test_create_view_expense_payment_no_allocations(self):
        resp = self.client.post(reverse('accounting:payment_create'), {
            'direction': 'paid', 'contact': self.vendor.pk,
            'amount': '4500.00', 'date': timezone.localdate().isoformat(),
            'method': 'cash', 'discount_amount': '0',
            'expense_account': self.rent.pk, 'reset_allocations': '1',
        })
        self.assertEqual(resp.status_code, 302)
        pay = Payment.objects.get(contact=self.vendor, amount=Decimal('4500.00'))
        self.assertEqual(pay.allocations.count(), 0)
        self.assertEqual(account_balance('5201'), Decimal('4500.00'))
