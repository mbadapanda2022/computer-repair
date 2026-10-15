# A1 Computer Solutions — Complete User Guide (Staff + Customer + Public Website)

**Version:** 2026-10-15 · **Site:** https://a1computersolutions.in

Ye document poore website ka reference guide hai — **koi bhi cheez miss nahi**. Do bade hisse:
- **PART A — Staff Guide** (dukaan ka kaam: hisab, marammat, bill, report)
- **PART B — Customer Portal Guide** (grahak apna kaam khud dekhe)
- **PART C — Public Website** (landing page, blog, tracking)

Screen par jo English label dikhta hai wahi bracket `()` me diya hai, taaki aapko turant mil jaye.

> **Login kaise karein:** `/login/` par **email, phone, ya username** — koi bhi daal sakte ho. Staff login ke baad **Staff Dashboard** par jate hain; customer **Customer Portal** par. Rate-limit: 5 galat attempts ke baad 5 minute rukna padta hai.

---

# PART A — STAFF GUIDE (Dukaan / Accounting)

## A1. Dashboard (`/dashboard/`)

Login ke baad pehla page. **Har stat card click karne par usi cheez ki filtered list khulti hai.**

**Main cards:**
| Card | Kya dikhta hai | Click par |
|---|---|---|
| Today's Sales | Aaj ke invoices ka total | Aaj ke bills |
| This Month Sales | Is mahine ki sales | Month bills |
| Pending Repairs | Chalu (open) marammat ke kaam | Open repairs |
| Low Stock | Kam stock wale products | Low-stock list |
| Total Customers | Customer contacts | Contacts |
| Total Vendors | Vendor contacts | Vendors |
| Total Products | Saare products | Products |
| Pending / Unpaid Invoices | Baaki bills (Unpaid + Partial) | Outstanding bills |
| Payments Received Today | Aaj mile payments | Payments |
| Total Due | Kul bakaia | Due invoices |
| Overdue | Time se zyada purane bakaia + amount | Overdue list |
| Cash in Hand | Abhi box me kitna cash | Cash Book |
| Bank Balance | Saare bank accounts ka total | Bank Accounts |
| Unread Notifications | Naye notifications | Notifications |

**Neeche ke sections:** Recent invoices, recent repairs, recent payments, aur top-10 low-stock table.

**Charts (HTMX refresh + `/api/dashboard/stats/`):** pichhle 7 din ki sales/purchases/repairs line, top-5 products, payment-method distribution, repair-status distribution.

> **Pending Repairs** me ab ye sab statuses aate hain: `pending, received, diagnosis, repairing, ready` (yani device abhi dukaan par / kaam chalu). `delivered` aur `cancelled` nahi aate.

---

## A2. Global Search (navbar search box, `/search/`)

Ek box se **6 cheezein ek saath** khojte hain (kam se kam 2 character):
- **Invoices** — invoice number / customer
- **Purchases** — purchase number / vendor
- **Repairs** — job number / device model / serial / customer
- **Payments** — reference / UPI ref / contact
- **Contacts** — name / company / phone / email / GSTIN
- **Products** — name / HSN

Sirf dikhata hai, kuch badalta nahi.

---

## A3. Contacts (`/contacts/`)

Menu → **Contacts**. Yahan **Customer**, **Vendor**, dono ek hi jagah.

- **Add Contact** (`/contacts/add/`): Name, Phone, Address, aur **Contact Type** = `Customer` / `Vendor` / `Both` (ek hi insaan dono ho to Both).
- Phone **unique** hona chahiye.
- Contact detail me uske saare **invoices, payments, running balance aur Advance Balance** dikhte hain.
- **Actions:** Edit, Delete (soft-delete), **Detail modal**, phone/name se **Search**, **Export Excel**.

> Rule: pehle contact banao, phir bill/payment me usse select karo.

---

## A4. Products & Categories

Menu → **Products** (`/products/`), Categories (`/products/categories/`).

