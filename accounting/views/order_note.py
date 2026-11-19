# accounting/views/order_note.py
"""
Temporary supplier Order Notes — Excel-like scratch sheets kept in the
user's session (never in the DB). Every supplier gets its own note, so
several orders can be prepared in parallel instead of overwriting one
sheet. Export a note to .xlsx and send it to the supplier on WhatsApp
so they can fill rates / availability themselves.
"""
import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..models import CompanyProfile, Contact
from .utils import htmx_response, is_htmx, toast_only_response

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
    from openpyxl.utils import get_column_letter
except ImportError:  # pragma: no cover
    Workbook = None

SESSION_KEY = 'order_notes'
LEGACY_SESSION_KEY = 'order_note'
TWO_PLACES = Decimal('0.01')


# ============================================================
# CELL PARSING (this sheet has no validation — bad input = blank)
# ============================================================
def _dec(value):
    if value is None:
        return None
    s = str(value).replace(',', '').strip()
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _money(value):
    if value is None:
        return None
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _compute_row(raw):
    """Normalise one sheet row and fill whichever price side is missing.

    price_src tells us which column the user typed last:
      'incl' -> derive excl from incl,  'excl'/default -> derive incl from excl.
    """
    qty = _dec(raw.get('qty')) or Decimal('0')
    tax = _dec(raw.get('tax'))
    if tax is None:
        tax = Decimal('18')
    excl = _dec(raw.get('price_excl'))
    incl = _dec(raw.get('price_incl'))
    factor = (Decimal('1') + tax / Decimal('100'))

    src = raw.get('price_src')
    if src == 'incl' and incl is not None:
        excl = _money(incl / factor)
    elif excl is not None:
        incl = _money(excl * factor)
    elif incl is not None:
        excl = _money(incl / factor)

    amount = _money(qty * incl) if incl is not None and qty else None
    line_excl = _money(qty * excl) if excl is not None and qty else None
    return {
        'date': (raw.get('date') or '').strip(),
        'product': (raw.get('product') or '').strip(),
        'qty': qty,
        'tax': tax,
        'price_excl': excl,
        'price_incl': incl,
        'price_src': 'incl' if src == 'incl' else 'excl',
        'amount': amount,
        'line_excl': line_excl,
    }


def _rows_from_post(request):
    def col(name):
        return request.POST.getlist(f'{name}[]')

    cols = {name: col(name) for name in
            ('date', 'product', 'qty', 'tax', 'price_excl', 'price_incl', 'price_src')}
    n = max((len(v) for v in cols.values()), default=0)
    rows = []
    for i in range(n):
        raw = {k: (v[i] if i < len(v) else '') for k, v in cols.items()}
        # date/tax come prefilled; a zero/junk qty isn't content either
        has_content = bool(raw['product'].strip()) or any(
            _dec(raw[k]) for k in ('qty', 'price_excl', 'price_incl'))
        if not has_content:
            continue
        rows.append(_compute_row(raw))
    return rows


def _totals(rows):
    subtotal_excl = sum((r['line_excl'] for r in rows if r['line_excl'] is not None), Decimal('0'))
    grand = sum((r['amount'] for r in rows if r['amount'] is not None), Decimal('0'))
    return {
        'qty': sum((r['qty'] for r in rows if r['qty'] is not None), Decimal('0')),
        'subtotal_excl': _money(subtotal_excl),
        'tax_total': _money(grand - subtotal_excl),
        'grand': _money(grand),
    }


def _cell(value):
    if value is None or value == Decimal('0'):
        return ''
    return str(value)


def _serialize_rows(rows):
    return [{k: _cell(v) for k, v in r.items()} for r in rows]


# ============================================================
# SESSION STORE (all notes live in one dict, keyed by note id)
# ============================================================
def _today():
    return timezone.localdate().isoformat()


