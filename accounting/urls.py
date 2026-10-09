from django.urls import path
from django.contrib.auth import views as auth_views

from .views import *
from .views import contact_messages as message_views
from .views.auth import (
    unified_login_view,
    register_view,
    unified_logout_view,
    CustomPasswordResetView,
    CustomPasswordResetConfirmView,
    password_change_view,
    validate_login_field,
    validate_register_field,
    verify_otp_view,
    resend_otp_view,
    password_reset_otp_request,
    reset_password_set_view,
    CustomSignupView,
)
from .views.repairs import staff_approve_estimate
from .views.utils import health_check
from .views.global_search import global_search

app_name = 'accounting'

urlpatterns = [
    # ===== Dashboard =====
    path('dashboard/', dashboard, name='dashboard'),
    path('dashboard/stats/', dashboard_stats, name='dashboard_stats'),
    path('dashboard/refresh-stats/', refresh_stats, name='refresh_stats'),
    path('dashboard/recent-transactions/', recent_transactions, name='recent_transactions'),
    path('dashboard/export/', dashboard_export, name='dashboard_export'),

    # ===== Contacts =====
    path('contacts/', contacts.contact_list, name='contact_list'),
    path('contacts/add/', contacts.contact_create, name='contact_create'),
    path('contacts/validate-field/', contacts.validate_contact_field, name='validate_accounting_contact_field'),
    path('contacts/<int:pk>/edit/', contacts.contact_update, name='contact_update'),
    path('contacts/<int:pk>/delete/', contacts.contact_delete, name='contact_delete'),
    path('contacts/search/', contacts.contact_search, name='contact_search'),
    path('contacts/export/excel/', contacts.export_contacts_excel, name='export_contacts_excel'),
    path('contacts/<int:pk>/detail-modal/', contacts.contact_detail_modal, name='contact_detail_modal'),

    # ===== Products =====
    path('products/', products.product_list, name='product_list'),
    path('products/print/', products.product_list_print, name='product_list_print'),
    path('products/create/', products.product_create, name='product_create'),
    path('products/categories/', products.category_list, name='category_list'),
    path('products/categories/create/', products.category_create, name='category_create'),
    path('products/categories/<int:pk>/update/', products.category_update, name='category_update'),
    path('products/categories/<int:pk>/delete/', products.category_delete, name='category_delete'),

    path('products/<int:pk>/update/', products.product_update, name='product_update'),
    path('products/<int:pk>/delete/', products.product_delete, name='product_delete'),
    path('products/<int:pk>/detail/', products.product_detail_modal, name='product_detail_modal'),
    path('products/validate-field/', products.validate_product_field, name='validate_product_field'),
    path('validate-category-field/', products.validate_category_field, name='validate_category_field'),
    path('products/add-category/', products.add_category_inline, name='add_category_inline'),
    path('products/get-price/', products.get_product_price, name='get_product_price'),
    path('products/stock-history/<int:pk>/', products.product_stock_history, name='product_stock_history'),
    path('products/search/', products.product_search, name='product_search'),
    path('products/quick-add/', products.product_quick_add, name='product_quick_add'),
    path('products/export/excel/', products.export_products_excel, name='export_products_excel'),

    # ===== Sales / Invoices =====
    path('sales/', sales.invoice_list, name='invoice_list'),
    path('sales/print/', sales.invoice_list_print, name='invoice_list_print'),
    path('sales/export/excel/', sales.invoice_list_excel, name='invoice_list_excel'),
    path('sales/create/', sales.invoice_create, name='invoice_create'),
    path('sales/<int:pk>/', sales.invoice_detail, name='invoice_detail'),
    path('sales/<int:pk>/print/', sales.invoice_print, name='invoice_print'),
    path('sales/<int:pk>/update/', sales.invoice_update, name='invoice_update'),
    path('sales/<int:pk>/delete/', sales.invoice_delete, name='invoice_delete'),
    path('sales/<int:pk>/duplicate/', sales.invoice_duplicate, name='invoice_duplicate'),
    path('sales/<int:pk>/whatsapp/', sales.invoice_whatsapp, name='invoice_whatsapp'),

    path('credit-notes/', sales.credit_note_list, name='credit_note_list'),
    path('credit-notes/<int:pk>/', sales.credit_note_detail, name='credit_note_detail'),
    path('credit-notes/<int:pk>/print/', sales.credit_note_print, name='credit_note_print'),
    path('credit-notes/<int:pk>/delete/', sales.credit_note_delete, name='credit_note_delete'),
    path('credit-notes/<int:pk>/whatsapp/', sales.credit_note_whatsapp, name='credit_note_whatsapp'),
    path('sales/<int:invoice_pk>/credit-note/create/', sales.credit_note_create, name='credit_note_create'),
    path('sales/<int:pk>/add-payment/', payments.payment_create, name='add_payment'),
    path('sales/add-item/', sales.add_invoice_item, name='add_invoice_item'),
    path('sales/remove-item/<int:index>/', sales.remove_invoice_item, name='remove_invoice_item'),
    path('sales/search-products/', sales.product_search, name='sales_product_search'),

    # ===== Purchase Module =====
    path('purchases/', purchases.purchase_list, name='purchase_list'),
    path('purchases/print/', purchases.purchase_list_print, name='purchase_list_print'),
    path('purchases/export/excel/', purchases.purchase_list_excel, name='purchase_list_excel'),
    path('purchases/create/', purchases.purchase_create, name='purchase_create'),
    path('purchases/<int:pk>/', purchases.purchase_detail, name='purchase_detail'),
    path('purchases/<int:pk>/print/', purchases.purchase_print, name='purchase_print'),
    path('purchases/<int:pk>/edit/', purchases.purchase_update, name='purchase_update'),
    path('purchases/<int:pk>/delete/', purchases.purchase_delete, name='purchase_delete'),
    path('purchases/add-item/', purchases.add_purchase_item, name='add_purchase_item'),
    path('purchases/remove-item/<int:index>/', purchases.remove_purchase_item, name='remove_purchase_item'),
    path('purchases/validate-field/', purchases.validate_purchase_field, name='validate_purchase_field'),
    path('purchase/product-search/', purchases.purchase_product_search, name='purchase_product_search'),
    path('purchase/product-quick-add/', purchases.purchase_product_quick_add, name='purchase_product_quick_add'),

    # ===== Repairs =====
    path('repairs/', repairs.repair_list, name='repair_list'),
    path('repairs/create/', repairs.repair_create, name='repair_create'),
    path('repairs/create-for-contact/<int:contact_id>/', repairs.repair_create_for_contact, name='repair_create_for_contact'),
    path('repairs/<int:pk>/', repairs.repair_detail, name='repair_detail'),
    path('repairs/<int:pk>/edit/', repairs.repair_update, name='repair_update'),
    path('repairs/<int:pk>/status/', repairs.update_repair_status, name='update_repair_status'),
    path('repairs/<int:pk>/add-part/', repairs.add_repair_part, name='add_repair_part'),
    path('repairs/remove-part/<int:part_pk>/', repairs.remove_repair_part, name='remove_repair_part'),
    path('repairs/<int:pk>/add-service/', repairs.add_repair_service, name='add_repair_service'),
    path('repairs/remove-service/<int:service_pk>/', repairs.remove_repair_service, name='remove_repair_service'),
    path('repairs/<int:pk>/create-invoice/', repairs.create_invoice_from_repair, name='create_invoice_from_repair'),
    path('repairs/validate-field/', repairs.validate_repair_field, name='validate_repair_field'),
    path('repairs/<int:pk>/delete/', repairs.repair_delete, name='repair_delete'),
    path('repairs/<int:pk>/print/', repairs.repair_print, name='repair_print'),
    path('repairs/print-list/', repairs.repair_list_print, name='repair_list_print'),
    path('repairs/export/excel/', repairs.export_repairs_excel, name='export_repairs_excel'),
    path('repairs/<int:pk>/staff-approve/', repairs.staff_approve_estimate, name='staff_approve_estimate'),
    path('repairs/<int:pk>/quick-update/', repairs.quick_update_repair, name='quick_update_repair'),
    path('repairs/<int:pk>/send-estimate/', repairs.send_estimate_to_customer, name='send_estimate_to_customer'),
    path('repairs/<int:pk>/estimate/print/', repairs.estimate_print, name='estimate_print'),
    path('repairs/<int:pk>/warranty-card/', repairs.warranty_card_print, name='warranty_card_print'),
    # ===== Repair → Invoice Item Management =====
    path('repairs/<int:pk>/create-invoice/add-item/', repairs.add_repair_invoice_item, name='add_repair_invoice_item'),
    path('repairs/<int:pk>/create-invoice/remove-item/<int:index>/', repairs.remove_repair_invoice_item, name='remove_repair_invoice_item'),
    path('repairs/invoice/product-search/', repairs.repair_invoice_product_search, name='repair_invoice_product_search'),

    # ===== Reports =====
    path('reports/sales/', sales_report, name='sales_report'),
    path('reports/purchases/', purchase_report, name='purchase_report'),
    path('reports/gst/', gst_report, name='gst_report'),
    path('reports/profit-loss/', profit_loss, name='profit_loss'),
    path('reports/stock/', stock_report, name='stock_report'),
    path('reports/aging/', aging_report, name='aging_report'),
    path('reports/trial-balance/', trial_balance, name='trial_balance'),
    path('reports/balance-sheet/', balance_sheet, name='balance_sheet'),

    # ===== Payments =====
    path('payments/', payments.payment_list, name='payment_list'),
    path('payments/create/', payments.payment_create, name='payment_create'),
    path('payments/<int:pk>/update/', payments.payment_update, name='payment_update'),
    path('payments/<int:pk>/delete/', payments.payment_delete, name='payment_delete'),
    path('payments/<int:pk>/reconcile/', payments.reconcile_payment, name='reconcile_payment'),
    path('payments/load-unpaid-invoices/', payments.load_unpaid_invoices, name='load_unpaid_invoices'),

    # ===== Statements =====
    path('statements/combined/<int:contact_id>/', statements.combined_statement, name='combined_statement'),
    path('statements/combined/<int:contact_id>/print/', statements.combined_statement, {'is_print': '1'}, name='combined_statement_print'),
    path('statements/combined/<int:contact_id>/excel/', statements.combined_statement_excel, name='combined_statement_excel'),
    path('statements/combined/<int:contact_id>/whatsapp/', statements.combined_statement_whatsapp, name='combined_statement_whatsapp'),
    path('statements/customer/<int:contact_id>/excel/', statements.customer_statement_excel, name='customer_statement_excel'),
    path('statements/customer/<int:contact_id>/', statements.customer_statement, name='customer_statement'),
    path('statements/customer/<int:contact_id>/print/', statements.customer_statement_print, name='customer_statement_print'),
    path('statements/customer/<int:contact_id>/whatsapp/', statements.customer_statement_whatsapp, name='customer_statement_whatsapp'),
    path('statements/vendor/<int:contact_id>/', statements.vendor_statement, name='vendor_statement'),
    path('statements/vendor/<int:contact_id>/csv/', statements.vendor_statement_csv, name='vendor_statement_csv'),
    path('statements/vendor/<int:contact_id>/print/', statements.vendor_statement_print, name='vendor_statement_print'),
    path('statements/vendor/<int:contact_id>/excel/', statements.vendor_statement_excel, name='vendor_statement_excel'),
    path('statements/vendor/<int:contact_id>/whatsapp/', statements.vendor_statement_whatsapp, name='vendor_statement_whatsapp'),

    # ===== Journal Entries =====
    path('journals/', journal.journal_list, name='journal_list'),
    path('journals/create/', journal.journal_create, name='journal_create'),
    path('journals/create/<int:contact_id>/', journal.journal_create_for_contact, name='journal_create_for_contact'),
    path('journals/<int:pk>/update/', journal.journal_update, name='journal_update'),
    path('journals/<int:pk>/delete/', journal.journal_delete, name='journal_delete'),
    path('journals/validate-field/', journal.validate_journal_field, name='validate_journal_field'),

    # ===== Stock =====
    path('stock/adjustment/', stock.stock_adjustment_list, name='stock_adjustment_list'),
    path('stock/adjustment/add/', stock.stock_adjustment_add, name='stock_adjustment_add'),
    path('stock/adjustment/<int:pk>/delete/', stock.stock_adjustment_delete, name='stock_adjustment_delete'),
    path('stock/price-info/', stock.get_product_price_info, name='get_product_price_info'),
    path('stock/search/', stock.product_search_stock, name='product_search_stock'),
    path('stock/dashboard/', stock.stock_dashboard, name='stock_dashboard'),
    path('stock/export/excel/', stock.export_stock_excel, name='export_stock_excel'),

    # ===== Bank Accounts =====
    path('bank/accounts/', bank_account_list, name='bank_account_list'),
    path('bank/accounts/add/', bank_account_add, name='bank_account_add'),
    path('bank/accounts/<int:pk>/edit/', bank_account_edit, name='bank_account_edit'),
    path('bank/accounts/<int:pk>/delete/', bank_account_delete, name='bank_account_delete'),
    path('bank/statement/<int:pk>/', bank_statement, name='bank_statement'),
    path('bank/statement/<int:pk>/excel/', bank_statement_excel, name='bank_statement_excel'),
    path('bank/transactions/', bank_transaction_list, name='bank_transaction_list'),
    path('bank/transaction/<int:account_pk>/add/', bank_transaction_add, name='bank_transaction_add'),
    path('bank/transaction/<int:pk>/edit/', bank_transaction_edit, name='bank_transaction_edit'),
    path('bank/transaction/<int:pk>/delete/', bank_transaction_delete, name='bank_transaction_delete'),

    # ===== Settings =====
    path('settings/', company_settings, name='company_settings'),
    path('settings/backup/', backup_database, name='backup_database'),
    path('settings/restore/', restore_database, name='restore_database'),
    path('settings/validate-field/', validate_setting_field, name='validate_setting_field'),
    path('settings/audit-log/stats/', audit_log_stats, name='audit_log_stats'),
    path('settings/audit-log/purge/', audit_log_purge, name='audit_log_purge'),

    # ===== Authentication =====
    path('login/', unified_login_view, name='login'),
    path('register/', register_view, name='register'),
    path('logout/', unified_logout_view, name='logout'),

    path('password-reset/', CustomPasswordResetView.as_view(), name='password_reset'),
    path('password-reset/done/', auth_views.PasswordResetDoneView.as_view(template_name='auth/password_reset_done.html'), name='password_reset_done'),
    path('password-reset/<uidb64>/<token>/', CustomPasswordResetConfirmView.as_view(), name='password_reset_confirm'),
    path('password-reset/complete/', auth_views.PasswordResetCompleteView.as_view(template_name='auth/password_reset_complete.html'), name='password_reset_complete'),

    path('password-reset-otp/', password_reset_otp_request, name='password_reset_otp'),
    path('verify-otp/', verify_otp_view, name='verify_otp'),
    path('resend-otp/', resend_otp_view, name='resend_otp'),
    path('reset-password-set/', reset_password_set_view, name='reset_password_set'),
    path('password-change/', password_change_view, name='password_change'),
    path('password-change/done/', auth_views.PasswordChangeDoneView.as_view(template_name='auth/password_change_done.html'), name='password_change_done'),

    path('auth/validate-login-field/', validate_login_field, name='validate_login_field'),
    path('auth/validate-register-field/', validate_register_field, name='validate_register_field'),

    # ===== Notifications =====
    path('notifications/', notifications.notification_list, name='notification_list'),
    path('notifications/dropdown/', notifications.notification_dropdown, name='notification_dropdown'),
    path('notifications/mark-read/<int:pk>/', notifications.mark_as_read, name='notification_mark_read'),
    path('notifications/mark-all-read/', notifications.mark_all_read, name='notification_mark_all_read'),
    path('notifications/delete/<int:pk>/', notifications.delete_notification, name='notification_delete'),
    path('notifications/delete-all/', notifications.delete_all_notifications, name='notification_delete_all'),
    path('notifications/unread-count/', notifications.get_unread_count_json, name='notification_unread_count'),
    path('notifications/unread-count-text/', notifications.unread_count_text, name='notification_unread_count_text'),

    # ===== Quick Messages =====
    path('messages/', message_views.message_list, name='message_list'),
    path('messages/<int:pk>/detail/', message_views.message_detail, name='message_detail'),
    path('messages/<int:pk>/status/', message_views.message_mark_status, name='message_mark_status'),
    path('messages/<int:pk>/delete/', message_views.message_delete, name='message_delete'),
    path('messages/unread-count/', message_views.message_unread_count, name='message_unread_count'),

    # ===== Landing Page Management (HTMX CRUD) =====
    path('landing/services/', landing_views.service_list_partial, name='service_list_partial'),
    path('landing/services/create/', landing_views.service_create, name='service_create'),
    path('landing/services/<int:pk>/edit/', landing_views.service_edit, name='service_edit'),
    path('landing/services/<int:pk>/delete/', landing_views.service_delete, name='service_delete'),

    path('landing/testimonials/', landing_views.testimonial_list_partial, name='testimonial_list_partial'),
    path('landing/testimonials/create/', landing_views.testimonial_create, name='testimonial_create'),
    path('landing/testimonials/<int:pk>/edit/', landing_views.testimonial_edit, name='testimonial_edit'),
    path('landing/testimonials/<int:pk>/delete/', landing_views.testimonial_delete, name='testimonial_delete'),
    path('landing/testimonials/google-sync/', landing_views.google_reviews_sync, name='google_reviews_sync'),

    path('landing/faqs/', landing_views.faq_list_partial, name='faq_list_partial'),
    path('landing/faqs/create/', landing_views.faq_create, name='faq_create'),
    path('landing/faqs/<int:pk>/edit/', landing_views.faq_edit, name='faq_edit'),
    path('landing/faqs/<int:pk>/delete/', landing_views.faq_delete, name='faq_delete'),

    path('landing/manage/services/', landing_views.manage_services, name='manage_services'),
    path('landing/manage/testimonials/', landing_views.manage_testimonials, name='manage_testimonials'),
    path('landing/manage/faqs/', landing_views.manage_faqs, name='manage_faqs'),

    # ===== Global Search =====
    path('search/', global_search, name='global_search'),

    path('health/', health_check, name='health_check'),
]