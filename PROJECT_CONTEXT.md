Namaste. Mera project hai computer_repair (A1 Computer Solutions). Ye mera PROJECT_CONTEXT.md hai. Poora padhkar samjho. Jab ready ho jao, "Ready" bolo. Phir main apna issue bataunga.

# PROJECT CONTEXT — computer_repair (A1 Computer Solutions)

> **AI Assistant Instructions:**
> - Chat in **Hindi** (user preference)
> - Code, UI strings, comments in **English**
> - Preserve ALL existing features — only add/fix, never break
> - **Do NOT give git commands after every small change** — user wants:
>   1. First verify code works locally (user tests)
>   2. Only after confirmation, give ONE complete git command block
> - When replacing files, prefer **full file replace** over partial patches (user finds patches confusing)
> - If a file is needed for context, ask first — don't assume

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
- **Customer views:** `accounting/views/customer_views.py`
- **Statements:** `accounting/views/statements.py` (shared staff+customer)
- **Global search:** `accounting/views/global_search.py`
- **Notifications:** `accounting/utils/notification_helpers.py`, `accounting/views/notifications.py`
- **Utils:** `accounting/views/utils.py` (is_htmx, htmx_response, toast_only_response)
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
- Bulk delete iterates per-instance for Contact (to run anonymization)

### Authentication & Security
- Custom backend: `EmailOrPhoneBackend` (email → username → phone lookup)
- OTP via `secrets` module, never logged
- Login rate limit: 5/5min (DatabaseCache)
- CSRF token in `<meta name="csrf-token">` — JS reads it for HTMX

### HTMX patterns
- **Modal target:** `#mainModalContent` (staff), `#mainModal` (customer)
- **Toast-only response:** `toast_only_response()` sets `HX-Reswap: none`
- **Redirect after save:** `HX-Redirect` header
- **Notifications polling:** every 30s (NO SSE — SSE was removed earlier)
- **Global progress bar:** `#htmx-progress-bar` (currently commented in base.html)

### Contacts
- `contact_type` can be `customer` | `vendor` | `both`
- Each Contact can be linked to one User (OneToOne, nullable)
- Phone normalized to last 10 digits

### Statement sorting
- Default: **Newest First** (`sort=desc`) — user preference, professional UX
- Toggle available in all statement pages
- **Print/Excel/CSV/WhatsApp always chronological** (`sort=asc`) — compliance

### Ledger / Accounting
- Double-entry ledger via `LedgerEntry` + `LedgerLine`
- `subledger_type`: 'receivable' (customer side) or 'payable' (vendor side)
- `Contact.recalc_balance()` recalculates from ledger lines
- Invoice sync via `sync_invoice_ledger()`, Purchase via `sync_purchase_ledger()`

---

## User Preferences
- Chat in **Hindi**
- Code/UI strings/comments in **English**
- Preserve ALL existing features — only add/fix
- **Do NOT push git commands after every change** — wait until user confirms everything works locally, then ONE complete git command
- **Full file replace** preferred over small patches (reduces user confusion)
- If a file is needed for context, ask user to send it — don't assume

---

## Recent Major Changes (Last Sessions)

### 1. Contact.soft_delete() anonymizes User
- Frees up email/username for re-registration
- Sets `is_active=False`, `set_unusable_password()`
- Idempotent (checks `deleted_user_` prefix)

### 2. cleanup_orphans management command
- Removes orphan Contact.User links
- Cleans up unlinked OTPs

### 3. Status change → context-aware modal (staff)
- `update_repair_status` returns different modal per target status
- Fill-in fields shown based on new_status (e.g., delivery details only for 'delivered')

### 4. Send Estimate to Customer
- Modal with method choice (email / WhatsApp / both)
- Sends in-app notification + email + WhatsApp link
- Estimate print template

### 5. Estimate print + Warranty card templates
- `repairs/estimate_print.html`
- `repairs/warranty_card_print.html` (30-day default warranty)

### 6. Customer estimate review UI
- Customer portal shows estimate breakdown + 3 buttons (Approve / Hold / Reject)
- Estimate status flow tracked

### 7. Email change via OTP (customer)
- Step 1: request new email → OTP sent
- Step 2: verify OTP → email updated, force re-login
- `messages.success()` called **BEFORE** `logout()` (session trick)

### 8. Customer Portal — Purchases (view-only)
- `customer:purchases` — Order list (view only)
- Sidebar label: **"Orders"** (not "Purchases")
- Header: "Orders from A1 Computer Solutions"
- Detail: "Order for {vendor name}", "Issued by A1 Computer Solutions"
- No edit/delete — pure view
- Only shown in sidebar if `has_purchases(user)` — via `purchase_tags.py`

### 9. Statements — Purchase integration
- Purchase rows now clickable → Purchase detail (staff + customer)
- `_build_combined_rows()` includes `purchase_id`, `purchase_no`
- Vendor statement also has clickable purchase links

### 10. Statements — Sort toggle (Newest First default)
- All three statements (combined, customer, vendor) + customer portal statement
- Sort dropdown in filter bar
- Opening balance placement:
  - Newest First → bottom (before totals)
  - Oldest First → top
- Pagination preserves `sort` param
- Print/Excel always chronological