**Add Product** (`/products/create/`): Name, Category, **Purchase Price** (kharid mulya), **Selling Price**, GST/HSN, **Current Stock**, **Low Stock Threshold**. Service (jaise Labour) ke liye **Is Service** tick karo — service ka stock 0 rehta hai.

- **Category** alag se banao ya product form me **inline add** kar sakte ho.
- Product detail modal, **Stock History**, **price lookup**, **quick-add**, **search**, **Export Excel**, **Print list**.
- **Selling price suggest:** product ka apna selling price; na ho to purchase price + 20%.

---

## A5. Stock (`/stock/dashboard/`, `/stock/adjustment/`)

- **Stock Dashboard:** low-stock highlight + kul quantities.
- **Stock Adjustments** (`/stock/adjustment/add/`): manual stock sudhar. **Signed quantity** — positive = maal badha (ginti me zyada), negative = ghataya (kharab/chori/tuta). Quantity 0 nahi ho sakti. Delete karne par us product ka stock utna hi **reverse** ho jata hai (poori history dobara re-sum nahi hoti).
- **Export Excel:** current stock dump.

> Naya maal **vendor se kharidne par Purchase banao** (A6) — usse stock apne aap badh jata hai aur ledger bhi banta hai. Sirf **sudhar/ghate** ke liye Adjustment use karo.

---

## A6. Purchases / Kharid (`/purchases/`)

Menu → **Purchases** → **Add Purchase**.

- Vendor chuno, item lines (quantity + price) jodo. GST/Hsn bhi.
- Save par **stock apne aap badhta hai** aur **Purchase In** ledger entry banti hai.
- Yeh **Direct Expense** hai → Profit & Loss ki **Trading Account** me gina jata hai.
- **Actions:** list, print, **Export Excel**, detail, edit, delete, add/remove item, product search + quick-add.

---

## A7. Sales / Invoices (`/sales/`)

Menu → **Sales**. Sab bills yahan.

**Create Invoice** (`/sales/create/`): customer chuno → item lines jodo (qty > 0, price ≥ 0, tax 0–100%) → date/due date/GST type/discount → save. Ledger + customer balance apne aap update.

**Invoice detail** (`/sales/<pk>/`) par:
- Totals: Grand total, Paid, **Balance Due**.
- Status: `Unpaid` / `Partial` / `Paid` / `Overdue`. **Pending Invoices = Unpaid + Partial.**
- **Apply ₹__ / Un-apply:** agar customer ka **Advance Balance** pool me pada hai to bill par apply karo (Balance Due ghat jata hai); galti ho to un-apply. (Repair se bill bante waqt ye apne aap ho jata hai.)
- **Add Payment:** usi bill par balance payment.
- **Credit Note**, **Print**, **WhatsApp**, **Duplicate**, **Edit**, **Delete**.

### Credit Notes (`/credit-notes/`, aur invoice se **Credit Note**)
Pehle se issued invoice ko **kam** karne ka official document (return, rate difference, extra quantity, post-sale discount, order cancel, GST sudhar).
- Kaunsi lines aur kitni quantity (qty > 0 aur us line ki invoiced qty se zyada nahi).
- Reason (default: sales return), refund method (`Cash/Bank/Credit Note/None`), **Is Stock Return** (default on — non-service wapas inventory me aata hai).
- Number `CN-####`. Ledger: **Dr Sales Returns (4020) + Dr GST Payable (2010) / Cr Customer Receivable (1011)** — invoice ka effective bakaia ghat jata hai.
- **Print / WhatsApp / Detail / Delete** (delete par uski ledger + lines hatati hain).

> List page: `/credit-notes/`.

---

## A8. Repairs — Marammat (`/repairs/`)  ⭐ (sabse important)

Menu → **Repairs**.

### A8.1 Intake — nayi marammat
**Add Repair** (`/repairs/create/`): Customer (ya phone), Device model, Serial, Issue, Accessories, Device condition. Agar customer ne **advance** diya hai to alag se Payment me `Advance` tick karke darj karo (A9).
- Contact ke page se bhi seedha repair ban sakti hai (`create-for-contact`).

