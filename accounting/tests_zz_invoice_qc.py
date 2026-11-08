"""Temporary: measure SQL query counts for invoice create/edit/delete at
different line counts, to see whether sales has the same production
timeout problem purchases had."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounting import models
from accounting.models import Contact, Invoice, InvoiceItem, Product

User = get_user_model()


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class InvoiceQueryCountTests(TestCase):

    def setUp(self):
        self.staff = User.objects.create_user(
            username='iqc_staff', password='TestPass123!',
            is_staff=True, is_active=True)
        self.client = Client()
        self.client.force_login(self.staff)
        self.customer = Contact.objects.create(
            contact_type='customer', name='IQC Customer', phone='9000000002')

    def _bill(self, n):
        invoice = Invoice.objects.create(
            customer=self.customer, date=timezone.localdate(),
            gst_type='regular')
        for i in range(n):
            product = Product.objects.create(
                name=f'IQC Product {i}', selling_price=Decimal('1000.00'),
                purchase_price=Decimal('800.00'), tax_rate=Decimal('18.00'),
                current_stock=Decimal('100.00'), is_service=False,
                is_active=True)
            InvoiceItem.objects.create(
                invoice=invoice, product=product, quantity=Decimal('1'),
                unit_price=Decimal('1000.00'), tax_rate=Decimal('18.00'))
        invoice.calculate_totals()
        invoice.save()
        return invoice

    def _payload(self, invoice):
        return {
            'customer': self.customer.pk, 'date': str(invoice.date),
            'gst_type': 'regular', 'discount_amount': '0.00',
            'discount_type': '', 'discount_note': '', 'discount_date': '',
            'notes': '', 'due_date': '',
        }

    def test_counts(self):
        from unittest import mock
        for n in (1, 2, 5):
            inv = self._bill(n)
            self.client.get(reverse('accounting:invoice_update', args=[inv.pk]))
            with mock.patch.object(models, 'sync_invoice_ledger',
                                   wraps=models.sync_invoice_ledger) as sync:
                with CaptureQueriesContext(connection) as ctx:
                    resp = self.client.post(
                        reverse('accounting:invoice_update', args=[inv.pk]),
                        self._payload(inv))
            edit_q, edit_sync = len(ctx), sync.call_count
            with mock.patch.object(models, 'sync_invoice_ledger',
                                   wraps=models.sync_invoice_ledger) as sync2:
                with CaptureQueriesContext(connection) as ctx2:
                    resp2 = self.client.delete(
                        reverse('accounting:invoice_delete', args=[inv.pk]),
                        HTTP_HX_REQUEST='true')
            print(f'\nIQC lines={n}: edit={edit_q} queries / {edit_sync} rebuilds '
                  f'(HTTP {resp.status_code}), delete={len(ctx2)} queries / '
                  f'{sync2.call_count} rebuilds (HTTP {resp2.status_code})')