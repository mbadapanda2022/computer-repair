"""
Purchase settlement (bill ↔ vendor payment) tests.

Chalane ka tareeka:
    python manage.py test accounting.tests_purchase_settlement -v 2

Covers the purchase-system audit fixes:
    - Purchase.paid_amount / balance_due / settlement_status / recalc
    - PurchaseAllocation model + Payment plumbing (trim, delete, whatsapp)
    - Vendor-payment discount ledger signs (Dr Vendor / Cr Discount Received)
    - purchase_record_payment / purchase_apply_payment / purchase_toggle_paid views
    - Payment form vendor-bills allocation (load endpoint + ?purchase= prefill)
    - Purchase form date no longer wiped on validation errors
    - Session items scoped per purchase (cross-bill contamination fixed)
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounting.models import (
    Account, Contact, LedgerEntry, Payment, Product, Purchase,
    PurchaseAllocation, PurchaseItem,
)

User = get_user_model()


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class PurchaseSettlementTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='psettle_staff', password='TestPass123!',
            is_staff=True, is_active=True,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)
        suffix = timezone.now().strftime('%H%M%S%f')[:8]
        self.vendor = Contact.objects.create(
            contact_type='vendor', name=f'Luna Vendor {suffix}',
            phone='98' + suffix,
        )
        self.customer = Contact.objects.create(
            contact_type='customer', name=f'Psettle Cust {suffix}',
            phone='88' + suffix,
        )

    # ── helpers ──

    def _purchase(self, amount='5200.00', **kw):
        return Purchase.objects.create(
            vendor=self.vendor,
            date=timezone.localdate(),
            subtotal=Decimal(amount),
            grand_total=Decimal(amount),
            **kw,
        )

    def _payment(self, amount='5200.00', **kw):
        return Payment.objects.create(
            direction=kw.pop('direction', 'paid'),
            contact=kw.pop('contact', self.vendor),
            amount=Decimal(amount),
            method=kw.pop('method', 'cash'),
            date=kw.pop('date', timezone.localdate()),
            **kw,
        )

    def _product(self, name, price='100.00'):
        return Product.objects.create(
            name=f'{name} {timezone.now().strftime("%H%M%S%f")[:6]}',
            selling_price=Decimal(price),
            purchase_price=Decimal(price),
            current_stock=Decimal('0.00'),
            is_service=False, is_active=True,
            tax_rate=Decimal('0.00'),
        )

    # ════════════════════════════════════════════════════════
    # Model layer
    # ════════════════════════════════════════════════════════

    def test_balance_due_and_status_defaults(self):
        p = self._purchase('1000.00')
        self.assertEqual(p.balance_due, Decimal('1000.00'))
        self.assertEqual(p.settlement_status, 'unpaid')

    def test_recalc_settlement_partial_then_paid(self):
        p = self._purchase('1000.00')
        pay1 = self._payment('600.00')
        PurchaseAllocation.objects.create(payment=pay1, purchase=p, amount=Decimal('600.00'))
        p.recalc_settlement()
        self.assertEqual(p.paid_amount, Decimal('600.00'))
        self.assertFalse(p.paid)
        self.assertEqual(p.settlement_status, 'partial')
        self.assertEqual(p.balance_due, Decimal('400.00'))

        pay2 = self._payment('400.00')
        PurchaseAllocation.objects.create(payment=pay2, purchase=p, amount=Decimal('400.00'))
        p.recalc_settlement()
        self.assertEqual(p.paid_amount, Decimal('1000.00'))
        self.assertTrue(p.paid)
        self.assertEqual(p.settlement_status, 'paid')
        self.assertEqual(p.balance_due, Decimal('0.00'))

    def test_manual_mark_paid_survives_recalc_without_allocations(self):
        p = self._purchase('500.00')
        Purchase.objects.filter(pk=p.pk).update(paid=True)
        p.refresh_from_db()
        p.recalc_settlement()  # no allocation rows at all → flag untouched
        self.assertTrue(p.paid)

    def test_payment_trim_includes_purchase_allocations(self):
        p = self._purchase('1000.00')
        pay = self._payment('1000.00')
        PurchaseAllocation.objects.create(payment=pay, purchase=p, amount=Decimal('1000.00'))
        p.recalc_settlement()
        self.assertTrue(p.paid)

        # Edit the payment to a smaller amount → allocation must shrink
        pay.amount = Decimal('600.00')
        pay.save()

        alloc = PurchaseAllocation.objects.filter(payment=pay).first()
        self.assertEqual(alloc.amount, Decimal('600.00'))
        p.refresh_from_db()
        self.assertEqual(p.paid_amount, Decimal('600.00'))
        self.assertFalse(p.paid)

    def test_payment_delete_resets_purchase_settlement(self):
        p = self._purchase('1000.00')
        pay = self._payment('1000.00')
        PurchaseAllocation.objects.create(payment=pay, purchase=p, amount=Decimal('1000.00'))
        p.recalc_settlement()
        self.assertTrue(p.paid)

        pay.delete()

        p.refresh_from_db()
        self.assertEqual(p.paid_amount, Decimal('0.00'))
        self.assertFalse(p.paid)

    def test_vendor_payment_discount_ledger_signs_create(self):
        """Dr Vendor (net+disc) / Cr Cash net / Cr Discount Received disc."""
        pay = self._payment('950.00', discount_amount=Decimal('50.00'))
        entry = LedgerEntry.objects.get(reference_id=pay.pk, entry_type='payment')
        lines = list(entry.lines.all())

        vendor = Account.objects.get(code='2011')
        discount = Account.objects.get(code='4011')
        cash = Account.objects.get(code='1001')

        v_lines = [l for l in lines if l.account_id == vendor.pk]
        d_lines = [l for l in lines if l.account_id == discount.pk]
        c_lines = [l for l in lines if l.account_id == cash.pk]

        # Vendor debited FULL amount (900 + 100... here net 900 + disc 50 = 950? amount=950, disc=50 → net 900, vendor Dr 900+50=950)
        self.assertEqual(sum(l.debit for l in v_lines), Decimal('950.00'))
        self.assertEqual(sum(l.credit for l in v_lines), Decimal('0.00'))
        # Discount Received is INCOME → credit side, never debit
        self.assertEqual(sum(l.credit for l in d_lines), Decimal('50.00'))
        self.assertEqual(sum(l.debit for l in d_lines), Decimal('0.00'))
        self.assertEqual(sum(l.credit for l in c_lines), Decimal('900.00'))

    def test_vendor_payment_discount_ledger_signs_update(self):
        pay = self._payment('950.00')
        pay.discount_amount = Decimal('50.00')
        pay.save()

        entry = LedgerEntry.objects.get(reference_id=pay.pk, entry_type='payment')
        vendor = Account.objects.get(code='2011')
        discount = Account.objects.get(code='4011')

        self.assertEqual(
            sum(l.debit for l in entry.lines.filter(account=vendor)),
            Decimal('950.00'),
        )
        self.assertEqual(
            sum(l.credit for l in entry.lines.filter(account=discount)),
            Decimal('50.00'),
        )
        self.assertEqual(
            sum(l.debit for l in entry.lines.filter(account=discount)),
            Decimal('0.00'),
        )

    def test_purchase_whatsapp_share_url(self):
        p = self._purchase('5200.00')
        url = p.whatsapp_share_url
        self.assertIsNotNone(url)
        self.assertTrue(url.startswith('https://wa.me/9198'))
        self.assertIn(p.purchase_number, url.replace('%20', ' ').replace('%20', ' '))

    # ════════════════════════════════════════════════════════
    # Views — settlement actions
    # ════════════════════════════════════════════════════════

    def test_apply_existing_payment_settles_bill(self):
        """The Payment #14 → PUR-0003 reconciliation flow."""
        p = self._purchase('5200.00')
        pay = self._payment('5200.00')

        resp = self.client.post(
            reverse('accounting:purchase_apply_payment', args=[p.pk]),
            {'payment_id': pay.pk},
        )
        self.assertEqual(resp.status_code, 302)

        alloc = PurchaseAllocation.objects.filter(payment=pay, purchase=p).first()
        self.assertIsNotNone(alloc)
        self.assertEqual(alloc.amount, Decimal('5200.00'))
        p.refresh_from_db()
        self.assertTrue(p.paid)
        self.assertEqual(p.paid_amount, Decimal('5200.00'))

    def test_apply_payment_clamps_to_balance_and_remaining(self):
        p = self._purchase('100.00')
        pay = self._payment('1000.00')

        self.client.post(
            reverse('accounting:purchase_apply_payment', args=[p.pk]),
            {'payment_id': pay.pk, 'amount': '99999'},
        )
        alloc = PurchaseAllocation.objects.get(payment=pay, purchase=p)
        self.assertEqual(alloc.amount, Decimal('100.00'))

    def test_apply_payment_rejects_advance_and_wrong_contact(self):
        p = self._purchase('500.00')
        adv = self._payment('500.00', is_advance=True)
        resp = self.client.post(
            reverse('accounting:purchase_apply_payment', args=[p.pk]),
            {'payment_id': adv.pk}, follow=True,
        )
        self.assertFalse(PurchaseAllocation.objects.filter(purchase=p).exists())

        other_pay = self._payment('500.00', contact=self.customer)
        self.client.post(
            reverse('accounting:purchase_apply_payment', args=[p.pk]),
            {'payment_id': other_pay.pk}, follow=True,
        )
        self.assertFalse(PurchaseAllocation.objects.filter(purchase=p).exists())

    def test_record_payment_view_creates_payment_and_allocation(self):
        p = self._purchase('1200.00')
        resp = self.client.post(
            reverse('accounting:purchase_record_payment', args=[p.pk]),
            {'amount': '1200', 'date': str(timezone.localdate()),
             'method': 'cash', 'reference': 'CASH-1'},
        )
        self.assertEqual(resp.status_code, 302)

        pay = Payment.objects.filter(contact=self.vendor, direction='paid').first()
        self.assertIsNotNone(pay)
        self.assertEqual(pay.amount, Decimal('1200.00'))
        alloc = PurchaseAllocation.objects.get(payment=pay, purchase=p)
        self.assertEqual(alloc.amount, Decimal('1200.00'))
        p.refresh_from_db()
        self.assertTrue(p.paid)
        self.assertEqual(
            str(pay.description), f'Payment against {p.purchase_number}'
        )

    def test_toggle_paid_manual_and_blocked_with_allocations(self):
        p = self._purchase('700.00')
        self.client.post(reverse('accounting:purchase_toggle_paid', args=[p.pk]))
        p.refresh_from_db()
        self.assertTrue(p.paid)

        # unmark allowed when manual
        self.client.post(reverse('accounting:purchase_toggle_paid', args=[p.pk]))
        p.refresh_from_db()
        self.assertFalse(p.paid)

        # with allocations → unmark blocked
        pay = self._payment('700.00')
        PurchaseAllocation.objects.create(payment=pay, purchase=p, amount=Decimal('700.00'))
        p.recalc_settlement()
        self.assertTrue(p.paid)
        self.client.post(reverse('accounting:purchase_toggle_paid', args=[p.pk]))
        p.refresh_from_db()
        self.assertTrue(p.paid)

    # ════════════════════════════════════════════════════════
    # Views — payment form vendor bills
    # ════════════════════════════════════════════════════════

    def test_load_endpoint_returns_vendor_bills_partial(self):
        p = self._purchase('5200.00')
        resp = self.client.get(
            reverse('accounting:load_unpaid_invoices'),
            {'contact': self.vendor.pk, 'direction': 'paid'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f'purchase_alloc_{p.pk}')
        self.assertContains(resp, p.purchase_number)

    def test_load_endpoint_with_payment_prefills_allocations(self):
        p = self._purchase('5200.00')
        pay = self._payment('5200.00')
        PurchaseAllocation.objects.create(payment=pay, purchase=p, amount=Decimal('5200.00'))
        resp = self.client.get(
            reverse('accounting:load_unpaid_invoices'),
            {'contact': self.vendor.pk, 'direction': 'paid', 'payment': pay.pk},
        )
        self.assertContains(resp, f'purchase_alloc_{p.pk}')
        self.assertContains(resp, 'value="5200.00"')

    def test_payment_form_prefill_from_purchase(self):
        p = self._purchase('5200.00')
        resp = self.client.get(
            reverse('accounting:payment_create'), {'purchase': p.pk},
            headers={'hx-request': 'true'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f'purchase_alloc_{p.pk}')
        # direction prefilled to 'paid' and amount to balance due
        self.assertContains(resp, 'value="5200.00"')

    def test_payment_create_with_purchase_alloc_field(self):
        p = self._purchase('5200.00')
        resp = self.client.post(
            reverse('accounting:payment_create'),
            {
                'direction': 'paid', 'contact': self.vendor.pk,
                'amount': '5200', 'date': str(timezone.localdate()),
                'method': 'cash',
                f'purchase_alloc_{p.pk}': '5200',
            },
            headers={'hx-request': 'true'},
        )
        self.assertEqual(resp.status_code, 200)
        pay = Payment.objects.filter(contact=self.vendor, amount=Decimal('5200.00')).first()
        self.assertIsNotNone(pay)
        self.assertTrue(PurchaseAllocation.objects.filter(payment=pay, purchase=p).exists())
        p.refresh_from_db()
        self.assertTrue(p.paid)

    def test_payment_update_reset_keeps_purchase_allocations(self):
        p = self._purchase('5200.00')
        pay = self._payment('5200.00')
        PurchaseAllocation.objects.create(payment=pay, purchase=p, amount=Decimal('5200.00'))
        p.recalc_settlement()

        # Edit only the reference — allocations must be re-collected, not lost
        self.client.post(
            reverse('accounting:payment_update', args=[pay.pk]),
            {
                'direction': 'paid', 'contact': self.vendor.pk,
                'amount': '5200', 'date': str(timezone.localdate()),
                'method': 'cash', 'reference': 'NEW-REF',
                'reset_allocations': '1',
                f'purchase_alloc_{p.pk}': '5200',
            },
            headers={'hx-request': 'true'},
        )
        pay.refresh_from_db()
        self.assertEqual(pay.reference, 'NEW-REF')
        self.assertTrue(PurchaseAllocation.objects.filter(payment=pay, purchase=p).exists())
        p.refresh_from_db()
        self.assertTrue(p.paid)

    def test_payment_detail_modal_shows_purchase_alloc(self):
        p = self._purchase('5200.00')
        pay = self._payment('5200.00')
        PurchaseAllocation.objects.create(payment=pay, purchase=p, amount=Decimal('5200.00'))
        resp = self.client.get(
            reverse('accounting:payment_detail_modal', args=[pay.pk]))
        self.assertContains(resp, p.purchase_number)
        self.assertContains(resp, 'Payment #' + str(pay.pk))

    # ════════════════════════════════════════════════════════
    # Views — purchase form defects
    # ════════════════════════════════════════════════════════

    def test_new_purchase_form_defaults_to_today(self):
        resp = self.client.get(reverse('accounting:purchase_create'))
        self.assertContains(resp, f'value="{timezone.localdate().strftime("%Y-%m-%d")}"')

    def test_date_preserved_after_invalid_post(self):
        """Date must not be wiped when the form re-renders with errors."""
        resp = self.client.post(
            reverse('accounting:purchase_create'),
            {'vendor': '', 'date': '2026-10-14', 'gst_type': 'regular'},
        )
        self.assertEqual(resp.status_code, 200)  # re-rendered with errors
        self.assertContains(resp, 'value="2026-10-14"')

    def test_session_items_scoped_per_purchase(self):
        """Items from bill A must never leak into bill B's edit form."""
        prodA = self._product('PanelA')
        prodB = self._product('PanelB')
        a = self._purchase('100.00')
        b = self._purchase('200.00')
        PurchaseItem.objects.create(
            purchase=a, product=prodA, quantity=Decimal('1'),
            unit_price=Decimal('100'), tax_rate=Decimal('0'),
        )
        PurchaseItem.objects.create(
            purchase=b, product=prodB, quantity=Decimal('1'),
            unit_price=Decimal('200'), tax_rate=Decimal('0'),
        )
        # Poison the GLOBAL legacy key the old code used, plus A's scoped key
        session = self.client.session
        session['temp_purchase_items'] = [{
            'product_id': prodA.id, 'product_name': prodA.name,
            'quantity': '1', 'unit_price': '100', 'tax_rate': '0',
            'line_total': '100', 'is_office_use': False,
        }]
        session.save()

        resp = self.client.get(reverse('accounting:purchase_update', args=[b.pk]))
        self.assertContains(resp, prodB.name)
        self.assertNotContains(resp, prodA.name)

    def test_add_item_writes_to_scoped_key(self):
        b = self._purchase('200.00')
        prod = self._product('ScopedProd')
        self.client.post(
            reverse('accounting:add_purchase_item'),
            {'purchase_id': str(b.pk), 'product': prod.pk,
             'quantity': '1', 'unit_price': '50', 'tax_rate': '0'},
        )
        session = self.client.session
        self.assertEqual(len(session[f'temp_purchase_items_{b.pk}']), 1)
        # global legacy key untouched
        self.assertNotIn('temp_purchase_items', session)

    def test_purchase_detail_shows_settlement_and_apply_list(self):
        p = self._purchase('5200.00')
        pay = self._payment('5200.00')
        resp = self.client.get(reverse('accounting:purchase_detail', args=[p.pk]))
        self.assertContains(resp, 'Payment &amp; Settlement')
        self.assertContains(resp, f'payment_id')
        self.assertContains(resp, f'Payment #{pay.pk}')
        self.assertContains(resp, '/record-payment/')
        self.assertContains(resp, '/apply-payment/')