def _load_notes(request):
    """Return the session notes dict, migrating the old single-sheet key."""
    notes = request.session.get(SESSION_KEY)
    if not isinstance(notes, dict):
        notes = {}

    legacy = request.session.pop(LEGACY_SESSION_KEY, None)
    if isinstance(legacy, dict) and (
        legacy.get('supplier') or legacy.get('note') or legacy.get('rows')
    ):
        note_id = uuid.uuid4().hex[:12]
        stamped = _today()
        notes[note_id] = {
            'id': note_id,
            'supplier': (legacy.get('supplier') or '').strip(),
            'note': (legacy.get('note') or '').strip(),
            'created': stamped,
            'updated': stamped,
            'rows': legacy.get('rows') or [],
        }
        request.session[SESSION_KEY] = notes
    return notes


def _store_notes(request, notes):
    request.session[SESSION_KEY] = notes


def _get_note(notes, note_id):
    note = notes.get(note_id)
    if not isinstance(note, dict):
        raise Http404('Order note not found')
    return note


def _note_rows(note):
    return [_compute_row(r) for r in note.get('rows', [])]


def _fmt_date(iso):
    try:
        return date.fromisoformat(iso).strftime('%d %b %Y')
    except (TypeError, ValueError):
        return iso or ''


def _summary(note):
    rows = _note_rows(note)
    totals = _totals(rows)
    return {
        'id': note.get('id'),
        'supplier': note.get('supplier') or '',
        'note': note.get('note') or '',
        'item_count': len(rows),
        'qty': totals['qty'],
        'grand': totals['grand'],
        'created': note.get('created') or '',
        'updated': note.get('updated') or '',
        'updated_display': _fmt_date(note.get('updated') or ''),
    }


def _sorted_summaries(notes):
    summaries = [_summary(note) for note in notes.values()]
    summaries.sort(key=lambda s: (s['updated'], s['created']), reverse=True)
    return summaries


def _vendor_names():
    return list(
        Contact.objects
        .filter(contact_type__in=('vendor', 'both'))
        .order_by('name')
        .values_list('name', flat=True)
        .distinct()
    )


def _sheet_context(rows, supplier, note_text, is_new, note_id=''):
    return {
        'is_new': is_new,
        'note_id': note_id,
        'supplier': supplier,
        'note': note_text,
        'rows': rows,
        'totals': _totals(rows),
        'today': timezone.localdate().isoformat(),
        'company': CompanyProfile.get_instance(),
        'vendor_names': _vendor_names(),
    }


# ============================================================
# LIST (all draft orders, one row per supplier)
# ============================================================
@login_required
def order_note_list(request):
    notes = _load_notes(request)
    return render(request, 'order_note/list.html', {
        'summaries': _sorted_summaries(notes),
        'note_count': len(notes),
        'company': CompanyProfile.get_instance(),
    })


# ============================================================
# CREATE (new blank sheet)
# ============================================================
@login_required
def order_note_create(request):
    notes = _load_notes(request)

    if request.method == 'POST':
        rows = _rows_from_post(request)
        supplier = (request.POST.get('supplier') or '').strip()
        note_text = (request.POST.get('note') or '').strip()

        if not rows and not supplier:
            messages.warning(
                request,
                'Add a supplier name or at least one item before saving.')
            return render(request, 'order_note/sheet.html',
                          _sheet_context(rows, supplier, note_text, True))

        note_id = uuid.uuid4().hex[:12]
        stamped = _today()
        notes[note_id] = {
            'id': note_id,
            'supplier': supplier,
            'note': note_text,
            'created': stamped,
            'updated': stamped,
            'rows': _serialize_rows(rows),
        }
        _store_notes(request, notes)
        messages.success(
            request,
            f'Order note for {supplier or "unnamed supplier"} created.')
        if request.POST.get('after') == 'list':
            return redirect('accounting:order_note_list')
        return redirect('accounting:order_note_edit', note_id=note_id)

    return render(request, 'order_note/sheet.html',
                  _sheet_context([], '', '', True))


