"""Free UPI QR payment (no gateway) — tests.

Run:  python manage.py test accounting.tests_upi_payment -v 2

Design the tests protect: a customer's "I have paid" note is only a claim.
Money enters the ledger exclusively through staff verification, which creates an
ordinary Payment + PaymentAllocation — so invoice totals can never drift from
what the shop actually received.
"""
from __future__ import annotations

import datetime
import html as html_module
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounting.forms import CompanyProfileForm
from accounting.models import (
    BankAccount, BankTransaction, CompanyProfile, Contact, Invoice,
    InvoiceItem, Notification, Payment, PaymentAllocation, Product,
    RepairJob, UpiPaymentRequest,
)
from accounting.utils.tracking import generate_tracking_token
from accounting.utils.upi import (
    build_upi_uri, invoice_upi_options, qr_data_uri,
)

User = get_user_model()


def profile(**over):
    """The singleton, with UPI collection switched on."""
    data = dict(name='A1 Computer Solutions', phone='9800000000',
                whatsapp_number='9800000001',
                upi_id='a1computers@ybl', upi_holder_name='A1 Computer Solutions')
    data.update(over)
    company = CompanyProfile.get_instance()
    for key, value in data.items():
        setattr(company, key, value)
    company.save()
    return company


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class UpiLinkBuilderTests(TestCase):
    """The QR is a plain NPCI deep link — shape matters, apps are strict."""

    def test_exact_amount_link(self):
        company = profile()
        uri = build_upi_uri(company, amount=Decimal('1400.50'),
                            txn_ref='INV-2026-0001', note='Invoice INV-2026-0001')
        self.assertIn('upi://pay?', uri)
        self.assertIn('pa=a1computers%40ybl', uri)
        self.assertIn('am=1400.50', uri)
        self.assertIn('cu=INR', uri)
        self.assertIn('tr=INV-2026-0001', uri)

    def test_flexible_link_has_no_amount(self):
        """No am= is what lets the payer type a partial amount."""
        company = profile()
        uri = build_upi_uri(company, amount=None)
        self.assertIn('pa=a1computers%40ybl', uri)
        self.assertNotIn('am=', uri)

    def test_missing_vpa_produces_no_link(self):
        company = profile(upi_id='', upi_holder_name='')
        self.assertEqual(build_upi_uri(company, amount=Decimal('10')), '')

    def test_holder_name_defaults_to_company_name(self):
        company = profile(upi_holder_name='')
        self.assertIn('pn=A1%20Computer%20Solutions',
                      build_upi_uri(company, amount=Decimal('10')))

    def test_qr_is_inline_png_and_cached(self):
        company = profile()
        uri = build_upi_uri(company, amount=Decimal('99'))
        first = qr_data_uri(uri)
        self.assertTrue(first.startswith('data:image/png;base64,'))
        self.assertEqual(first, qr_data_uri(uri))
        self.assertEqual(qr_data_uri(''), '')


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class CompanyUpiFieldTests(TestCase):
    """Settings must accept a normal VPA and reject junk, so no broken QR."""

    def test_valid_vpa_saved(self):
        for vpa in ('a1computers@ybl', '9876543210@paytm', 'shop-name@okhdfcbank',
                    'Name.123@ybl'):
            company = profile(upi_id=vpa)
            self.assertTrue(company.accepts_upi)
            self.assertEqual(company.upi_id, vpa)

    def test_blank_vpa_is_allowed_and_hides_every_qr(self):
        company = profile(upi_id='')
        self.assertFalse(company.accepts_upi)
        self.assertIsNone(invoice_upi_options(
            Invoice.objects.create(customer=Contact.objects.create(
                name='Blank Cust', phone='9811100011', contact_type='customer'),
                subtotal=Decimal('100'), grand_total=Decimal('100'),
                balance_due=Decimal('100')), company))

    def test_invalid_vpa_rejected(self):
        for vpa in ('not-a-vpa', '@ybl', 'a@', 'a@b', 'a b@c', 'x@y@z'):
            company = profile()
            company.upi_id = vpa
            with self.subTest(vpa=vpa):
                with self.assertRaises(ValidationError):
                    company.full_clean()


