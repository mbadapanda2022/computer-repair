# PROJECT CONTEXT — computer_repair (A1 Computer Solutions)

> **AI Assistant Instructions:**
> - Chat in **Hindi** (user preference)
> - Code, UI strings, comments in **English**
> - Preserve ALL existing features — only add/fix, never break
> - **Do NOT give git commands after every small change** — user wants:
>   1. First verify code works locally (user tests)
>   2. Only after confirmation, give ONE complete git command block
> - When replacing files, prefer **full file replace** for small templates, **surgical before/after** for large Python files
> - If a file is needed for context, ask first — don't assume
> - **Always audit before fixing** — user values data safety over speed
> - **Check models.py thoroughly** before migrations (indentation, FK types, negative values)
> - **Financial data safety first** — Payments, Bank, Journal, Ledger = HIGH RISK

---

## Deployment
- **GitHub:** https://github.com/mbadapanda2022/computer-repair
- **Production:** https://a1computersolutions.onrender.com
- **Hosting:** Render free tier + Supabase Postgres + Cloudinary
- **Tech:** Django 5.1.5 + HTMX + Bootstrap 5 + Chart.js

---

## Project Structure
- Django project: `computer_repair/`
- Main app: `accounting/`
- Templates: `templates/` (staff) + `templates/customer/` (customer portal)
- Static: `static/`
- Two URL namespaces: `accounting` (staff), `customer` (customer portal)
- Tracking namespace: `accounting.tracking_urls` (public repair tracking, no login)

---

## Key Files
- **Models:** `accounting/models.py`
- **Auth:** `accounting/views/auth.py`, `accounting/auth_backends.py`, `accounting/utils/otp_helpers.py`
- **Repairs:** `accounting/views/repairs.py`
- **Sales/Invoices:** `accounting/views/sales.py`
- **Purchases:** `accounting/views/purchases.py`
- **Products:** `accounting/views/products.py`
- **Stock:** `accounting/views/stock.py`
- **Payments:** `accounting/views/payments.py`
- **Bank:** `accounting/views/bank.py`
- **Journal:** `accounting/views/journal.py`
- **Customer views:** `accounting/views/customer_views.py`
- **Statements:** `accounting/views/statements.py` (shared staff+customer)
- **Global search:** `accounting/views/global_search.py`
- **Notifications:** `accounting/utils/notification_helpers.py`, `accounting/views/notifications.py`
- **OTP:** `accounting/utils/otp_helpers.py`
- **Tracking helpers:** `accounting/utils/tracking.py`
- **Tracking views:** `accounting/views/tracking.py`
- **Image processing:** `accounting/utils/image_processor.py`
- **HTMX utils:** `accounting/views/utils.py` (is_htmx, htmx_response, toast_only_response, redirect_to_staff, redirect_to_customer)
- **Decorators:** `accounting/decorators.py` (handle_errors)
- **Middleware:** `accounting/middleware.py` (AccessControlMiddleware)
- **Signals:** `accounting/signals.py` (only notification cleanup on hard delete)
- **Context processors:** `accounting/context_processors.py` (company, logo_url)
- **Settings:** `computer_repair/settings.py`
- **URLs:** `accounting/urls.py`, `accounting/customer_urls.py`, `accounting/tracking_urls.py`, `computer_repair/urls.py`
- **Forms:** `accounting/forms.py`
- **JS:** `static/js/app.js`
- **CSS:** `static/css/custom.css`
- **Layouts:** `templates/base.html` (staff), `templates/base_customer.html` (customer)
- **Template tags:** `accounting/templatetags/math_filters.py`, `notification_tags.py`, `purchase_tags.py`, `cloudinary_filters.py`
- **Validators:** `accounting/validators.py` (image validation)
- **Build:** `build.sh`

---

## Core Patterns (NEVER break)