### A8.2 Status flow — kaam ki sthiti
Cr: `Pending → Received → Diagnosis → Repairing → Ready → Delivered`, aur kahin se bhi **Cancelled**.
Status badalne ke liye repair detail par **status buttons / Update Status** use karo (ye state machine se chalta hai — guards + history + notification apne aap).

**Guards (kaun chalta hai, kaun nahi):**
- **Ready** karne se pehle **kam se kam 1 part ya service line** chahiye (warna customer ko zero bill jayega — block).
- **Delivered** karne ke liye **receiver ka naam ya phone** chahiye (naam/phone se kaam chalta hai, dono zaroori nahi).
- **Invoice ban chuka job** status me aage nahi badh sakta (sirf `ready → delivered` allowed).
- **Cancelled** status ke alawa device sirf delivered se wapas (re-open) force se hi hota hai.
- **Cancelled → Delivered** allowed hai (device dukaan se customer ko wapas de diya).
- **Rejected estimate** par `repairing`/`ready` **block** — pehle approve (re-approve) karo ya cancel+charge.

### A8.3 Parts & Services
Repair detail me **Add Part** (`add-repair_part`) aur **Add Service** (`add-repair_service`).
- **Part add karte hi stock kat jata hai** (availability check job ke committed qty bhi ginta hai). Remove par stock wapas.
- **Service** = flat labour amount, stock par asar nahi.
- Invoice banne ke baad parts/services **add/remove block** (jab tak invoice delete na ho).

### A8.4 Estimate (anumanit keemat) — Approve / Hold / Reject / Re-approve ⭐
Jab aap **Estimated Cost** daalte ho, job par **Estimate decision** buttons dikhte hain (approve ke liye pehle estimated_cost hona chahiye). Status: `pending / approved / on_hold / rejected`.

| Button | Kya karta hai |
|---|---|
| **Approve Estimate** (`staff-approve/`) | Customer ne haan kaha — estimate **approved**, source + remarks save. Agar job `pending/diagnosis` me hai to apne aap **repairing** me chala jata hai. |
| **Hold** (`estimate/hold/`) | Customer soch raha hai — estimate **on_hold**. Customer ko notification jata hai. |
| **Reject** (`estimate/reject/`) | Customer mana kiya — estimate **rejected**. Sath me (optional) **job cancel + charge** bhi kar sakte ho (neeche A8.6). |
| **Re-approve** | Hold/rejected estimate ko baad me dobara **Approve** kar sakte ho (customer man gaya). Approve par hold/reject **band** ho jate hain (approved ko undo nahi kar sakte). |

**Source** field: Staff (Phone / In-Person / WhatsApp) ya Customer Portal — record hota hai ki approval kaise mila.

### A8.5 Send Estimate & Quick Update
- **Send Estimate** (`send-estimate/`): method chuno (`email` / `whatsapp` / `both`) + optional message. Hamesha **in-app Notification** banta hai; email (agar customer ka email hai); WhatsApp = `wa.me/91<phone>` deep-link. Cancelled job ya bina estimated_cost par nahi chalta.
- **Quick Update** (`quick-update/`): sirf `estimated_cost, diagnosis_report, action_taken, received_by, received_remarks, ready_at` (ready_at sirf received/diagnosis/repairing me). Delivered/cancelled par block.
- **Prints:** Repair slip (`print/`), **Estimate** (`estimate/print/`), **Warranty Card** (`warranty-card/` — sirf `delivered` par; expiry = delivery + 30 din).

### A8.6 Cancel / Reject with partial charge ⭐ (naya)
Kabhi kaam adhura hi ruk jata hai (customer mana kiya, repair economical nahi, duplicate entry) lekin **diagnosis/service ka kuch charge** lena hota hai.

