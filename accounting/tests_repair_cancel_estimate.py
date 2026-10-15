"""
Repair cancel / estimate decision (hold/reject/re-approve) — HTTP journey tests.

Chalane:
    python manage.py test accounting.tests_repair_cancel_estimate -v 2
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounting.models import Contact, Invoice, Product, RepairJob, RepairPart
from accounting.utils.tracking import generate_tracking_token

User = get_user_model()


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class CancelAndEstimateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='cancel_staff', password='TestPass123!',
            is_staff=True, is_active=True,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)
        suffix = timezone.now().strftime('%H%M%S%f')[:8]
        self.customer = Contact.objects.create(
            contact_type='customer', name=f'Cancel Cust {suffix}',
            phone=f'7{suffix}'.ljust(10, '0')[:10],
        )
        self.ssd = Product.objects.create(
            name=f'Cancel SSD {suffix}', selling_price=Decimal('2500.00'),
            current_stock=Decimal('20.00'), is_service=False, is_active=True,
            tax_rate=Decimal('0.00'),
        )

    # ── helpers ──
    def _job(self, status='received', **kw):
        return RepairJob.objects.create(
            customer=self.customer, device_model='Cancel Laptop',
            issue_description='x', status=status, **kw,
        )

    def _add_part(self, job):
        return self.client.post(
            reverse('accounting:add_repair_part', args=[job.pk]),
            {'product': self.ssd.pk, 'quantity': 1, 'unit_price': '2500.00'},
            headers={'HX-Request': 'true'},
        )

    def _cancel(self, job, reason='Customer declined', charge=''):
        return self.client.post(
            reverse('accounting:update_repair_status', args=[job.pk]),
            {'new_status': 'cancelled',
             'cancel_reason': reason, 'cancel_charge': charge},
            headers={'HX-Request': 'true'},
        )

    # ══════════════ 1. CANCEL FROM ANY OPEN STATUS ══════════════
    def test_cancel_from_ready_allowed(self):
        job = self._job(status='ready')
        self._add_part(job)
        self.ssd.refresh_from_db()
        before = self.ssd.current_stock

        resp = self._cancel(job, reason='pickup nahi kiya')
        self.assertIn(resp.status_code, (200, 204))
        job.refresh_from_db()
        self.ssd.refresh_from_db()

        self.assertEqual(job.status, 'cancelled')
        self.assertEqual(job.cancellation_reason, 'pickup nahi kiya')
        self.assertIsNotNone(job.cancelled_at, 'cancelled_at stamp nahi hua')
        self.assertEqual(
            self.ssd.current_stock, before + Decimal('1.00'),
            'cancel par stock wapas nahi aaya',
        )
        self.assertEqual(
            RepairPart.objects.filter(repair_job=job).count(), 0,
            'cancel ke baad parts delete nahi hue',
        )

    def test_cancel_requires_reason(self):
        job = self._job(status='received')
        resp = self.client.post(
            reverse('accounting:update_repair_status', args=[job.pk]),
            {'new_status': 'cancelled'},
            headers={'HX-Request': 'true'},
        )
        self.assertEqual(resp.status_code, 400)
        job.refresh_from_db()
        self.assertEqual(job.status, 'received', 'reason ke bina cancel ho gaya')

    def test_cancel_with_no_charge_creates_no_invoice(self):
        job = self._job(status='received')
        resp = self._cancel(job, reason='duplicate entry', charge='0')
        self.assertIn(resp.status_code, (200, 204))
        job.refresh_from_db()
        self.assertEqual(job.status, 'cancelled')
        self.assertIsNone(job.invoice_id)
        self.assertEqual(job.final_amount, Decimal('0.00'))

    def test_cancel_with_charge_creates_invoice(self):
        job = self._job(status='ready')
        self._add_part(job)

        resp = self._cancel(job, reason='customer declined repair', charge='350')
        self.assertIn(resp.status_code, (200, 204))
        job.refresh_from_db()

        self.assertEqual(job.status, 'cancelled')
        self.assertIsNotNone(job.invoice_id, 'charge invoice nahi bana')
        self.assertEqual(job.cancellation_charge, Decimal('350.00'))
        self.assertEqual(job.final_amount, Decimal('350.00'))

        inv = job.invoice
        self.assertEqual(inv.grand_total, Decimal('350.00'),
                         f'invoice total galat: {inv.grand_total}')
        self.assertEqual(inv.balance_due, Decimal('350.00'))
        self.assertIn(job.job_number, inv.notes or '')

    def test_delivered_cannot_cancel_without_force(self):
        job = self._job(status='delivered')
        job.delivered_at = timezone.now()
        job.save(update_fields=['delivered_at'])
        resp = self._cancel(job)
        self.assertEqual(resp.status_code, 400)
        job.refresh_from_db()
        self.assertEqual(job.status, 'delivered')

    # ══════════════ 2. ESTIMATE HOLD / REJECT / RE-APPROVE ══════════════
    def test_staff_hold(self):
        job = self._job(status='diagnosis', estimated_cost=Decimal('1000'))
        resp = self.client.post(
            reverse('accounting:staff_estimate_hold', args=[job.pk]),
            {'source': 'staff_phone', 'remarks': 'sochne dega'},
        )
        self.assertIn(resp.status_code, (302, 200))
        job.refresh_from_db()
        self.assertEqual(job.estimate_status, 'on_hold')
        self.assertIsNotNone(job.estimate_decided_at)

    def test_staff_reject_only_estimate(self):
        job = self._job(status='diagnosis', estimated_cost=Decimal('1000'))
        resp = self.client.post(
            reverse('accounting:staff_estimate_reject', args=[job.pk]),
            {'source': 'staff_phone', 'remarks': 'cost zyada',
             'cancel_job': '0'},
        )
        self.assertIn(resp.status_code, (302, 200))
        job.refresh_from_db()
        self.assertEqual(job.estimate_status, 'rejected')
        self.assertEqual(job.status, 'diagnosis', 'sirf reject par job cancel ho gayi')

    def test_staff_reject_with_cancel_and_charge(self):
        job = self._job(status='diagnosis', estimated_cost=Decimal('1000'))
        resp = self.client.post(
            reverse('accounting:staff_estimate_reject', args=[job.pk]),
            {'source': 'staff_whatsapp', 'remarks': 'customer mana',
             'cancel_job': '1', 'cancel_reason': 'estimate reject',
             'cancel_charge': '200'},
        )
        self.assertIn(resp.status_code, (302, 200))
        job.refresh_from_db()
        self.assertEqual(job.estimate_status, 'rejected')
        self.assertEqual(job.status, 'cancelled')
        self.assertIsNotNone(job.invoice_id)
        self.assertEqual(job.invoice.grand_total, Decimal('200.00'))

    def test_rejected_estimate_blocks_forward_work(self):
        job = self._job(status='diagnosis', estimated_cost=Decimal('1000'))
        job.estimate_status = 'rejected'
        job.save(update_fields=['estimate_status'])
        self._add_part(job)

        resp = self.client.post(
            reverse('accounting:update_repair_status', args=[job.pk]),
            {'new_status': 'repairing'},
            headers={'HX-Request': 'true'},
        )
        self.assertEqual(resp.status_code, 400)
        job.refresh_from_db()
        self.assertEqual(job.status, 'diagnosis',
                         'rejected estimate par repairing block nahi hui')

    def test_re_approve_from_rejected(self):
        job = self._job(status='diagnosis', estimated_cost=Decimal('1000'))
        job.estimate_status = 'rejected'
        job.estimate_decided_at = timezone.now()
        job.save(update_fields=['estimate_status', 'estimate_decided_at'])

        resp = self.client.post(
            reverse('accounting:staff_approve_estimate', args=[job.pk]),
            {'source': 'staff_phone', 'remarks': 'ab maan gaya'},
        )
        self.assertIn(resp.status_code, (302, 200))
        job.refresh_from_db()
        self.assertEqual(job.estimate_status, 'approved')
        self.assertIsNone(job.estimate_decided_at,
                          're-approve par decided_at clear nahi hua')

    def test_approve_already_approved_blocked(self):
        job = self._job(status='diagnosis', estimated_cost=Decimal('1000'))
        job.estimate_status = 'approved'
        job.save(update_fields=['estimate_status'])
        resp = self.client.get(
            reverse('accounting:staff_approve_estimate', args=[job.pk]))
        self.assertIn(resp.status_code, (302,))

    # ══════════════ 3. TRACKING PAGE SHOWS CANCELLATION ══════════════
    def test_tracking_page_shows_cancellation(self):
        job = self._job(status='ready')
        self._add_part(job)
        self._cancel(job, reason='diagnosis fee pending', charge='300')
        job.refresh_from_db()

        token = generate_tracking_token(job)
        resp = self.client.get(reverse('tracking:repair_track', args=[token]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'diagnosis fee pending')
        self.assertContains(resp, 'Job Cancelled')