class UpiTestCase(TestCase):
    """Shared fixtures for the claim / verify flows."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='upi_staff', password='TestPass123!',
            is_staff=True, is_active=True)
        cls.portal_user = User.objects.create_user(
            username='upi_customer', password='TestPass123!', is_active=True)
        cls.customer = Contact.objects.create(
            name='UPI Customer', phone='9845111222', contact_type='customer',
            user=cls.portal_user)
        cls.other_customer = Contact.objects.create(
            name='Other Customer', phone='9845111333', contact_type='customer')

    def setUp(self):
        self.company = profile()
        self.client = Client()

    def invoice(self, total='1500.00', customer=None):
        """A real invoice has lines — Invoice.save() derives grand_total from
        them, so a header-only fixture would silently be worth ₹0."""
        amount = Decimal(total)
        inv = Invoice.objects.create(customer=customer or self.customer)
        product = Product.objects.create(
            name=f'UPI Svc {total} {timezone.now().strftime("%H%M%S%f")[:10]}',
            selling_price=amount, purchase_price=amount,
            tax_rate=Decimal('0'), current_stock=Decimal('0'),
            is_service=True, is_active=True,
        )
        InvoiceItem.objects.create(
            invoice=inv, product=product, quantity=Decimal('1'),
            unit_price=amount, tax_rate=Decimal('0'),
        )
        inv.refresh_from_db()
        return inv

    def mark_settled(self, inv, amount):
        """Force the stored settlement fields.

        ``Invoice.save()`` recomputes them from allocations, so a fixture that
        just assigns balance_due/paid_amount would be rejected; the queryset
        update writes the state the screen is being tested against.
        """
        Invoice.objects.filter(pk=inv.pk).update(
            balance_due=Decimal('0'),
            paid_amount=Decimal(amount),
            payment_status='paid',
        )
        inv.refresh_from_db()

    def claim(self, invoice=None, amount='1500.00', reference='4630123456789012'):
        return UpiPaymentRequest.objects.create(
            invoice=invoice or self.invoice(), contact=self.customer,
            amount=Decimal(amount), upi_reference=reference,
            paid_on=timezone.localdate())

    def pay_url(self, invoice):
        return reverse('customer:customer_upi_pay', args=[invoice.pk])

    def claim_post(self, invoice, **over):
        """POST the pay screen form. `reference=` maps to the form field name."""
        reference = over.pop('reference', None)
        data = {'amount': '1500.00', 'upi_reference': '4630999888777666',
                'paid_on': timezone.localdate().isoformat(), 'note': ''}
        if reference is not None:
            data['upi_reference'] = reference
        data.update(over)
        return self.client.post(self.pay_url(invoice), data)


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class CustomerPayScreenTests(UpiTestCase):

    def test_login_required(self):
        inv = self.invoice()
        r = self.client.get(self.pay_url(inv))
        self.assertEqual(r.status_code, 302)
        self.assertIn('login', r.url.lower())

    def test_other_customers_invoice_not_found(self):
        self.client.force_login(self.portal_user)
        inv = self.invoice(customer=self.other_customer)
        self.assertEqual(self.client.get(self.pay_url(inv)).status_code, 404)

    def test_screen_shows_both_qr_and_form(self):
        self.client.force_login(self.portal_user)
        inv = self.invoice('2300.00')
        r = self.client.get(self.pay_url(inv))
        self.assertContains(r, 'Scan and pay')
        self.assertContains(r, 'Any amount')
        self.assertContains(r, 'data:image/png;base64,')
        self.assertContains(r, 'a1computers@ybl')
        self.assertContains(r, 'I have paid by UPI')
        self.assertContains(r, '2,300.00')

    def test_screen_offers_nothing_without_a_vpa(self):
        profile(upi_id='')
        self.client.force_login(self.portal_user)
        r = self.client.get(self.pay_url(self.invoice()))
        self.assertContains(r, 'not available right now')
        self.assertNotContains(r, 'data:image/png;base64,')

    def test_fully_paid_invoice_shows_nothing_to_pay(self):
        self.client.force_login(self.portal_user)
        inv = self.invoice('500.00')
        self.mark_settled(inv, '500')
        r = self.client.get(self.pay_url(inv))
        self.assertContains(r, 'fully paid')
        self.assertNotContains(r, 'data:image/png;base64,')

    def test_settled_invoice_still_shows_own_payment_reports(self):
        """The customer's own trail must survive settlement — that page is
        where they can see the shop finally accepted their UPI reference."""
        self.client.force_login(self.portal_user)
        inv = self.invoice('600.00')
        self.claim(inv, amount='600.00', reference='4630SETTLED01')
        self.mark_settled(inv, '600')

        r = self.client.get(self.pay_url(inv))
        self.assertContains(r, 'Your payment reports')
        self.assertContains(r, '4630SETTLED01')
        self.assertContains(r, '₹600.00')


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class CustomerClaimTests(UpiTestCase):
    """A claim informs the shop and moves no money."""

    def test_valid_claim_is_recorded_and_staff_notified(self):
        self.client.force_login(self.portal_user)
        inv = self.invoice('1200.00')
        r = self.claim_post(inv, amount='1200.00')
        self.assertEqual(r.status_code, 302)

        claim = UpiPaymentRequest.objects.get()
        self.assertEqual(claim.invoice, inv)
        self.assertEqual(claim.contact, self.customer)
        self.assertEqual(claim.status, 'pending')
        self.assertEqual(claim.amount, Decimal('1200.00'))
        self.assertEqual(claim.submitted_by, self.portal_user)
        self.assertIsNone(claim.payment)

        # Nothing was booked — a claim is only a report.
        inv.refresh_from_db()
        self.assertEqual(inv.paid_amount, Decimal('0'))
        self.assertEqual(inv.balance_due, Decimal('1200.00'))
        self.assertEqual(inv.payment_status, 'unpaid')
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(PaymentAllocation.objects.count(), 0)

        notice = Notification.objects.filter(category='payment').first()
        self.assertIsNotNone(notice)
        # Staff read money the same way the screen shows it — Indian grouping.
        self.assertIn('₹1,200.00', notice.message)
        self.assertIn('4630999888777666', notice.message)

    def test_amount_above_balance_due_rejected(self):
        self.client.force_login(self.portal_user)
        inv = self.invoice('800.00')
        r = self.claim_post(inv, amount='900.00')
        self.assertContains(r, 'more than the balance due of ₹800.00')
        self.assertEqual(UpiPaymentRequest.objects.count(), 0)

    def test_zero_amount_rejected(self):
        self.client.force_login(self.portal_user)
        r = self.claim_post(self.invoice('800.00'), amount='0')
        self.assertEqual(UpiPaymentRequest.objects.count(), 0)
        self.assertContains(r, 'Ensure this value is greater than or equal to')

    def test_duplicate_reference_rejected(self):
        """Double submit must not create two claims for one payment."""
        self.client.force_login(self.portal_user)
        inv = self.invoice('2000.00')
        self.claim_post(inv, amount='1000.00', reference='4630AAAA')
        r = self.claim_post(inv, amount='1000.00', reference='4630AAAA')
        self.assertContains(r, 'already been submitted')
        self.assertEqual(UpiPaymentRequest.objects.count(), 1)

    def test_future_payment_date_rejected(self):
        self.client.force_login(self.portal_user)
        future = (timezone.localdate()
                  + timezone.timedelta(days=2)).isoformat()
        r = self.claim_post(self.invoice(), paid_on=future)
        self.assertContains(r, 'cannot be in the future')
        self.assertEqual(UpiPaymentRequest.objects.count(), 0)

    def test_garbage_reference_rejected(self):
        self.client.force_login(self.portal_user)
        r = self.claim_post(self.invoice(), reference='## %% !!')
        self.assertEqual(UpiPaymentRequest.objects.count(), 0)
        self.assertContains(r, 'exactly as shown in your UPI app')

    def test_part_payment_claim_is_allowed(self):
        self.client.force_login(self.portal_user)
        inv = self.invoice('3000.00')
        self.claim_post(inv, amount='1000.00')
        claim = UpiPaymentRequest.objects.get()
        self.assertEqual(claim.amount, Decimal('1000.00'))
        inv.refresh_from_db()
        self.assertEqual(inv.balance_due, Decimal('3000.00'))

    def test_claim_list_shown_on_screen_after_submitting(self):
        self.client.force_login(self.portal_user)
        inv = self.invoice('1200.00')
        self.claim_post(inv, amount='1200.00', reference='4630SHOW')
        r = self.client.get(self.pay_url(inv))
        self.assertContains(r, 'Your payment reports')
        self.assertContains(r, '4630SHOW')
        self.assertContains(r, 'Checking')


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class StaffVerifyTests(UpiTestCase):
    """Verification is the only thing that books money, via the Payment path."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff)

    def _verify(self, claim, **over):
        data = {'amount': str(claim.amount),
                'paid_on': claim.paid_on.isoformat()}
        data.update(over)
        return self.client.post(
            reverse('accounting:upi_claim_verify', args=[claim.pk]), data)

    def test_portal_customer_cannot_open_claims_console(self):
        self.client.force_login(self.portal_user)
        r = self.client.get(reverse('accounting:upi_claim_list'))
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse('customer:customer_dashboard'), r.url)

    def test_list_page_shows_pending_claim(self):
        claim = self.claim(self.invoice('1500.00'), amount='1500.00',
                           reference='4630LIST')
        r = self.client.get(reverse('accounting:upi_claim_list'))
        self.assertContains(r, 'UPI Payment Claims')
        self.assertContains(r, '4630LIST')
        self.assertContains(r, 'Verify')

    def test_search_filters_claims(self):
        inv = self.invoice('1500.00')
        self.claim(inv, reference='4630FINDME')
        self.claim(self.invoice('700.00', customer=self.other_customer),
                   reference='9999OTHER')
        r = self.client.get(reverse('accounting:upi_claim_list')
                            + '?search=4630FINDME')
        self.assertContains(r, '4630FINDME')
        self.assertNotContains(r, '9999OTHER')

    def test_verify_books_payment_and_marks_invoice_paid(self):
        inv = self.invoice('1500.00')
        claim = self.claim(inv, amount='1500.00', reference='4630FULL')

        r = self._verify(claim)
        self.assertEqual(r.status_code, 302)

        claim.refresh_from_db()
        self.assertEqual(claim.status, 'verified')
        self.assertIsNotNone(claim.payment)
        self.assertEqual(claim.verified_by, self.staff)

        payment = claim.payment
        self.assertEqual(payment.amount, Decimal('1500.00'))
        self.assertEqual(payment.method, 'upi')
        self.assertEqual(payment.direction, 'received')
        self.assertEqual(payment.upi_ref, '4630FULL')
        self.assertEqual(payment.reference, inv.invoice_number)
        self.assertFalse(payment.is_advance)

        PaymentAllocation.objects.get(payment=payment, invoice=inv,
                                      amount=Decimal('1500.00'))
        inv.refresh_from_db()
        self.assertEqual(inv.paid_amount, Decimal('1500.00'))
        self.assertEqual(inv.balance_due, Decimal('0.00'))
        self.assertEqual(inv.payment_status, 'paid')

    def test_verify_part_payment_keeps_invoice_partial(self):
        inv = self.invoice('2500.00')
        claim = self.claim(inv, amount='1000.00', reference='4630PART')

        self._verify(claim, amount='1000.00')
        inv.refresh_from_db()
        self.assertEqual(inv.paid_amount, Decimal('1000.00'))
        self.assertEqual(inv.balance_due, Decimal('1500.00'))
        self.assertEqual(inv.payment_status, 'partial')

    def test_staff_can_correct_the_amount(self):
        """Customer claims 900, shop actually received 850 — ledger says 850."""
        inv = self.invoice('1000.00')
        claim = self.claim(inv, amount='900.00', reference='4630FIX')

        self._verify(claim, amount='850.00')
        claim.refresh_from_db()
        self.assertEqual(claim.payment.amount, Decimal('850.00'))
        inv.refresh_from_db()
        self.assertEqual(inv.balance_due, Decimal('150.00'))

    def test_verify_above_live_balance_blocked(self):
        """A cash payment can land after the claim was submitted."""
        inv = self.invoice('1000.00')
        claim = self.claim(inv, amount='1000.00', reference='4630OVER')

        cash = Payment.objects.create(direction='received', contact=self.customer,
                                      amount=Decimal('600'), method='cash')
        PaymentAllocation.objects.create(payment=cash, invoice=inv,
                                         amount=Decimal('600'))

        r = self._verify(claim)
        inv.refresh_from_db()
        self.assertEqual(inv.paid_amount, Decimal('600.00'))
        self.assertEqual(inv.balance_due, Decimal('400.00'))
        claim.refresh_from_db()
        self.assertEqual(claim.status, 'pending')
        self.assertContains(self.client.get(r.url), 'current balance due of ₹400.00')

    def test_already_reviewed_claim_cannot_be_verified_twice(self):
        inv = self.invoice('1000.00')
        claim = self.claim(inv, amount='1000.00', reference='4630TWICE')
        self._verify(claim)
        self._verify(claim)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(PaymentAllocation.objects.count(), 1)
        inv.refresh_from_db()
        self.assertEqual(inv.paid_amount, Decimal('1000.00'))

    def test_bank_transaction_only_when_account_chosen(self):
        """A UPI claim without a bank account must not create ledger noise."""
        inv = self.invoice('1000.00')
        claim = self.claim(inv, amount='1000.00', reference='4630NOBANK')
        self._verify(claim)
        self.assertEqual(BankTransaction.objects.count(), 0)

        wallet = BankAccount.objects.create(name='UPI Wallet',
                                            account_type='upi', is_active=True)
        inv2 = self.invoice('1000.00')
        claim2 = self.claim(inv2, amount='1000.00', reference='4630WITHBANK')
        self._verify(claim2, bank_account=wallet.pk)
        self.assertEqual(BankTransaction.objects.count(), 1)
        self.assertEqual(BankTransaction.objects.get().amount,
                         Decimal('1000.00'))

    def test_reject_moves_no_money_and_notifies_customer(self):
        inv = self.invoice('1000.00')
        claim = self.claim(inv, amount='1000.00', reference='4630REJ')

        r = self.client.post(reverse('accounting:upi_claim_reject',
                                     args=[claim.pk]),
                             {'reason': 'No such reference in my Passbook app'})
        self.assertEqual(r.status_code, 302)

        claim.refresh_from_db()
        self.assertEqual(claim.status, 'rejected')
        self.assertEqual(claim.rejection_reason,
                         'No such reference in my Passbook app')
        self.assertIsNone(claim.payment)
        self.assertEqual(Payment.objects.count(), 0)
        inv.refresh_from_db()
        self.assertEqual(inv.balance_due, Decimal('1000.00'))

        notice = Notification.objects.filter(
            recipient=self.portal_user, category='payment').first()
        self.assertIsNotNone(notice)
        self.assertIn('No such reference', notice.message)

    def test_reject_is_post_only(self):
        claim = self.claim(self.invoice(), reference='4630GET')
        r = self.client.get(reverse('accounting:upi_claim_reject',
                                    args=[claim.pk]))
        self.assertEqual(r.status_code, 405)

    def test_rejecting_a_reviewed_claim_is_refused(self):
        inv = self.invoice('1000.00')
        claim = self.claim(inv, amount='1000.00', reference='4630REJ2')
        self._verify(claim)
        self.client.post(reverse('accounting:upi_claim_reject', args=[claim.pk]),
                         {'reason': 'oops'})
        claim.refresh_from_db()
        self.assertEqual(claim.status, 'verified')
        self.assertEqual(Payment.objects.count(), 1)

    def test_action_returns_to_the_tab_it_came_from(self):
        claim = self.claim(self.invoice(), reference='4630NEXT')
        r = self._verify(claim, next='status=verified')
        self.assertIn('/upi-claims/?status=verified', r.url)

    def test_next_cannot_point_off_site(self):
        claim = self.claim(self.invoice(), reference='4630EVIL')
        r = self._verify(claim, next='//evil.example.com/')
        self.assertTrue(r.url.startswith('/upi-claims/'))


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class UpiPlacementTests(UpiTestCase):
    """Every surface the owner asked for must actually carry the code."""

    def test_staff_invoice_page_shows_code_and_claims(self):
        self.client.force_login(self.staff)
        inv = self.invoice('1700.00')
        self.claim(inv, amount='1700.00', reference='4630STAFF')
        r = self.client.get(reverse('accounting:invoice_detail', args=[inv.pk]))
        self.assertContains(r, 'UPI payments')
        self.assertContains(r, 'data:image/png;base64,')
        self.assertContains(r, '4630STAFF')

    def test_staff_print_carries_a_scannable_code(self):
        self.client.force_login(self.staff)
        inv = self.invoice('1700.00')
        r = self.client.get(reverse('accounting:invoice_print', args=[inv.pk]))
        self.assertContains(r, 'Scan to pay ₹1,700.00')
        self.assertContains(r, 'a1computers@ybl')

    def test_staff_print_hides_code_when_settled(self):
        self.client.force_login(self.staff)
        inv = self.invoice('900.00')
        self.mark_settled(inv, '900')
        r = self.client.get(reverse('accounting:invoice_print', args=[inv.pk]))
        self.assertNotContains(r, 'Scan to pay')

    def test_customer_print_carries_a_code(self):
        self.client.force_login(self.portal_user)
        inv = self.invoice('1100.00')
        r = self.client.get(reverse('customer:customer_invoice_print',
                                    args=[inv.pk]))
        self.assertContains(r, 'Scan to pay ₹1,100.00')

    def test_customer_invoice_list_and_detail_link_to_pay(self):
        self.client.force_login(self.portal_user)
        inv = self.invoice('1100.00')
        r = self.client.get(reverse('customer:customer_invoices'))
        self.assertContains(r, self.pay_url(inv))
        r = self.client.get(reverse('customer:customer_invoice_detail',
                                    args=[inv.pk]))
        self.assertContains(r, 'Pay ₹1,100.00 by UPI')

    def test_tracking_page_shows_code_without_login(self):
        inv = self.invoice('1300.00')
        job = RepairJob.objects.create(
            customer=self.customer, device_model='HP 15',
            issue_description='dead battery', invoice=inv, status='ready')
        token = generate_tracking_token(job)
        r = self.client.get(reverse('tracking:repair_track', args=[token]))
        self.assertContains(r, 'Pay ₹1,300.00 by UPI')
        self.assertContains(r, 'wa.me/919800000001')
        self.assertIn('reference%20number%20is%3A',
                      r.context['upi_whatsapp_link'])
        # A phone cannot scan its own screen, so every code shown here needs a
        # tap link: the balance-locked one and the type-it-yourself one.
        page = html_module.unescape(r.content.decode())
        self.assertEqual(page.count('upi://pay?'), 2)
        self.assertIn('Pay any amount', page)
        # Public page stays read-only.
        self.assertEqual(UpiPaymentRequest.objects.count(), 0)

    def test_tracking_page_has_no_pay_block_when_settled(self):
        inv = self.invoice('1300.00')
        self.mark_settled(inv, '1300')
        job = RepairJob.objects.create(
            customer=self.customer, device_model='HP 15',
            issue_description='dead battery', invoice=inv, status='ready')
        r = self.client.get(reverse(
            'tracking:repair_track', args=[generate_tracking_token(job)]))
        self.assertNotContains(r, 'Pay ₹')

    def test_settings_preview_shows_saved_vpa(self):
        self.client.force_login(self.staff)
        r = self.client.get(reverse('accounting:company_settings'))
        self.assertContains(r, 'UPI payments')
        self.assertContains(r, 'a1computers@ybl')
        self.assertContains(r, 'data:image/png;base64,')

    def test_settings_without_vpa_shows_how_to_enable(self):
        profile(upi_id='')
        self.client.force_login(self.staff)
        r = self.client.get(reverse('accounting:company_settings'))
        self.assertContains(r, 'No UPI ID saved yet')

    def test_sidebar_links_to_the_claims_console(self):
        self.client.force_login(self.staff)
        r = self.client.get(reverse('accounting:dashboard'))
        self.assertContains(r, reverse('accounting:upi_claim_list'))
        self.assertContains(r, 'UPI Claims')


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class SettingsFormUpiTests(TestCase):
    """The owner edits the UPI ID themselves, in Company Settings."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='upi_cfg_staff', password='TestPass123!',
            is_staff=True, is_active=True)

    def setUp(self):
        profile(upi_id='', upi_holder_name='')
        self.client.force_login(self.staff)

    def _post(self, **over):
        """Re-post the settings form with only the UPI fields changed."""
        company = CompanyProfile.get_instance()
        form = CompanyProfileForm(instance=company)
        data = {}
        for name, field in form.fields.items():
            initial = field.initial() if callable(field.initial) \
                else field.initial
            if isinstance(initial, (datetime.date, datetime.datetime)):
                initial = initial.strftime('%Y-%m-%d')
            data[name] = '' if initial is None else str(initial)
        data.update({'name': 'A1 Computer Solutions', 'phone': '9800000000'})
        data.update(over)
        return self.client.post(reverse('accounting:company_settings'), data)

    def test_owner_can_save_and_edit_upi_details(self):
        r = self._post(upi_id='myshop@paytm',
                       upi_holder_name='A1 Computer Solutions')
        self.assertEqual(r.status_code, 302)
        company = CompanyProfile.get_instance()
        company.refresh_from_db()
        self.assertEqual(company.upi_id, 'myshop@paytm')
        self.assertEqual(company.upi_payee_name, 'A1 Computer Solutions')

    def test_invalid_vpa_is_rejected_with_a_clear_message(self):
        r = self._post(upi_id='not a valid id')
        self.assertContains(r, 'Enter a valid UPI ID')
        company = CompanyProfile.get_instance()
        self.assertEqual(company.upi_id, '')

    def test_saving_upi_id_hides_the_setup_notice(self):
        self._post(upi_id='myshop@paytm')
        r = self.client.get(reverse('accounting:company_settings'))
        self.assertNotContains(r, 'No UPI ID saved yet')
