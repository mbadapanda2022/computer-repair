"""Regression tests for the money-accuracy and journal UX fixes.

Covers:
  * P&L revenue is GST-exclusive (invoice subtotal, not grand total).
  * Invoiced repair services are not counted twice in P&L.
  * Credit notes reduce the invoice due and restore it when removed.
  * Vendor opening balance is not double-counted for ``both`` contacts.
  * Bank statement closing balance ignores display-only filters.
  * Journal edit preserves the selected bank account (cash ⇄ bank).
  * Journal HTMX create retargets the table and keeps the Contact column.

Run:
    python manage.py test accounting.tests_money_fixes -v 2
"""
import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounting.models import (
    BankAccount,
    BankTransaction,
    Contact,
    CreditNote,
    CreditNoteItem,
    Invoice,
    InvoiceItem,
    LedgerEntry,
    LedgerLine,
    Product,
    RepairJob,
    RepairService,
)
from accounting.views.reports import _profit_loss_totals
from accounting.views.statements import _get_vendor_opening_balance

User = get_user_model()

TODAY = timezone.localdate()


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class MoneyBaseTestCase(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='money_staff', password='TestPass123!',
            is_staff=True, is_active=True,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)
        self.customer = Contact.objects.create(
            contact_type='customer', name='Money Customer', phone='9000000901',
        )
        self.service_product = Product.objects.create(
            name='Labour', is_service=True, selling_price=Decimal('0'),
            purchase_price=Decimal('0'), current_stock=Decimal('0'),
            tax_rate=Decimal('0'),
        )
        self.goods_product = Product.objects.create(
            name='Spare Part', is_service=False, selling_price=Decimal('1000'),
            purchase_price=Decimal('800'), current_stock=Decimal('50'),
            tax_rate=Decimal('18'),
        )

    def make_invoice(self, unit_price='1000.00', tax_rate='18.00'):
        invoice = Invoice.objects.create(
            customer=self.customer, date=TODAY, gst_type='regular',
        )
        InvoiceItem.objects.create(
            invoice=invoice, product=self.goods_product,
            quantity=Decimal('1'), unit_price=Decimal(unit_price),
            tax_rate=Decimal(tax_rate),
        )
        invoice.refresh_from_db()
        return invoice


class ProfitLossRevenueTests(MoneyBaseTestCase):

    def test_sales_are_gst_exclusive(self):
        invoice = self.make_invoice()
        self.assertEqual(invoice.subtotal, Decimal('1000.00'))
        self.assertEqual(invoice.grand_total, Decimal('1180.00'))

        figures = _profit_loss_totals()
        self.assertEqual(figures['sales_total'], Decimal('1000.00'))

    def test_invoiced_repair_is_not_double_counted(self):
        # Standalone repair (billed directly, no invoice) → P&L income.
        standalone = RepairJob.objects.create(
            customer=self.customer, device_model='Laptop',
            issue_description='No display',
        )
        RepairService.objects.create(
            repair_job=standalone, product=self.service_product,
            amount=Decimal('500.00'),
        )

        # Repair converted to an invoice → invoice subtotal already covers it.
        invoice = self.make_invoice()
        invoiced_job = RepairJob.objects.create(
            customer=self.customer, device_model='Desktop',
            issue_description='Slow boot',
        )
        RepairService.objects.create(
            repair_job=invoiced_job, product=self.service_product,
            amount=Decimal('700.00'),
        )
        # Link via queryset.update: the invoice-consistency guard blocks
        # adding lines to an already-invoiced job by design.
        RepairJob.objects.filter(pk=invoiced_job.pk).update(invoice=invoice)

        figures = _profit_loss_totals()
        self.assertEqual(figures['repair_income'], Decimal('500.00'))

    def test_date_range_excludes_other_periods(self):
        invoice = self.make_invoice()
        Invoice.objects.filter(pk=invoice.pk).update(
            date=TODAY.replace(year=TODAY.year - 1),
        )
        figures = _profit_loss_totals(date_from=TODAY, date_to=TODAY)
        self.assertEqual(figures['sales_total'], Decimal('0'))


class CreditNoteDueTests(MoneyBaseTestCase):

    def test_credit_note_reduces_due_and_restores_on_delete(self):
        invoice = self.make_invoice()   # 1000 + 180 GST = 1180
        self.assertEqual(invoice.balance_due, Decimal('1180.00'))

        credit_note = CreditNote.objects.create(
            invoice=invoice, customer=self.customer, date=TODAY,
            reason='sales_return', is_stock_return=False,
        )
        CreditNoteItem.objects.create(
            credit_note=credit_note, product=self.service_product,
            quantity_returned=Decimal('1'), unit_price=Decimal('500.00'),
            tax_rate=Decimal('18.00'),
        )

        invoice.refresh_from_db()
        self.assertEqual(invoice.balance_due, Decimal('590.00'))
        self.assertEqual(invoice.payment_status, 'partial')

        credit_note.delete()
        invoice.refresh_from_db()
        self.assertEqual(invoice.balance_due, Decimal('1180.00'))
        self.assertEqual(invoice.payment_status, 'unpaid')


