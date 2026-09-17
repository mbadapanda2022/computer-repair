Namaste. Mera project hai computer_repair (A1 Computer Solutions). Ye mera PROJECT_CONTEXT.md hai. Poora padhkar samjho. Jab ready ho jao, "Ready" bolo. Phir main apna issue bataunga.

# PROJECT CONTEXT — computer_repair (A1 Computer Solutions)

> **AI Assistant Instructions:**
> - Chat in **Hindi** (user preference)
> - Code, UI strings, comments in **English**
> - Preserve ALL existing features — only add/fix, never break
> - **Do NOT give git commands after every small change** — user wants:
>   1. First verify code works locally (user tests)
>   2. Only after confirmation, give ONE complete git command block
> - When replacing files, prefer **full file replace** over partial patches
> - If a file is needed for context, ask first — don't assume
> - **Always audit before fixing** — user values data safety over speed
> - **Check models.py thoroughly** before migrations (indentation, FK types, negative values)

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
- **Customer views:** `accounting/views/customer_views.py`
- **Statements:** `accounting/views/statements.py` (shared staff+customer)
- **Global search:** `accounting/views/global_search.py`
- **Notifications:** `accounting/utils/notification_helpers.py`, `accounting/views/notifications.py`
- **Utils:** `accounting/views/utils.py` (is_htmx, htmx_response, toast_only_response)
- **Decorators:** `accounting/decorators.py` (handle_errors)
- **Middleware:** `accounting/middleware.py`
- **Settings:** `computer_repair/settings.py`
- **URLs:** `accounting/urls.py`, `accounting/customer_urls.py`
- **JS:** `static/js/app.js`
- **CSS:** `static/css/custom.css`
- **Layouts:** `templates/base.html`, `templates/base_customer.html`
- **Template tags:** `accounting/templatetags/math_filters.py`, `notification_tags.py`, `purchase_tags.py`
- **Build:** `build.sh`

---

## Core Patterns (NEVER break)

### Soft delete
- Everywhere via `.soft_delete()`, `is_deleted` flag
- `Contact.soft_delete()` also anonymizes linked User (email/username freed)
- `SoftDeleteQuerySet.delete()` — **Special handling for Contact AND StockMovement** (both need per-instance delete to run custom logic)
- StockMovement delete reverses stock effect (`current_stock -= quantity`)
- CreditNoteItem delete reverses stock per-instance

### Authentication & Security
- Custom backend: `EmailOrPhoneBackend` (email → username → phone lookup)
- OTP via `secrets` module, never logged
- Login rate limit: 5/5min (DatabaseCache)
- CSRF token in `<meta name="csrf-token">` — JS reads it for HTMX

### HTMX patterns
- **Modal target:** `#mainModalContent` (staff), `#mainModal` (customer)
- **Toast-only response:** `toast_only_response()` sets `HX-Reswap: none`
- **Redirect after save:** `HX-Redirect` header
- **Error rendering in modal:** `HX-Retarget: #mainModalContent` (via `htmx_response` extra_headers)
- **Notifications polling:** every 30s (NO SSE — SSE was removed earlier)
- **Global progress bar:** `#htmx-progress-bar` (currently commented in base.html)
- **OOB swap:** For stats cards auto-refresh after CRUD — `<div hx-swap-oob="outerHTML:#element-id">`

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
- **`LedgerEntry.reference_id` is PositiveIntegerField** — NEVER use negative values. Use distinct `entry_type` instead (e.g., `entry_type='credit_note', reference_id=cn.id`)

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
- **Full file replace** preferred over small patches
- If a file is needed for context, ask user to send it — don't assume
- **Audit first, fix later** — user values understanding before changes
- **Check migrations carefully** — no data loss, no negative values in PositiveIntegerField

---

## Recent Major Changes (Last Sessions)