**Update Status → Cancel Job** (`status/`) me:
- **Cancellation Reason** — **zaroori** (khaali ho to cancel block). Ye **customer ko tracking page par dikhta hai**.
- **Charge to customer (₹)** — optional (0 = koi charge nahi).
- Cancel par: job **cancelled**, lage parts/services **delete** + **stock wapas**, `cancelled_at` stamp, customer ko ek baar notification.
- Agar **charge > 0** → ek chhota **cancellation invoice** apne aap banta hai line: *"Diagnosis / Service Charge (Cancelled Repair)"*, aur customer ka **advance** us par **auto-adjust** ho jata hai (bilkul normal bill ki tarah). Balance Due dikhta hai.

**Reject-estimate se cancel:** `estimate/reject/` kholte waqt **"job bhi cancel karein"** checkbox + reason + charge — ek hi step me reject + cancel + charge invoice.

> Kisi bhi **open** status se cancel ho sakta hai (pending/received/diagnosis/repairing/ready). **Delivered** job cancel nahi hoti. Jiska normal **invoice already bana hai** use cancel karne par charge invoice nahi banta (pehle invoice delete karo).

### A8.7 Create Invoice from Repair (`create-invoice/`)
Parts/services add karne ke baad **Create Invoice** button (sirf `ready`/`delivered` par, aur estimated_cost ho to estimate **approved** hona chahiye). Ye job ki parts+services se lines bhar deta hai; aap date/due/GST/discount chun sakte ho, extra manual lines add/remove kar sakte ho; advance auto-apply; ledger post.

---

## A9. Payments (`/payments/`)

Menu → **Payments** → **Add Payment**. Ek hi screen se teen kaam:

| Mode | Kaise |
|---|---|
| **Payment Received** (customer se) | Direction = `Received`, customer chuno, amount, method. Advance tick **na** karo. Bill se allocate karo → Balance Due ghat jata hai. |
| **Advance** (pehle ka payment) | Direction = `Received` + **Advance tick**. Ye pool me jama hota hai, baad me bill par auto/manual apply. |
| **Expense Paid** ( kiraaya/salary/bijli) | Direction = `Paid to Supplier`, contact (vendor), amount, method (Cash/UPI/Bank), aur **Expense Account** dropdown se sahi kharcha chuno (Rent / Salaries / Electricity…). Seedha **Dr Expense / Cr Cash|Bank** banta hai — koi bill/invoice nahi. |

**Guardrails:** amount > 0; Bank/UPI par bank account chahiye; Cash par clear; `expense_account` sirf **Paid**, non-advance, no-discount par.

**Allocation:** ek ya kai invoices par baanto (har invoice par uske Balance Due se zyada nahi). **Advance aur Expense payments auto-allocate nahi hote.**

- **Reconcile** (`reconcile/`): sirf **haan/na flag** — payment bank statement se "match ho gaya" mark karna. Ledger/allocation dobara nahi banta.
- **Load unpaid invoices**: Received direction me customer/vendor ke khule bills laane ke liye (allocate karne ke liye).

> **3 mahine ka kiraaya ek saath (₹4,500):** ek Payment, amount 4500, Expense Account = Rent. Dhyan: poora 4500 usi mahine ki P&L me girega jiski **Date** dali. Teeno mahino me baantna ho to **₹1,500 × 3** alag entries, har entry ki date us mahine ki.

---

## A10. Cash & Bank

### Cash Book (`/cash-book/`)
Dukaan ka roz ka register — **Cash account (1001)** ki har ledger line date-wise, **running balance**, aaj ka **cash-in-hand**. Default current month-to-date. Contra rows "Contra (Cash ⇄ Bank)" label ke saath, in/out/closing totals.

### Contra (Cash ⇄ Bank) (`/cash-book/contra/`)
- **Deposit** = cash → bank, **Withdrawal** = bank → cash. Bank account + amount (> 0.01) + date + note.
- Guard: deposit > cash-in-hand to reject; withdrawal > bank balance to reject. Bank transaction `transfer` source se banta hai.

