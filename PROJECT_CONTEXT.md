# PROJECT CONTEXT — computer_repair (A1 Computer Solutions)

> **Read this file FIRST before touching any code.**
> This is the single source of truth for project conventions,
> architecture, and non-negotiable rules.

---

## 🧭 Project Snapshot

| Item | Value |
|------|-------|
| **Purpose** | Repair shop + accounting management system (invoices, purchases, repairs, payments, ledger, bank, journal) |
| **Stack** | Django 5.1.5 + HTMX + Bootstrap 5 + Chart.js |
| **Hosting** | Render (free tier) + Supabase Postgres + Cloudinary (media) |
| **Repo** | https://github.com/mbadapanda2022/computer-repair |
| **Production** | https://a1computersolutions.onrender.com |
| **Django project** | `computer_repair/` |
| **Main app** | `accounting/` |
| **Python** | 3.13.5 |

---

## 👤 User & Team Preferences

- **Chat language:** Hindi (Devanagari or Hinglish)
- **Code / UI strings / comments:** English only
- **Philosophy:** *Audit first, fix later.* Understanding > speed.
- **Data safety:** Financial data (Payments, Bank, Journal, Ledger) = HIGH RISK. Extra caution.
- **Never break existing features.** Only add or fix.
- **Git workflow:** User prefers ONE git command block, only AFTER local verification passes.
- **File editing style:**
  - Small templates → full file replace
  - Large Python files → surgical before/after blocks (no full replace)
- **If a file is needed for context, ASK the user first.** Do not assume.

---

## 🏗️ Architecture Overview

### URL Namespaces
- `accounting` — staff portal (login required, staff-only via middleware)
- `customer` — customer portal (login required, any authenticated user)
- `accounting.tracking_urls` — public repair tracking (no login, signed URLs, 90-day expiry)
- `account` — django-allauth (login, signup, password reset)

### Two Portals, One Codebase
| Concern | Staff Portal | Customer Portal |
|---------|--------------|-----------------|
| Base template | `templates/base.html` | `templates/base_customer.html` |
| HTMX modal target | `#mainModalContent` | `#mainModal` |
| URL namespace | `accounting:` | `customer:` |
| Access | `user.is_staff` = True | any authenticated |

### App Layout