### Soft delete
- Everywhere via `.soft_delete()`, `is_deleted` flag
- `Contact.soft_delete()` also anonymizes linked User (email/username freed)
- `SoftDeleteQuerySet.delete()` — **Special handling for Contact AND StockMovement** (both need per-instance delete to run custom logic)
- StockMovement delete reverses stock effect (`current_stock -= quantity`)
- CreditNoteItem delete reverses stock per-instance
- **IMPORTANT:** Bulk `.delete()` skips per-instance signals. For Payment, LedgerEntry, etc., must delete children per-instance.

### Authentication & Security
- Custom backend: `EmailOrPhoneBackend` (email → username → phone lookup)
- OTP via `secrets` module, never logged, constant-time compare
- Login rate limit: 5/5min (DatabaseCache)
- CSRF token in `<meta name="csrf-token">` — JS reads it for HTMX
- Public tracking via signed URLs (no login required, 90-day expiry)

### HTMX patterns
- **Modal target:** `#mainModalContent` (staff), `#mainModal` (customer)
- **Toast-only response:** `toast_only_response()` sets `HX-Reswap: none`
- **Redirect after save:** `HX-Redirect` header
- **Error rendering in modal:** `HX-Retarget: #mainModalContent` (via `htmx_response` extra_headers)
- **Notifications polling:** every 30s (NO SSE — SSE was removed; `send_notification_sse` is a **no-op stub** for backward compatibility)
- **OOB swap:** For stats cards auto-refresh after CRUD — `<div hx-swap-oob="outerHTML:#element-id">`
- **Progress bar:** `#htmx-progress-bar` (currently commented in base.html)

### Contacts
- `contact_type` can be `customer` | `vendor` | `both`
- Each Contact can be linked to one User (OneToOne, nullable)
- Phone normalized to last 10 digits

### Statement sorting
- Default: **Newest First** (`sort=desc`) — user preference
- Toggle available in all statement pages
- **Print/Excel/CSV/WhatsApp always chronological** (`sort=asc`)

### Ledger / Accounting
- Double-entry ledger via `LedgerEntry` + `LedgerLine`
- `subledger_type`: 'receivable' (customer side) or 'payable' (vendor side)
- `Contact.recalc_balance()` recalculates from ledger lines
- Invoice sync via `sync_invoice_ledger()`, Purchase via `sync_purchase_ledger()`
- Credit Note via `sync_credit_note_ledger()`
- Payment via `Payment.create_ledger_entry()` / `update_ledger_entry()`
- **`LedgerEntry.reference_id` is PositiveIntegerField** — NEVER use negative values
- Journal entries use `LedgerEntry.entry_type='journal'` + `journal_type` field
- `create_journal_lines()` in `accounting/utils.py` — shared by Journal + Payment

### Document numbering (Race-safe)
- `InvoiceCounter.get_next_number(prefix)` with `select_for_update()`
- Prefixes: `INV`, `PUR`, `REP`, `CN`
- Retry logic (5 attempts) on IntegrityError

### Session-based line items (Purchase, Invoice)
- Items stored in session while form open
- Reset on fresh GET: `if request.method == 'GET': request.session['temp_items'] = []`
- Guard against leaking cancelled form items to new form

---

## User Preferences
- Chat in **Hindi**
- Code/UI strings/comments in **English**
- Preserve ALL existing features — only add/fix
- **Do NOT push git commands after every change** — wait until user confirms everything works locally, then ONE complete git command
- **Full file replace** preferred for small templates; **surgical before/after** for large Python files (to avoid breaking unrelated code)
- If a file is needed for context, ask user to send it — don't assume
- **Audit first, fix later** — user values understanding before changes
- **Check migrations carefully** — no data loss, no negative values in PositiveIntegerField
- **User-friendly, good-looking UI** — every change must maintain or improve UX
- **Financial data safety** — Payments, Bank, Journal are HIGH RISK; extra caution

---

## Recent Major Changes (Last Sessions)