### Bank (`/bank/accounts/`, `/bank/transactions/`)
- **Accounts:** opening balance (current me judta hai), edit (recalculate), delete sirf tab jab koi transaction na ho.
- **Bank Statement** (`/bank/statement/<pk>/`): deposits/withdrawals, running balance, carried-forward opening, **Print/Excel**.
- **Transactions** (`/bank/transactions/`): manual add/edit/delete. **Withdrawal > balance to block** ("Insufficient balance"). Source type rules: deposit ∈ {payment received, transfer, interest, manual}; withdrawal ∈ {payment made, transfer, bank charge, manual}. Transfer-linked row edit/delete se related payment ka reconciled flag + balance sync.

---

## A11. Statements

Menu → Statements. Teeno ke **Print / Excel / WhatsApp** variants.
- **Customer Statement** (`/statements/customer/<id>/`): us customer ka receivable ledger — invoices, payments, running balance, opening/closing, type filter + search.
- **Vendor Statement** (`/statements/vendor/<id>/`): mirror payable side (+ **CSV/Excel**).
- **Combined Statement** (`/statements/combined/<id>/`): receivable + payable dono, **net figure** (receivable − payable), bank/detailed mode, sort.

---

## A12. Journal (`/journals/`)
Accountant ke liye **manual double-entry** (adjustment, sudhar). Roz ka kiraaya/salary/bijli **yahan se NAHI** — woh Payments (Paid + Expense Account, A9) se. Aam bikri/kharid/payment bhi apne modules se hi.

---

## A13. Reports (Menu → **Reports**)

Sab reports par **date range**, **`?excel=1` export** aur **`?print=1` print**.

| Report | Kya deta hai |
|---|---|
| **Sales** | Period ki invoiced sales |
| **Purchases** | Period ki kharid |
| **GST** (`/reports/gst/`) | CGST/SGST/IGST split, input credit, net payable (regular/interstate/intrastate) |
| **Stock** (`/reports/stock/`) | On-hand qty + valuation (qty × purchase cost), low-stock flag, category filter |
| **Aging** (`/reports/aging/`) | Bakaia buckets 0–30/31–60/61–90/90+ (customer ya vendor) |
| **Trial Balance** (`/reports/trial-balance/`) | Sab accounts Dr/Cr as-on-date |
| **Balance Sheet** (`/reports/balance-sheet/`) | Cash/bank/receivable/inventory vs payable/equity/net profit + check figure |
| **Profit & Loss** (`/reports/profit-loss/`) | Neeche A14 |

---

## A14. Profit & Loss (`/reports/profit-loss/`)
Do hisse (Tally jaisa), upar date range:

**Trading Account (Gross Profit)** = Sales + Repair Income − **Direct Expenses (group 51)**
**P&L Account (Net Profit)** = Gross Profit + Other Income − **Indirect Expenses (group 52)**
- Direct = maal/kaam se Jude (Purchases, Wages, Freight, …). Indirect = dukaan/office (Rent, Salary, Electricity, …).
- Hara (+) profit, laal (−) nuqsaan. Excel/Print par yahi do-hissa shape.

---

## A15. Notifications & Messages

- **Notifications** (`/notifications/`): in-app records (repair updates, estimate decisions, customer replies). Dropdown (20/page, kholte hi read-mark), mark-read / mark-all-read / delete / delete-all, unread count.
- **Messages** (`/messages/`): ye **inter-office chat nahi** — ye **public website contact form** ki **inbox** hai (`ContactMessage`). Filters: new/read/replied/spam. Detail modal, status mark (reply se `replied_at`), delete, unread/new count.

---

## A16. Landing Page Content & Blog (staff)