### Purchase Module (COMPLETE ✅)
1. **HTMX target error fixed** — `_purchase_form_content.html` now has `action` attr + conditional `hx-post`/`hx-target` only when `is_htmx=True`
2. **Ledger sync `advance_adjustments` bug fixed** — uses `hasattr(purchase, 'advance_adjustments')` guard (Purchase doesn't have this relation, only Invoice does)
3. **Quick Add Product modal** — full professional with Category dropdown + inline category add
4. **Auto-add product to purchase items** — `productCreated` event listener in `purchase_items.html`
5. **Double-add bug fixed** — `document.addEventListener` with `window.__purchaseItemsHandlersBound` guard (prevents listener stacking on HTMX swaps)

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
- Product count column per category
- "Categories" button on Inventory page header
- **Preserved**: inline-add flow (product form), `ProductCategoryForm`, `add_category_inline`

### Sales Module (COMPLETE ✅)
**Phase B.1 — Professional upgrade:**
- Form modal target bug fixed (`action` attr + conditional `hx-post`)
- Session cleanup on fresh invoice open (no leak from cancelled forms)
- Delete safety: blocks if payments linked
- **Overdue detection**: `is_overdue` / `days_overdue` properties + red badge + filter
- **WhatsApp share**: `whatsapp_share_url` property + button + dropdown action
- **Duplicate invoice**: clone invoice + items as fresh draft
- **Repair-linked badge**: on invoice detail + link to RepairJob
- **Inline edit from list dropdown** (modal, not full page reload)
- Professional invoice detail redesign with sections: Info | Financials | Items | Payment History | Customer Quick View

**Phase B.3 — Credit Notes / Sales Returns (COMPLETE ✅):**
- **Models:** `CreditNote`, `CreditNoteItem` (soft delete pattern)
- `sync_credit_note_ledger()` — reverses invoice ledger
- Invoice properties: `credit_note_total`, `net_amount`, `is_fully_returned`, `has_credit_notes`
- `LedgerEntry.ENTRY_TYPE` includes `'credit_note'`
- **Views:** `credit_note_list/create/detail/print/delete/whatsapp`
- **Templates:** 5 new CN templates + invoice_detail update (button + section) + base.html sidebar link
- **Stock behavior:** Full/partial return → auto `StockMovement('return_in')` adds stock back; value-only → no stock movement
- **Refund methods:** Cash / Bank / Credit to Account / No Refund
- **Safety:** `on_delete=PROTECT` on invoice FK, blocks invoice delete if CN exists
- **Migration applied successfully**

### Contact.soft_delete() anonymizes User
- Frees up email/username for re-registration
- Sets `is_active=False`, `set_unusable_password()`

### cleanup_orphans management command
- Removes orphan Contact.User links

### Send Estimate to Customer (Repairs)
- Modal with method choice (email / WhatsApp / both)
- In-app notification + email + WhatsApp link
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

**NOTE:** Credit Notes section in customer portal is PENDING (Phase B.3.1 — next work)

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

### Contacts / Products / Sales / Purchases / Repairs / Payments — standard CRUD

### Sales (updated)
- `accounting:invoice_list` + detail + print + create + update + delete
- `accounting:invoice_duplicate` (B.1)
- `accounting:invoice_whatsapp` (B.1)
- `accounting:invoice_list_print` + `invoice_list_excel`

### Credit Notes (B.3 — NEW)
- `accounting:credit_note_list` → `/credit-notes/`
- `accounting:credit_note_create` → `/sales/<invoice_pk>/credit-note/create/`
- `accounting:credit_note_detail` + `_print` + `_delete` + `_whatsapp`

### Categories (Phase 2 — NEW)
- `accounting:category_list` → `/products/categories/`
- `accounting:category_create` + `_update` + `_delete`

### Statements
- `accounting:customer_statement` + `_print` + `_excel` + `_whatsapp`
- `accounting:vendor_statement` + `_print` + `_csv` + `_excel`
- `accounting:combined_statement` + `_print` + `_excel` + `_whatsapp`

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
- `has_purchases(user)`

---

## Known Gotchas

1. **Django templates don't allow leading underscore variables** — use `invoice.linked_repair` (not `invoice._repair_job`)

2. **`{% url %}` inside `<script>` block** — use `data-*` attributes OR `{% url ... as var %}`

3. **HTMX `hx-post` with forloop.counter0** — safer to use `{% url ... as var %}`

4. **Customer-side `_build_combined_rows()`** — same function used by staff and customer. Additions must not break either side.

5. **Print/Excel** must always use `sort=asc`

6. **Login rate limit** uses DatabaseCache

7. **Contact.soft_delete** iterates per-instance (bulk operations slow for many contacts)

8. **`LedgerEntry.reference_id` is PositiveIntegerField** — NEVER use negative values. Use distinct `entry_type` instead.

9. **Session-based items leak** — always reset session on fresh GET (not POST failure)

10. **HTMX click listener stacking** — when partials re-render on swap, event listeners accumulate on same parent. Use `document.addEventListener` + guard flag (`window.__xxxBound`).

11. **StockMovement queryset delete skips stock reversal** — `SoftDeleteQuerySet.delete()` must route StockMovement through per-instance delete.

12. **PositiveIntegerField constraint** — `models.PositiveIntegerField` rejects negative values at DB level (Postgres CHECK). Test in shell before assuming.

---

## Current Production Status (as of latest deploy)

### Working ✅
- Staff dashboard, contacts, products, sales, purchases, repairs, payments
- **Purchase module** — HTMX form, ledger sync, quick-add product, auto-add, no double-add
- **Product module** — Phase 1 fixes + Category Management page
- **Sales module** — B.1 (overdue, WhatsApp, duplicate, repair-linked) + B.3 (Credit Notes)
- Statements (all 3 types) with sort toggle
- Global search (Ctrl+K) on staff portal
- Customer portal (dashboard, invoices, orders, repairs, payments, statement, profile)
- Invoice type badges (Sale / Repair)
- Purchase detail/print with GST breakup + HSN + signatures
- Estimate send to customer
- Email change via OTP
- Notifications polling (30s)
- Soft delete with User anonymization
- Audit log (backend, no UI viewer yet)
- Credit Notes (Sales Returns) — full workflow

### Not yet built (planned)
- **Customer portal: Credit Notes section** (Phase B.3.1) — customers can view their CNs
- **Payments module upgrade** — Receipt print, WhatsApp, bulk recording, reconciliation
- **Repairs module audit** — User has not shared repairs files yet. Need: `accounting/views/repairs.py` + templates. Next major module.
- **Overdue invoice alerts** (dashboard widget)
- **Audit log viewer UI** (superuser)
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

## Roadmap / Next Steps (User's Plan)

User wants to work in this order (each in separate chat session):

1. **Repairs Module** (NEXT) — Full audit + professional upgrade
   - User said: "Repair job ka kaam me kar chuka hun wo sahi kaam kar raha hai, agar usme kaam karenge toh ye chat me nehi kisi aur chat me"
   - Files needed: `accounting/views/repairs.py`, all `templates/repairs/*`
   - Preserve existing features — audit first, then fix/add

2. **Accounts Module** — Statements, Ledgers, Journal, Reports

3. **Payments Module** (LAST) — Receipt print, WhatsApp, bulk, reconciliation
   - User's rationale: "payment ka kaam tab karenge jab tak tume baki sab ke bareme pata chal jaye"

4. **Customer Portal: Credit Notes section** (may be combined with another phase)

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
- Give full file replaces (not patches) for templates
- **Check models.py carefully before migrations**
- Wait for local verification before git commands
- Give ONE complete git command at the end

---

## Git Workflow (User Preference)

**User wants only ONE git command block, and only after everything works locally.**

Standard git command format:
```bash
git add -A
git commit -m "<meaningful message with bullet points>"
git push origin main
```

---

## Lessons Learned (from past sessions)

1. **HTML forms with HTMX need BOTH `action` and `hx-post`** — action for native fallback, hx-post for HTMX. Otherwise broken UX on validation errors.

2. **`hx-target` should always be `#mainModalContent` for modal forms** — not the table container. Use `HX-Retarget` header if needed for errors.

3. **Session-based item lists** — reset on GET, not on POST failure. Guard against leaks.

4. **Listeners on parent containers** — use `document.addEventListener` + guard flag if the partial re-renders on swap.

5. **Soft delete querysets** — if the model has custom `delete()` logic (stock reversal, user anonymization), route through per-instance delete in `SoftDeleteQuerySet.delete()`.

6. **Positive fields reject negative values** — always check `PositiveIntegerField`, `PositiveSmallIntegerField`, `MinValueValidator(0)` before using negative reference IDs.

7. **`hasattr(model, 'relation')` guard** — for optional FK relations that may not exist across all model instances (e.g., `Purchase.advance_adjustments`).

8. **Full file replace for templates** — user finds patches confusing. Full file is safer.

9. **Test in shell before migration** — `python manage.py shell` → import models, test number generation, verify no constraint issues.

10. **Backward compatibility** — always ask "will this break existing features?" before adding. Preserve first, add second.