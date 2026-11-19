"""
Vendor / third-party repair jobs — models, forms, HTTP flows, parts
verification, print/Excel export — regression tests.

Chalane:
    python manage.py test accounting.tests_vendor_repairs -v 2
"""
from __future__ import annotations

import io
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounting.forms import VendorRepairJobForm
from accounting.models import (
    Contact, InvoiceCounter, VendorRepairJob, VendorRepairPart,
)

User = get_user_model()


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class VendorRepairTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='vrp_staff', password='TestPass123!',
            is_staff=True, is_active=True,
        )
        cls.customer = Contact.objects.create(
            contact_type='customer', name='Vendor Repair Customer',
            phone='9876500001',
        )
        cls.vendor = Contact.objects.create(
            contact_type='vendor', name='Big Repair Vendor Co',
            phone='9876500002',
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)

    # ── helpers ──
    def _job(self, **kw):
        defaults = {
            'customer': self.customer,
            'vendor': self.vendor,
            'device_model': 'HP Pavilion 15',
            'issue_description': 'No display, board level issue',
            'outbound_date': timezone.localdate(),
        }
        defaults.update(kw)
        return VendorRepairJob.objects.create(**defaults)

    def _part(self, job, **kw):
        defaults = {
            'job': job,
            'name': '8GB DDR4 RAM',
            'identifier': 'RAM-SN-001',
            'quantity': 1,
        }
        defaults.update(kw)
        return VendorRepairPart.objects.create(**defaults)

    def _post(self, name, args=None, data=None, htmx=True):
        headers = {'HX-Request': 'true'} if htmx else {}
        return self.client.post(
            reverse(f'accounting:{name}', args=args or []),
            data or {},
            headers=headers,
        )

    def _with_parts(self, data, rows):
        """Attach inline parts-grid arrays to a job form payload."""
        def col(key):
            return [str(row.get(key, '')) for row in rows]

        data = dict(data)
        data['part_id'] = col('id')
        data['part_name'] = col('name')
        data['part_identifier'] = col('identifier')
        data['part_quantity'] = col('quantity')
        data['part_presence'] = col('presence')
        data['part_condition'] = col('condition')
        data['part_remove'] = [str(row.get('remove', '0')) for row in rows]
        return data

    # ══════════════ 1. NUMBERING & MODEL ══════════════
    def test_job_number_sequence(self):
        first = self._job()
        second = self._job(device_model='Dell Inspiron')
        self.assertEqual(first.job_number, 'VRP-0001')
        self.assertEqual(second.job_number, 'VRP-0002')
        counter = InvoiceCounter.objects.get(prefix='VRP')
        self.assertEqual(counter.last_number, 2)

    def test_amount_difference_and_flags(self):
        job = self._job(
            estimate_amount=Decimal('1200.00'),
            final_amount=Decimal('1500.00'),
            status='sent',
            expected_return_date=timezone.localdate() - timezone.timedelta(days=2),
        )
        self.assertEqual(job.amount_difference, Decimal('300.00'))
        self.assertTrue(job.is_overdue)
        self.assertTrue(job.is_open)

    def test_cancelled_stamp(self):
        job = self._job()
        job.status = 'cancelled'
        job.save()
        self.assertIsNotNone(job.cancelled_at)

    def test_is_warranty_claim(self):
        self.assertFalse(self._job().is_warranty_claim)
        self.assertTrue(self._job(purpose='warranty').is_warranty_claim)

    # ══════════════ 2. FORM VALIDATION ══════════════
    def _form_data(self, **overrides):
        data = {
            'customer': self.customer.pk,
            'vendor': self.vendor.pk,
            'purpose': 'repair',
            'device_model': 'HP Pavilion 15',
            'issue_description': 'Board issue',
            'status': 'sent',
            'outbound_date': timezone.localdate().isoformat(),
            'final_amount': '0',
        }
        data.update(overrides)
        return data

    def test_form_rejects_expected_before_outbound(self):
        today = timezone.localdate()
        form = VendorRepairJobForm(data=self._form_data(
            expected_return_date=(today - timezone.timedelta(days=1)).isoformat(),
        ))
        self.assertFalse(form.is_valid())
        self.assertIn('expected_return_date', form.errors)

    def test_form_rejects_vendor_equal_customer(self):
        both = Contact.objects.create(
            contact_type='both', name='Same Party Ltd',
        )
        form = VendorRepairJobForm(data=self._form_data(
            customer=both.pk, vendor=both.pk,
        ))
        self.assertFalse(form.is_valid())
        self.assertIn('vendor', form.errors)

    def test_form_rejects_expiry_before_purchase(self):
        today = timezone.localdate()
        form = VendorRepairJobForm(data=self._form_data(
            purpose='warranty',
            purchase_date=today.isoformat(),
            warranty_expiry_date=(today - timezone.timedelta(days=10)).isoformat(),
        ))
        self.assertFalse(form.is_valid())
        self.assertIn('warranty_expiry_date', form.errors)

    def test_form_requires_return_date_for_returned_status(self):
        form = VendorRepairJobForm(data=self._form_data(status='returned'))
        self.assertFalse(form.is_valid())
        self.assertIn('received_back_at', form.errors)

    def test_form_blocks_manual_completion(self):
        form = VendorRepairJobForm(data=self._form_data(
            status='completed',
            received_back_at=timezone.localdate().isoformat(),
        ))
        self.assertFalse(form.is_valid())
        self.assertIn('status', form.errors)

    def test_form_blocks_manual_cancel(self):
        form = VendorRepairJobForm(data=self._form_data(status='cancelled'))
        self.assertFalse(form.is_valid())
        self.assertIn('status', form.errors)

    def test_form_allows_editing_already_completed_job(self):
        job = self._job(
            status='completed', received_back_at=timezone.localdate(),
        )
        form = VendorRepairJobForm(
            data=self._form_data(
                status='completed',
                received_back_at=timezone.localdate().isoformat(),
                device_model='HP Pavilion 15 v2',
            ),
            instance=job,
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_form_accepts_warranty_claim(self):
        today = timezone.localdate()
        form = VendorRepairJobForm(data=self._form_data(
            purpose='warranty',
            warranty_claim_number='CLM-778',
            warranty_claim_status='approved',
            purchase_date=(today - timezone.timedelta(days=200)).isoformat(),
            warranty_expiry_date=(today + timezone.timedelta(days=200)).isoformat(),
            warranty_document_ref='BILL-991',
        ))
        self.assertTrue(form.is_valid(), form.errors)

    # ══════════════ 3. HTTP CREATE / UPDATE ══════════════
    def test_create_via_http(self):
        resp = self._post(
            'vendor_repair_create', data=self._form_data(), htmx=False,
        )
        job = VendorRepairJob.objects.get()
        self.assertEqual(resp.status_code, 302)
        self.assertIn(str(job.pk), resp['Location'])
        self.assertEqual(job.created_by, self.staff)
        self.assertEqual(job.job_number, 'VRP-0001')

    def test_create_via_htmx_redirects(self):
        resp = self._post('vendor_repair_create', data=self._form_data())
        job = VendorRepairJob.objects.get()
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            reverse('accounting:vendor_repair_detail', args=[job.pk]),
            resp['HX-Redirect'],
        )
        self.assertIn('showToast', resp['HX-Trigger'])

    def test_update_via_http(self):
        job = self._job()
        data = self._form_data(
            device_model='HP Pavilion 15-eg',
            estimate_amount='900',
            final_amount='1100',
        )
        resp = self._post(
            'vendor_repair_update', args=[job.pk], data=data, htmx=False,
        )
        self.assertEqual(resp.status_code, 302)
        job.refresh_from_db()
        self.assertEqual(job.device_model, 'HP Pavilion 15-eg')
        self.assertEqual(job.final_amount, Decimal('1100.00'))

    def test_create_page_renders(self):
        resp = self.client.get(reverse('accounting:vendor_repair_create'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Purpose of Sending')

    # ══════════════ 3b. INLINE PARTS GRID ON THE FORM ══════════════
    def test_create_with_inline_parts_grid(self):
        data = self._with_parts(self._form_data(), [
            {'name': '500GB HDD', 'identifier': 'HDD-1', 'quantity': '1',
             'presence': 'installed', 'condition': 'working'},
            {'name': 'RAM Slot 2', 'quantity': '',
             'presence': 'not_installed'},
        ])
        resp = self._post(
            'vendor_repair_create', data=data, htmx=False,
        )
        self.assertEqual(resp.status_code, 302)
        job = VendorRepairJob.objects.get()
        parts = list(job.parts.order_by('id'))
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0].name, '500GB HDD')
        self.assertEqual(parts[0].presence, 'installed')
        self.assertEqual(parts[0].quantity, 1)
        self.assertEqual(parts[0].condition_sent, 'working')
        self.assertEqual(parts[1].presence, 'not_installed')
        self.assertEqual(parts[1].quantity, 0)

    def test_create_rejects_parts_row_without_name(self):
        data = self._with_parts(self._form_data(), [
            {'name': '', 'identifier': 'X-1', 'quantity': '1',
             'presence': 'installed'},
        ])
        resp = self._post(
            'vendor_repair_create', data=data, htmx=False,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'enter a part name')
        self.assertEqual(VendorRepairJob.objects.count(), 0)

    def test_blank_parts_rows_are_ignored(self):
        data = self._with_parts(self._form_data(), [
            {'name': '', 'identifier': '', 'quantity': '1',
             'presence': 'installed', 'condition': ''},
            {'name': '', 'identifier': '', 'quantity': '1',
             'presence': 'installed', 'condition': ''},
        ])
        resp = self._post(
            'vendor_repair_create', data=data, htmx=False,
        )
        self.assertEqual(resp.status_code, 302)
        job = VendorRepairJob.objects.get()
        self.assertEqual(job.parts.count(), 0)

    def test_update_edits_unverified_part_from_grid(self):
        job = self._job()
        part = self._part(job)
        data = self._with_parts(self._form_data(), [
            {'id': part.pk, 'name': '16GB DDR4 RAM', 'identifier': 'RAM-2',
             'quantity': '2', 'presence': 'installed',
             'condition': 'working'},
        ])
        resp = self._post(
            'vendor_repair_update', args=[job.pk], data=data, htmx=False,
        )
        self.assertEqual(resp.status_code, 302)
        part.refresh_from_db()
        self.assertEqual(part.name, '16GB DDR4 RAM')
        self.assertEqual(part.quantity, 2)
        self.assertEqual(part.identifier, 'RAM-2')

    def test_update_can_remove_unverified_part(self):
        job = self._job()
        part = self._part(job)
        data = self._with_parts(self._form_data(), [
            {'id': part.pk, 'name': part.name, 'quantity': '1',
             'presence': 'installed', 'remove': '1'},
        ])
        self._post(
            'vendor_repair_update', args=[job.pk], data=data, htmx=False,
        )
        self.assertFalse(VendorRepairPart.objects.filter(pk=part.pk).exists())
        self.assertTrue(
            VendorRepairPart.all_objects.filter(pk=part.pk).exists())

    def test_verified_part_is_locked_against_grid_edits(self):
        job = self._job()
        part = self._part(job, verification_status='ok')
        data = self._with_parts(self._form_data(), [
            {'id': part.pk, 'name': 'Tampered Name', 'quantity': '9',
             'presence': 'not_installed', 'remove': '1'},
        ])
        self._post(
            'vendor_repair_update', args=[job.pk], data=data, htmx=False,
        )
        part.refresh_from_db()
        self.assertEqual(part.name, '8GB DDR4 RAM')
        self.assertEqual(part.quantity, 1)
        self.assertEqual(part.presence, 'installed')
        self.assertEqual(part.verification_status, 'ok')

    def test_detail_page_shows_not_installed_badge(self):
        job = self._job()
        self._part(job, name='HDD Bay', presence='not_installed', quantity=0)
        resp = self.client.get(
            reverse('accounting:vendor_repair_detail', args=[job.pk]))
        self.assertContains(resp, 'HDD Bay')
        self.assertContains(resp, 'Not Installed')

    def test_add_part_modal_accepts_not_installed(self):
        job = self._job()
        self._post('vendor_repair_add_part', args=[job.pk], data={
            'name': 'HDD Bay',
            'identifier': '',
            'quantity': '',
            'presence': 'not_installed',
            'condition_sent': '',
        })
        part = job.parts.get()
        self.assertEqual(part.presence, 'not_installed')
        self.assertEqual(part.quantity, 0)

    # ══════════════ 4. PARTS — ADD / EDIT / DELETE ══════════════
    def test_add_part_htmx(self):
        job = self._job()
        resp = self._post('vendor_repair_add_part', args=[job.pk], data={
            'name': '512GB NVMe SSD',
            'identifier': 'SSD-SN-42',
            'quantity': 1,
            'condition_sent': 'working',
            'remarks': '',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['HX-Retarget'], '#vendor-parts-section')
        part = VendorRepairPart.objects.get(job=job)
        self.assertEqual(part.name, '512GB NVMe SSD')
        self.assertEqual(part.verification_status, 'pending')

    def test_add_part_blocked_on_completed_job(self):
        job = self._job(status='completed', received_back_at=timezone.localdate())
        resp = self._post('vendor_repair_add_part', args=[job.pk], data={
            'name': 'RAM', 'quantity': 1,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(VendorRepairPart.objects.count(), 0)

    def test_edit_part(self):
        job = self._job()
        part = self._part(job)
        resp = self._post('vendor_repair_edit_part', args=[part.pk], data={
            'name': '16GB DDR4 RAM',
            'identifier': 'RAM-SN-001',
            'quantity': 2,
            'condition_sent': 'working',
            'remarks': 'two sticks',
        })
        self.assertEqual(resp.status_code, 200)
        part.refresh_from_db()
        self.assertEqual(part.name, '16GB DDR4 RAM')
        self.assertEqual(part.quantity, 2)

    def test_delete_part_soft(self):
        job = self._job()
        part = self._part(job)
        resp = self._post('vendor_repair_delete_part', args=[part.pk])
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(VendorRepairPart.objects.filter(pk=part.pk).exists())
        self.assertTrue(
            VendorRepairPart.all_objects.filter(pk=part.pk).exists(),
            'soft delete ne record hard delete kar diya',
        )
        job.refresh_from_db()
        self.assertEqual(job.parts_line_count, 0)

    def test_add_part_resets_parts_verified(self):
        job = self._job(status='returned', received_back_at=timezone.localdate())
        job.parts_verified = True
        job.save(update_fields=['parts_verified'])
        self._post('vendor_repair_add_part', args=[job.pk], data={
            'name': 'WiFi Card', 'quantity': 1,
        })
        job.refresh_from_db()
        self.assertFalse(job.parts_verified)

    # ══════════════ 5. RETURN, VERIFY, COMPLETE ══════════════
    def test_mark_returned(self):
        job = self._job()
        resp = self._post('vendor_repair_mark_returned', args=[job.pk], data={
            'received_back_at': timezone.localdate().isoformat(),
            'received_by': 'Ravi',
            'return_condition': 'Screen replaced',
        })
        self.assertEqual(resp.status_code, 200)
        job.refresh_from_db()
        self.assertEqual(job.status, 'returned')
        self.assertEqual(job.received_by, 'Ravi')

    def test_mark_returned_requires_date_after_outbound(self):
        job = self._job(
            outbound_date=timezone.localdate(),
        )
        resp = self._post('vendor_repair_mark_returned', args=[job.pk], data={
            'received_back_at': (
                timezone.localdate() - timezone.timedelta(days=2)
            ).isoformat(),
            'received_by': 'Ravi',
        })
        job.refresh_from_db()
        self.assertEqual(job.status, 'sent')
        self.assertContains(resp, 'cannot be before the date sent')

    def test_verify_part_before_return_blocked(self):
        job = self._job()
        part = self._part(job)
        resp = self._post('vendor_repair_verify_part', args=[part.pk], data={
            'verification_status': 'ok',
        })
        self.assertEqual(resp.status_code, 400)
        part.refresh_from_db()
        self.assertEqual(part.verification_status, 'pending')

    def test_verify_part_missing(self):
        job = self._job(status='returned', received_back_at=timezone.localdate())
        part = self._part(job)
        resp = self._post('vendor_repair_verify_part', args=[part.pk], data={
            'verification_status': 'missing',
            'condition_returned': '',
            'remarks': 'RAM gayab tha',
        })
        self.assertEqual(resp.status_code, 200)
        part.refresh_from_db()
        self.assertEqual(part.verification_status, 'missing')
        self.assertTrue(part.is_discrepancy)
        job.refresh_from_db()
        self.assertTrue(job.has_parts_discrepancy)
        self.assertEqual(job.parts_missing_count, 1)

    def test_verify_all_parts(self):
        job = self._job(status='returned', received_back_at=timezone.localdate())
        self._part(job)
        self._part(job, name='512GB SSD', identifier='SSD-1')
        resp = self._post('vendor_repair_verify_all_parts', args=[job.pk])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(job.parts_pending_count, 0)
        self.assertEqual(job.parts_ok_count, 2)

    def test_complete_blocked_with_pending_parts(self):
        job = self._job(status='returned', received_back_at=timezone.localdate())
        self._part(job)
        resp = self._post('vendor_repair_mark_completed', args=[job.pk], data={
            'confirm': 'on',
            'verification_notes': '',
        })
        self.assertEqual(resp.status_code, 200)
        job.refresh_from_db()
        self.assertEqual(job.status, 'returned')
        self.assertFalse(job.parts_verified)
        self.assertContains(resp, 'Pending Check')

    def test_complete_success(self):
        job = self._job(status='returned', received_back_at=timezone.localdate())
        part = self._part(job)
        part.verification_status = 'ok'
        part.save(update_fields=['verification_status'])

        resp = self._post('vendor_repair_mark_completed', args=[job.pk], data={
            'confirm': 'on',
            'verification_notes': 'Sab parts sahi mile',
        })
        self.assertEqual(resp.status_code, 200)
        job.refresh_from_db()
        self.assertEqual(job.status, 'completed')
        self.assertTrue(job.parts_verified)
        self.assertEqual(job.verified_by, self.staff)
        self.assertIsNotNone(job.verified_at)
        self.assertIn('Sab parts sahi mile', job.verification_notes or '')

    def test_complete_requires_confirm_checkbox(self):
        job = self._job(status='returned', received_back_at=timezone.localdate())
        resp = self._post('vendor_repair_mark_completed', args=[job.pk], data={})
        job.refresh_from_db()
        self.assertEqual(job.status, 'returned')
        self.assertContains(resp, 'physically checked')

    def test_complete_not_returned_blocked(self):
        job = self._job()
        resp = self._post('vendor_repair_mark_completed', args=[job.pk])
        self.assertEqual(resp.status_code, 400)

    # ══════════════ 6. CANCEL ══════════════
    def test_cancel_with_reason(self):
        job = self._job()
        resp = self._post('vendor_repair_cancel', args=[job.pk], data={
            'reason': 'Vendor ne part hi nahi lagaya',
            'confirm': 'on',
        })
        self.assertEqual(resp.status_code, 200)
        job.refresh_from_db()
        self.assertEqual(job.status, 'cancelled')
        self.assertEqual(job.cancel_reason, 'Vendor ne part hi nahi lagaya')
        self.assertIsNotNone(job.cancelled_at)

    def test_cancel_completed_blocked(self):
        job = self._job(status='completed', received_back_at=timezone.localdate())
        resp = self._post('vendor_repair_cancel', args=[job.pk], data={
            'reason': 'x', 'confirm': 'on',
        })
        self.assertEqual(resp.status_code, 400)
        job.refresh_from_db()
        self.assertEqual(job.status, 'completed')

    def test_cancel_requires_reason_and_confirm(self):
        job = self._job()
        self._post('vendor_repair_cancel', args=[job.pk], data={'confirm': 'on'})
        job.refresh_from_db()
        self.assertEqual(job.status, 'sent')

    # ══════════════ 7. DELETE JOB (SOFT, CASCADE PARTS) ══════════════
    def test_delete_job_soft_deletes_parts(self):
        job = self._job()
        part = self._part(job)
        resp = self._post('vendor_repair_delete', args=[job.pk], htmx=False)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(VendorRepairJob.objects.filter(pk=job.pk).exists())
        self.assertFalse(VendorRepairPart.objects.filter(pk=part.pk).exists())
        self.assertTrue(
            VendorRepairJob.all_objects.filter(pk=job.pk).exists())
        self.assertTrue(
            VendorRepairPart.all_objects.filter(pk=part.pk).exists())

    # ══════════════ 7b. MODAL GET RENDERING ══════════════
    def test_part_modals_render(self):
        job = self._job()
        part = self._part(job)

        resp = self.client.get(
            reverse('accounting:vendor_repair_add_part', args=[job.pk]),
            headers={'HX-Request': 'true'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Part Name')

        resp = self.client.get(
            reverse('accounting:vendor_repair_edit_part', args=[part.pk]),
            headers={'HX-Request': 'true'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '8GB DDR4 RAM')

    def test_verify_part_modal_renders_when_returned(self):
        job = self._job(status='returned', received_back_at=timezone.localdate())
        part = self._part(job)
        resp = self.client.get(
            reverse('accounting:vendor_repair_verify_part', args=[part.pk]),
            headers={'HX-Request': 'true'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Verification Result')

    def test_status_modals_render(self):
        job = self._job()
        for name, needle in [
            ('vendor_repair_mark_returned', 'Received By'),
            ('vendor_repair_cancel', 'Reason'),
        ]:
            resp = self.client.get(
                reverse(f'accounting:{name}', args=[job.pk]),
                headers={'HX-Request': 'true'},
            )
            self.assertEqual(resp.status_code, 200, name)
            self.assertContains(resp, needle)

        job.status = 'returned'
        job.received_back_at = timezone.localdate()
        job.save(update_fields=['status', 'received_back_at'])
        resp = self.client.get(
            reverse('accounting:vendor_repair_mark_completed', args=[job.pk]),
            headers={'HX-Request': 'true'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'physically checked')

    def test_delete_job_htmx_returns_table(self):
        job = self._job()
        resp = self._post('vendor_repair_delete', args=[job.pk])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp['HX-Retarget'], '#vendor-repair-table-container')
        self.assertContains(resp, 'No vendor repair jobs found')

    def test_global_search_finds_vendor_job(self):
        job = self._job(device_model='HP Pavilion Searchable')
        resp = self.client.get(
            reverse('accounting:global_search'), {'q': 'Searchable'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, job.job_number)

    # ══════════════ 8. LIST / FILTERS / STATS ══════════════
    def test_list_filters(self):
        a = self._job(device_model='HP Pavilion')
        b = self._job(
            device_model='Dell Inspiron', purpose='warranty',
            status='completed', received_back_at=timezone.localdate(),
            final_amount=Decimal('500'),
        )
        c = self._job(device_model='Lenovo IdeaPad', status='cancelled')

        resp = self.client.get(
            reverse('accounting:vendor_repair_list'), {'search': 'Dell'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(list(resp.context['jobs']), [b])
        self.assertEqual(resp.context['filtered_total'], Decimal('500.00'))

        resp = self.client.get(
            reverse('accounting:vendor_repair_list'), {'status': 'open'},
        )
        self.assertIn(a, list(resp.context['jobs']))
        self.assertNotIn(b, list(resp.context['jobs']))
        self.assertNotIn(c, list(resp.context['jobs']))

        resp = self.client.get(
            reverse('accounting:vendor_repair_list'), {'purpose': 'warranty'},
        )
        self.assertEqual(list(resp.context['jobs']), [b])

    def test_list_vendor_filter_and_stats(self):
        self._job()
        self._job(
            purpose='warranty', status='completed',
            received_back_at=timezone.localdate(),
            final_amount=Decimal('800'),
        )
        resp = self.client.get(reverse('accounting:vendor_repair_list'))
        stats = resp.context['stats']
        self.assertEqual(stats['total_count'], 2)
        self.assertEqual(stats['warranty_count'], 1)
        self.assertEqual(stats['completed_count'], 1)
        self.assertEqual(stats['total_final'], Decimal('800.00'))

        resp = self.client.get(
            reverse('accounting:vendor_repair_list'),
            {'vendor': self.vendor.pk},
        )
        self.assertEqual(len(resp.context['jobs']), 2)

    def test_list_htmx_returns_table_only(self):
        self._job()
        resp = self.client.get(
            reverse('accounting:vendor_repair_list'),
            headers={'HX-Request': 'true'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(
            resp, 'vendor_repairs/partials/vendor_repair_table.html')

    def test_discrepancy_stat(self):
        job = self._job(status='returned', received_back_at=timezone.localdate())
        self._part(job, verification_status='replaced')
        resp = self.client.get(reverse('accounting:vendor_repair_list'))
        self.assertEqual(resp.context['stats']['discrepancy_count'], 1)

    # ══════════════ 9. DETAIL / PRINT ══════════════
    def test_detail_renders_warranty_fields(self):
        job = self._job(
            purpose='warranty', warranty_claim_number='CLM-55',
            warranty_claim_status='approved',
        )
        self._part(job)
        resp = self.client.get(
            reverse('accounting:vendor_repair_detail', args=[job.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'CLM-55')
        self.assertContains(resp, 'Claim Approved')
        self.assertContains(resp, '8GB DDR4 RAM')

    def test_print_job_sheet(self):
        job = self._job(purpose='warranty', warranty_claim_number='CLM-9')
        self._part(job)
        resp = self.client.get(
            reverse('accounting:vendor_repair_print', args=[job.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, job.job_number)
        self.assertContains(resp, 'CLM-9')

    def test_print_list(self):
        self._job()
        resp = self.client.get(reverse('accounting:vendor_repair_list_print'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'VRP-0001')

    # ══════════════ 10. EXCEL EXPORT ══════════════
    def test_excel_export(self):
        import openpyxl

        job = self._job(
            purpose='warranty', warranty_claim_number='CLM-100',
            estimate_amount=Decimal('1000'), final_amount=Decimal('1200'),
        )
        self._part(job)

        resp = self.client.get(reverse('accounting:export_vendor_repairs_excel'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('spreadsheetml', resp['Content-Type'])
        self.assertIn('attachment', resp['Content-Disposition'])

        wb = openpyxl.load_workbook(io.BytesIO(resp.content))
        self.assertEqual(wb.sheetnames, ['Vendor Repairs', 'Parts'])
        ws = wb['Vendor Repairs']
        self.assertEqual(ws['A1'].value, 'Vendor Repair Jobs')
        self.assertEqual(ws['A3'].value, 'VRP-0001')
        self.assertEqual(ws['B3'].value, 'Warranty Claim')
        self.assertEqual(ws['R3'].value, 'CLM-100')
        parts_ws = wb['Parts']
        self.assertEqual(parts_ws['A2'].value, 'VRP-0001')
        self.assertEqual(parts_ws['E2'].value, '8GB DDR4 RAM')

    def test_excel_export_empty(self):
        resp = self.client.get(reverse('accounting:export_vendor_repairs_excel'))
        self.assertEqual(resp.status_code, 200)
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(resp.content))
        self.assertEqual(wb.sheetnames, ['Vendor Repairs', 'Parts'])

    # ══════════════ 11. PERMISSIONS ══════════════
    def test_anonymous_redirected(self):
        anon = Client()
        for name, args in [
            ('vendor_repair_list', []),
            ('vendor_repair_create', []),
            ('vendor_repair_list_print', []),
            ('export_vendor_repairs_excel', []),
        ]:
            resp = anon.get(reverse(f'accounting:{name}', args=args))
            self.assertEqual(resp.status_code, 302, f'{name} open hai')

    # ══════════════ 12. CONTACT DELETE GUARD ══════════════
    def test_vendor_with_vendor_job_not_deletable(self):
        self._job()
        from accounting.views.contacts import _has_real_transactions
        self.assertTrue(_has_real_transactions(self.vendor))
        self.assertTrue(_has_real_transactions(self.customer))