Public homepage ka content staff manage karta hai (HTMX modal CRUD, `@staff_member_required`):
- **Manage Services** (`/landing/manage/services/`) — homepage "Services"
- **Manage Testimonials** — customer reviews
- **Manage FAQs** — FAQ section
- Har one me Add / Edit / Delete; `order` field se sequence.
- **Blog** (`/blog/` ke staff pages, `manage_blog`) — posts banao/edit/delete; **Publish** karne par public list me. (Note: staff list sirf apne posts dikhata hai; create/edit id se hota hai — doosre staff ke post ka id pata ho to technically edit possible hai.)
- **Company Settings** (A17) me hi tagline, hero image, about, Google Map, working hours, social links, WhatsApp number, review link, SEO meta set hote hain.

---

## A17. Settings (`/settings/`)

- **Company Settings** (`settings/`): identity (name, address, phone, email, GSTIN, state, logo); invoicing (prefix, start number, default tax rate, financial-year start); website/landing content + SEO.
- **Backup** (`settings/backup/`): **file-based JSON** (SQLite + PostgreSQL dono), **superuser-only** → `backup_<timestamp>.json` download.
- **Restore** (`settings/restore/`): `.json` upload (valid JSON, ≤ 100 MB), **superuser-only**.
- **Audit Log** (`settings/audit-log/stats/`, `purge/`): immutable "kisne kab kya badla". Stats + purge (30–3650 din purane hatao, default 1095) — **superuser-only**.

---

## A18. Staff Print Summary
Print karne layak documents: **Repair slip**, **Estimate**, **Warranty card**, **Invoice**, **Credit note**, **Statement**, saari **Reports**, aur **list pages** ka print/Excel.

---

# PART B — CUSTOMER PORTAL GUIDE (namespace `customer:`, `/customer/`)

Grahak login karke apna kaam khud dekh/sakhta hai. Staff portal se **alag** hai.

## B1. Dashboard (`/customer/`)
Paid/due/this-month invoice totals, repair status counts (pending/approved/on-hold/rejected estimates samet), recent payments/activity, unread notifications. HTMX refresh + 7-day charts.

## B2. Invoices (`/customer/invoices/`)
Apne **sirf apne** bills — list, detail, **print**. (View-only; khud nahi bana sakte.)

## B3. Purchases (`/customer/purchases/`)
Jo samaan kharida uski **view-only** list/detail/print + **Excel**.

## B4. Repairs (`/customer/repairs/`)
- **List / Detail / Print** apni marammatein.
- **Book Repair (Add)** (`/customer/repairs/create/`): Device model, Serial, Issue, Accessories, Device condition → job **pending** me banti hai, staff ko notification, aur aapko device dukaan lane ka message. **Sirf tab tak** edit/delete jab tak job `pending` ho **aur** device physically receive na hua ho.
- **Estimate decision** (`approve`/`hold`/`reject`): apni estimate par aap haan/hold/manna kar sakte ho. Approve karne se repair start ho sakti hai. **Dhyan:** customer side kabhi **cancel ya cancellation-charge trigger nahi** karta — wo sirf staff kar sakta hai.

## B5. Payments (`/customer/payments/`)
Apne received-side payments, **Print/Excel**.

## B6. Statement (`/customer/statement/`)
Aapka poora account ledger (receivable + payable), default bank summary + detailed mode, **Print/Excel**.

## B7. Profile (`/customer/profile/`)
Name, Company, Phone, Address, State, GSTIN badal sakte ho (phone unique, kam se kam 10 digit).

## B8. Password & Email
- **Change Password** (`/customer/password-change/`): login reh kar change.
- **Change Email** (`profile/email-change/`): naya email → **OTP verify** → process ke end me jaan-boojh kar **logout** (naye email se dobara login karo).

## B9. Notifications (`/customer/notifications/`)
Apne in-app notifications — list/dropdown, mark-all-read, mark-read, delete.

---

# PART C — PUBLIC WEBSITE (no login)

## C1. Home / Landing (`/`)
Company info, active **Services**, **Testimonials**, **FAQs** (order-wise), latest **3 blog posts**, SEO meta, aur **Track Repair widget**.