# ============================================================
# EDIT (sheet for one supplier's order)
# ============================================================
@login_required
def order_note_edit(request, note_id):
    notes = _load_notes(request)
    note = _get_note(notes, note_id)

    if request.method == 'POST':
        rows = _rows_from_post(request)
        note['supplier'] = (request.POST.get('supplier') or '').strip()
        note['note'] = (request.POST.get('note') or '').strip()
        note['rows'] = _serialize_rows(rows)
        note['updated'] = _today()
        _store_notes(request, notes)
        messages.success(request, 'Order note saved.')
        if request.POST.get('after') == 'list':
            return redirect('accounting:order_note_list')
        return redirect('accounting:order_note_edit', note_id=note_id)

    return render(request, 'order_note/sheet.html', _sheet_context(
        _note_rows(note), note.get('supplier', ''), note.get('note', ''),
        False, note_id,
    ))


# ============================================================
# DELETE (one note; everything else stays)
# ============================================================
@login_required
@require_POST
def order_note_delete(request, note_id):
    notes = _load_notes(request)
    note = _get_note(notes, note_id)
    label = note.get('supplier') or 'unnamed supplier'
    del notes[note_id]
    _store_notes(request, notes)

    if is_htmx(request):
        return htmx_response(
            request, 'order_note/partials/list_table.html',
            {'summaries': _sorted_summaries(notes), 'note_count': len(notes)},
            toast={'level': 'success',
                   'message': f'Order note for {label} deleted.'},
        )

    messages.success(request, f'Order note for {label} deleted.')
    return redirect('accounting:order_note_list')


# ============================================================
# EXCEL EXPORT (POST saves the live grid first, then downloads)
# ============================================================
@login_required
def order_note_excel(request, note_id):
    notes = _load_notes(request)
    note = _get_note(notes, note_id)

    if request.method == 'POST':
        rows = _rows_from_post(request)
        note['supplier'] = (request.POST.get('supplier') or '').strip()
        note['note'] = (request.POST.get('note') or '').strip()
        note['rows'] = _serialize_rows(rows)
        note['updated'] = _today()
        _store_notes(request, notes)
    else:
        rows = _note_rows(note)

    if not rows:
        if is_htmx(request):
            return toast_only_response(
                {'level': 'warning',
                 'message': 'Order note is empty — add some items first.'})
        messages.warning(request,
                         'Order note is empty — add some items first.')
        return redirect('accounting:order_note_edit', note_id=note_id)

    if Workbook is None:  # pragma: no cover
        if is_htmx(request):
            return toast_only_response(
                {'level': 'danger',
                 'message': 'Openpyxl library is not installed.'}, status=400)
        messages.error(request, 'Openpyxl library is not installed.')
        return redirect('accounting:order_note_edit', note_id=note_id)

    wb = _build_workbook(note, rows, CompanyProfile.get_instance())

    supplier = note.get('supplier', '')
    slug = re.sub(r'[^a-z0-9]+', '-', supplier.lower()).strip('-') or 'supplier'
    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = (
        f'attachment; filename="order-note_{slug}_'
        f'{timezone.localdate().strftime("%Y%m%d")}.xlsx"'
    )
    wb.save(response)
    return response


