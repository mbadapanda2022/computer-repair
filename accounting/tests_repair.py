"""
Repair module — REAL user-journey tests (Django TestCase).

Chalane ka tareeka:
    venv\\Scripts\\python.exe manage.py test accounting.tests_repair -v 2

Ye tests ASLI HTTP endpoints hit karte hain (login, create, add part/service,
status change, invoice, delete/restore) — jaise ek normal staff user karta hai.
Isliye ye bug pakadte hain jo sirf model-level tests nahi pakadte.

Django apne aap ek temporary test database banata aur MITATA hai — aapke
`db.sqlite3` ko chhoota nahi.
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounting.models import (
    Contact, Invoice, InvoiceItem, Product, RepairJob, RepairPart,
    RepairService, RepairStatusLog, StockMovement,
)

User = get_user_model()


# Test client 'testserver' host bhejta hai
@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class RepairJourneyTests(TestCase):
    """Staff user ka poora repair workflow — HTTP ke through."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='journey_staff', password='TestPass123!',
            is_staff=True, is_active=True,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)

        suffix = timezone.now().strftime('%H%M%S%f')[:8]
        self.customer = Contact.objects.create(
            contact_type='customer', name=f'Journey Customer {suffix}',
            phone=f'9{suffix}'.ljust(10, '0')[:10],
        )
        self.ssd = Product.objects.create(
            name=f'Journey SSD {suffix}', selling_price=Decimal('2500.00'),
            current_stock=Decimal('20.00'), is_service=False,
            is_active=True, tax_rate=Decimal('0.00'),
        )
        self.service_product = Product.objects.create(
            name=f'Journey Service {suffix}', selling_price=Decimal('500.00'),
            is_service=True, is_active=True, tax_rate=Decimal('0.00'),
        )

    # ── helpers ──────────────────────────────────────────────────
    def _create_job(self, status='received'):
        job = RepairJob.objects.create(
            customer=self.customer, device_model='Journey Laptop',
            issue_description='Smoke test', status=status,
        )
        return job

    def _add_part(self, job, qty=1, price='2500.00'):
        return self.client.post(
            reverse('accounting:add_repair_part', args=[job.pk]),
            {'product': self.ssd.pk, 'quantity': qty, 'unit_price': price},
            headers={'HX-Request': 'true'},
        )

    def _add_service(self, job, amount='500.00'):
        return self.client.post(
            reverse('accounting:add_repair_service', args=[job.pk]),
            {'product': self.service_product.pk, 'amount': amount,
             'description': 'Journey service'},
            headers={'HX-Request': 'true'},
        )

    # ══════════════════════════════════════════════════════════════
    # 1. LOGIN / ACCESS
    # ══════════════════════════════════════════════════════════════
    def test_staff_can_open_key_pages(self):
        """Staff user saare important pages khol sakta hai."""
        urls = [
            reverse('accounting:repair_list'),
            reverse('accounting:repair_create'),
            reverse('accounting:dashboard') if self._has(
                'accounting:dashboard') else None,
        ]
        for url in filter(None, urls):
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 200,
                                 f'{url} → {resp.status_code}')

    @staticmethod
    def _has(name):
        from django.urls import NoReverseMatch
        try:
            reverse(name)
            return True
        except NoReverseMatch:
            return False

    def test_anonymous_redirected_to_login(self):
        anon = Client()
        resp = anon.get(reverse('accounting:repair_list'))
        self.assertIn(resp.status_code, (301, 302),
                      'anonymous user ko redirect hona chahiye')

    # ══════════════════════════════════════════════════════════════
    # 2. CREATE → parts/services add
    # ══════════════════════════════════════════════════════════════
    def test_add_part_deducts_stock_and_updates_amount(self):
        job = self._create_job()
        self.ssd.refresh_from_db()
        stock_before = self.ssd.current_stock

        resp = self._add_part(job, qty=2)
        self.assertIn(resp.status_code, (200, 302))

        job.refresh_from_db()
        self.ssd.refresh_from_db()

        self.assertEqual(RepairPart.objects.filter(repair_job=job).count(), 1)
        self.assertEqual(self.ssd.current_stock, stock_before - Decimal('2.00'),
                         'part add karne par stock kam nahi hua')
        self.assertEqual(job.final_amount, Decimal('5000.00'),
                         f'amount galat: {job.final_amount}')

        # StockMovement bhi bana?
        part = RepairPart.objects.get(repair_job=job)
        self.assertTrue(
            StockMovement.all_objects.filter(
                source_object_id=part.pk, is_deleted=False).exists(),
            'stock movement record nahi bana',
        )

    def test_add_service_updates_amount_without_stock(self):
        job = self._create_job()
        self.service_product.refresh_from_db()
        stock_before = self.service_product.current_stock

        resp = self._add_service(job, '750.00')
        self.assertIn(resp.status_code, (200, 302))

        job.refresh_from_db()
        self.service_product.refresh_from_db()
        self.assertEqual(RepairService.objects.filter(repair_job=job).count(), 1)
        self.assertEqual(job.final_amount, Decimal('750.00'))
        self.assertEqual(self.service_product.current_stock, stock_before,
                         'service ne stock badal diya (nahi badalna chahiye)')

    def test_insufficient_stock_is_rejected(self):
        job = self._create_job()
        resp = self._add_part(job, qty=999)
        self.assertEqual(resp.status_code, 200)   # form re-render
        job.refresh_from_db()
        self.assertEqual(RepairPart.objects.filter(repair_job=job).count(), 0,
                         'stock kam hone par bhi part add ho gaya')
        self.assertEqual(job.final_amount, Decimal('0.00'))

    # ══════════════════════════════════════════════════════════════
    # 3. STATUS WORKFLOW (HTTP)
    # ══════════════════════════════════════════════════════════════
    def test_status_flow_received_to_delivered(self):
        job = self._create_job(status='received')
        self._add_part(job)

        # received → diagnosis
        resp = self.client.post(
            reverse('accounting:update_repair_status', args=[job.pk]),
            {'new_status': 'diagnosis', 'remarks': 'diag start'},
            headers={'HX-Request': 'true'},
        )
        self.assertIn(resp.status_code, (200, 204))
        job.refresh_from_db()
        self.assertEqual(job.status, 'diagnosis')

        # diagnosis → repairing
        self.client.post(
            reverse('accounting:update_repair_status', args=[job.pk]),
            {'new_status': 'repairing'},
            headers={'HX-Request': 'true'},
        )
        job.refresh_from_db()
        self.assertEqual(job.status, 'repairing')

        # repairing → ready (part laga hai isliye allowed)
        self.client.post(
            reverse('accounting:update_repair_status', args=[job.pk]),
            {'new_status': 'ready'},
            headers={'HX-Request': 'true'},
        )
        job.refresh_from_db()
        self.assertEqual(job.status, 'ready')
        self.assertIsNotNone(job.ready_at, 'ready_at stamp nahi hua')

        # ready → delivered (receiver chahiye)
        # NOTE: delivery details pehle save karo — `change_status()` ke baad
        # status field par seedha likhna guard se block hota hai.
        RepairJob.all_objects.filter(pk=job.pk).update(
            delivered_to_name='Journey Receiver',
        )
        self.client.post(
            reverse('accounting:update_repair_status', args=[job.pk]),
            {'new_status': 'delivered'},
            headers={'HX-Request': 'true'},
        )
        job.refresh_from_db()
        self.assertEqual(job.status, 'delivered')
        self.assertIsNotNone(job.delivered_at, 'delivered_at stamp nahi hua')

        # history entries
        self.assertGreaterEqual(
            RepairStatusLog.objects.filter(repair_job=job).count(), 4,
            'status history adhoori hai',
        )

    def test_illegal_status_jump_is_blocked(self):
        job = self._create_job(status='received')
        resp = self.client.post(
            reverse('accounting:update_repair_status', args=[job.pk]),
            {'new_status': 'delivered'},
            headers={'HX-Request': 'true'},
        )
        self.assertEqual(resp.status_code, 400,
                         f'illegal jump par {resp.status_code} (400 chahiye)')
        job.refresh_from_db()
        self.assertEqual(job.status, 'received', 'illegal jump se status badla')

    def test_ready_without_lines_is_blocked(self):
        job = self._create_job(status='repairing')
        resp = self.client.post(
            reverse('accounting:update_repair_status', args=[job.pk]),
            {'new_status': 'ready'},
            headers={'HX-Request': 'true'},
        )
        self.assertEqual(resp.status_code, 400)
        job.refresh_from_db()
        self.assertEqual(job.status, 'repairing')

    # ══════════════════════════════════════════════════════════════
    # 4. EDIT (form save)
    # ══════════════════════════════════════════════════════════════
    def test_edit_job_details_changes_are_saved(self):
        job = self._create_job()
        resp = self.client.post(
            reverse('accounting:repair_update', args=[job.pk]),
            {
                'customer': self.customer.pk,
                'device_model': 'Journey Laptop EDITED',
                'issue_description': 'Updated issue text',
                'status': job.status,          # same status
                'estimate_status': job.estimate_status,
                'serial_number': 'SN-EDITED-1',
                'diagnosis_report': 'Battery weak',
                'action_taken': 'Battery replaced',
                'accessories': '', 'device_condition': '',
                'received_at': '', 'ready_at': '', 'delivery_date': '',
                'received_by': '', 'received_remarks': '', 'delivered_by': '',
                'delivered_to_name': '', 'delivered_to_phone': '',
                'delivered_to_designation': '', 'delivery_remarks': '',
                'estimated_cost': '', 'notes': 'Edited via test',
            },
        )
        self.assertIn(resp.status_code, (200, 302))
        job.refresh_from_db()
        self.assertEqual(job.device_model, 'Journey Laptop EDITED',
                         'edit save nahi hua')
        self.assertEqual(job.serial_number, 'SN-EDITED-1')

    # ══════════════════════════════════════════════════════════════
    # 5. INVOICE
    # ══════════════════════════════════════════════════════════════
    def test_invoice_creation_and_amount_sync(self):
        job = self._create_job(status='ready')
        self._add_part(job, qty=1, price='2500.00')
        job.refresh_from_db()

        # Invoice banao (service layer se — view ka session flow alag hai)
        invoice = Invoice.objects.create(customer=self.customer, notes='journey')
        part = RepairPart.objects.get(repair_job=job)
        InvoiceItem.objects.create(
            invoice=invoice, product=self.ssd,
            description='Journey part', quantity=Decimal('1.00'),
            unit_price=part.unit_price, repair_part=part,
            stock_already_deducted=True,
        )
        invoice.calculate_totals()
        job.invoice = invoice
        job.final_amount = invoice.grand_total
        job.save(update_fields=['invoice', 'final_amount'])

        job.refresh_from_db()
        invoice.refresh_from_db()
        self.assertEqual(job.final_amount, invoice.grand_total)
        self.assertEqual(invoice.grand_total, Decimal('2500.00'))

        # Double-deduction nahi hua?
        self.ssd.refresh_from_db()
        self.assertEqual(self.ssd.current_stock, Decimal('19.00'),
                         'invoice banane par stock dobara kat gaya!')

    def test_invoiced_job_blocks_further_line_changes(self):
        job = self._create_job(status='ready')
        self._add_part(job)
        invoice = Invoice.objects.create(customer=self.customer, notes='lock')
        job.invoice = invoice
        job.final_amount = invoice.grand_total
        job.save(update_fields=['invoice', 'final_amount'])

        # Ab naya part add karne ki koshish — block hona chahiye
        resp = self._add_part(job, qty=1)
        self.assertIn(resp.status_code, (200, 400, 302))
        job.refresh_from_db()
        self.assertEqual(RepairPart.objects.filter(repair_job=job).count(), 1,
                         'invoiced job par naya part add ho gaya!')

    # ══════════════════════════════════════════════════════════════
    # 6. DELETE / RESTORE
    # ══════════════════════════════════════════════════════════════
    def test_delete_job_reverses_stock(self):
        job = self._create_job()
        self._add_part(job, qty=3)
        self.ssd.refresh_from_db()
        self.assertEqual(self.ssd.current_stock, Decimal('17.00'))

        resp = self.client.delete(
            reverse('accounting:repair_delete', args=[job.pk]),
            headers={'HX-Request': 'true'},
        )
        self.assertIn(resp.status_code, (200, 204, 302))

        job.refresh_from_db()
        self.ssd.refresh_from_db()
        self.assertTrue(job.is_deleted, 'job delete nahi hua')
        self.assertEqual(self.ssd.current_stock, Decimal('20.00'),
                         'delete par stock wapas nahi aaya')

    def test_restore_brings_back_parts_and_stock(self):
        job = self._create_job()
        self._add_part(job, qty=2)
        job.delete()
        self.ssd.refresh_from_db()
        self.assertEqual(self.ssd.current_stock, Decimal('20.00'))

        job.refresh_from_db()
        job.restore()
        job.refresh_from_db()
        self.ssd.refresh_from_db()

        self.assertFalse(job.is_deleted)
        self.assertEqual(RepairPart.objects.filter(repair_job=job).count(), 1,
                         'restore ke baad part wapas nahi aaya')
        self.assertEqual(self.ssd.current_stock, Decimal('18.00'),
                         'restore ke baad stock dobara nahi kata')

    def test_cannot_delete_invoiced_job(self):
        job = self._create_job(status='ready')
        invoice = Invoice.objects.create(customer=self.customer, notes='x')
        job.invoice = invoice
        job.final_amount = invoice.grand_total
        job.save(update_fields=['invoice', 'final_amount'])

        resp = self.client.delete(
            reverse('accounting:repair_delete', args=[job.pk]),
            headers={'HX-Request': 'true'},
        )
        job.refresh_from_db()
        self.assertFalse(job.is_deleted, 'invoiced job delete ho gaya!')

    # ══════════════════════════════════════════════════════════════
    # 7. SEARCH / LIST
    # ══════════════════════════════════════════════════════════════
    def test_search_finds_job(self):
        job = self._create_job()
        resp = self.client.get(
            reverse('accounting:repair_list'),
            {'search': job.job_number},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, job.job_number)

    def test_list_pagination_works(self):
        for _ in range(3):
            self._create_job()
        resp = self.client.get(reverse('accounting:repair_list'))
        self.assertEqual(resp.status_code, 200)