## C2. Track Repair (`/tracking/`)
- **By link:** staff aapko ek **tracking link** (token) deta hai → `/tracking/repair/<token>/` par status, poora **timeline**, device details, reported issue, estimate dikhta hai. **Costs/advance** sirf tab dikhte hain jab job `ready`/`delivered` ho ya invoice bana ho. Progress % (pending 10 → delivered 100). **Cancelled** job par "Closed" + **reason + cancellation charge ka status** professional tarike se dikhta hai. Rate-limit 30/min.
- **By lookup:** job number + apna **10-digit phone** daalo → link ban jata hai. (Dono validate hote hain; rate-limit 8/min.)

## C3. Blog (`/blog/`)
Sirf **published** posts. Category/tag filter, post detail (view count + 3 related), SEO meta.

## C4. Contact Form (`/contact-message/`)
Website par message bhejo → staff ke **Messages inbox** (A15) me aata hai.

## C5. Policies & SEO
`/privacy-policy/`, `/cookie-policy/`, `/robots.txt`, `/sitemap.xml`.

---

# PART D — Signup / OTP / Password (common)

- **Public signup** (`/register/`): customer ke liye khula hai. Account **inactive** banta hai + **6-digit email OTP** jata hai. OTP (`/verify-otp/`) verify karo → active + auto-login. Honeypot field bots rokta hai.
- **Resend OTP** (`/resend-otp/`): 60 second cooldown.
- **Forgot password (OTP)** (`/password-reset-otp/`): email daalo → OTP → `/reset-password-set/`. (Account existence leak na ho isliye hamesha same message.)
- **Forgot password (link)** (`/password-reset/`): purana token email flow bhi maujood.

---

# Cheat Sheet

## Staff — quick table
| Kaam | Kahan |
|---|---|
| Naya customer/vendor | Contacts → Add |
| Naya product + stock | Products → Add (Current Stock) |
| Stock sudhar | Stock → Adjustments |
| Samaan kharidna | Purchases → Add |
| Repair intake | Repairs → Add |
| Estimate Approve/Hold/Reject | Repair detail → estimate buttons |
| Repair cancel (+charge) | Repair → Update Status → Cancel (reason + charge) |
| Part/Service lagana | Repair detail → Add Part/Service |
| Repair ka bill | Repair → Create Invoice (ready/delivered + approved estimate) |
| Advance lena | Payments → Add (Received + Advance ✓) |
| Advance bill par | Invoice → Apply ₹__ |
| Balance payment | Payments → Add (Received, Advance ✗) |
| Rent/Salary/Bijli | Payments → Add (Paid + Expense Account) |
| Credit note | Invoice → Credit Note |
| Cash jam/nikasi | Cash Book → Contra |
| Statement | Statements → Customer/Vendor/Combined |
| Profit dekhna | Reports → Profit & Loss |
| Backup/Restore | Settings → Backup / Restore (superuser) |

## Customer — quick table
| Kaam | Kahan |
|---|---|
| Apne bills dekhna | Portal → Invoices |
| Repair book karna | Portal → Repairs → Add |
| Estimate approve/hold/reject | Repair detail → estimate buttons |
| Apna status track karna | `/tracking/` (link ya job no + phone) |
| Purana hisab | Portal → Statement |
| Profile/surName/GSTIN | Portal → Profile |
| Password badalna | Portal → Change Password |
| Email badalna | Portal → Email change (OTP) |

---

## Common Rules & Notes
1. Pehle **contact + product banao**, phir bill/payment me chuno.
2. **Purchase** = stock+ledger dono; **Adjustment** sirf sudhar.
3. Roz ka kharcha **Payments (Paid + Expense Account)** se, Journal se nahi.
4. Repair **cancel karne par reason zaroori** (customer ko tracking par dikhta hai); charge optional.
5. Kuch galat hua to **Un-apply / Delete** dekho, phir dobara darj karo.
6. Dashboard ka koi number ajeeb lage to **card par click** karke poori list dekho.
7. **Customer side cancel/charge nahi** kar sakta — ye staff ka kaam hai.
