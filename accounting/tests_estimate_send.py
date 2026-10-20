# accounting/tests_estimate_send.py
"""Send-Estimate-to-Customer: message completeness, phone normalization,
channel fallbacks. (Old message was one thin line and built wa.me/91{phone}
blindly, corrupting numbers stored with a leading 0 or +91.)"""

import json
from decimal import Decimal
from urllib.parse import unquote

from django.contrib.auth.models import User
from django.test import Client, TestCase

from .models import (
    CompanyProfile, Contact, Product, RepairJob, RepairPart, RepairService,
)


class WhatsappTargetTests(TestCase):
    def _job(self, phone):
        c = Contact.objects.create(contact_type='customer', name='X',
                                   phone=phone)
        return RepairJob(customer=c, device_model='TV',
                         issue_description='no picture')

    def test_plain_10_digit(self):
        self.assertEqual(self._job('9876543210').customer_whatsapp_target,
                         '919876543210')

    def test_leading_zero_11_digit(self):
        self.assertEqual(self._job('09876543210').customer_whatsapp_target,
                         '919876543210')

    def test_plus91_stored(self):
        self.assertEqual(self._job('+919876543210').customer_whatsapp_target,
                         '919876543210')

    def test_already_91_12_digit(self):
        self.assertEqual(self._job('919876543210').customer_whatsapp_target,
                         '919876543210')


class EstimateSendTests(TestCase):
    def setUp(self):
        CompanyProfile.objects.all().delete()
        CompanyProfile.objects.create(
            name='A1 Test Shop', phone='9437000000')
        self.staff = User.objects.create_user(
            'est_staff', password='TestPass123!', is_staff=True,
            is_active=True)
        self.client = Client()
        self.client.force_login(self.staff)
        self.customer = Contact.objects.create(
            contact_type='customer', name='Pitambara Naik',
            phone='09437251003', email='pit@example.com')
        self.job = RepairJob.objects.create(
            customer=self.customer, device_model='Vu 32 in Led TV',
            issue_description='Screen has no picture, sound is coming',
            diagnosis_report='Backlight driver IC failed',
            action_taken='Replace backlight strip and test',
            estimated_cost=Decimal('5500.00'))
        prod = Product.objects.create(
            name='LED Strip 32in', selling_price=Decimal('4000.00'),
            current_stock=Decimal('10'))
        svc = Product.objects.create(
            name='Repair Labour', selling_price=Decimal('1000.00'),
            is_service=True)
        RepairPart.objects.create(repair_job=self.job, product=prod,
                                  quantity=1, unit_price=Decimal('4000.00'))
        RepairService.objects.create(repair_job=self.job, product=svc,
                                     amount=Decimal('1000.00'),
                                     description='Full panel work')

    def _send(self, url, method='POST', data=None):
        return self.client.post(
            url, data or {'method': 'whatsapp'},
            HTTP_HX_REQUEST='true')

    def test_modal_prefills_full_message(self):
        resp = self.client.get(f'/repairs/{self.job.pk}/send-estimate/')
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('estimateDefaultMessage', html)
        self.assertIn('Reset to default', html)
        self.assertIn('LED Strip 32in', html)
        self.assertIn('Repair Labour', html)

    def test_default_message_lets_customer_decide(self):
        resp = self._send(f'/repairs/{self.job.pk}/send-estimate/')
        trigger = json.loads(resp['HX-Trigger'])
        url = trigger['openWhatsApp']
        text = unquote(url.split('?text=', 1)[1])
        for must in ['A1 Test Shop', 'REP-', 'Vu 32 in Led TV',
                     'no picture', 'Backlight driver IC failed',
                     'LED Strip 32in', 'Rs.4000.00', 'Repair Labour',
                     'Grand Total (Estimate): Rs.5500.00',
                     'Repair work starts only after your approval',
                     'Approve / Hold / Reject', 'APPROVE / HOLD / REJECT',
                     '/repairs/{}/'.format(self.job.pk), '/tracking/']:
            self.assertIn(must, text, f'missing in message: {must}')

    def test_wa_number_normalized_not_blind_91_concat(self):
        resp = self._send(f'/repairs/{self.job.pk}/send-estimate/')
        url = json.loads(resp['HX-Trigger'])['openWhatsApp']
        # Phone stored as 09437251003 -> must become 919437251003,
        # never 9109437251003.
        self.assertIn('wa.me/919437251003?', url)

    def test_custom_message_kept_plus_portal_link(self):
        resp = self._send(
            f'/repairs/{self.job.pk}/send-estimate/',
            data={'method': 'whatsapp',
                  'message': 'Bhai 5500 finalize kar rahe.'})
        text = unquote(
            json.loads(resp['HX-Trigger'])['openWhatsApp'].split('?text=', 1)[1])
        self.assertIn('Bhai 5500 finalize kar rahe.', text)
        self.assertIn('Approve/Hold/Reject:', text)

    def test_whatsapp_without_phone_reports_skip_not_fake_success(self):
        self.customer.phone = ''
        self.customer.save()
        resp = self._send(f'/repairs/{self.job.pk}/send-estimate/')
        self.assertEqual(resp.status_code, 200)
        trigger = json.loads(resp['HX-Trigger'])
        self.assertNotIn('openWhatsApp', trigger)
        self.assertIn('WhatsApp', trigger['showToast']['message'])

    def test_requires_estimated_cost(self):
        self.job.estimated_cost = None
        self.job.save()
        resp = self._send(f'/repairs/{self.job.pk}/send-estimate/')
        self.assertEqual(resp.status_code, 400)

    def test_cancelled_job_blocked(self):
        self.job.change_status('cancelled', by=self.staff)
        resp = self._send(f'/repairs/{self.job.pk}/send-estimate/')
        self.assertEqual(resp.status_code, 400)

    def test_resend_reopens_rejected_decision_for_customer(self):
        self.job.estimate_status = 'rejected'
        self.job.save(update_fields=['estimate_status'])
        self._send(f'/repairs/{self.job.pk}/send-estimate/')
        self.job.refresh_from_db()
        self.assertEqual(self.job.estimate_status, 'pending')
        self.assertIsNone(self.job.estimate_decided_at)
