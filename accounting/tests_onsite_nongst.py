"""On-site repair support + Non-GST invoice presentation — tests.

Run:  python manage.py test accounting.tests_onsite_nongst -v 2
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounting.utils.tracking import generate_tracking_token
from .models import Contact, Invoice, InvoiceItem, Product, RepairJob


class RepairServiceModeModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = Contact.objects.create(
            name='Site Customer', phone='9876500021', contact_type='customer')

    def _job(self, **over):
        data = dict(customer=self.customer, device_model='HP 15',
                    issue_description='no display')
        data.update(over)
        return RepairJob.objects.create(**data)

    def test_default_is_shop_mode(self):
        job = self._job()
        self.assertEqual(job.service_mode, 'shop')
        self.assertEqual(job.get_service_mode_display(),
                         'At Shop (device brought here)')

    def test_site_fields_persist(self):
        job = self._job(service_mode='site',
                        service_location='Bank Colony, Keonjhar',
                        site_contact_name='Sita Sharma',
                        site_contact_phone='9876500022',
                        site_contact_designation='Manager')
        job.refresh_from_db()
        self.assertEqual(job.service_mode, 'site')
        self.assertEqual(job.service_location, 'Bank Colony, Keonjhar')
        self.assertEqual(job.site_contact_name, 'Sita Sharma')
        self.assertEqual(job.site_contact_designation, 'Manager')

    def test_site_whatsapp_target_normalization(self):
        bare = self._job(site_contact_phone='9876500022')
        self.assertEqual(bare.site_whatsapp_target, '919876500022')
        plus = self._job(site_contact_phone='+91 98765-00022')
        self.assertEqual(plus.site_whatsapp_target, '919876500022')
        none = self._job()
        self.assertEqual(none.site_whatsapp_target, '')

    def test_customer_whatsapp_target_unchanged_after_refactor(self):
        job = self._job()
        self.assertEqual(job.customer_whatsapp_target, '919876500021')

    def test_received_status_label_is_mode_neutral(self):
        job = self._job()
        self.assertEqual(job.get_status_display(), 'Pending (Awaiting Receipt)')
        job.status = 'received'
        self.assertEqual(job.get_status_display(), 'Received')


class RepairJobFormSiteValidationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            'site_staff', password='x', is_staff=True)
        cls.customer = Contact.objects.create(
            name='Form Customer', phone='9876500031', contact_type='customer')

    def setUp(self):
        self.client.force_login(self.staff)

    def _post(self, **over):
        data = {'customer': self.customer.pk,
                'device_model': 'Dell Optiplex',
                'issue_description': 'PC not powering on',
                'status': 'pending',
                'service_mode': 'shop'}
        data.update(over)
        return self.client.post(reverse('accounting:repair_create'), data)

    def test_site_mode_requires_location_and_contact(self):
        r = self._post(service_mode='site')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(RepairJob.objects.count(), 0)
        self.assertContains(r, 'Work location address is required')
        self.assertContains(r, 'Site contact person name is required')

    def test_site_mode_with_details_saves(self):
        r = self._post(service_mode='site',
                       service_location='12A Station Rd, Keonjhar',
                       site_contact_name='Gopal',
                       site_contact_phone='9876500032',
                       site_contact_designation='Receptionist')
        self.assertEqual(r.status_code, 302)
        job = RepairJob.objects.get()
        self.assertEqual(job.service_mode, 'site')
        self.assertEqual(job.site_contact_name, 'Gopal')

    def test_shop_mode_needs_no_site_fields(self):
        r = self._post()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(RepairJob.objects.get().service_mode, 'shop')

    def test_invalid_site_phone_rejected(self):
        r = self._post(service_mode='site',
                       service_location='Somewhere',
                       site_contact_name='Gopal',
                       site_contact_phone='12345')
        self.assertEqual(RepairJob.objects.count(), 0)
        self.assertContains(r, 'Enter a valid mobile number')


class BookRepairOnSiteTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def _post(self, **over):
        data = {'name': 'Remote Payer', 'phone': '9876500041',
                'device_model': 'Canon MG2570', 'serial_number': '',
                'issue_description': 'Printer jammed, needs on-site fix',
                'website': ''}
        data.update(over)
        return self.client.post(reverse('book_repair'), data)

    def test_booking_without_location_stays_shop(self):
        self._post()
        job = RepairJob.objects.get()
        self.assertEqual(job.service_mode, 'shop')
        self.assertFalse(job.service_location)

    def test_booking_with_location_marks_site(self):
        self._post(service_location='SBI Branch, Lidhua Bazar, Keonjhar')
        job = RepairJob.objects.get()
        self.assertEqual(job.service_mode, 'site')
        self.assertEqual(job.service_location,
                         'SBI Branch, Lidhua Bazar, Keonjhar')
        # The booker is the assumed site contact until staff adjusts.
        self.assertEqual(job.site_contact_name, 'Remote Payer')
        self.assertEqual(job.site_contact_phone, '9876500041')


class TrackingPageSiteModeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = Contact.objects.create(
            name='Track Customer', phone='9876500051', contact_type='customer')

    def _get_track(self, job):
        token = generate_tracking_token(job)
        return self.client.get(f'/tracking/repair/{token}/')

    def test_site_job_shows_location_and_neutral_timeline(self):
        job = RepairJob.objects.create(
            customer=self.customer, device_model='LG Monitor',
            issue_description='flickering screen',
            service_mode='site',
            service_location='Collectorate, Block C')
        r = self._get_track(job)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Repair Location')
        self.assertContains(r, 'Collectorate, Block C')
        self.assertContains(r, 'Work Started at Location')
        self.assertNotContains(r, 'Device Received at Shop')

    def test_shop_job_wording_unchanged(self):
        job = RepairJob.objects.create(
            customer=self.customer, device_model='APC UPS',
            issue_description='not charging')
        r = self._get_track(job)
        self.assertContains(r, 'Device Received at Shop')
        self.assertNotContains(r, 'Repair Location')


def _make_invoice(customer, gst_type):
    product = Product.objects.create(
        name=f'RAM {gst_type}', selling_price=Decimal('1400.00'),
        current_stock=Decimal('10.00'), is_service=False, is_active=True,
        tax_rate=Decimal('18.00'))
    invoice = Invoice.objects.create(
        customer=customer, date=timezone.localdate(), gst_type=gst_type)
    InvoiceItem.objects.create(
        invoice=invoice, product=product,
        quantity=Decimal('1'), unit_price=Decimal('1400.00'),
        tax_rate=Decimal('18'))
    invoice.refresh_from_db()
    return invoice


class NonGstInvoicePresentationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            'gst_staff', password='x', is_staff=True)
        cls.customer = Contact.objects.create(
            name='GST Customer', phone='9876500061', contact_type='both',
            gstin='21AAAAA0000A1Z5', state='Odisha')

    def setUp(self):
        self.client.force_login(self.staff)

    def test_non_gst_invoice_zeroes_item_tax(self):
        inv = _make_invoice(self.customer, 'non_gst')
        item = inv.items.get()
        self.assertEqual(item.tax_amount, Decimal('0.00'))
        self.assertEqual(inv.grand_total, Decimal('1400.00'))
        self.assertFalse(inv.is_tax_invoice)

    def test_staff_print_renders_plain_invoice_for_non_gst(self):
        inv = _make_invoice(self.customer, 'non_gst')
        r = self.client.get(
            reverse('accounting:invoice_print', args=[inv.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'INVOICE')
        self.assertNotContains(r, 'TAX INVOICE')
        self.assertNotContains(r, 'Tax %')
        self.assertNotContains(r, 'Tax Amt')
        self.assertNotContains(r, 'Tax Amount')
        # Customer GSTIN must not headline a non-tax bill.
        self.assertNotContains(r, '21AAAAA0000A1Z5')

    def test_staff_print_keeps_tax_invoice_for_gst(self):
        inv = _make_invoice(self.customer, 'intrastate')
        r = self.client.get(
            reverse('accounting:invoice_print', args=[inv.pk]))
        self.assertContains(r, 'TAX INVOICE')
        self.assertContains(r, 'Tax Amt')
        self.assertContains(r, 'CGST')
        self.assertContains(r, '21AAAAA0000A1Z5')

    def test_staff_detail_hides_tax_rows_for_non_gst(self):
        inv = _make_invoice(self.customer, 'non_gst')
        r = self.client.get(
            reverse('accounting:invoice_detail', args=[inv.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'Tax Amount')
        self.assertNotContains(r, 'Tax Amt')

    def test_non_gst_total_still_shown(self):
        inv = _make_invoice(self.customer, 'non_gst')
        r = self.client.get(
            reverse('accounting:invoice_print', args=[inv.pk]))
        self.assertContains(r, 'GRAND TOTAL')
        self.assertContains(r, '1,400.00')
