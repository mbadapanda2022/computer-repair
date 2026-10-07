# AUDIT.md — A1 Computer Solutions (Phase 0)

**Project:** `src/computer_repair/` · **App:** `accounting` · **Date:** Phase-0 audit
**Scope:** full project · **Rule:** kuch bhi delete/modify nahi — pehle sirf padha gaya

> ⚠️ **Verification caveat:** is session me shell (pwsh) `[exit code: 3221225794]`
> de raha hai (Windows DLL init failure). Isliye maine **koi command nahi chalayi** —
> neeche ki har baat **source code padh kar** likhi gayi hai, aur har claim ke saath
> file:line diya gaya hai taaki aap khud verify kar sakein.

---

## 1. Stack (verified from source)

| Cheez | Value | Source |
|---|---|---|
| Django | **5.1.5** | `requirements.txt:15` |
| Python | 3.11+ (typing_extensions 4.15, ast.unparse use) | `requirements.txt` |
| Project module | `computer_repair` | `manage.py` |
| Apps | `accounting` (business) | `settings.py:41` |
| DB | `DATABASE_URL` (Postgres/Supabase) warna SQLite | `settings.py:117-145` |
| Auth | allauth 65.x + custom `EmailOrPhoneBackend` | `settings.py:60-64` |
| HTMX | `django-htmx` 1.27 + `django_htmx.middleware.HtmxMiddleware` | `settings.py:37,56` |
| Async/queue | `django-q` 1.3.9 | `requirements.txt:26` |
| Storage | Cloudinary (prod) / filesystem (dev) | `settings.py:185-205` |
| Static | WhiteNoise + Bootstrap Icons **local** (`static/bootstrap-icons/`) | `settings.py:191`, glob |
| Styles/JS | Bootstrap 5.3 classes use ho rahe hain (btn/form-control/form-select) | templates |
| Cache | **DatabaseCache** (`django_cache_table`) | `settings.py:80-90` |
| Timezone | `Asia/Kolkata`, `USE_TZ=True` | `settings.py:155-158` |
| Deploy target | Render (`a1computersolutions.onrender.com`) — **abhi live, free tier** | `settings.py:297` |
| Middleware | RBAC `AccessControlMiddleware` (audit thread-local bhi) | `middleware.py` |
| URL namespaces | `accounting:`, `customer:`, root `home` | `urls.py` |

**Project size (files scanned, venv/.kilo excluded):**
- Templates: **231**
- Static: **2,079** files (mostly Bootstrap Icons ke SVG — asli code nahi)
- Migrations: **34** (`0001` … `0034`)
- `accounting/models.py`: **3,492 lines**
- `accounting/views/repairs.py`: **2,465+ lines, ~30 views**
- 30+ view modules, 17 templatetags files

---

## 2. 🔴 Critical Findings (evidence ke saath)

### C-1. Repair status workflow **enforce hi nahi hota** (state machine nahi hai)

| Evidence | Detail |
|---|---|
| `models.py:2057-2065` | `STATUS_CHOICES` — **sirf choices hain, koi allowed-transition table nahi** |
| grep | `VALID_TRANSITIONS` → **0 hits** poore project me |
| `models.py:2225-2338` | `RepairJob.save()` me status ke liye **koi guard nahi** — jo bhi status set ho, save ho jata hai |
| `models.py:2277` | Sirf itna hai: `if old_status != self.status:` → dates stamp + notification (validation nahi) |
| `views/repairs.py:857` | `update_repair_status(request, pk)` — **160-line view** (line 857-1016) me validation hai, model me nahi |

**Iska matlab:** status ka asli guard **view layer** me hai. Admin, shell, django-q task,
management command, ya koi bhi script status ko **directly** badal sakta hai —
`pending → delivered` bhi. Aur `ready → cancelled`, `delivered → repairing` jaisi
transitions ka koi rule hi define nahi hai.

**Aur kya missing hai:** kisne, kab, kyun status badla — iska **koi record nahi**
(notification to jata hai, par history row nahi banti).

---

### C-2. Soft-delete restore **toota hua** hai (stock wapas nahi aata)

