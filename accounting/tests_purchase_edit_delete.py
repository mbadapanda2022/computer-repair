"""
Purchase edit / delete regression tests.

Run:
    venv/Scripts/python.exe manage.py test accounting.tests_purchase_edit_delete -v 2

The owner reported errors when editing and deleting purchase bills. Two
root causes are pinned here:

1. Fractional line amounts (e.g. 0.5 kg x Rs.5.01 = 2.505) were posted to
   the ledger as raw unrounded debits while the vendor credit used the
   rounded header subtotal. LedgerEntry.full_clean() then rejected the
   entry ("Debit and Credit must be equal"), which aborted every save /
   edit / delete of such a bill. Per-item amounts must be rounded the
   same way everywhere (line row, subtotal, ledger) so all three agree.

2. An explicitly entered 0% tax or Rs.0 price was silently replaced by
   the product's defaults on every PurchaseItem.save(), so exempt goods
   came back taxed after an edit.

3. Every line save/delete rebuilt the whole ledger and recalculated the
   vendor balance, so editing a 5-line bill ran 19 rebuilds / 906 SQL
   statements. On local SQLite that is merely slow; against the
   production Postgres pooler it ran past the platform's request limit,
   the worker was killed and the browser got "Internal Server Error".
   Multi-line callers must batch the rebuild (defer_purchase_sync).
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
    Contact, LedgerEntry, Product, Purchase, PurchaseItem,
)

User = get_user_model()


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class PurchaseEditDeleteTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='ped_staff', password='TestPass123!',
            is_staff=True, is_active=True,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)
        suffix = timezone.now().strftime('%H%M%S%f')[:8]
        self.vendor = Contact.objects.create(
            contact_type='vendor', name=f'PED Vendor {suffix}',
            phone='98' + suffix,
        )

    def _product(self, price='5.01', tax='0.00'):
        return Product.objects.create(
            name=f'PED Product {timezone.now().strftime("%H%M%S%f")[:8]}',
            selling_price=Decimal('10.00'),
            purchase_price=Decimal(price),
            current_stock=Decimal('0.00'),
            is_service=False, is_active=True,
            tax_rate=Decimal(tax),
        )

    def _purchase_with_items(self, lines):
        purchase = Purchase.objects.create(
            vendor=self.vendor, date=timezone.localdate(),
        )
        for product, qty, price, tax in lines:
            PurchaseItem.objects.create(
                purchase=purchase, product=product,
                quantity=Decimal(qty), unit_price=Decimal(price),
                tax_rate=Decimal(tax),
            )
        purchase.calculate_totals()
        purchase.save()
        return purchase

    def _form_post(self, purchase, **extra):
        data = {
            'vendor': self.vendor.pk,
            'date': str(purchase.date),
            'gst_type': purchase.gst_type,
            'discount_amount': str(purchase.discount_amount),
            'freight_charge': str(purchase.freight_charge),
            'discount_type': '', 'discount_note': '', 'discount_date': '',
            'notes': '',
        }
        data.update(extra)
        return self.client.post(
            reverse('accounting:purchase_update', args=[purchase.pk]), data,
        )

    # ════════════════════════════════════════════════════════
    # Fractional amounts must not break the ledger
    # ════════════════════════════════════════════════════════

    def test_fractional_line_amount_saves_and_balances(self):
        product = self._product()
        purchase = self._purchase_with_items([(product, '0.5', '5.01', '0')])
        purchase.refresh_from_db()
        entry = LedgerEntry.objects.get(
            entry_type='purchase', reference_id=purchase.pk)
        debit = sum((l.debit for l in entry.lines.all()), Decimal('0'))
        credit = sum((l.credit for l in entry.lines.all()), Decimal('0'))
        self.assertEqual(debit, credit)
        self.assertEqual(purchase.grand_total, Decimal('2.51'))

    def test_two_fractional_lines_balance(self):
        product = self._product()
        purchase = self._purchase_with_items([
            (product, '0.5', '5.01', '0'),
            (product, '0.5', '5.01', '0'),
        ])
        entry = LedgerEntry.objects.get(
            entry_type='purchase', reference_id=purchase.pk)
        debit = sum((l.debit for l in entry.lines.all()), Decimal('0'))
        credit = sum((l.credit for l in entry.lines.all()), Decimal('0'))
        self.assertEqual(debit, credit)

    def test_edit_view_with_fractional_items(self):
        product = self._product()
        purchase = self._purchase_with_items([(product, '0.5', '5.01', '0')])
        # GET seeds the session items, exactly like the browser flow
        resp = self.client.get(
            reverse('accounting:purchase_update', args=[purchase.pk]))
        self.assertEqual(resp.status_code, 200)
        resp = self._form_post(purchase, freight_charge='10.00')
        self.assertRedirects(
            resp, reverse('accounting:purchase_detail', args=[purchase.pk]),
            fetch_redirect_response=True,
        )
        purchase.refresh_from_db()
        self.assertEqual(purchase.freight_charge, Decimal('10.00'))

    def test_delete_view_with_fractional_items(self):
        product = self._product()
        purchase = self._purchase_with_items([(product, '0.5', '5.01', '0')])
        resp = self.client.delete(
            reverse('accounting:purchase_delete', args=[purchase.pk]),
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Purchase.objects.filter(pk=purchase.pk).exists())
        self.assertFalse(LedgerEntry.objects.filter(
            entry_type='purchase', reference_id=purchase.pk).exists())

    # ════════════════════════════════════════════════════════
    # Explicit zeros must survive a save
    # ════════════════════════════════════════════════════════

    def test_zero_tax_rate_is_not_overwritten_by_product_default(self):
        product = self._product(price='100.00', tax='18.00')
        purchase = self._purchase_with_items([(product, '1', '100.00', '0')])
        item = purchase.items.get()
        self.assertEqual(item.tax_rate, Decimal('0'))
        self.assertEqual(item.tax_amount, Decimal('0.00'))

    def test_zero_price_is_not_overwritten_by_product_default(self):
        product = self._product(price='100.00', tax='0.00')
        purchase = self._purchase_with_items([(product, '1', '0.00', '0')])
        item = purchase.items.get()
        self.assertEqual(item.unit_price, Decimal('0.00'))

    def test_edit_preserves_zero_tax(self):
        product = self._product(price='100.00', tax='18.00')
        purchase = self._purchase_with_items([(product, '1', '100.00', '0')])
        self.client.get(
            reverse('accounting:purchase_update', args=[purchase.pk]))
        resp = self._form_post(purchase)
        self.assertEqual(resp.status_code, 302)
        item = purchase.items.get()
        self.assertEqual(item.tax_rate, Decimal('0'))
        self.assertEqual(purchase.grand_total, Decimal('100.00'))


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class PurchaseSyncBatchingTests(TestCase):
    """A multi-line bill must rebuild its ledger once, not once per line.

    The budgets below are deliberately generous next to the batched cost
    (164 queries for a 5-line edit, 97 for its delete) and far below the
    unbatched cost (906 / 593) that timed out on production Postgres.
    """

    EDIT_BUDGET = 250
    DELETE_BUDGET = 150
    PER_LINE_BUDGET = 30

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='psb_staff', password='TestPass123!',
            is_staff=True, is_active=True,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)
        suffix = timezone.now().strftime('%H%M%S%f')[:8]
        self.vendor = Contact.objects.create(
            contact_type='vendor', name=f'PSB Vendor {suffix}',
            phone='97' + suffix,
        )

    def _bill(self, line_count):
        """Shape of the production bill that 500'd: mixed 0%/18% stock
        lines plus freight."""
        purchase = Purchase.objects.create(
            vendor=self.vendor, date=timezone.localdate(),
            gst_type='regular', freight_charge=Decimal('100.00'),
        )
        products = []
        for i in range(line_count):
            tax = Decimal('18.00') if i % 2 else Decimal('0.00')
            price = Decimal('1000.00') + Decimal(i) * Decimal('50.00')
            product = Product.objects.create(
                name=f'PSB Product {i} {timezone.now().strftime("%H%M%S%f")[:8]}',
                selling_price=price, purchase_price=price, tax_rate=tax,
                current_stock=Decimal('10.00'),
                is_service=False, is_active=True,
            )
            products.append(product)
            PurchaseItem.objects.create(
                purchase=purchase, product=product,
                quantity=Decimal('1.00'), unit_price=price, tax_rate=tax,
            )
        purchase.calculate_totals()
        purchase.save()
        return purchase, products

    def _edit(self, purchase):
        self.client.get(
            reverse('accounting:purchase_update', args=[purchase.pk]))
        return self._post_edit(purchase)

    def _post_edit(self, purchase):
        return self.client.post(
            reverse('accounting:purchase_update', args=[purchase.pk]),
            {
                'vendor': self.vendor.pk, 'date': str(purchase.date),
                'gst_type': purchase.gst_type,
                'discount_amount': str(purchase.discount_amount),
                'freight_charge': str(purchase.freight_charge),
                'discount_type': '', 'discount_note': '', 'discount_date': '',
                'notes': '',
            },
        )

    def test_edit_rebuilds_ledger_once(self):
        purchase, _ = self._bill(5)
        with mock.patch.object(
            models, 'sync_purchase_ledger',
            wraps=models.sync_purchase_ledger,
        ) as sync:
            with CaptureQueriesContext(connection) as ctx:
                resp = self._edit(purchase)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(sync.call_count, 1)
        self.assertLessEqual(len(ctx), self.EDIT_BUDGET)

        purchase.refresh_from_db()
        self.assertEqual(purchase.items.count(), 5)
        entry = LedgerEntry.objects.get(
            entry_type='purchase', reference_id=purchase.pk)
        debit = sum((l.debit for l in entry.lines.all()), Decimal('0'))
        credit = sum((l.credit for l in entry.lines.all()), Decimal('0'))
        self.assertEqual(debit, credit)
        self.assertEqual(credit, purchase.grand_total)

    def test_delete_never_rebuilds_ledger(self):
        purchase, products = self._bill(5)
        for product in products:
            product.refresh_from_db()
            self.assertEqual(product.current_stock, Decimal('11.00'))

        with mock.patch.object(
            models, 'sync_purchase_ledger',
            wraps=models.sync_purchase_ledger,
        ) as sync:
            with CaptureQueriesContext(connection) as ctx:
                resp = self.client.delete(
                    reverse('accounting:purchase_delete', args=[purchase.pk]),
                    HTTP_HX_REQUEST='true',
                )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(sync.call_count, 0)
        self.assertLessEqual(len(ctx), self.DELETE_BUDGET)
        self.assertFalse(Purchase.objects.filter(pk=purchase.pk).exists())
        self.assertFalse(LedgerEntry.objects.filter(
            entry_type='purchase', reference_id=purchase.pk).exists())
        for product in products:
            product.refresh_from_db()
            self.assertEqual(product.current_stock, Decimal('10.00'))

    def test_each_extra_line_costs_a_bounded_number_of_queries(self):
        small, _ = self._bill(2)
        with CaptureQueriesContext(connection) as small_ctx:
            self.assertEqual(self._edit(small).status_code, 302)
        large, _ = self._bill(8)
        with CaptureQueriesContext(connection) as large_ctx:
            self.assertEqual(self._edit(large).status_code, 302)
        marginal = (len(large_ctx) - len(small_ctx)) / 6
        self.assertLessEqual(marginal, self.PER_LINE_BUDGET)

    def test_restore_rebuilds_the_ledger_once(self):
        purchase, products = self._bill(5)
        purchase.delete()
        self.assertFalse(LedgerEntry.objects.filter(
            entry_type='purchase', reference_id=purchase.pk).exists())

        with mock.patch.object(
            models, 'sync_purchase_ledger',
            wraps=models.sync_purchase_ledger,
        ) as sync:
            purchase.restore()
        self.assertEqual(sync.call_count, 1)
        purchase.refresh_from_db()
        self.assertFalse(purchase.is_deleted)
        self.assertEqual(purchase.items.count(), 5)
        entry = LedgerEntry.objects.get(
            entry_type='purchase', reference_id=purchase.pk)
        debit = sum((l.debit for l in entry.lines.all()), Decimal('0'))
        credit = sum((l.credit for l in entry.lines.all()), Decimal('0'))
        self.assertEqual(debit, credit)
        for product in products:
            product.refresh_from_db()
            self.assertEqual(product.current_stock, Decimal('11.00'))

    def test_edit_removing_every_line_clears_the_ledger(self):
        purchase, products = self._bill(2)
        self.assertTrue(LedgerEntry.objects.filter(
            entry_type='purchase', reference_id=purchase.pk).exists())
        self.client.get(
            reverse('accounting:purchase_update', args=[purchase.pk]))
        for _ in range(2):
            resp = self.client.post(
                reverse('accounting:remove_purchase_item', args=[0]),
                {'purchase_id': str(purchase.pk)}, HTTP_HX_REQUEST='true')
            self.assertEqual(resp.status_code, 200)
        # No GET here: a fresh GET re-seeds the session from the DB and
        # would put the removed lines back.
        resp = self._post_edit(purchase)
        self.assertEqual(resp.status_code, 302)
        purchase.refresh_from_db()
        self.assertEqual(purchase.items.count(), 0)
        self.assertEqual(purchase.subtotal, Decimal('0.00'))
        self.assertEqual(purchase.grand_total, purchase.freight_charge)
        self.assertFalse(LedgerEntry.objects.filter(
            entry_type='purchase', reference_id=purchase.pk).exists())
        self.vendor.refresh_from_db()
        self.assertEqual(self.vendor.balance, Decimal('0.00'))
        for product in products:
            product.refresh_from_db()
            self.assertEqual(product.current_stock, Decimal('10.00'))