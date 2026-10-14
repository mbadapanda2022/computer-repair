"""
Cash Book & contra entry tests.

Chalane ka tareeka:
    python manage.py test accounting.tests_cash_book -v 2

Dukaan ka asli scenario: customer se cash milta hai (cash box me aata
hai), cash bank me deposit hota hai, bank se cash nikal ke counter par
rakha jata hai. Pehle cash ka koi register nahi tha — ab Cash 1001 ledger
account hi cash book hai, aur 'transfer' BankTransaction contra entry banati
hai (Dr/Cr Cash ⇄ Bank). Payment method bhi ab decide karta hai ki ledger
side Cash (1001) hogi ya Bank (1010).
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Sum
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounting.forms import ContraForm, PaymentForm
from accounting.models import (
    BankAccount, BankTransaction, Contact, LedgerEntry, LedgerLine,
    Payment, get_cash_in_hand, get_total_bank_balance,
)

User = get_user_model()

CASH = '1001'
BANK = '1010'


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
class CashBookTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='cash_staff', password='TestPass123!',
            is_staff=True, is_active=True,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)
        suffix = timezone.now().strftime('%H%M%S%f')[:8]
        self.customer = Contact.objects.create(
            contact_type='customer', name=f'Cash Cust {suffix}',
            phone='8' + suffix,
        )
        self.bank = BankAccount.objects.create(
            name=f'BoB {suffix}', bank_name='Bank of Baroda',
            account_type='savings', opening_balance=Decimal('0.00'),
        )

    # ---------- factories ----------

    def cash_receipt(self, amount):
        return Payment.objects.create(
            direction='received', contact=self.customer,
            amount=Decimal(amount), method='cash',
        )

    def upi_receipt(self, amount):
        return Payment.objects.create(
            direction='received', contact=self.customer,
            amount=Decimal(amount), method='upi',
            bank_account=self.bank, upi_ref='TXN' + str(amount),
        )

    # ---------- payment method → ledger side (#9) ----------

    def test_cash_payment_posts_to_cash_account_not_bank(self):
        self.cash_receipt('500.00')
        self.assertEqual(get_cash_in_hand(), Decimal('500.00'))
        self.assertEqual(account_balance(BANK), Decimal('0.00'))

    def test_cash_payment_never_creates_bank_transaction(self):
        pay = Payment.objects.create(
            direction='received', contact=self.customer,
            amount=Decimal('500.00'), method='cash',
            bank_account=self.bank,  # staff galti se select kar le
        )
        self.assertFalse(
            BankTransaction.objects.filter(payment=pay).exists()
        )

    def test_upi_payment_creates_bank_deposit(self):
        self.upi_receipt('1000.00')
        txn = BankTransaction.objects.get(
            bank_account=self.bank, source_type='payment_received'
        )
        self.assertEqual(txn.transaction_type, 'deposit')
        self.assertEqual(txn.amount, Decimal('1000.00'))
        self.bank.refresh_from_db()
        self.assertEqual(self.bank.current_balance, Decimal('1000.00'))
        self.assertEqual(account_balance(BANK), Decimal('1000.00'))

    def test_payment_form_clears_bank_account_for_cash_method(self):
        form = PaymentForm(data={
            'direction': 'received', 'contact': self.customer.pk,
            'amount': '6000.00', 'date': timezone.localdate().isoformat(),
            'method': 'cash', 'bank_account': self.bank.pk,
            'discount_amount': '0', 'is_advance': False,
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.cleaned_data['bank_account'])

    # ---------- contra movements (#8) ----------

    def test_contra_deposit_moves_cash_into_bank(self):
        self.cash_receipt('5000.00')

        resp = self.client.post(reverse('accounting:contra_create'), {
            'direction': 'deposit', 'bank_account': self.bank.pk,
            'amount': '3000.00', 'date': timezone.localdate().isoformat(),
            'notes': 'Weekly deposit',
        })
        self.assertEqual(resp.status_code, 302)

        self.assertEqual(get_cash_in_hand(), Decimal('2000.00'))
        self.bank.refresh_from_db()
        self.assertEqual(self.bank.current_balance, Decimal('3000.00'))
        txn = BankTransaction.objects.get(source_type='transfer')
        self.assertEqual(txn.transaction_type, 'deposit')

        entry = LedgerEntry.objects.get(entry_type='bank_manual', reference_id=txn.pk)
        lines = {l.account.code: l for l in entry.lines.all()}
        self.assertEqual(lines[BANK].debit, Decimal('3000.00'))
        self.assertEqual(lines[CASH].credit, Decimal('3000.00'))

    def test_contra_withdrawal_brings_cash_from_bank(self):
        self.upi_receipt('4000.00')  # bank me paisa aaya

        self.client.post(reverse('accounting:contra_create'), {
            'direction': 'withdrawal', 'bank_account': self.bank.pk,
            'amount': '1500.00', 'date': timezone.localdate().isoformat(),
        })

        self.assertEqual(get_cash_in_hand(), Decimal('1500.00'))
        self.bank.refresh_from_db()
        self.assertEqual(self.bank.current_balance, Decimal('2500.00'))

    def test_contra_deposit_blocked_more_than_cash_in_hand(self):
        self.cash_receipt('500.00')
        form = ContraForm(data={
            'direction': 'deposit', 'bank_account': self.bank.pk,
            'amount': '600.00', 'date': timezone.localdate().isoformat(),
        })
        self.assertFalse(form.is_valid())
        self.assertIn('Cash in hand is only', str(form.errors['amount'][0]))

    def test_contra_withdrawal_blocked_more_than_bank_balance(self):
        form = ContraForm(data={
            'direction': 'withdrawal', 'bank_account': self.bank.pk,
            'amount': '100.00', 'date': timezone.localdate().isoformat(),
        })
        self.assertFalse(form.is_valid())
        self.assertIn('balance is only', str(form.errors['amount'][0]))

    def test_deleting_contra_txn_restores_balances(self):
        self.cash_receipt('5000.00')
        self.client.post(reverse('accounting:contra_create'), {
            'direction': 'deposit', 'bank_account': self.bank.pk,
            'amount': '2000.00', 'date': timezone.localdate().isoformat(),
        })
        txn = BankTransaction.objects.get(source_type='transfer')
        txn.delete()

        self.assertEqual(get_cash_in_hand(), Decimal('5000.00'))
        self.bank.refresh_from_db()
        self.assertEqual(self.bank.current_balance, Decimal('0.00'))
        self.assertEqual(
            LedgerLine.objects.filter(
                ledger_entry__bank_transaction=txn).count(), 0,
        )

    # ---------- cash book view (#10) ----------

    def test_cash_book_page_renders_with_running_register(self):
        today = timezone.localdate().isoformat()
        self.cash_receipt('5000.00')
        self.client.post(reverse('accounting:contra_create'), {
            'direction': 'deposit', 'bank_account': self.bank.pk,
            'amount': '2000.00', 'date': today,
        })

        page = self.client.get(reverse('accounting:cash_book'), {
            'date_from': today, 'date_to': today,
        })
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Cash Book')
        self.assertContains(page, 'Contra (Cash')
        # closing = 5000 in − 2000 contra out
        self.assertContains(page, '3000.00')

    def test_cash_book_rejects_anonymous(self):
        anon = Client()
        resp = anon.get(reverse('accounting:cash_book'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp['Location'])

    def test_contra_view_rejects_get(self):
        resp = self.client.get(reverse('accounting:contra_create'))
        self.assertEqual(resp.status_code, 405)

    def test_total_bank_balance_sums_active_accounts(self):
        self.upi_receipt('700.00')
        self.assertEqual(get_total_bank_balance(), Decimal('700.00'))