| Evidence | Detail |
|---|---|
| `views/repairs.py:1728-1730` | `repair_delete` view → `RepairJob.delete()` |
| `models.py:2341-2358` | `RepairJob.delete()` — parts/services ko `delete()` karta hai (stock reverse) ✅ |
| `models.py:63-82` | `SoftDeleteQuerySet` me `'RepairJob'` listed hai → **instance delete** hota hai ✅ |
| **`models.py:2056-2360`** | `RepairJob` me **`restore()` override NAHI hai** ❌ |
| `models.py:99-105` | `SoftDeleteQuerySet.restore()` = `qs.update(is_deleted=False, ...)` → **bulk update** |
| `models.py:149-154` | `SoftDeleteModel.restore()` = `save(update_fields=[...])` |

**To kya hota hai:**
1. Job delete → parts bhi soft-delete + **stock reverse** ✅
2. Job restore (`settings/restore.html` + queryset restore) → sirf job ka `is_deleted=False` hota hai
3. Parts/services **deleted hi rehte hain** (`is_deleted=True`) — aur `SoftDeleteQuerySet.restore()` bulk `update()` hai, isliye `RepairPart.save()` ka stock re-apply code **kabhi chalta hi nahi**
4. Job ka `final_amount` purana pada rehta hai → **display par galat amount**

**Impact:** restore kiya job khaali dikhta hai (parts/services gayab), stock kam pada rehta hai,
aur amount purana. Ye wahi `StockMovement` restore bug hai jo aapke `models_new.py` me fix tha
(line 733-737) — **yahan wapas aa gaya hai**.

---

### C-3. Invoice ban jaane ke baad bhi parts/services badalte hain → **do numbers**

| Evidence | Detail |
|---|---|
| `models.py:2212-2213` | `can_be_invoiced` = `not self.invoice and status in ('ready','delivered')` |
| `models.py:2215-2223` | `calculate_final_amount()` → sirf `final_amount` update; **invoice ko touch nahi karta** |
| `views/repairs.py:1020` | `add_repair_part()` — invoiced job par bhi naya part add ho sakta hai |
| `views/repairs.py:1102` | `remove_repair_part()` — same |
| `views/repairs.py:1175 / 1243` | `add_repair_service()` / `remove_repair_service()` — same |
| `models.py:2181-2204` | `invoiced_amount` (invoice.grand_total) vs `final_amount` — **do alag source** |

**To kya hota hai:**
1. Job `ready` → invoice bana (`grand_total = ₹4,500`), `final_amount = ₹4,500` (sync)
2. Staff job me SSD aur add karta hai (`+₹4,500`) → `final_amount = ₹9,000`
3. Invoice **`₹4,500` hi rehta hai** (koi resync nahi)
4. `display_amount` → invoice hai isliye `₹4,500` dikhata hai
5. Par stock **₹4,500 ka extra kat chuka hai** → **inventory + books me farq**

Ye classic "silent divergence" hai — isi wajah se books aur physical stock match nahi karte.

---

## 3. 🟠 Medium Findings

| # | Finding | Evidence |
|---|---|---|
| M-1 | **`delivered_at` DateTimeField hai, `ready_at`/`received_at` DateField** — mix types, timezone/print comparisons me risk | `models.py:2124-2130` |
| M-2 | **Legacy `labour_charge` field** amount me judta hai (`base_amount` me) + naya `RepairService` model bhi hai → **do jagah labour** | `models.py:2115, 2175-2178` |
| M-3 | **4 date fields ek hi cheez ke liye**: `date_in`, `received_at`, `delivered_at`, `delivery_date` — har save par `date_in` overwrite hota hai | `models.py:2122-2130, 2235-2238` |
| M-4 | `RepairService.product` par `limit_choices_to={'is_service': True}` hai (form model-level ok) par `RepairPart.product` par **koi `limit_choices_to` nahi** → service product part ban sakta hai, aur `create_invoice` jaise flows me chup-chaap skip ho jata hai | `models.py:2363, 2425` |
| M-5 | `models.py:2295-2306` — `save()` ke andar **do extra aggregate queries** har save par (parts + services sums) — chhota performance cost | `models.py:2296-2306` |
| M-6 | `SoftDeleteQuerySet.restore()` bulk update — **ledger/stock/balance recalc signals bypass** (sirf RepairJob nahi, sab models) | `models.py:99-105` |
| M-7 | Backup copies workspace me maujood hain jo galat scan/build ho sakti hain: `.kilo/worktrees/pickled-eucalyptus/`, `.kilo/worktrees/marshy-twilight/`, `venv/` | glob results |
| M-8 | `status` field ka max_length 15 hai par `STATUS_CHOICES` ke saare values isi me fit hain — koi issue nahi, sirf note | `models.py:2087` |

