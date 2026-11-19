"""
Temporary Order Note tests — session-only Excel-like sheets, one per
supplier, with bidirectional tax calculation and .xlsx export.

    python manage.py test accounting.tests_order_note -v 2
"""
from __future__ import annotations

from io import BytesIO

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

User = get_user_model()


def post_rows(rows, supplier='Luna Mahanta', note='', after=''):
    data = {'supplier': supplier, 'note': note}
    if after:
        data['after'] = after
    for key in ('date', 'product', 'qty', 'tax', 'price_excl', 'price_incl', 'price_src'):
        data[f'{key}[]'] = [row.get(key, '') for row in rows]
    return data


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class OrderNoteTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='note_staff', password='TestPass123!',
            is_staff=True, is_active=True,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)

    # ---- helpers ----
    def create_note(self, supplier, rows, note=''):
        resp = self.client.post(
            reverse('accounting:order_note_create'),
            post_rows(rows, supplier=supplier, note=note),
        )
        notes = self.client.session.get('order_notes', {})
        note_id = [k for k, v in notes.items() if v['supplier'] == supplier][-1]
        return note_id, resp

    def edit_url(self, note_id):
        return reverse('accounting:order_note_edit', args=[note_id])

    # ---- access ----
    def test_anonymous_redirected(self):
        anon = Client()
        r = anon.get('/order-note/')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login/', r['Location'].replace('%2F', '/'))

    def test_list_page_loads_with_empty_state(self):
        r = self.client.get(reverse('accounting:order_note_list'))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Order Notes')
        self.assertContains(r, 'No order notes yet')

    # ---- creating notes ----
    def test_create_requires_supplier_or_items(self):
        r = self.client.post(reverse('accounting:order_note_create'), post_rows([], supplier=''))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Add a supplier name or at least one item')
        self.assertFalse(self.client.session.get('order_notes'))

    def test_create_note_redirects_to_its_sheet(self):
        note_id, resp = self.create_note('Luna Mahanta', [
            {'product': 'Mouse', 'qty': '2', 'price_excl': '250'},
        ])
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], self.edit_url(note_id))
        note = self.client.session['order_notes'][note_id]
        self.assertEqual(note['supplier'], 'Luna Mahanta')
        self.assertEqual(len(note['rows']), 1)

    def test_separate_notes_per_supplier_do_not_overwrite(self):
        id_a, _ = self.create_note('Luna Mahanta', [
            {'product': 'Mouse', 'qty': '2', 'price_excl': '250'},
        ])
        id_b, _ = self.create_note('ABC Distributors', [
            {'product': 'SSD', 'qty': '5', 'price_excl': '3000'},
            {'product': 'RAM', 'qty': '3', 'price_excl': '1000'},
        ])
        self.assertNotEqual(id_a, id_b)
        notes = self.client.session['order_notes']
        self.assertEqual(len(notes), 2)
        self.assertEqual(notes[id_a]['supplier'], 'Luna Mahanta')
        self.assertEqual(len(notes[id_a]['rows']), 1)
        self.assertEqual(notes[id_b]['supplier'], 'ABC Distributors')
        self.assertEqual(len(notes[id_b]['rows']), 2)

    def test_list_shows_all_suppliers(self):
        self.create_note('Luna Mahanta', [
            {'product': 'Mouse', 'qty': '2', 'price_excl': '250'}])
        self.create_note('ABC Distributors', [
            {'product': 'SSD', 'qty': '5', 'price_excl': '3000'}])
        r = self.client.get(reverse('accounting:order_note_list'))
        self.assertContains(r, 'Luna Mahanta')
        self.assertContains(r, 'ABC Distributors')

    def test_save_and_back_redirects_to_list(self):
        resp = self.client.post(
            reverse('accounting:order_note_create'),
            post_rows([{'product': 'Pen', 'qty': '1', 'price_excl': '10'}],
                      supplier='Quick Vendor', after='list'),
        )
        self.assertRedirects(resp, reverse('accounting:order_note_list'))

    # ---- editing ----
    def test_edit_saves_only_that_note(self):
        id_a, _ = self.create_note('Luna Mahanta', [
            {'product': 'Mouse', 'qty': '2', 'price_excl': '250'}])
        id_b, _ = self.create_note('ABC Distributors', [
            {'product': 'SSD', 'qty': '5', 'price_excl': '3000'}])

        self.client.post(self.edit_url(id_a), post_rows(
            [{'product': 'Mouse edited', 'qty': '9', 'price_excl': '250'}],
            supplier='Luna Mahanta',
        ))
        notes = self.client.session['order_notes']
        self.assertEqual(notes[id_a]['rows'][0]['product'], 'Mouse edited')
        self.assertEqual(notes[id_a]['rows'][0]['qty'], '9')
        self.assertEqual(notes[id_b]['rows'][0]['product'], 'SSD')

    def test_edit_unknown_note_is_404(self):
        r = self.client.get(reverse('accounting:order_note_edit', args=['doesnotexist']))
        self.assertEqual(r.status_code, 404)

    def test_sheet_page_loads_for_existing_note(self):
        note_id, _ = self.create_note('Luna Mahanta', [
            {'product': 'Mouse', 'qty': '2', 'price_excl': '250'}])
        r = self.client.get(self.edit_url(note_id))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'price_incl[]')
        self.assertContains(r, 'Luna Mahanta')
        self.assertContains(r, 'Download Excel')

    # ---- calculations (unchanged behaviour) ----
    def test_empty_rows_are_dropped(self):
        note_id, _ = self.create_note('Luna Mahanta', [
            {'product': 'Mouse', 'qty': '2', 'price_excl': '250'},
            {'product': 'X', 'qty': 'junk', 'price_excl': ''},
            {'date': '2026-10-17', 'product': '', 'qty': '', 'tax': '18',
             'price_excl': '', 'price_incl': '', 'price_src': 'excl'},
            {'product': '', 'qty': '0', 'price_excl': '', 'price_incl': ''},
        ])
        rows = self.client.session['order_notes'][note_id]['rows']
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['product'], 'Mouse')

    def test_excl_price_derives_incl_and_amount(self):
        note_id, _ = self.create_note('Luna Mahanta', [
            {'product': 'SSD', 'qty': '3', 'tax': '18', 'price_excl': '1000'}])
        row = self.client.session['order_notes'][note_id]['rows'][0]
        self.assertEqual(row['price_incl'], '1180.00')
        self.assertEqual(row['amount'], '3540.00')

    def test_incl_price_derives_excl(self):
        note_id, _ = self.create_note('Luna Mahanta', [
            {'product': 'RAM', 'qty': '2', 'tax': '18',
             'price_incl': '1180', 'price_src': 'incl'}])
        row = self.client.session['order_notes'][note_id]['rows'][0]
        self.assertEqual(row['price_excl'], '1000.00')
        self.assertEqual(row['amount'], '2360.00')

    def test_default_tax_18_when_blank(self):
        note_id, _ = self.create_note('Luna Mahanta', [
            {'product': 'Cable', 'qty': '1', 'tax': '', 'price_excl': '100'}])
        row = self.client.session['order_notes'][note_id]['rows'][0]
        self.assertEqual(row['price_incl'], '118.00')

    # ---- delete ----
    def test_delete_removes_only_that_note(self):
        id_a, _ = self.create_note('Luna Mahanta', [
            {'product': 'Mouse', 'qty': '1', 'price_excl': '250'}])
        id_b, _ = self.create_note('ABC Distributors', [
            {'product': 'SSD', 'qty': '1', 'price_excl': '3000'}])
        r = self.client.post(reverse('accounting:order_note_delete', args=[id_a]))
        self.assertRedirects(r, reverse('accounting:order_note_list'))
        notes = self.client.session['order_notes']
        self.assertNotIn(id_a, notes)
        self.assertIn(id_b, notes)

    def test_delete_htmx_returns_updated_table(self):
        id_a, _ = self.create_note('Luna Mahanta', [
            {'product': 'Mouse', 'qty': '1', 'price_excl': '250'}])
        self.create_note('ABC Distributors', [
            {'product': 'SSD', 'qty': '1', 'price_excl': '3000'}])
        r = self.client.post(
            reverse('accounting:order_note_delete', args=[id_a]),
            headers={'HX-Request': 'true'},
        )
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'order-note-table')
        self.assertContains(r, 'ABC Distributors')
        self.assertNotContains(r, 'Luna Mahanta')
        self.assertIn('deleted', r['HX-Trigger'])

    def test_delete_requires_post(self):
        note_id, _ = self.create_note('Luna Mahanta', [
            {'product': 'Mouse', 'qty': '1', 'price_excl': '250'}])
        r = self.client.get(reverse('accounting:order_note_delete', args=[note_id]))
        self.assertEqual(r.status_code, 405)

    def test_delete_unknown_note_is_404(self):
        r = self.client.post(
            reverse('accounting:order_note_delete', args=['doesnotexist']))
        self.assertEqual(r.status_code, 404)

    # ---- legacy session migration ----
    def test_old_single_sheet_is_migrated_into_list(self):
        session = self.client.session
        session['order_note'] = {
            'supplier': 'Old Vendor',
            'note': 'legacy',
            'rows': [{'date': '', 'product': 'Old Item', 'qty': '1', 'tax': '18',
                      'price_excl': '100', 'price_incl': '118.00',
                      'price_src': 'excl', 'amount': '118.00',
                      'line_excl': '100.00'}],
        }
        session.save()
        r = self.client.get(reverse('accounting:order_note_list'))
        self.assertContains(r, 'Old Vendor')
        self.assertNotIn('order_note', self.client.session)
        self.assertEqual(len(self.client.session['order_notes']), 1)

    def test_nothing_touches_database_tables(self):
        """Sheets live only in the session — hard requirement from user."""
        from accounting.models import Purchase, Invoice
        before_p, before_i = Purchase.objects.count(), Invoice.objects.count()
        note_id, _ = self.create_note('Luna Mahanta', [
            {'product': 'Monitor', 'qty': '1', 'price_excl': '9000'}])
        self.client.post(self.edit_url(note_id), post_rows([
            {'product': 'Monitor', 'qty': '2', 'price_excl': '9000'}]))
        self.client.post(reverse('accounting:order_note_delete', args=[note_id]))
        self.assertEqual(Purchase.objects.count(), before_p)
        self.assertEqual(Invoice.objects.count(), before_i)

    # ---- excel export ----
    def test_excel_export_returns_workbook_with_totals(self):
        note_id, _ = self.create_note('Luna Mahanta', [
            {'product': 'Mouse', 'qty': '2', 'price_excl': '250'},
            {'product': 'Keyboard', 'qty': '1', 'price_incl': '885', 'price_src': 'incl'},
        ])
        r = self.client.get(
            reverse('accounting:order_note_excel', args=[note_id]))
        self.assertEqual(r.status_code, 200)
        self.assertIn('spreadsheetml', r['Content-Type'])
        self.assertIn('order-note_luna-mahanta_', r['Content-Disposition'])

        from openpyxl import load_workbook
        wb = load_workbook(BytesIO(r.content))
        ws = wb.active
        self.assertIn('Order Note', ws['A1'].value)
        self.assertIn('Luna Mahanta', ws['A2'].value)
        # header row present
        self.assertEqual(ws.cell(row=5, column=3).value, 'Product / Item')
        # supplier-fill columns exist and are empty
        self.assertIn('Available', ws.cell(row=5, column=9).value)
        self.assertIsNone(ws.cell(row=6, column=9).value)
        # LIVE formulas so Excel recalculates when the supplier edits rates
        self.assertEqual(ws['F6'].value, 250)
        self.assertEqual(ws['G6'].value, '=ROUND(F6*(1+E6/100),2)')
        self.assertEqual(ws['G7'].value, 885)
        self.assertEqual(ws['F7'].value, '=ROUND(G7/(1+E7/100),2)')
        self.assertEqual(ws['H6'].value, '=ROUND(D6*G6,2)')
        self.assertEqual(ws['H7'].value, '=ROUND(D7*G7,2)')
        self.assertEqual(ws['F8'].value, '=ROUND(SUMPRODUCT(D6:D7,F6:F7),2)')
        self.assertEqual(ws['F9'].value, '=ROUND(H10-F8,2)')
        self.assertEqual(ws['H10'].value, '=ROUND(SUM(H6:H7),2)')
        # every table cell carries a border
        self.assertEqual(ws['I6'].border.left.style, 'medium')

    def test_excel_export_per_note_uses_that_supplier(self):
        id_a, _ = self.create_note('Luna Mahanta', [
            {'product': 'Mouse', 'qty': '1', 'price_excl': '250'}])
        id_b, _ = self.create_note('ABC Distributors', [
            {'product': 'SSD', 'qty': '1', 'price_excl': '3000'}])
        r = self.client.get(reverse('accounting:order_note_excel', args=[id_b]))
        self.assertIn('abc-distributors', r['Content-Disposition'])

        from openpyxl import load_workbook
        ws = load_workbook(BytesIO(r.content)).active
        self.assertIn('ABC Distributors', ws['A2'].value)
        self.assertIn('SSD', ws['C6'].value)

        # the other note is untouched by the export
        notes = self.client.session['order_notes']
        self.assertEqual(notes[id_a]['rows'][0]['product'], 'Mouse')

    def test_excel_post_saves_grid_before_download(self):
        note_id, _ = self.create_note('Luna Mahanta', [
            {'product': 'Pen', 'qty': '10', 'price_excl': '10'}])
        r = self.client.post(
            reverse('accounting:order_note_excel', args=[note_id]),
            post_rows([{'product': 'Pen updated', 'qty': '12', 'price_excl': '10'}],
                      supplier='Luna Mahanta'),
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn('spreadsheetml', r['Content-Type'])
        row = self.client.session['order_notes'][note_id]['rows'][0]
        self.assertEqual(row['product'], 'Pen updated')
        self.assertEqual(row['qty'], '12')

    def test_excel_export_empty_note_redirects_with_warning(self):
        note_id, _ = self.create_note('Empty Vendor', [])
        r = self.client.get(
            reverse('accounting:order_note_excel', args=[note_id]))
        self.assertRedirects(r, self.edit_url(note_id))
