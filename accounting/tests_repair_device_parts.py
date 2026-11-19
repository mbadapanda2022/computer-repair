"""
Repair module — internal device-parts snapshot on the job form.

These tests cover the OPTIONAL line-by-line record of what is inside the
customer's device at reception (HDD present/missing, RAM, WiFi card, ...):

  • entry through the create form
  • edit / delete through the update form
  • display on the detail page and the printed job sheet
  • blank rows are ignored, and no billing/stock side effects occur

Run:
    venv\\Scripts\\python.exe manage.py test accounting.tests_repair_device_parts -v 2
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounting.models import (
    Contact, RepairJob, RepairJobDevicePart, StockMovement,
)

User = get_user_model()


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class RepairDevicePartsTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='device_parts_staff', password='TestPass123!',
            is_staff=True, is_active=True,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)

        suffix = timezone.now().strftime('%H%M%S%f')[:8]
        self.customer = Contact.objects.create(
            contact_type='customer', name=f'Parts Customer {suffix}',
            phone=f'8{suffix}'.ljust(10, '0')[:10],
        )

    # ── helpers ──────────────────────────────────────────────────
    def _payload(self, **overrides):
        data = {
            'customer': self.customer.pk,
            'device_model': 'Test Laptop',
            'issue_description': 'Device does not power on',
            'status': 'received',
        }
        data.update(overrides)
        return data

    @staticmethod
    def _with_parts(data, rows):
        """Attach inline device-parts-grid arrays to a job form payload."""
        def col(key):
            return [str(row.get(key, '')) for row in rows]

        data = dict(data)
        data['device_part_id'] = col('id')
        data['device_part_name'] = col('name')
        data['device_part_identifier'] = col('identifier')
        data['device_part_quantity'] = col('quantity')
        data['device_part_presence'] = col('presence')
        data['device_part_condition'] = col('condition')
        data['device_part_remove'] = [
            str(row.get('remove', '0')) for row in rows
        ]
        return data

    def _create_job(self):
        return RepairJob.objects.create(
            customer=self.customer, device_model='Existing Laptop',
            issue_description='Existing issue', status='received',
        )

    # ══════════════════════════════════════════════════════════════
    # 1. CREATE
    # ══════════════════════════════════════════════════════════════
    def test_create_with_device_parts_grid(self):
        data = self._with_parts(self._payload(device_model='HP Pavilion 15'), [
            {
                'name': '500GB HDD', 'identifier': 'HDD-ABC123',
                'quantity': 1, 'presence': 'installed', 'condition': 'working',
            },
            {
                'name': 'RAM Slot 2', 'identifier': '',
                'quantity': 0, 'presence': 'not_installed', 'condition': '',
            },
        ])
        resp = self.client.post(reverse('accounting:repair_create'), data)
        self.assertEqual(resp.status_code, 302)

        job = RepairJob.objects.get(device_model='HP Pavilion 15')
        parts = list(job.device_parts.order_by('id'))
        self.assertEqual(len(parts), 2)

        hdd, ram = parts
        self.assertEqual(hdd.name, '500GB HDD')
        self.assertEqual(hdd.identifier, 'HDD-ABC123')
        self.assertEqual(hdd.quantity, 1)
        self.assertEqual(hdd.presence, 'installed')
        self.assertEqual(hdd.condition_sent, 'working')

        self.assertEqual(ram.name, 'RAM Slot 2')
        self.assertEqual(ram.presence, 'not_installed')
        self.assertEqual(ram.quantity, 0)

        # Purely a custody record — no billing or stock impact.
        self.assertEqual(job.final_amount, Decimal('0.00'))
        self.assertFalse(
            StockMovement.objects.filter(
                source_object_id__in=[p.pk for p in parts],
            ).exists()
        )

    def test_blank_device_parts_rows_are_ignored(self):
        data = self._with_parts(self._payload(device_model='Blank Rows Laptop'), [
            {'name': '', 'identifier': '', 'quantity': 1,
             'presence': 'installed', 'condition': ''},
            {'name': '', 'identifier': '', 'quantity': 1,
             'presence': 'installed', 'condition': ''},
        ])
        resp = self.client.post(reverse('accounting:repair_create'), data)
        self.assertEqual(resp.status_code, 302)

        job = RepairJob.objects.get(device_model='Blank Rows Laptop')
        self.assertEqual(job.device_parts.count(), 0)

    def test_create_rejects_row_without_name(self):
        data = self._with_parts(self._payload(device_model='Bad Row Laptop'), [
            {'name': '', 'identifier': 'SN-ONLY', 'quantity': 1,
             'presence': 'installed', 'condition': ''},
        ])
        resp = self.client.post(reverse('accounting:repair_create'), data)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'enter a part name or clear the row')
        self.assertFalse(
            RepairJob.objects.filter(device_model='Bad Row Laptop').exists()
        )

    def test_create_rejects_non_numeric_quantity(self):
        data = self._with_parts(self._payload(device_model='Bad Qty Laptop'), [
            {'name': 'SSD', 'identifier': '', 'quantity': 'two',
             'presence': 'installed', 'condition': ''},
        ])
        resp = self.client.post(reverse('accounting:repair_create'), data)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'quantity must be a whole number')
        self.assertFalse(
            RepairJob.objects.filter(device_model='Bad Qty Laptop').exists()
        )

    def test_create_form_shows_optional_parts_grid(self):
        resp = self.client.get(reverse('accounting:repair_create'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Internal Parts Inside the Device')
        self.assertContains(resp, 'devicePartsGrid')
        self.assertContains(resp, 'Add Part Line')
        self.assertContains(resp, 'Not Installed')

    def test_create_form_modal_htmx_shows_parts_grid(self):
        resp = self.client.get(
            reverse('accounting:repair_create'),
            headers={'HX-Request': 'true'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'devicePartsGrid')
        self.assertContains(resp, 'preset-device-part')

    def test_create_with_parts_via_htmx(self):
        data = self._with_parts(self._payload(device_model='HTMX Parts Laptop'), [
            {'name': 'WiFi Card', 'identifier': 'WIFI-1',
             'quantity': 1, 'presence': 'installed', 'condition': 'working'},
        ])
        resp = self.client.post(
            reverse('accounting:repair_create'), data,
            headers={'HX-Request': 'true'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn('HX-Redirect', resp.headers)
        job = RepairJob.objects.get(device_model='HTMX Parts Laptop')
        self.assertEqual(job.device_parts.count(), 1)
        self.assertEqual(job.device_parts.first().name, 'WiFi Card')

    # ══════════════════════════════════════════════════════════════
    # 2. UPDATE (edit / remove)
    # ══════════════════════════════════════════════════════════════
    def test_update_edits_device_part_from_grid(self):
        job = self._create_job()
        part = RepairJobDevicePart.objects.create(
            repair_job=job, name='4GB RAM', identifier='OLD-SN',
            quantity=1, presence='installed', condition_sent='working',
        )

        data = self._with_parts(self._payload(), [
            {'id': part.pk, 'name': '8GB DDR4 RAM', 'identifier': 'NEW-SN',
             'quantity': 2, 'presence': 'installed', 'condition': 'working'},
        ])
        resp = self.client.post(
            reverse('accounting:repair_update', args=[job.pk]), data)
        self.assertEqual(resp.status_code, 302)

        part.refresh_from_db()
        self.assertEqual(part.name, '8GB DDR4 RAM')
        self.assertEqual(part.identifier, 'NEW-SN')
        self.assertEqual(part.quantity, 2)

    def test_update_can_remove_device_part(self):
        job = self._create_job()
        part = RepairJobDevicePart.objects.create(
            repair_job=job, name='Old HDD', quantity=1,
            presence='installed',
        )

        data = self._with_parts(self._payload(), [
            {'id': part.pk, 'name': 'Old HDD', 'identifier': '',
             'quantity': 1, 'presence': 'installed', 'condition': '',
             'remove': '1'},
        ])
        resp = self.client.post(
            reverse('accounting:repair_update', args=[job.pk]), data)
        self.assertEqual(resp.status_code, 302)

        self.assertEqual(job.device_parts.count(), 0)
        self.assertTrue(
            RepairJobDevicePart.all_objects.get(pk=part.pk).is_deleted
        )

    def test_update_adds_new_rows_without_touching_existing(self):
        job = self._create_job()
        kept = RepairJobDevicePart.objects.create(
            repair_job=job, name='WiFi Card', quantity=1,
            presence='installed',
        )

        data = self._with_parts(self._payload(), [
            {'id': kept.pk, 'name': 'WiFi Card', 'identifier': '',
             'quantity': 1, 'presence': 'installed', 'condition': ''},
            {'name': 'CMOS Battery', 'identifier': '', 'quantity': 1,
             'presence': 'installed', 'condition': 'weak'},
        ])
        resp = self.client.post(
            reverse('accounting:repair_update', args=[job.pk]), data)
        self.assertEqual(resp.status_code, 302)

        self.assertEqual(job.device_parts.count(), 2)
        self.assertTrue(job.device_parts.filter(pk=kept.pk).exists())
        self.assertTrue(job.device_parts.filter(name='CMOS Battery').exists())

    def test_edit_form_prefills_existing_parts(self):
        job = self._create_job()
        RepairJobDevicePart.objects.create(
            repair_job=job, name='500GB HDD', identifier='HDD-77',
            quantity=1, presence='not_installed', condition_sent='missing',
        )
        resp = self.client.get(
            reverse('accounting:repair_update', args=[job.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '500GB HDD')
        self.assertContains(resp, 'HDD-77')
        self.assertContains(resp, 'Not Installed')

    # ══════════════════════════════════════════════════════════════
    # 3. DISPLAY
    # ══════════════════════════════════════════════════════════════
    def test_detail_page_shows_recorded_parts(self):
        job = self._create_job()
        RepairJobDevicePart.objects.create(
            repair_job=job, name='500GB HDD', identifier='HDD-1',
            quantity=1, presence='installed', condition_sent='working',
        )
        RepairJobDevicePart.objects.create(
            repair_job=job, name='RAM Slot 2', quantity=0,
            presence='not_installed',
        )

        resp = self.client.get(
            reverse('accounting:repair_detail', args=[job.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Internal Parts Recorded')
        self.assertContains(resp, '500GB HDD')
        self.assertContains(resp, 'RAM Slot 2')
        self.assertContains(resp, 'Installed')
        self.assertContains(resp, 'Not Installed')

    def test_detail_page_shows_empty_state(self):
        job = self._create_job()
        resp = self.client.get(
            reverse('accounting:repair_detail', args=[job.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'No internal parts recorded yet')

    def test_print_sheet_lists_recorded_parts(self):
        job = self._create_job()
        RepairJobDevicePart.objects.create(
            repair_job=job, name='8GB DDR4 RAM', identifier='RAM-9',
            quantity=1, presence='installed',
        )
        resp = self.client.get(
            reverse('accounting:repair_print', args=[job.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Internal Parts at Reception')
        self.assertContains(resp, '8GB DDR4 RAM')
        self.assertContains(resp, 'RAM-9')

    def test_print_sheet_omits_section_when_empty(self):
        job = self._create_job()
        resp = self.client.get(
            reverse('accounting:repair_print', args=[job.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'Internal Parts at Reception')
