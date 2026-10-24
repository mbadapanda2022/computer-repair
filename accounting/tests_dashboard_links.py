"""
Dashboard stat-card deep-link tests.

The "Pending Invoices"/"Total Due"/"Pending Repairs" cards use composite
filter tokens (status=pending / status=open) — the list content must match
the card's count exactly.

    python manage.py test accounting.tests_dashboard_links -v 2
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounting.models import Contact, Invoice, RepairJob

User = get_user_model()


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class DashboardCardLinkTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='dash_staff', password='TestPass123!',
            is_staff=True, is_active=True,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)
        suffix = timezone.now().strftime('%H%M%S%f')[:8]
        self.customer = Contact.objects.create(
            contact_type='customer', name=f'Dash Cust {suffix}',
            phone='8' + suffix,
        )

    def make_invoice(self, status):
        total = Decimal('1000.00')
        paid = {'paid': total, 'partial': Decimal('400.00'), 'unpaid': Decimal('0')}[status]
        return Invoice.objects.create(
            customer=self.customer,
            subtotal=total, grand_total=total,
            paid_amount=paid,
            balance_due=total - paid,
            payment_status=status,
        )

    def make_job(self, status):
        return RepairJob.objects.create(
            customer=self.customer, device_model='Dash Test Device',
            issue_description='link test', status=status,
        )

    # ---------- invoice status=pending ----------

    def test_invoice_list_pending_shows_unpaid_and_partial_only(self):
        unpaid = self.make_invoice('unpaid')
        partial = self.make_invoice('partial')
        paid = self.make_invoice('paid')

        resp = self.client.get(
            reverse('accounting:invoice_list') + '?status=pending'
        )
        self.assertEqual(resp.status_code, 200)
        shown = {inv.pk for inv in resp.context['invoices']}
        self.assertEqual(shown, {unpaid.pk, partial.pk})
        self.assertNotIn(paid.pk, shown)

    def test_invoice_pending_count_matches_dashboard_card(self):
        for status in ('unpaid', 'partial', 'partial', 'paid'):
            self.make_invoice(status)

        dash = self.client.get(reverse('accounting:dashboard'))
        self.assertEqual(
            dash.context['pending_invoices'],
            Invoice.objects.filter(
                payment_status__in=Invoice.OUTSTANDING_STATUSES
            ).count(),
        )

        resp = self.client.get(
            reverse('accounting:invoice_list') + '?status=pending'
        )
        self.assertEqual(resp.context['page_obj'].paginator.count, 3)

    # ---------- repair status=open ----------

    def test_repair_list_open_shows_card_status_set(self):
        open_jobs = [self.make_job(s) for s in RepairJob.OPEN_STATUSES]
        delivered = self.make_job('delivered')
        cancelled = self.make_job('cancelled')

        resp = self.client.get(
            reverse('accounting:repair_list') + '?status=open'
        )
        self.assertEqual(resp.status_code, 200)
        shown = {job.pk for job in resp.context['jobs']}
        self.assertEqual(shown, {job.pk for job in open_jobs})
        for excluded in (delivered, cancelled):
            self.assertNotIn(excluded.pk, shown)

    def test_repair_open_count_matches_dashboard_card(self):
        for s in RepairJob.OPEN_STATUSES:
            self.make_job(s)
            self.make_job(s)
        self.make_job('delivered')

        dash = self.client.get(reverse('accounting:dashboard'))
        resp = self.client.get(
            reverse('accounting:repair_list') + '?status=open'
        )
        self.assertEqual(
            dash.context['pending_repairs'],
            resp.context['page_obj'].paginator.count,
        )

    # ---------- single statuses still work ----------

    def test_single_status_filters_unaffected(self):
        inv_paid = self.make_invoice('paid')
        resp = self.client.get(
            reverse('accounting:invoice_list') + '?status=paid'
        )
        shown = {inv.pk for inv in resp.context['invoices']}
        self.assertEqual(shown, {inv_paid.pk})

        job_ready = self.make_job('ready')
        self.make_job('repairing')
        resp = self.client.get(
            reverse('accounting:repair_list') + '?status=ready'
        )
        shown = {job.pk for job in resp.context['jobs']}
        self.assertEqual(shown, {job_ready.pk})

    # ---------- cards are links ----------

    def test_dashboard_cards_render_deep_links(self):
        dash = self.client.get(reverse('accounting:dashboard'))
        self.assertEqual(dash.status_code, 200)
        content = dash.content.decode()
        self.assertIn('status=open', content)
        self.assertIn('status=pending', content)
        self.assertIn('stock_filter=low', content)
        self.assertIn('direction=received', content)