---

## 4. ✅ Jo pehle se achha hai (isme haath nahi lagayenge)

| Feature | Evidence |
|---|---|
| Race-safe job numbering (retry loop + `InvoiceCounter.select_for_update`) | `models.py:2243-2265`, `294-317` |
| Soft-delete manager + `all_objects` pattern | `models.py:107-136` |
| `instance_delete_models` guard (signals ke liye per-instance delete) | `models.py:63-82` |
| Rubust RBAC middleware (HTMX 401 + `HX-Redirect`) | `middleware.py:104-124` |
| Audit thread-local (`set_current_request`/`clear_current_request`) | `middleware.py:28,62-67` |
| `send_notification_to_contact` fail-safe hai (try/except + log) | `notification_helpers.py:210-233` |
| Repair views me `@login_required` + `@csrf_protect` + `@handle_errors` discipline | `views/repairs.py` (grep: 30+ views) |
| SSE deliberately no-op (Render free tier multi-worker) — soch samajh kar liya decision | `notification_helpers.py:240-256` |
| DatabaseCache (multi-worker safe) + `createcachetable` documented | `settings.py:73-90` |
| HTMX-aware error templates (400/403/404/500 + `_htmx` variants) | `templates/` glob |
| Per-field error partials (`templates/repairs/partials/field_errors.html`) | glob |
| Number-to-words/print views, warranty card, estimate print — business depth | `views/repairs.py:2361, 2407` |

---

## 5. 🗑 Cleanup candidates — **APPROVAL CHAHIYE (abhi kuch delete nahi kiya)**

Aapki choice ke hisaab se: **"sab delete + fresh DB"**. To ye list approve karni hai:

### 5.1 Delete (safe, regenerate ho jayega)

| Target | Count | Kyun safe |
|---|---|---|
| `accounting/migrations/*.py` (0001-0034) | 34 files | Fresh `makemigrations` se naya 0001 banega |
| `accounting/migrations/__pycache__/` | dirs | build artifact |
| `**/__pycache__/` | sab | build artifact |
| `db.sqlite3` | 1 (agar hai) | aapne kaha koi real data nahi |
| `*.pyc` | sab | build artifact |

### 5.2 Delete — **par mere se confirm karwa lein**

| Target | Risk |
|---|---|
| `.kilo/worktrees/pickled-eucalyptus/` | duplicate project copy — kya ye aapki backup hai? Agar haan to **rakhein** |
| `.kilo/worktrees/marshy-twilight/` | same |
| `venv/` | hata dena chahiye (Render apna banata hai) — par local dev ke liye chahiye to rakhein |
| `staticfiles/` | `collectstatic` se wapas ban jayega |
| `media/` | user-uploaded files — **DANGER: asli data ho sakta hai** |

### 5.3 🚫 DELETE **NAHI** karenge (bilkul zaroori)

- `templates/` (231 files — aapki poori UI)
- `static/bootstrap-icons/` + aapki CSS/JS
- `accounting/management/commands/*` (14 commands — business logic)
- `accounting/utils/`, `templatetags/`, `signals.py`, `audit.py`, `decorators.py`, `tasks.py`
- Koi bhi `.py` source file
- `.env` (aapne skip kaha — main bhi touch nahi karunga)

---

## 6. 📋 Repair Feature Inventory (kuch bhi missing na ho — ye manifest hai)

Aapke repair module me **ye sab features maujood hain** aur upgrade ke baad **sab intact rahenge**:

**Job lifecycle:** job_number auto (`REP-0001`) · status 7-stage · estimate flow (pending/approved/on_hold/rejected) · approval source 4 type · approval remarks · estimated_cost · final_amount · legacy labour_charge
**Device:** device_model · serial_number · issue_description · accessories · device_condition · action_taken · diagnosis_report
**Dates:** date_in · submitted_at · received_at · received_remarks · ready_at · delivered_at · delivery_date
**Delivery:** delivered_to_name · delivered_to_phone (20 char) · delivered_to_designation · delivery_remarks · delivered_by · received_by
**Parts/Services:** RepairPart (quantity, unit_price, line_total, auto stock `repair_out`) · RepairService (flat amount) · auto `calculate_final_amount`
**Invoice:** OneToOne invoice · `can_be_invoiced` · `invoiced_amount` · `display_amount` · invoice session helpers (`_ensure_repair_invoice_session`, `_session_item_from_*`)
**Customer portal:** repair create/list/detail/update/delete · estimate approve/hold/reject · print · email OTP flows
**Staff side:** list + pagination + aging · status pipeline · activity timeline · warranty compute/card print · estimate send/print · quick_update · create-for-contact · **Excel export** · product search for invoice · invoice item add/remove
**Notifications:** status change par customer ko (`category='repairs'`)
**Other:** `_safe_notify` · `validate_repair_field` (HTMX live validation) · `_modal_error_response`

👉 **Ye poora manifest upgrade ke baad `PRESERVATION.md` me line-by-line verify hoga.**

---

## 7. 🎯 Upgrade Plan (phases) — aapke "poora project" scope ke hisaab se

| Phase | Kaam | Deliverable | Aapko kya karna hoga |
|---|---|---|---|
| **0** | Audit (yahi file) + full scan | `AUDIT.md`, `audit_scan.py` | `python audit_scan.py --out AUDIT_SCAN.md` chala kar bhejein |
| **1** | 🔴 C-1: Repair state machine — model-layer guards, `change_status()`, immutable `RepairStatusLog`, force + audit | `models_repair_pro.py` (project ke hisaab se adapt) | review |
| **2** | 🔴 C-2: soft-delete restore theek — per-instance restore + stock re-apply + amount recalc | models patch | review |
| **3** | 🔴 C-3: invoiced-job lock + `resync_invoice()` + drift report | models patch | review |
| **4** | Migrations clean-slate + fresh DB | nayi `0001`…, `db.sqlite3` | ✅ approval (5.1) |
| **5** | Views/URLs/HTMX integration — status buttons, timeline, inline errors | `views/repairs.py` patch + partials | review |
| **6** | Medium findings (M-1…M-6) fix | patch | review |
| **7** | Admin hardening + tests + docs | `admin.py`, `tests/`, README | `python manage.py test` |

**Har phase ke baad:** diff summary + `python manage.py check` / `test` output aapse.

---

## 8. ❓ Aapke 3 faisle (in par main aage badhunga)

1. **Delete approval** — §5.1 ki list (migrations 34 + `__pycache__` + `db.sqlite3`) — **haan/nahi?**
2. **§5.2** — `.kilo/worktrees/*` backups hain ya duplicate? `venv/`, `staticfiles/`, `media/` — inme kya rakhna hai?
3. **Shell** — abhi main commands chala nahi sakta. Aap `python audit_scan.py --out AUDIT_SCAN.md` chala kar bhej dein, to audit 100% complete ho jayega (line-by-line views/forms/templates ka hisaab). Warna main source padh kar hi aage badhunga — bas verification aapke machine par hogi.

---

**Agla step (jab aap "haan" bolein):** Phase 1 — Repair state machine, aapke **asli**
`models.py` ke hisaab se (maine dekha hai ki yahan `STATUS_CHOICES`, `InvoiceCounter`,
`delivered_at` DateTimeField, aur legacy `labour_charge` hai — isliye jo module pehle banaya
tha, usko is structure ke hisaab se **dobara adapt** karna padega, blindly copy nahi karna).

---

## 9. 🔴 C-4 (naya, Phase 1 me mila) — Status seedha assign karne ki jagahें