### 11. Customer Portal — Invoice Type badges
- Each invoice now shows Sale / Repair badge
- Backend annotates `is_repair` via `Exists(RepairJob)`
- `_attach_repair_jobs()` prefetches linked repair (attribute `linked_repair` — NOT `_repair_job` — Django templates can't use leading underscore)
- Type filter dropdown on invoice list
- Invoice detail header shows "Repair Invoice" / "Sale Invoice" tag

### 12. Staff Purchases — Professional module
- Print with GST breakup + HSN + signatures + terms
- Detail page with Vendor Quick View, WhatsApp share, Delete
- List table with dropdown (View / Edit / Print / Vendor Statement / WhatsApp / Delete)
- Removed duplicate `_customer_statement_excel`, restored `_customer_invoices_excel`
- `purchase_items.html` — uses `{% url ... as var %}` for safe URL generation (no JS-string url tag)

### 13. Global Search (Ctrl+K) — Staff Portal
- Command palette style dropdown
- Searches across: Invoices, Purchases, Repairs, Payments, Contacts, Products
- **Vanilla JS `fetch`** (NOT HTMX) — for full control and debuggability
- 300ms debounce
- Keyboard nav: ↓↑ Enter Esc
- Ctrl+K to focus
- 5 results per category, grouped
- Backend: `accounting/views/global_search.py` → `templates/search/results.html`
- **Not for customer portal** — user decided it's not needed (correct call: industry standard)

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

### Key URLs (customer namespace)
- `customer:customer_dashboard`
- `customer:customer_invoices` + `customer_invoice_detail` + `customer_invoice_print`
- `customer:customer_purchases` + `customer_purchase_detail` + `customer_purchase_print`
- `customer:customer_repairs` + detail/print/create/update/delete
- `customer:customer_estimate_approve` / `_hold` / `_reject`
- `customer:customer_payments`
- `customer:customer_statement`
- `customer:customer_profile` + `customer_profile_update`
- `customer:customer_password_change`
- `customer:email_change_request` + `email_change_verify`
- `customer:customer_notifications` + dropdown + unread-count

---

## Staff Portal — Key URLs (accounting namespace)

### Dashboard
- `accounting:dashboard` + `dashboard_stats` + `refresh_stats` + `recent_transactions` + `dashboard_export`

### Contacts / Products / Sales / Purchases / Repairs / Payments — standard CRUD
### Statements
- `accounting:customer_statement` + `customer_statement_print` + `customer_statement_excel` + `customer_statement_whatsapp`
- `accounting:vendor_statement` + `_print` + `_csv` + `_excel`
- `accounting:combined_statement` + `_print` + `_excel` + `_whatsapp`

### Global Search
- `accounting:global_search` → `/search/?q=...`

### Health
- `accounting:health_check` → `/health/` (for UptimeRobot ping)

---

## Template Tag Libraries

### `math_filters.py`
- `divide`, `multiply`, `subtract`, `add_filter`, `abs_filter`, `percentage`, `currency`, `sum_line_totals`, `total`, `floatformat`, `positive`, `negative`, `round_number`, `sum_value`

### `notification_tags.py`
- `unread_count(user)` — notification count
- `message_unread_count(user)` — contact message count (added recently)

### `purchase_tags.py`
- `has_purchases(user)` — check if customer/vendor has purchase records (for sidebar visibility)

---

## Known Gotchas

1. **Django templates don't allow leading underscore variables** — `invoice._repair_job` errors. Use `invoice.linked_repair` instead.

2. **`{% url %}` inside `<script>` block** causes `'url' takes at least one argument` error if not properly closed. Use `data-*` attributes and read them in JS, OR use `{% url ... as var %}` in a safe place.

3. **HTMX `hx-post` with forloop.counter0** — safer to use `{% url ... as var %}` inside loop.

4. **Customer-side `_build_combined_rows()`** — same function used by staff and customer. Additions to row dict must not break either side.

5. **Both staff and customer use `is_htmx(request)`** — same detection helper.

6. **Print/Excel** must always use `sort=asc` — even if user has toggled `desc` on screen.

7. **Login rate limit** uses DatabaseCache — if you clear cache, counters reset.

8. **Contact.soft_delete** iterates per-instance — bulk operations may be slow for many contacts.

---

## Current Production Status (as of latest deploy)

### Working
- ✅ Staff dashboard, contacts, products, sales, purchases, repairs, payments
- ✅ Statements (all 3 types) with sort toggle
- ✅ Global search (Ctrl+K) on staff portal
- ✅ Customer portal (dashboard, invoices, orders, repairs, payments, statement, profile)
- ✅ Invoice type badges (Sale / Repair)
- ✅ Purchase detail/print with GST breakup + HSN + signatures
- ✅ Estimate send to customer
- ✅ Email change via OTP
- ✅ Notifications polling (30s)
- ✅ Soft delete with User anonymization
- ✅ Audit log (backend, no UI viewer yet)

### Not yet built (optional future)
- Overdue invoice alerts (dashboard widget)
- Audit log viewer UI (superuser)
- Bulk actions (multi-select on lists)
- Customer portal: "Payment due" prominent banner
- WhatsApp Business API automation
- SMS notifications
- Customer feedback / rating system
- GSTR-1 auto-summary
- Multi-branch support

---

## Environment Variables (Render)

Required:
- `SECRET_KEY`
- `DEBUG` (False in production)
- `DATABASE_URL` (Supabase Postgres)
- `CLOUDINARY_URL` (or CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET)
- `ALLOWED_HOSTS`
- `EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` (for OTP email)

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
- Ask for specific files if needed (don't assume)
- Give full file replaces (not patches) for templates
- Wait for local verification before git commands
- Give ONE complete git command at the end

---

## Git Workflow (User Preference)

**User wants only ONE git command block, and only after everything works locally.**

Standard git command format:
```bash
git add -A
git commit -m "<meaningful message>"
git push origin main