class VendorOpeningTests(TestCase):

    def test_both_contact_opening_not_double_counted(self):
        both = Contact.objects.create(
            contact_type='both', name='Both Party', phone='9000000902',
            opening_balance=Decimal('5000.00'),
        )
        self.assertEqual(_get_vendor_opening_balance(both), Decimal('0'))

        vendor = Contact.objects.create(
            contact_type='vendor', name='Pure Vendor', phone='9000000903',
            opening_balance=Decimal('5000.00'),
        )
        self.assertEqual(_get_vendor_opening_balance(vendor), Decimal('5000.00'))


class BankStatementFilterTests(MoneyBaseTestCase):

    def test_display_filter_keeps_true_closing_balance(self):
        account = BankAccount.objects.create(
            name='Test Bank', opening_balance=Decimal('1000.00'),
        )
        BankTransaction.objects.create(
            bank_account=account, transaction_type='deposit',
            source_type='manual', amount=Decimal('1000.00'), date=TODAY,
            description='Cash deposit',
        )
        BankTransaction.objects.create(
            bank_account=account, transaction_type='withdrawal',
            source_type='manual', amount=Decimal('400.00'), date=TODAY,
            description='Shop expense',
        )

        resp = self.client.get(
            reverse('accounting:bank_statement', args=[account.pk]),
            {'txn_type': 'deposit'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['closing_balance'], Decimal('1600.00'))
        self.assertEqual(len(resp.context['statement_lines']), 1)


class JournalEditTests(MoneyBaseTestCase):

    def setUp(self):
        super().setUp()
        self.bank = BankAccount.objects.create(
            name='HDFC Current', account_number='1234567890',
        )

    def _create_journal(self, money_account='bank'):
        payload = {
            'contact': self.customer.pk,
            'entry_type': 'advance_received',
            'money_account': money_account,
            'amount': '2500.00',
            'date': str(TODAY),
            'narration': 'Advance from customer',
        }
        if money_account == 'bank':
            payload['bank_account'] = self.bank.pk
        return self.client.post(
            reverse('accounting:journal_create'),
            payload,
            HTTP_HX_REQUEST='true',
        )

    def test_htmx_create_retargets_table_and_shows_contact(self):
        response = self._create_journal()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['HX-Retarget'], '#journal-table-container')

        content = response.content.decode()
        self.assertIn('Money Customer', content)

    def test_bank_account_survives_edit(self):
        self._create_journal(money_account='bank')
        entry = LedgerEntry.objects.get(entry_type='journal')
        self.assertEqual(entry.bank_account_id, self.bank.pk)
        self.assertTrue(entry.lines.filter(account__code='1010').exists())

        # The edit form must pre-select the stored bank account.
        edit_url = reverse('accounting:journal_update', args=[entry.pk])
        form_resp = self.client.get(edit_url, HTTP_HX_REQUEST='true')
        self.assertContains(
            form_resp, f'value="{self.bank.pk}" selected', html=False,
        )

        # Saving without touching the money account must keep it as bank.
        update_payload = {
            'contact': self.customer.pk,
            'entry_type': 'advance_received',
            'money_account': 'bank',
            'bank_account': self.bank.pk,
            'amount': '2500.00',
            'date': str(TODAY),
            'narration': 'Advance from customer',
        }
        self.client.post(edit_url, update_payload, HTTP_HX_REQUEST='true')

        entry.refresh_from_db()
        self.assertEqual(entry.bank_account_id, self.bank.pk)
        self.assertTrue(entry.lines.filter(account__code='1010').exists())

    def test_switching_to_cash_clears_bank_account(self):
        self._create_journal(money_account='bank')
        entry = LedgerEntry.objects.get(entry_type='journal')

        self.client.post(
            reverse('accounting:journal_update', args=[entry.pk]),
            {
                'contact': self.customer.pk,
                'entry_type': 'advance_received',
                'money_account': 'cash',
                'amount': '2500.00',
                'date': str(TODAY),
                'narration': 'Advance from customer',
            },
            HTTP_HX_REQUEST='true',
        )

        entry.refresh_from_db()
        self.assertIsNone(entry.bank_account_id)
        self.assertTrue(entry.lines.filter(account__code='1001').exists())
        self.assertFalse(entry.lines.filter(account__code='1010').exists())

    def test_create_for_contact_redirects_to_statement(self):
        response = self.client.post(
            reverse('accounting:journal_create_for_contact',
                    args=[self.customer.pk]),
            {
                'contact': self.customer.pk,
                'entry_type': 'advance_received',
                'money_account': 'cash',
                'amount': '100.00',
                'date': str(TODAY),
                'narration': 'Advance',
            },
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response['HX-Redirect'],
            reverse('accounting:customer_statement',
                    args=[self.customer.pk]),
        )