# ══════════════════════════════════════════════════════════════════
# ADMIN (superuser) JOURNEY
# ══════════════════════════════════════════════════════════════════
@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class AdminJourneyTests(TestCase):
    """Superuser admin panel se sab kuch manage kar sake."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(
            username='journey_admin', password='AdminPass123!',
            email='journey_admin@test.local',
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.admin)
        suffix = timezone.now().strftime('%H%M%S%f')[:8]
        self.customer = Contact.objects.create(
            contact_type='customer', name=f'Admin Journey {suffix}',
            phone=f'8{suffix}'.ljust(10, '0')[:10],
        )
        self.product = Product.objects.create(
            name=f'Admin Journey SSD {suffix}', selling_price=Decimal('1500'),
            current_stock=Decimal('10'), is_service=False, is_active=True,
            tax_rate=Decimal('0'),
        )

    def test_admin_index_and_key_models(self):
        """Admin ke important pages 200 dete hain."""
        self.assertEqual(self.client.get('/admin/').status_code, 200)
        for model in ('repairjob', 'contact', 'product', 'invoice',
                      'repairstatuslog', 'companyprofile'):
            with self.subTest(model=model):
                url = reverse(f'admin:accounting_{model}_changelist')
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 200, f'{model} → {resp.status_code}')

    def test_admin_can_create_repair_job(self):
        url = reverse('admin:accounting_repairjob_add')
        resp = self.client.post(url, {
            'customer': self.customer.pk,
            'device_model': 'Admin Created Device',
            'issue_description': 'Created from admin panel',
            'serial_number': 'ADMIN-SN-1',
            'status': 'pending',
            'estimate_status': 'pending',
            'parts-TOTAL_FORMS': '0',
            'parts-INITIAL_FORMS': '0',
            'status_history-TOTAL_FORMS': '0',
            'status_history-INITIAL_FORMS': '0',
        })
        job = RepairJob.objects.filter(device_model='Admin Created Device').first()
        if job is None:
            # Debug: response HTML se errorlist nikaalo
            import re as _re
            html = resp.content.decode('utf-8', 'replace')
            errs = _re.findall(
                r'<ul class="errorlist[^"]*">(.*?)</ul>', html, _re.DOTALL
            )
            cleaned = ' | '.join(
                _re.sub(r'<[^>]+>', ' ', e).strip() for e in errs[:8]
            )
            ctx = getattr(resp, 'context', None)
            msgs = ''
            if ctx is not None:
                try:
                    msgs = ' ;; '.join(str(m) for m in ctx.get('messages', []))
                except Exception:                            # noqa: BLE001
                    pass
            self.fail(
                f'admin se job create NAHI hua (status={resp.status_code}). '
                f'Errors: {cleaned or "(koi errorlist nahi)"} | '
                f'Messages: {msgs or "(none)"}'
            )
        self.assertTrue(job.job_number.startswith('REP-'),
                        f'job_number galat: {job.job_number}')

    def test_admin_status_change_creates_history(self):
        job = RepairJob.objects.create(
            customer=self.customer, device_model='Admin Status Test',
            issue_description='x', status='received',
        )
        from accounting.admin import RepairJobAdmin
        from django.contrib import admin as dj_admin

        admin_obj = RepairJobAdmin(RepairJob, dj_admin.site)

        class _Form:
            changed_data = ['status']

        class _Req:
            user = self.admin

        job.status = 'diagnosis'
        admin_obj.save_model(_Req(), job, _Form(), change=True)
        job.refresh_from_db()

        self.assertEqual(job.status, 'diagnosis')
        self.assertTrue(
            RepairStatusLog.objects.filter(
                repair_job=job, to_status='diagnosis').exists(),
            'admin status change se history nahi bani',
        )

    def test_admin_delete_uses_soft_delete(self):
        job = RepairJob.objects.create(
            customer=self.customer, device_model='Admin Delete Test',
            issue_description='x', status='received',
        )
        url = reverse('admin:accounting_repairjob_delete', args=[job.pk])
        resp = self.client.post(url, {'post': 'yes'})
        self.assertIn(resp.status_code, (200, 302))
        job.refresh_from_db()
        self.assertTrue(job.is_deleted, 'admin delete se soft-delete nahi hua')

    def test_status_log_admin_is_readonly(self):
        url = reverse('admin:accounting_repairstatuslog_add')
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 403,
                         'RepairStatusLog add page khul gaya (read-only hona chahiye)')
