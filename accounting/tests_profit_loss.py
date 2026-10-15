"""
Trading Account / P&L (Direct-Indirect expense) tests.

Chalane ka tareeka:
    python manage.py test accounting.tests_profit_loss -v 2

Production scenario: expenses ka ek hi flat "Expenses" group tha, isliye
P&L report me Gross Profit aur Net Profit alag-alag nahi dikhate the.
Migration 0008 se Direct (51) / Indirect (52) groups bane. Report ab:
  Trading: Sales + Repair − Direct Expenses = Gross Profit
  P&L:     Gross + Other Income − Indirect = Net Profit
Ledger sums date range ke BAHAR ke entries ko ignore karna chahiye.
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from accounting.models import Account, AccountGroup, Invoice, LedgerEntry, LedgerLine

User = get_user_model()


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class ProfitLossTradingSplitTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='pl_staff', password='TestPass123!',
            is_staff=True, is_active=True,
        )
        cls.purchase_acc = Account.objects.get(code='5011')   # Direct
        cls.rent_acc = Account.objects.get(code='5201')       # Indirect
        cls.other_income_acc = Account.objects.get(code='4012')  # Interest Income
        cls.creditors = Account.objects.filter(
            account_type='liability'
        ).first()

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)
        self._cust = None

    def make_invoice(self, total, day='2026-01-10'):
        from accounting.models import Contact
        import datetime
        self._cust = self._cust or Contact.objects.create(
            contact_type='customer', name='PL Cust', phone='9000000111',
        )
        return Invoice.objects.create(
            customer=self._cust,
            subtotal=Decimal(total), grand_total=Decimal(total),
            balance_due=Decimal(total),
            date=datetime.date.fromisoformat(day),
        )

    def make_ledger(self, entry_type, day, debit_acc=None, debit='0',
                     credit_acc=None, credit='0'):
        import datetime
        entry = LedgerEntry.objects.create(
            entry_type=entry_type,
            date=datetime.date.fromisoformat(day),
            description='test entry',
            total_amount=max(Decimal(debit), Decimal(credit)),
        )
        if debit_acc and Decimal(debit) > 0:
            LedgerLine.objects.create(
                ledger_entry=entry, account=debit_acc,
                debit=Decimal(debit), credit=Decimal('0'),
            )
        if credit_acc and Decimal(credit) > 0:
            LedgerLine.objects.create(
                ledger_entry=entry, account=credit_acc,
                debit=Decimal('0'), credit=Decimal(credit),
            )
        return entry

    # ---------- migration / chart structure ----------

    def test_expense_groups_and_accounts_migrated(self):
        direct = AccountGroup.objects.get(code='51')
        indirect = AccountGroup.objects.get(code='52')
        parent = AccountGroup.objects.get(code='5')
        self.assertEqual(direct.parent_id, parent.id)
        self.assertEqual(indirect.parent_id, parent.id)
        self.assertEqual(
            Account.objects.get(code='5011').group_id, direct.id)
        self.assertEqual(
            Account.objects.get(code='5013').group_id, indirect.id)
        self.assertGreaterEqual(
            Account.objects.filter(group=direct).count(), 8)
        self.assertGreaterEqual(
            Account.objects.filter(group=indirect).count(), 20)

    def test_migration_is_idempotent(self):
        """Do baar chalne par duplicate account/group NAHI banne chahiye."""
        import importlib
        mig = importlib.import_module(
            'accounting.migrations.0008_direct_indirect_expenses_accounts'
        )
        from django.apps import apps as global_apps
        before_groups = AccountGroup.objects.filter(code__in=['51', '52']).count()
        before_accounts = Account.objects.filter(
            group__code__in=['51', '52']).count()

        mig.create_expense_groups_and_accounts(global_apps, None)

        self.assertEqual(
            AccountGroup.objects.filter(code__in=['51', '52']).count(),
            before_groups)
        self.assertEqual(
            Account.objects.filter(group__code__in=['51', '52']).count(),
            before_accounts)

    # ---------- report maths ----------

    def test_trading_and_net_profit_split_in_range(self):
        self.make_invoice('10000.00', '2026-01-10')
        # Purchase: Dr 5011 6000 (direct) in range
        self.make_ledger('purchase', '2026-01-11',
                         debit_acc=self.purchase_acc, debit='6000',
                         credit_acc=self.creditors, credit='6000')
        # Journal: Dr 5201 Rent 1500 (indirect) in range
        self.make_ledger('journal', '2026-01-12',
                         debit_acc=self.rent_acc, debit='1500',
                         credit_acc=self.creditors, credit='1500')
        # Journal: Dr 5201 Rent 9999 OUT of range — ignore hona chahiye
        self.make_ledger('journal', '2025-12-01',
                         debit_acc=self.rent_acc, debit='9999',
                         credit_acc=self.creditors, credit='9999')
        # Other income: Cr 4012 Interest 500 in range
        self.make_ledger('journal', '2026-01-15',
                         credit_acc=self.other_income_acc, credit='500',
                         debit_acc=self.creditors, debit='500')

        resp = self.client.get(
            reverse('accounting:profit_loss')
            + '?date_from=2026-01-01&date_to=2026-01-31'
        )
        self.assertEqual(resp.status_code, 200)
        c = resp.context
        self.assertEqual(c['sales_total'], Decimal('10000.00'))
        self.assertEqual(c['total_direct_expenses'], Decimal('6000.00'))
        self.assertEqual(c['total_indirect_expenses'], Decimal('1500.00'))
        self.assertEqual(c['other_income_total'], Decimal('500.00'))
        self.assertEqual(c['gross_profit'], Decimal('4000.00'))
        self.assertEqual(c['net_profit'], Decimal('3000.00'))

    def test_no_date_filter_includes_all(self):
        self.make_ledger('journal', '2025-12-01',
                         debit_acc=self.rent_acc, debit='200',
                         credit_acc=self.creditors, credit='200')
        resp = self.client.get(reverse('accounting:profit_loss'))
        self.assertEqual(
            resp.context['total_indirect_expenses'], Decimal('200.00'))

    def test_deleted_entries_excluded(self):
        e = self.make_ledger('journal', '2026-02-01',
                             debit_acc=self.rent_acc, debit='700',
                             credit_acc=self.creditors, credit='700')
        e.delete()  # soft delete
        resp = self.client.get(
            reverse('accounting:profit_loss')
            + '?date_from=2026-02-01&date_to=2026-02-28'
        )
        self.assertEqual(resp.context['total_indirect_expenses'],
                         Decimal('0.00'))

    def test_template_shows_trading_and_pl_sections(self):
        self.make_invoice('5000.00', '2026-03-01')
        resp = self.client.get(
            reverse('accounting:profit_loss')
            + '?date_from=2026-03-01&date_to=2026-03-31'
        )
        self.assertContains(resp, 'Trading Account')
        self.assertContains(resp, 'Gross Profit')
        self.assertContains(resp, 'Indirect Expenses')
        self.assertContains(resp, 'Net Profit')