### Repairs Module — Phase 1 (COMPLETE ✅)
Fixed 6 bugs:
1. Duplicate customer notifications on status change (model + view both fired)
2. `submitted_at` incorrectly set on staff-created repairs
3. `repair_table.html`: Edit + Create Invoice links → HTMX modal (were broken/partial page)
4. `_repair_form_content.html`: added `action` attribute for native fallback
5. `create_invoice_modal.html`: fixed Bootstrap modal structure (footer was nested)
6. `repair_detail.html`: Edit button → HTMX modal
7. Email pause: `send_email=False` on repair_create, repair_create_for_contact, create_invoice_from_repair

### Payments + Bank + Journal — Phase 1 (COMPLETE ✅)
Fixed 5 bugs:
1. `Payment.delete()`: recalc affected invoices after bulk allocation delete
2. `journal_delete`: delete LedgerLines per-instance before entry (no orphans)
3. `bank_transaction_edit`: reverse sync `reconciled` flag → Payment
4. `payments.py`: removed 4 legacy `send_notification_sse` loops (no-op waste)
5. Added `action` attribute to 5 HTMX forms (payment_form, journal_form, journal_form_modal, bank_account_form, bank_transaction_form)

### Purchase Module (COMPLETE ✅)
1. HTMX target error fixed — `_purchase_form_content.html` has `action` attr + conditional `hx-post`
2. Ledger sync `advance_adjustments` bug fixed — `hasattr(purchase, 'advance_adjustments')` guard
3. Quick Add Product modal — full professional with Category dropdown
4. Auto-add product to purchase items via `productCreated` event
5. Double-add bug fixed via `window.__purchaseItemsHandlersBound` guard

### Product Module (COMPLETE ✅)
**Phase 1 — Critical fixes:**
- Product form modal target bug (validation errors now render inside modal via `HX-Retarget`)
- Stats cards auto-refresh after CRUD (OOB swap)
- Pagination preserves `stock_filter` param
- `close_modal` flag on product delete
- Stock dashboard "Add Stock" pre-fills product via GET param
- Stock adjustment delete uses `StockMovement.delete()` (preserves manual opening stock)
- `handle_errors` decorator: forces `HX-Retarget: #mainModalContent`

**Phase 2 — Category Management:**
- New page `/products/categories/` with list, search, stats
- Category CRUD (create/update/delete) via HTMX modals
- Delete safety: blocks if active products use the category

### Sales Module (COMPLETE ✅)
**Phase B.1 — Professional upgrade:**
- Form modal target bug fixed
- Session cleanup on fresh invoice open
- Delete safety: blocks if payments linked
- Overdue detection: `is_overdue` / `days_overdue` properties + red badge
- WhatsApp share: `whatsapp_share_url` property + button
- Duplicate invoice: clone invoice + items as fresh draft
- Repair-linked badge: on invoice detail + link to RepairJob
- Inline edit from list dropdown

**Phase B.3 — Credit Notes (COMPLETE ✅):**
- Models: `CreditNote`, `CreditNoteItem` (soft delete pattern)
- `sync_credit_note_ledger()` — reverses invoice ledger
- Views: `credit_note_list/create/detail/print/delete/whatsapp`
- Stock: Full/partial return → auto `StockMovement('return_in')`

### Contact.soft_delete() anonymizes User
- Frees up email/username for re-registration
- Sets `is_active=False`, `set_unusable_password()`

### Send Estimate to Customer (Repairs)
- Modal with method choice (email / WhatsApp / both)
- In-app notification + email (email currently paused for repairs)
- Estimate print template

### Customer estimate review UI
- Customer portal shows estimate breakdown + 3 buttons (Approve / Hold / Reject)

### Email change via OTP (customer)
- Step 1: request new email → OTP sent
- Step 2: verify OTP → email updated, force re-login

### Global Search (Ctrl+K) — Staff Portal
- Command palette style, vanilla JS fetch (NOT HTMX)
- 300ms debounce, keyboard nav, Ctrl+K
- Searches: Invoices, Purchases, Repairs, Payments, Contacts, Products

