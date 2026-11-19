"""Credit-note form guards.

Before this the over-quantity branch only *broke* out of the loop: if an
earlier line was valid, the credit note was still created without the
offending line and the user only saw a flash message. Now any bad quantity
rejects the whole submission, keeps the typed boxes on screen, and creates
nothing.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounting.models import Contact, CreditNote, Invoice, InvoiceItem, Product

User = get_user_model()


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class CreditNoteGuardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='cn_guard_staff', password='TestPass123!',
            is_staff=True, is_active=True,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)
        stamp = timezone.now().strftime('%H%M%S%f')[:8]
        self.customer = Contact.objects.create(
            contact_type='customer', name=f'CN Cust {stamp}',
            phone='93' + stamp)
        self.invoice = Invoice.objects.create(
            customer=self.customer, date=timezone.localdate())
        self.items = []
        for i in range(2):
            product = Product.objects.create(
                name=f'CN Item {i} {stamp}', selling_price=Decimal('500.00'),
                purchase_price=Decimal('400.00'), tax_rate=Decimal('0'),
                current_stock=Decimal('0'), is_service=True, is_active=True)
            self.items.append(InvoiceItem.objects.create(
                invoice=self.invoice, product=product,
                quantity=Decimal('2.00'), unit_price=Decimal('500.00'),
                tax_rate=Decimal('0')))
        self.invoice.refresh_from_db()

    def _post(self, data):
        return self.client.post(
            reverse('accounting:credit_note_create',
                    args=[self.invoice.pk]),
            data, HTTP_HX_REQUEST='true')

    def test_rejects_over_quantity_even_when_another_line_is_valid(self):
        valid, bad = self.items
        resp = self._post({
            'date': str(timezone.localdate()),
            'reason': 'sales_return',
            'refund_method': 'credit',
            'is_stock_return': 'on',
            'notes': 'customer said screen flickers',
            f'item_{valid.pk}': 'on',
            f'qty_{valid.pk}': '1',
            f'item_{bad.pk}': 'on',
            f'qty_{bad.pk}': '99',
        })

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(CreditNote.objects.exists())
        body = resp.content.decode()
        self.assertIn('cannot exceed', body)
        # The typed quantities and note stay in the boxes.
        self.assertIn('value="99"', body)
        self.assertIn('customer said screen flickers', body)
        self.assertIn(f'name="item_{valid.pk}"', body)

    def test_rejects_when_nothing_is_selected(self):
        resp = self._post({
            'date': str(timezone.localdate()),
            'reason': 'sales_return',
            'refund_method': 'credit',
        })

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(CreditNote.objects.exists())
        self.assertIn('at least one item', resp.content.decode())

    def test_accepts_valid_selection_and_creates_note(self):
        first, _ = self.items
        resp = self._post({
            'date': str(timezone.localdate()),
            'reason': 'rate_difference',
            'refund_method': 'credit',
            'is_stock_return': 'on',
            f'item_{first.pk}': 'on',
            f'qty_{first.pk}': '1',
        })

        # htmx success answers with HX-Redirect (200 + header), not a 302.
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp['HX-Redirect'])
        note = CreditNote.objects.get()
        self.assertEqual(note.invoice_id, self.invoice.pk)
        self.assertEqual(note.reason, 'rate_difference')
        self.assertEqual(note.items.count(), 1)
        self.assertEqual(note.items.get().quantity_returned, Decimal('1'))
