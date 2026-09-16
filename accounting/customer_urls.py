# accounting/customer_urls.py
from django.urls import path

from accounting.views import customer_views

app_name = 'customer'

urlpatterns = [
    # ── Dashboard ──────────────────────────────────────
    path('', customer_views.dashboard, name='customer_dashboard'),
    path('dashboard/refresh/', customer_views.refresh_dashboard_stats,
         name='refresh_dashboard_stats'),
    path('dashboard/stats.json', customer_views.dashboard_stats_json,
         name='customer_dashboard_stats_json'),

    # ── Invoices ───────────────────────────────────────
    path('invoices/', customer_views.invoice_list, name='customer_invoices'),
    path('invoices/<int:pk>/', customer_views.invoice_detail,
         name='customer_invoice_detail'),
    path('invoices/<int:pk>/print/', customer_views.invoice_print,
         name='customer_invoice_print'),

    # ── Repairs ────────────────────────────────────────
    path('repairs/', customer_views.repair_list, name='customer_repairs'),
    path('repairs/create/', customer_views.repair_create,
         name='customer_repair_create'),
    path('repairs/export/excel/', customer_views.customer_repairs_excel,
         name='customer_repairs_excel'),
    path('repairs/validate-field/', customer_views.validate_repair_field,
         name='customer_validate_repair_field'),
    path('repairs/<int:pk>/', customer_views.repair_detail,
         name='customer_repair_detail'),
    path('repairs/<int:pk>/print/', customer_views.repair_print,
         name='customer_repair_print'),
    path('repairs/<int:pk>/edit/', customer_views.repair_update,
         name='customer_repair_update'),
    path('repairs/<int:pk>/delete/', customer_views.repair_delete,
         name='customer_repair_delete'),
    path('repairs/<int:pk>/estimate/approve/',
         customer_views.repair_estimate_approve, name='customer_estimate_approve'),
    path('repairs/<int:pk>/estimate/hold/',
         customer_views.repair_estimate_hold, name='customer_estimate_hold'),
    path('repairs/<int:pk>/estimate/reject/',
         customer_views.repair_estimate_reject, name='customer_estimate_reject'),

    # ── Payments ───────────────────────────────────────
    path('payments/', customer_views.payment_list, name='customer_payments'),

    # ── Statement ──────────────────────────────────────
    path('statement/', customer_views.statement, name='customer_statement'),

    # ── Profile ────────────────────────────────────────
    path('profile/', customer_views.profile, name='customer_profile'),
    path('profile/update/', customer_views.profile_update,
         name='customer_profile_update'),
    path('profile/validate-field/',
         customer_views.validate_customer_profile_field,
         name='validate_customer_profile_field'),

    # ── Password Change ────────────────────────────────
    path('password-change/', customer_views.customer_password_change,
         name='customer_password_change'),

    # ── Email Change (OTP) ─────────────────────────────
    path('profile/email-change/', customer_views.email_change_request,
         name='email_change_request'),
    path('profile/email-change-verify/', customer_views.email_change_verify,
         name='email_change_verify'),

    # ── Notifications ──────────────────────────────────
    path('notifications/', customer_views.notification_list,
         name='customer_notifications'),
    path('notifications/dropdown/', customer_views.notification_dropdown,
         name='customer_notification_dropdown'),
    path('notifications/unread-count/', customer_views.unread_count_text,
         name='customer_unread_count_text'),
    path('notifications/mark-all-read/', customer_views.notification_mark_all_read,
         name='customer_notification_mark_all_read'),
    path('notifications/mark-read/<int:pk>/',
         customer_views.notification_mark_read,
         name='customer_notification_mark_read'),
    path('notifications/delete/<int:pk>/',
         customer_views.notification_delete,
         name='customer_notification_delete'),
    path('notifications/delete-all/',
         customer_views.notification_delete_all,
         name='customer_notification_delete_all'),
]



