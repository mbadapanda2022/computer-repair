"""
Regression tests for the full-project audit fixes (2026-10-19).

Run:
    venv\\Scripts\\python.exe manage.py test accounting.tests_audit_fixes -v 2

Each test pins one audit bug:
  B2  status change via repair_update form → state machine (previously 500)
  B3  GET-mutation guards (remove part/service, message delete)
  B4  final_amount single source of truth (invoiced = invoice.grand_total)
  B5  Invoice restore → items + stock + ledger restored
  B7  negative estimated_cost reject
  B8  stock floor + oversell block
  B9  advance must not be applied twice (double-spend cap)
  B12 DateField defaults timezone.localdate
  B23 rupee filter Indian grouping
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounting.models import (
    AdvanceAdjustment, Contact, Invoice, InvoiceItem, LedgerEntry,
    Payment, Product, RepairJob, RepairPart, RepairStatusLog,
)
from accounting.templatetags.math_filters import rupee

User = get_user_model()


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class AuditFixTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='audit_staff', password='TestPass123!',
            is_staff=True, is_active=True,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)
        suffix = timezone.now().strftime('%H%M%S%f')[:8]
        self.customer = Contact.objects.create(
            contact_type='customer', name=f'Audit Customer {suffix}',
            phone=f'8{suffix}'.ljust(10, '0')[:10],
        )
        self.product = Product.objects.create(
            name=f'Audit SSD {suffix}', selling_price=Decimal('2500.00'),
            current_stock=Decimal('10.00'), is_service=False,
            is_active=True, tax_rate=Decimal('0.00'),
        )

    def _job(self, status='received'):
        return RepairJob.objects.create(
            customer=self.customer, device_model='Audit Laptop',
            issue_description='Audit test', status=status,
        )

    # ── B2: full-form edit with a status change must route through the
    # state machine, not crash on the raw-assignment guard ──
    def test_repair_update_form_status_change_uses_state_machine(self):
        job = self._job(status='received')
        response = self.client.post(
            reverse('accounting:repair_update', args=[job.pk]),
            {
                'customer': self.customer.pk,
                'device_model': 'Audit Laptop v2',
                'issue_description': 'Updated issue',
                'status': 'diagnosis',
                'priority': 'normal',
                'payment_method': 'cash',
            },
        )
        self.assertIn(response.status_code, (200, 302))
        job.refresh_from_db()
        self.assertEqual(job.status, 'diagnosis')
        self.assertEqual(job.device_model, 'Audit Laptop v2')
        self.assertTrue(
            RepairStatusLog.objects.filter(
                repair_job=job, to_status='diagnosis',
            ).exists(),
            'status change did not write history through change_status()',
        )

    # ── B3: deletion endpoints must refuse GET ──
    def test_remove_repair_part_rejects_get(self):
        job = self._job()
        part = RepairPart.objects.create(
            repair_job=job, product=self.product, quantity=1,
            unit_price=Decimal('2500.00'),
        )
        response = self.client.get(
            reverse('accounting:remove_repair_part', args=[part.pk])
        )
        self.assertEqual(response.status_code, 405)
        part.refresh_from_db()
        self.assertFalse(part.is_deleted)

    def test_remove_repair_service_rejects_get(self):
        from accounting.models import RepairService
        job = self._job()
        service_product = Product.objects.create(
            name='Audit Labour', is_service=True, is_active=True,
            selling_price=Decimal('500.00'), tax_rate=Decimal('0.00'),
        )
        svc = RepairService.objects.create(
            repair_job=job, product=service_product,
            line_total=Decimal('500.00'),
        )
        response = self.client.get(
            reverse('accounting:remove_repair_service', args=[svc.pk])
        )
        self.assertEqual(response.status_code, 405)

    # ── B7: negative estimate must be rejected, not silently saved ──
    def test_quick_update_rejects_negative_estimated_cost(self):
        job = self._job()
        response = self.client.post(
            reverse('accounting:quick_update_repair', args=[job.pk]),
            {'estimated_cost': '-500'},
            headers={'HX-Request': 'true'},
        )
        self.assertEqual(response.status_code, 400)
        job.refresh_from_db()
        self.assertIsNone(job.estimated_cost)

    # ── B4: once invoiced, final_amount must track invoice.grand_total
    # (pre-tax recomputation used to silently revert it) ──
    def test_final_amount_tracks_invoice_grand_total(self):
        job = self._job(status='ready')
        invoice = Invoice.objects.create(
            customer=self.customer, date=timezone.localdate(),
        )
        item = InvoiceItem.objects.create(
            invoice=invoice, product=self.product,
            quantity=Decimal('1'), unit_price=Decimal('1000.00'),
            tax_rate=Decimal('18'),
        )
        item.refresh_from_db()
        invoice.refresh_from_db()
        self.assertEqual(invoice.grand_total, Decimal('1180.00'))

        job.invoice = invoice
        job.save()
        job.refresh_from_db()
        self.assertEqual(job.final_amount, Decimal('1180.00'))

        # Any later plain save must NOT drop back to the pre-tax 1000.
        job.device_model = 'Audit Laptop v3'
        job.save()
        job.refresh_from_db()
        self.assertEqual(job.final_amount, Decimal('1180.00'))

    # ── B5: soft-deleted invoice must restore WITH items, stock, ledger ──
    def test_invoice_restore_restores_items_stock_and_ledger(self):
        invoice = Invoice.objects.create(
            customer=self.customer, date=timezone.localdate(),
        )
        InvoiceItem.objects.create(
            invoice=invoice, product=self.product,
            quantity=Decimal('2'), unit_price=Decimal('500.00'),
            tax_rate=Decimal('0'),
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, Decimal('8.00'))

        invoice.delete()
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, Decimal('10.00'))
        self.assertFalse(
            LedgerEntry.objects.filter(
                entry_type='sales', reference_id=invoice.pk,
            ).exists()
        )

        invoice.refresh_from_db()
        invoice.restore()

        invoice.refresh_from_db()
        self.assertFalse(invoice.is_deleted)
        self.assertEqual(invoice.items.count(), 1)
        self.assertEqual(invoice.grand_total, Decimal('1000.00'))
        self.product.refresh_from_db()
        self.assertEqual(
            self.product.current_stock, Decimal('8.00'),
            'restore did not re-deduct stock',
        )
        self.assertTrue(
            LedgerEntry.objects.filter(
                entry_type='sales', reference_id=invoice.pk,
            ).exists(),
            'restore did not re-create ledger entries',
        )

    # ── B8: model layer must refuse overselling ──
    def test_invoice_item_blocks_insufficient_stock(self):
        invoice = Invoice.objects.create(
            customer=self.customer, date=timezone.localdate(),
        )
        with self.assertRaises(ValidationError):
            InvoiceItem.objects.create(
                invoice=invoice, product=self.product,
                quantity=Decimal('50'), unit_price=Decimal('500.00'),
                tax_rate=Decimal('0'),
            )
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, Decimal('10.00'))

    def test_repair_part_blocks_insufficient_stock(self):
        job = self._job()
        with self.assertRaises(ValidationError):
            RepairPart.objects.create(
                repair_job=job, product=self.product,
                quantity=50, unit_price=Decimal('500.00'),
            )

    # ── B9: same advance cannot be applied twice beyond its amount ──
    def test_advance_cannot_be_double_spent(self):
        advance = Payment.objects.create(
            contact=self.customer, direction='received',
            amount=Decimal('100.00'), is_advance=True, method='cash',
        )
        inv1 = Invoice.objects.create(
            customer=self.customer, date=timezone.localdate(),
        )
        InvoiceItem.objects.create(
            invoice=inv1, product=self.product,
            quantity=Decimal('1'), unit_price=Decimal('100.00'),
            tax_rate=Decimal('0'),
        )
        inv2 = Invoice.objects.create(
            customer=self.customer, date=timezone.localdate(),
        )
        InvoiceItem.objects.create(
            invoice=inv2, product=self.product,
            quantity=Decimal('1'), unit_price=Decimal('100.00'),
            tax_rate=Decimal('0'),
        )

        applied1 = inv1.apply_available_advance()
        applied2 = inv2.apply_available_advance()
        self.assertTrue(applied1)
        self.assertFalse(applied2, 'same advance settled twice')

        total_settled = sum(
            (a.amount for a in advance.advance_adjustments.all()),
            Decimal('0'),
        )
        self.assertLessEqual(total_settled, Decimal('100.00'))

    # ── B12: date defaults must be IST local date, not UTC datetime ──
    def test_date_field_defaults_use_localdate(self):
        for model in (Invoice, Payment, AdvanceAdjustment):
            default = model._meta.get_field('date').default
            self.assertIs(
                default, timezone.localdate,
                f'{model.__name__}.date default is not localdate',
            )

    # ── B23: Indian money grouping ──
    def test_rupee_filter_groups_indian_digits(self):
        self.assertEqual(rupee(Decimal('4500000')), '45,00,000.00')
        self.assertEqual(rupee(Decimal('1234.5')), '1,234.50')
        self.assertEqual(rupee(Decimal('999')), '999.00')
        self.assertEqual(rupee(Decimal('-12345')), '-12,345.00')
        self.assertEqual(rupee(None), '0.00')