### Repair Tracking (Public)
- Signed URL via `django.core.signing`
- 90-day expiry, no DB writes
- Rate limited (30/min per IP)
- Shows: status, timeline, device, amount only

---

## Customer Portal — Structure

### Sidebar Menu (in order)
1. Home
2. Dashboard
3. Invoices (with Sale/Repair type badges)
4. **Orders** (only if has_purchases — via `purchase_tags.py::has_purchases`)
5. Repairs
6. Notifications (with badge)
7. Payments
8. Statement
9. Profile
10. Logout

**NOTE:** Credit Notes section in customer portal is PENDING (Phase B.3.1 — was skipped)

### Key URLs (customer namespace)
- `customer:customer_dashboard`
- `customer:customer_invoices` + detail/print
- `customer:customer_purchases` + detail/print
- `customer:customer_repairs` + detail/print/create/update/delete
- `customer:customer_estimate_approve` / `_hold` / `_reject`
- `customer:customer_payments`
- `customer:customer_statement`
- `customer:customer_profile` + `_update`
- `customer:customer_password_change`
- `customer:email_change_request` + `email_change_verify`
- `customer:customer_notifications` + dropdown + unread-count

---

## Staff Portal — Key URLs (accounting namespace)

### Dashboard
- `accounting:dashboard` + `dashboard_stats` + `refresh_stats` + `recent_transactions` + `dashboard_export`

### Contacts / Products / Sales / Purchases / Repairs / Payments / Bank / Journal — standard CRUD

### Sales
- `accounting:invoice_list` + detail + print + create + update + delete + duplicate + whatsapp
- `accounting:credit_note_list` + detail + print + delete + whatsapp + create

### Purchases
- `accounting:purchase_list` + detail + print + create + update + delete

### Repairs
- `accounting:repair_list` + create + detail + update + status + add_part + remove_part + create_invoice + print + list_print + export_excel + staff_approve + quick_update + send_estimate + estimate_print + warranty_card_print + create_for_contact

### Payments
- `accounting:payment_list` + create + update + delete + reconcile + load_unpaid_invoices

### Bank
- `accounting:bank_account_list` + add + edit + delete + statement + statement_excel + transaction_list + transaction_add + transaction_edit + transaction_delete

### Journal
- `accounting:journal_list` + create + create_for_contact + update + delete + validate_field

### Statements
- `accounting:customer_statement` + `_print` + `_excel` + `_whatsapp`
- `accounting:vendor_statement` + `_print` + `_csv` + `_excel`
- `accounting:combined_statement` + `_print` + `_excel` + `_whatsapp`

### Tracking (public, no login)
- `tracking:repair_track` — signed URL with 90-day expiry

### Global Search
- `accounting:global_search` → `/search/?q=...`

### Health
- `accounting:health_check` → `/health/`

---

## Template Tag Libraries

### `math_filters.py`
- `divide`, `multiply`, `subtract`, `add_filter`, `abs_filter`, `percentage`, `currency`, `sum_line_totals`, `total`, `floatformat`, `positive`, `negative`, `round_number`, `sum_value`

### `notification_tags.py`
- `unread_count(user)`, `message_unread_count(user)`

### `purchase_tags.py`
- `has_purchases(user)` — check if contact has purchases

### `cloudinary_filters.py`
- `fix_cloudinary_url(value)` — fix malformed Cloudinary URLs

---

## Known Gotchas

1. **Django templates don't allow leading underscore variables** — use `invoice.linked_repair` (not `invoice._repair_job`)

2. **`{% url %}` inside `<script>` block** — use `data-*` attributes OR `{% url ... as var %}`

3. **HTMX `hx-post` with forloop.counter0** — safer to use `{% url ... as var %}`

4. **Customer-side `_build_combined_rows()`** — same function used by staff and customer. Additions must not break either side.

