"""
Payment list professional-features tests.

Chalane ka tareeka:
    python manage.py test accounting.tests_payment_list_features -v 2

Coverage for the payments-list overhaul:
    - payment_detail_modal (HTMX modal partial)
    - payment_print (single receipt/voucher A4)
    - payment_list_print (filtered register, no pagination)
    - payment_list_excel (styled xlsx honoring filters)
    - Payment.whatsapp_share_url + Payment.net_amount
    - reconciled filter on the list
    - responsive table (desktop d-none d-lg-block + mobile d-lg-none cards)
"""
from __future__ import annotations

from decimal import Decimal
from io import BytesIO
from urllib.parse import unquote

import openpyxl
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounting.models import (
    Account, Contact, Invoice, Payment, PaymentAllocation,
)

User = get_user_model()


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class PaymentListFeatureTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='plist_staff', password='TestPass123!',
            is_staff=True, is_active=True,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)
        suffix = timezone.now().strftime('%H%M%S%f')[:8]
        self.vendor = Contact.objects.create(
            contact_type='vendor', name=f'Rent Vendor {suffix}',
            phone='98' + suffix,
        )
        self.customer = Contact.objects.create(
            contact_type='customer', name=f'Walkin Cust {suffix}',
            phone='88' + suffix,
        )

    def make_payment(self, contact=None, direction='paid', amount='4500.00',
                     **kwargs):
        return Payment.objects.create(
            direction=direction,
            contact=contact or self.vendor,
            amount=Decimal(amount),
            method=kwargs.pop('method', 'cash'),
            date=kwargs.pop('date', timezone.localdate()),
            **kwargs,
        )

    def make_invoice(self, amount='2000.00'):
        return Invoice.objects.create(
            customer=self.customer,
            subtotal=Decimal(amount),
            grand_total=Decimal(amount),
            balance_due=Decimal(amount),
            date=timezone.localdate(),
        )

    # ---------- detail modal ----------

    def test_detail_modal_renders_payment(self):
        pay = self.make_payment(amount='4500.00')
        resp = self.client.get(
            reverse('accounting:payment_detail_modal', args=[pay.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.vendor.name)
        self.assertContains(resp, '4500.00')
        self.assertContains(resp, f'Payment #{pay.pk}')

    def test_detail_modal_shows_allocations(self):
        inv = self.make_invoice()
        pay = self.make_payment(self.customer, direction='received',
                                amount='2000.00')
        PaymentAllocation.objects.create(
            payment=pay, invoice=inv, amount=Decimal('2000.00'))
        resp = self.client.get(
            reverse('accounting:payment_detail_modal', args=[pay.pk]))
        self.assertContains(resp, inv.invoice_number)

    def test_detail_modal_404_for_missing(self):
        resp = self.client.get(
            reverse('accounting:payment_detail_modal', args=[999999]))
        self.assertEqual(resp.status_code, 404)

    # ---------- single print ----------

    def test_payment_print_received(self):
        pay = self.make_payment(self.customer, direction='received',
                                amount='1500.00')
        resp = self.client.get(
            reverse('accounting:payment_print', args=[pay.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'PAYMENT RECEIVED')
        self.assertContains(resp, self.customer.name)

    def test_payment_print_paid_voucher(self):
        pay = self.make_payment(direction='paid', amount='500.00')
        resp = self.client.get(
            reverse('accounting:payment_print', args=[pay.pk]))
        self.assertContains(resp, 'PAYMENT VOUCHER')

    # ---------- list print ----------

    def test_list_print_includes_payments_and_totals(self):
        self.make_payment(self.customer, direction='received',
                          amount='3000.00')
        self.make_payment(direction='paid', amount='1000.00')
        resp = self.client.get(reverse('accounting:payment_list_print'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.vendor.name)
        self.assertContains(resp, self.customer.name)

    def test_list_print_honors_direction_filter(self):
        self.make_payment(self.customer, direction='received',
                          amount='3000.00')
        self.make_payment(direction='paid', amount='1000.00')
        resp = self.client.get(
            reverse('accounting:payment_list_print') + '?direction=paid')
        self.assertContains(resp, self.vendor.name)
        self.assertNotContains(resp, self.customer.name)

    # ---------- excel export ----------

    def test_excel_export_content_type_and_attachment(self):
        self.make_payment(amount='4500.00')
        resp = self.client.get(reverse('accounting:payment_list_excel'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('spreadsheetml', resp['Content-Type'])
        self.assertIn('attachment', resp['Content-Disposition'])

    def test_excel_export_honors_filters(self):
        self.make_payment(self.customer, direction='received',
                          amount='3000.00')
        self.make_payment(direction='paid', amount='1000.00')
        resp = self.client.get(
            reverse('accounting:payment_list_excel')
            + f'?search={self.customer.name}')
        self.assertEqual(resp.status_code, 200)
        wb = openpyxl.load_workbook(BytesIO(resp.content))
        ws = wb.active
        rows = [r for r in ws.iter_rows(min_row=3, values_only=True)
                if r[0] is not None]
        contacts = [str(r[3]) for r in rows]
        self.assertTrue(any(self.customer.name in c for c in contacts))
        self.assertFalse(any(self.vendor.name in c for c in contacts))

    # ---------- whatsapp share ----------

    def test_whatsapp_url_paid_with_expense_head(self):
        rent = Account.objects.get(code='5201')
        pay = self.make_payment(amount='4500.00', method='upi',
                                upi_ref='TXN123', expense_account=rent)
        url = pay.whatsapp_share_url
        self.assertIsNotNone(url)
        self.assertTrue(url.startswith('https://wa.me/91'))
        msg = unquote(url)
        self.assertIn('made by A1 Computer Solutions', msg)
        self.assertIn('Ref: TXN123', msg)
        self.assertIn('Rent', msg)

    def test_whatsapp_url_received_with_allocations(self):
        inv = self.make_invoice()
        pay = self.make_payment(self.customer, direction='received',
                                amount='2000.00')
        PaymentAllocation.objects.create(
            payment=pay, invoice=inv, amount=Decimal('2000.00'))
        msg = unquote(pay.whatsapp_share_url)
        self.assertIn('Payment received', msg)
        self.assertIn(inv.invoice_number, msg)

    def test_whatsapp_url_none_without_phone(self):
        contact = Contact.objects.create(contact_type='vendor',
                                         name='No Phone Vendor')
        pay = self.make_payment(contact)
        self.assertIsNone(pay.whatsapp_share_url)

    def test_whatsapp_url_prefixes_10_digit(self):
        url = self.make_payment().whatsapp_share_url
        path = url.split('wa.me/')[1].split('?')[0]
        self.assertEqual(path, '91' + ''.join(
            filter(str.isdigit, self.vendor.phone)))

    # ---------- model properties ----------

    def test_net_amount_subtracts_discount(self):
        pay = self.make_payment(amount='1000.00',
                                discount_amount=Decimal('100'))
        self.assertEqual(pay.net_amount, Decimal('900.00'))

    # ---------- list page (HTMX + filters + responsive) ----------

    def test_list_reconciled_filter(self):
        reconciled_pay = self.make_payment(amount='500.00')
        reconciled_pay.reconciled = True
        reconciled_pay.save()
        self.make_payment(self.customer, amount='600.00')
        resp = self.client.get(
            reverse('accounting:payment_list') + '?reconciled=yes')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f'Pay #{reconciled_pay.pk}')
        resp2 = self.client.get(
            reverse('accounting:payment_list') + '?reconciled=no')
        self.assertEqual(resp2.status_code, 200)
        self.assertNotContains(resp2, f'Pay #{reconciled_pay.pk}')

    def test_list_has_responsive_blocks(self):
        self.make_payment()
        resp = self.client.get(reverse('accounting:payment_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'd-lg-none')
        self.assertContains(resp, 'd-none d-lg-block')
        self.assertContains(resp, 'payment-table-container')

    def test_list_excel_and_print_buttons_present(self):
        resp = self.client.get(reverse('accounting:payment_list'))
        self.assertContains(resp, '/payments/export/excel/')
        self.assertContains(resp, '/payments/print/')

    def test_list_search_hits_expense_account_name(self):
        rent = Account.objects.get(code='5201')
        # customer name 'Walkin Cust' does not contain 'Rent', so a hit
        # proves the expense_account search path works.
        pay = self.make_payment(self.customer, amount='4500.00',
                                expense_account=rent)
        resp = self.client.get(
            reverse('accounting:payment_list') + '?search=Rent')
        self.assertContains(resp, f'Pay #{pay.pk}')
