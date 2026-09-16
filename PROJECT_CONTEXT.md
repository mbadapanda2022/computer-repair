# PROJECT CONTEXT — computer_repair

## Deployment
- GitHub: https://github.com/mbadapanda2022/computer-repair
- Production: https://a1computersolutions.onrender.com
- Hosting: Render free tier + Supabase Postgres + Cloudinary
- Tech: Django 5.1.5 + HTMX + Bootstrap 5

## Structure
- Django project: computer_repair/
- Main app: accounting/
- Templates: templates/ (staff) + templates/customer/
- Static: static/
- Two URL namespaces: accounting (staff), customer

## Key files
- Models: accounting/models.py
- Auth: accounting/views/auth.py, accounting/auth_backends.py, accounting/utils/otp_helpers.py
- Repairs: accounting/views/repairs.py
- Customer: accounting/views/customer_views.py
- Notifications: accounting/utils/notification_helpers.py, accounting/views/notifications.py
- Utils: accounting/views/utils.py (is_htmx, htmx_response, toast_only_response)
- Middleware: accounting/middleware.py
- Settings: computer_repair/settings.py
- URLs: accounting/urls.py, accounting/customer_urls.py
- JS: static/js/app.js
- Layouts: templates/base.html, templates/base_customer.html
- Build: build.sh

## Core patterns (never break)
- Soft delete everywhere (.soft_delete(), is_deleted flag)
- Contact delete anonymizes linked User (email/username freed)
- OTP via secrets module, never logged
- Login rate limit: 5/5min (DatabaseCache)
- Notifications: HTMX polling every 30s (NO SSE)
- HTMX modal target: #mainModalContent
- Toast responses: toast_only_response sets HX-Reswap: none
- Email change: messages.success() BEFORE logout()
- contact_type can be customer | vendor | both

## User preferences
- Chat in Hindi
- Code/UI strings/comments in English
- Preserve ALL existing features - only add/fix
- Copy-paste ready git commands at end

## Recent changes
1. Contact.soft_delete() anonymizes User
2. cleanup_orphans management command
3. Status change -> context-aware modal
4. Send Estimate to Customer feature
5. Estimate print + Warranty card templates
6. toast_only_response HX-Reswap: none fix
7. Customer estimate review UI (breakdown table + 3 buttons)
8. Password reset HTMX redirect fix
9. Email change message order fix