5. **Print/Excel** must always use `sort=asc`

6. **Login rate limit** uses DatabaseCache

7. **Contact.soft_delete** iterates per-instance

8. **`LedgerEntry.reference_id` is PositiveIntegerField** — NEVER use negative values. Use distinct `entry_type` instead.

9. **Session-based items leak** — always reset session on fresh GET (not POST failure)

10. **HTMX click listener stacking** — when partials re-render on swap, event listeners accumulate. Use `document.addEventListener` + guard flag (`window.__xxxBound`).

11. **StockMovement queryset delete skips stock reversal** — `SoftDeleteQuerySet.delete()` must route StockMovement through per-instance delete.

12. **PositiveIntegerField constraint** — rejects negative values at DB level (Postgres CHECK).

13. **Bulk `.delete()` skips per-instance signals** — for Payment, LedgerEntry, RepairJob etc., must delete children per-instance to fire `post_delete`/`post_save` signals. This was Bug #1 in Payments audit.

14. **`send_notification_sse` is a NO-OP stub** — kept for backward compatibility. Never rely on it. HTMX polling every 30-60s handles notifications.

15. **HTMX forms need BOTH `action` and `hx-post`** — for native fallback when JS/HTMX fails. PROJECT_CONTEXT Lesson #1.

16. **`handle_errors` decorator forces `HX-Retarget: #mainModalContent`** — ensures modal validation errors stay in modal.

17. **Cloudinary URL fix** — `company.logo.url` can be malformed (`https:/` instead of `https://`). Use `logo_url` from context processor or `fix_cloudinary_url` filter.

---

## Current Production Status (as of latest deploy)

### Working ✅
- Staff dashboard, contacts, products, sales, purchases, repairs, payments, bank, journal
- **Repairs Module — Phase 1 fixes** (duplicate notifications, submitted_at, HTMX links, action attr, modal footer)
- **Payments + Bank + Journal — Phase 1 fixes** (data integrity, SSE cleanup, action attrs)
- **Purchase module** — HTMX form, ledger sync, quick-add product
- **Product module** — Phase 1 + Category Management
- **Sales module** — B.1 + B.3 (Credit Notes)
- Statements (all 3 types) with sort toggle
- Global search (Ctrl+K)
- Customer portal (dashboard, invoices, orders, repairs, payments, statement, profile)
- Repair tracking (public signed URL, 90-day expiry)
- Notifications polling (30s)
- Soft delete with User anonymization
- Audit log (backend, no UI viewer)

### Pending / Planned
- **Repairs Module — Phase 1 remaining:**
  - A1: `send_estimate_to_customer` email option — decision pending (currently still has email radio)
  - A2: `openWhatsApp` listener in `app.js` — missing, WhatsApp link doesn't auto-open
  - A4: `parts_table.html` — likely legacy, confirm/delete
- **Payments + Bank + Journal — Phase 2 remaining:**
  - Bug #7: payment update notification spam
  - Bug #8: TomSelect CSS/JS re-inject on modal open
  - Bug #9: Journal spinner listener stacking
  - Bug #12: Journal table contact duplicate rendering
- **Feature Gaps:**
  - F1: Ledger Viewer (superuser, all entries)
  - F2: Bank account selector in Journal (currently assumes cash)
  - F3: Purchase.paid_amount field (partial vendor payments)
  - F4: Bulk payment entry
  - F5: Bank Transaction → Payment direct link
  - F6: Audit log viewer UI
- **Repairs Module — Phase 2/3:**
  - Estimate status filter in list
  - `WARRANTY_DAYS` configurable
  - `repair_form_from_contact.html` refactor
  - Duplicate/Clone repair
  - Customer repair history sidebar
  - WhatsApp direct button
  - Photo attachments
  - Bulk status update
