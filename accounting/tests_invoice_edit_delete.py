"""
Invoice edit / delete / duplicate regression tests.

Run:
    venv/Scripts/python.exe manage.py test accounting.tests_invoice_edit_delete -v 2

Same defect the purchase bills had: every InvoiceItem save/delete rebuilt the
whole sales ledger and recalculated the customer balance, and invoice_update
deletes then re-creates every line. A 5-line invoice edit ran 19 rebuilds /
1134 SQL statements (754 / 13 for a delete), which on the production Postgres
pooler runs past the platform request limit and answers "Internal Server
Error" even though the same bill edits fine on local SQLite.
"""
from __future__ import annotations

from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounting import models
from accounting.models import (
    Contact, Invoice, InvoiceItem, LedgerEntry, Product,
)

User = get_user_model()


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class InvoiceSyncBatchingTests(TestCase):

    EDIT_BUDGET = 250
    DELETE_BUDGET = 200
    PER_LINE_BUDGET = 30

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='isb_staff', password='TestPass123!',
            is_staff=True, is_active=True,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)
        suffix = timezone.now().strftime('%H%M%S%f')[:8]
        self.customer = Contact.objects.create(
            contact_type='customer', name=f'ISB Customer {suffix}',
            phone='96' + suffix,
        )

    def _bill(self, line_count):
        invoice = Invoice.objects.create(
            customer=self.customer, date=timezone.localdate(),
            gst_type='regular',
        )
        products = []
        for i in range(line_count):
            price = Decimal('1000.00') + Decimal(i) * Decimal('50.00')
            product = Product.objects.create(
                name=f'ISB Product {i} {timezone.now().strftime("%H%M%S%f")[:8]}',
                selling_price=price, purchase_price=price,
                tax_rate=Decimal('18.00'), current_stock=Decimal('100.00'),
                is_service=False, is_active=True,
            )
            products.append(product)
            InvoiceItem.objects.create(
                invoice=invoice, product=product, quantity=Decimal('1.00'),
                unit_price=price, tax_rate=Decimal('18.00'),
            )
        invoice.calculate_totals()
        invoice.save()
        return invoice, products

    def _payload(self, invoice):
        return {
            'customer': self.customer.pk, 'date': str(invoice.date),
            'due_date': '', 'gst_type': invoice.gst_type,
            'discount_amount': str(invoice.discount_amount),
            'discount_type': '', 'discount_note': '', 'discount_date': '',
            'notes': '',
        }

    def _edit(self, invoice):
        self.client.get(reverse('accounting:invoice_update', args=[invoice.pk]))
        return self.client.post(
            reverse('accounting:invoice_update', args=[invoice.pk]),
            self._payload(invoice),
        )

    def _assert_balanced(self, invoice):
        entry = LedgerEntry.objects.get(
            entry_type='sales', reference_id=invoice.pk)
        debit = sum((l.debit for l in entry.lines.all()), Decimal('0'))
        credit = sum((l.credit for l in entry.lines.all()), Decimal('0'))
        self.assertEqual(debit, credit)

    def test_edit_rebuilds_ledger_once(self):
        invoice, _ = self._bill(5)
        with mock.patch.object(
            models, 'sync_invoice_ledger', wraps=models.sync_invoice_ledger,
        ) as sync:
            with CaptureQueriesContext(connection) as ctx:
                resp = self._edit(invoice)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(sync.call_count, 1)
        self.assertLessEqual(len(ctx), self.EDIT_BUDGET)
        invoice.refresh_from_db()
        self.assertEqual(invoice.items.count(), 5)
        self._assert_balanced(invoice)

    def test_delete_rebuilds_ledger_at_most_once(self):
        invoice, products = self._bill(5)
        with mock.patch.object(
            models, 'sync_invoice_ledger', wraps=models.sync_invoice_ledger,
        ) as sync:
            with CaptureQueriesContext(connection) as ctx:
                resp = self.client.delete(
                    reverse('accounting:invoice_delete', args=[invoice.pk]),
                    HTTP_HX_REQUEST='true',
                )
        self.assertEqual(resp.status_code, 200)
        self.assertLessEqual(sync.call_count, 1)
        self.assertLessEqual(len(ctx), self.DELETE_BUDGET)
        self.assertFalse(Invoice.objects.filter(pk=invoice.pk).exists())
        self.assertFalse(LedgerEntry.objects.filter(
            entry_type='sales', reference_id=invoice.pk).exists())
        for product in products:
            product.refresh_from_db()
            self.assertEqual(product.current_stock, Decimal('100.00'))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.balance, Decimal('0.00'))

    def test_duplicate_rebuilds_ledger_once(self):
        invoice, _ = self._bill(5)
        with mock.patch.object(
            models, 'sync_invoice_ledger', wraps=models.sync_invoice_ledger,
        ) as sync:
            resp = self.client.post(
                reverse('accounting:invoice_duplicate', args=[invoice.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(sync.call_count, 1)
        clone = Invoice.objects.exclude(pk=invoice.pk).get()
        self.assertEqual(clone.items.count(), 5)
        self.assertEqual(clone.grand_total, invoice.grand_total)
        self._assert_balanced(clone)

    def test_each_extra_line_costs_a_bounded_number_of_queries(self):
        small, _ = self._bill(2)
        with CaptureQueriesContext(connection) as small_ctx:
            self.assertEqual(self._edit(small).status_code, 302)
        large, _ = self._bill(8)
        with CaptureQueriesContext(connection) as large_ctx:
            self.assertEqual(self._edit(large).status_code, 302)
        marginal = (len(large_ctx) - len(small_ctx)) / 6
        self.assertLessEqual(marginal, self.PER_LINE_BUDGET)

    def test_edit_removing_every_line_clears_the_ledger(self):
        invoice, products = self._bill(2)
        self.assertTrue(LedgerEntry.objects.filter(
            entry_type='sales', reference_id=invoice.pk).exists())
        # Empty the session the same way the UI's remove buttons do, then
        # POST without a fresh GET (a GET would re-seed it from the DB).
        session = self.client.session
        session['temp_invoice_items'] = []
        session.save()
        resp = self.client.post(
            reverse('accounting:invoice_update', args=[invoice.pk]),
            self._payload(invoice),
        )
        self.assertEqual(resp.status_code, 302)
        invoice.refresh_from_db()
        self.assertEqual(invoice.items.count(), 0)
        self.assertEqual(invoice.grand_total, Decimal('0.00'))
        self.assertFalse(LedgerEntry.objects.filter(
            entry_type='sales', reference_id=invoice.pk).exists())
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.balance, Decimal('0.00'))
        for product in products:
            product.refresh_from_db()
            self.assertEqual(product.current_stock, Decimal('100.00'))