def _build_workbook(note, rows, company):
    wb = Workbook()
    ws = wb.active
    ws.title = 'Order Note'

    header_font = Font(bold=True, color='FFFFFF', size=11)
    header_fill = PatternFill(start_color='1F4E78', end_color='1F4E78', fill_type='solid')
    thin = Side(style='medium', color='808080')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal='center', vertical='center')
    right_a = Alignment(horizontal='right', vertical='center')
    money_fmt = '#,##0.00'

    ws.merge_cells('A1:J1')
    ws['A1'] = f"{company.name or 'My Company'} — Order Note"
    ws['A1'].font = Font(bold=True, size=14, color='1F4E78')
    ws['A1'].alignment = center

    ws.merge_cells('A2:J2')
    ws['A2'] = (f"Supplier: {note.get('supplier') or '—'}    "
                f"Date: {timezone.localdate().strftime('%d-%m-%Y')}")
    ws['A2'].font = Font(bold=True, size=11)
    ws['A2'].alignment = center

    if note.get('note'):
        ws.merge_cells('A3:J3')
        ws['A3'] = note['note']
        ws['A3'].alignment = center

    headers = ['Sr', 'Date', 'Product / Item', 'Qty', 'Tax %',
               'Rate (Excl Tax)', 'Rate (Incl Tax)', 'Amount (Incl Tax)',
               'Available? (Supplier fills)', 'Supplier Rate (Supplier fills)']
    for col, h in enumerate(headers, 1):
        c = ws.cell(row=5, column=col, value=h)
        c.font = header_font
        c.fill = header_fill
        c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        c.border = border

    row = 6
    for idx, r in enumerate(rows, 1):
        has_excl = r['price_excl'] is not None
        has_incl = r['price_incl'] is not None
        if r['price_src'] == 'incl' and has_incl:
            excl_cell = f'=ROUND(G{row}/(1+E{row}/100),2)'
            incl_cell = float(r['price_incl'])
        elif has_excl:
            excl_cell = float(r['price_excl'])
            incl_cell = f'=ROUND(F{row}*(1+E{row}/100),2)'
        elif has_incl:
            excl_cell = f'=ROUND(G{row}/(1+E{row}/100),2)'
            incl_cell = float(r['price_incl'])
        else:
            excl_cell = incl_cell = None
        amount_cell = f'=ROUND(D{row}*G{row},2)' if (has_excl or has_incl) else None

        values = [
            idx,
            r['date'],
            r['product'],
            float(r['qty']) if r['qty'] else None,
            float(r['tax']),
            excl_cell,
            incl_cell,
            amount_cell,
            None, None,
        ]
        for col, v in enumerate(values, 1):
            c = ws.cell(row=row, column=col, value=v)
            c.border = border
            if col in (4, 5, 6, 7, 8):
                c.alignment = right_a
                if col in (6, 7, 8):
                    c.number_format = money_fmt

        for col in (9, 10):
            ws.cell(row=row, column=col).fill = PatternFill(
                start_color='FFF2CC', end_color='FFF2CC', fill_type='solid')
        row += 1

    last = row - 1
    label_row = row
    grand_row = label_row + 2
    ws.cell(row=label_row, column=3, value='Subtotal (Excl Tax)').font = Font(bold=True)
    c = ws.cell(row=label_row, column=6,
                value=f'=ROUND(SUMPRODUCT(D6:D{last},F6:F{last}),2)')
    c.font = Font(bold=True); c.number_format = money_fmt; c.alignment = right_a
    ws.cell(row=label_row + 1, column=3, value='Total Tax').font = Font(bold=True)
    c = ws.cell(row=label_row + 1, column=6, value=f'=ROUND(H{grand_row}-F{label_row},2)')
    c.font = Font(bold=True); c.number_format = money_fmt; c.alignment = right_a
    ws.cell(row=grand_row, column=3, value='GRAND TOTAL').font = Font(bold=True, size=12)
    c = ws.cell(row=grand_row, column=8, value=f'=ROUND(SUM(H6:H{last}),2)')
    c.font = Font(bold=True, size=12); c.number_format = money_fmt; c.alignment = right_a
    for rr in (label_row, label_row + 1, grand_row):
        for col in range(1, 11):
            ws.cell(row=rr, column=col).border = border

    widths = [5, 12, 34, 8, 8, 15, 15, 16, 22, 22]
    for idx, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(idx)].width = w
    ws.freeze_panes = 'A6'
    return wb