- **Customer Portal:** Credit Notes section (B.3.1 — skipped)
- **Accounts Module:** Statements, Ledgers, Journal, Reports (broader scope)
- **Payments Module upgrade:** Receipt print, WhatsApp, bulk recording, reconciliation UI
- **Overdue invoice alerts** (dashboard widget)
- **Bulk actions** (multi-select on lists)
- **WhatsApp Business API automation**
- **SMS notifications**
- **Customer feedback / rating system**
- **GSTR-1 auto-summary**
- **Multi-branch support**

---

## Environment Variables (Render)

Required:
- `SECRET_KEY`
- `DEBUG` (False in production)
- `DATABASE_URL` (Supabase Postgres)
- `CLOUDINARY_URL` (or CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET)
- `ALLOWED_HOSTS`
- `EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`

---

## Roadmap / Next Steps

1. **Repairs Module Phase 1 remaining items** (A1, A2, A4)
2. **Payments + Bank + Journal Phase 2** (notification spam, TomSelect, spinner, journal duplicate)
3. **Accounts Module** (Ledger Viewer, Reports, Statements expansion)
4. **Feature Gaps** (F1-F6)
5. **Customer Portal: Credit Notes** (B.3.1 — if needed)
6. **Payments Module professional upgrade** (Receipt print, WhatsApp, bulk, reconciliation)

---

## Git Workflow (User Preference)

**User wants only ONE git command block, and only after everything works locally.**

Standard format:
```bash
git add -A
git commit -m "<meaningful message with bullet points>"
git push origin main
```

---

## Lessons Learned

1. **HTML forms with HTMX need BOTH `action` and `hx-post`** — action for native fallback, hx-post for HTMX.

2. **`hx-target` should always be `#mainModalContent` for modal forms** — use `HX-Retarget` header for errors.

3. **Session-based item lists** — reset on GET, not on POST failure.

4. **Listeners on parent containers** — use `document.addEventListener` + guard flag if partial re-renders.

5. **Soft delete querysets** — if model has custom `delete()` logic, route through per-instance delete.

6. **Positive fields reject negative values** — check `PositiveIntegerField`, `PositiveSmallIntegerField`, `MinValueValidator(0)`.

7. **`hasattr(model, 'relation')` guard** — for optional FK relations.

8. **Full file replace for templates** — user finds patches confusing for small files.

9. **Test in shell before migration** — `python manage.py shell` → test.

10. **Backward compatibility** — always ask "will this break existing features?" before adding.

11. **Bulk `.delete()` skips per-instance signals** — for financial data models (Payment, LedgerEntry), must iterate per-instance to fire `post_delete`/`post_save` hooks (invoice recalc, contact balance recalc, etc.).

12. **`send_notification_sse` is a no-op** — no need to call it. HTMX polling handles notifications. Removing it saves wasted DB queries per staff per operation.

13. **When auditing, check cross-module patterns** — same bug often appears in multiple modules (e.g., duplicate SSE loops in Repairs + Payments + Bank).

14. **Financial modules = extra caution** — Payments, Bank, Journal affect ledger and balances. Every change must be verified with data integrity tests (invoice recalc, contact balance, statement consistency).

15. **Surgical before/after for large Python files** — full file replace on a 1300-line models.py is risky. Use precise find/replace blocks.

---

## How to Ask AI for Help

When starting a new chat, paste:
1. This entire file
2. The specific issue/feature you want
3. Files that are relevant (AI will ask if needed)

Example:
> "मेरा issue ये है: [problem]। संबंधित files अगर देखने हों तो बताओ।"

AI should:
- Understand the full context from this file
- **Audit before fixing** — read all relevant files first
- Ask for specific files if needed (don't assume)
- Give **full file replaces** for small templates, **surgical before/after** for large Python files
- **Check models.py carefully** before migrations
- Wait for local verification before git commands
- Give ONE complete git command at the end
- **Preserve existing features** — no breaking changes

---

**Last Updated:** After Payments + Bank + Journal Phase 1 fixes + Repairs Phase 1 fixes.