Phase 1 implement karte waqt poore project me `status` likhne wale **saare** call sites
scan kiye. Ye 6 jagahें naye state machine se **takrayengi** (guard raise karega) — inhe
Phase 5 me `change_status()` par shift karna **compulsory** hai, warna ye flows toot jayenge:

| # | Jagah | Kya karta hai | Kya karna padega |
|---|---|---|---|
| 1 | `views/repairs.py:658-659` | Naya job banate waqt `pending → received` (staff-created) | **Safe hai** — pk `None` hai isliye guard skip. (Note: naye job ko `received` se `create()` karna zyada saaf hoga) |
| 2 | `views/repairs.py:980` | `update_repair_status()` — status seedha assign (160-line view) | `job.change_status(new_status, by=request.user, remarks=...)` |
| 3 | `views/repairs.py:1820` | `repair_create_for_contact()` — `pending → received` (naya job) | **Safe** (naya record) |
| 4 | `views/repairs.py:1896` | `staff_approve_estimate()` — `repair.status = 'repairing'` | `repair.start_repair(by=request.user, ...)` |
| 5 | `views/customer_views.py:711` | `job.status = 'pending'` (customer submit) | **Safe** (naya record) — par explicitly `status='pending'` se create karna behtar |
| 6 | `views/customer_views.py:898` | `repair.status = 'repairing'` (customer edit) | `repair.start_repair(by=request.user, ...)` |

### 9.1 Aur ek badi baat: `update_repair_status()` ke side-effects

Wo view sirf status set nahi karta — **cancel par parts/services delete bhi karta hai**
(`views/repairs.py:975-978`, stock reversal ke liye). Agar hum sirf guard lagayein to:

- status assign → guard raise → **500 error**
- aur cancel-reversal sirf usi view me hai (admin/shell se cancel karne par stock nahi reverse hota)

**Fix (Phase 1 me ho gaya):** cancel-reversal ab **model** me hai —
`RepairJob._on_status_change()` me `new_status == 'cancelled'` par parts/services delete hote
hain. Isse admin/shell/script/API — sab jagah stock sahi reverse hoga.

Config (agar aap cancelled job ke parts **record** rakhna chahein):
```python
# settings.py
REPAIR_REVERSE_LINES_ON_CANCEL = False   # default: True
```

> ⚠️ Ye ek business decision hai — default `True` rakha kyunki aapka **mojooda behaviour**
> yahi hai (view parts delete karta hai). Agar aap chahein ki cancel par parts job par
> dikhein (audit ke liye) to `False` kar dein — bas tab stock manually adjust karna padega.

---

## 10. 📦 Phase 1 Deliverables (ban chuke — review ke liye)

| File | Kya hai |
|---|---|
| `accounting/repair_workflow.py` | **Status state machine** (aapke asli structure par adapted): `StatusMachineMixin`, `Transition`, `InvalidStatusTransition`, `RepairStatusLog`, aur `RepairJob` (inherit — saare purane fields/methods intact) |
| `patch_notification_guard.py` | Aapke `models.py` me **1 chhota guard** lagane wala safe script (backup + syntax-verify + idempotent + `--revert`) |
| `audit_scan.py` | Poora project inventory scanner (aapke machine par chalayein) |
| `AUDIT.md` | Yahi file |

### Phase 1 kaise install karein (4 commands)

```bash
# 1. models.py ke aakhir me 1 line add karein:
#      from .repair_workflow import *          # noqa: F401,E402

# 2. Notification double-fire guard lagayein (backup ke saath):
python patch_notification_guard.py

# 3. Migrations + migrate
python manage.py makemigrations accounting
python manage.py migrate

# 4. Verify
python validate_syntax.py src/computer_repair/accounting/models.py src/computer_repair/accounting/repair_workflow.py
python manage.py check
```

**Uske baad (Phase 5 tak) ye yaad rakhein:** admin me status dropdown se change karna,
aur §9 ki 6 jagahें — inhe `change_status()` par shift karne tak status change **error**
dega. Ye jaan-boojh kar hai (warna guard bypass ho jata) — Phase 5 me main ye saare call
sites update kar dunga.

