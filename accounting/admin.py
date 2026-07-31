# accounting/admin.py

from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from django.db.models import Sum, Count
from django.contrib import messages
from django.utils import timezone
from decimal import Decimal
from django.utils.html import format_html

from .models import (
    CompanyProfile, LedgerEntry, LedgerLine, Contact,
    ProductCategory, Product, Invoice, InvoiceItem,
    Purchase, PurchaseItem, RepairJob, RepairPart,
    Payment, StockMovement, Transaction,
    Notification, NotificationPreference, ContactMessage, 
    FAQ, Testimonial, Service, EmailOTP  # 🔥 OTP Model Import करें
)
from .views.sales import create_or_update_invoice_ledger

admin.site.site_header = "A1 Computer Solutions"
admin.site.site_title = "A1 Computer Solutions Admin"
admin.site.index_title = "Welcome to the Dashboard"


# ============================================================
# COMPANY PROFILE ADMIN (Complete – All Fields)
# ============================================================
@admin.register(CompanyProfile)
class CompanyProfileAdmin(admin.ModelAdmin):
    fieldsets = (
        # 1. BASIC INFORMATION
        ('Basic Information', {
            'fields': ('name', 'tagline', 'address', 'phone', 'email', 'state')
        }),
        
        # 2. BRANDING & IMAGES
        ('Branding & Images', {
            'fields': ('logo', 'hero_image', 'og_image'),
            'classes': ('wide',)
        }),
        
        # 3. BUSINESS SETTINGS
        ('Business Settings', {
            'fields': ('gstin', 'invoice_prefix', 'invoice_start_number', 
                       'default_tax_rate', 'financial_year_start')
        }),
        
        # 4. LANDING PAGE CONTENT
        ('Landing Page Content', {
            'fields': ('about_text', 'working_hours', 'google_map_embed'),
            'description': 'Content displayed on the public landing page.'
        }),
        
        # 5. SOCIAL MEDIA & REVIEWS
        ('Social Media & Reviews', {
            'fields': ('facebook_url', 'instagram_url', 'youtube_url', 
                       'blog_url', 'whatsapp_number', 'google_review_link'),
            'classes': ('collapse',)
        }),
        
        # 6. SEO (Search Engine Optimization)
        ('SEO (Search Engine Optimization)', {
            'fields': ('meta_title', 'meta_description', 'meta_keywords'),
            'description': 'Optimize your site for search engines and social sharing.',
            'classes': ('collapse',)
        }),
    )

    list_display = (
        'name', 
        'phone', 
        'email', 
        'gstin', 
        'logo_preview', 
        'hero_preview', 
        'og_preview'
    )
    list_editable = ('phone', 'email')
    search_fields = ('name', 'phone', 'email', 'gstin', 'meta_title', 'meta_keywords')
    readonly_fields = ['id']

    # ----- IMAGE PREVIEWS -----
    def logo_preview(self, obj):
        if obj.logo:
            return format_html(
                '<img src="{}" width="50" height="50" style="object-fit:cover; border-radius:4px;" />',
                obj.logo.url
            )
        return "No logo"
    logo_preview.short_description = "Logo"

    def hero_preview(self, obj):
        if obj.hero_image:
            return format_html(
                '<img src="{}" width="80" height="50" style="object-fit:cover; border-radius:4px;" />',
                obj.hero_image.url
            )
        return "No image"
    hero_preview.short_description = "Hero Image"

    def og_preview(self, obj):
        if obj.og_image:
            return format_html(
                '<img src="{}" width="80" height="50" style="object-fit:cover; border-radius:4px;" />',
                obj.og_image.url
            )
        return "No image"
    og_preview.short_description = "OG Image"

    # ----- PREVENT DELETION OF THE ONLY PROFILE -----
    def has_delete_permission(self, request, obj=None):
        if obj and CompanyProfile.objects.count() == 1:
            return False
        return super().has_delete_permission(request, obj)
    
    
@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ['title', 'order', 'is_active', 'created_at']
    list_filter = ['is_active', 'created_at']
    search_fields = ['title', 'description']
    list_editable = ['order', 'is_active']
    fieldsets = (
        (None, {
            'fields': ('title', 'description', 'icon', 'image')
        }),
        ('Settings', {
            'fields': ('order', 'is_active')
        }),
    )

@admin.register(Testimonial)
class TestimonialAdmin(admin.ModelAdmin):
    list_display = ['customer_name', 'rating', 'order', 'is_active', 'created_at']
    list_filter = ['rating', 'is_active', 'created_at']
    search_fields = ['customer_name', 'review_text', 'designation']
    list_editable = ['order', 'is_active', 'rating']
    fieldsets = (
        ('Customer Info', {
            'fields': ('customer_name', 'customer_photo', 'designation', 'company_name')
        }),
        ('Review', {
            'fields': ('review_text', 'rating')
        }),
        ('Settings', {
            'fields': ('order', 'is_active')
        }),
    )
    

# ============================================================
# FAQ ADMIN
# ============================================================

@admin.register(FAQ)
class FAQAdmin(admin.ModelAdmin):
    list_display = ['question', 'order', 'is_active', 'created_at']
    list_filter = ['is_active', 'created_at']
    search_fields = ['question', 'answer']
    list_editable = ['order', 'is_active']
    ordering = ['order', 'created_at']
    fieldsets = (
        (None, {
            'fields': ('question', 'answer')
        }),
        ('Settings', {
            'fields': ('order', 'is_active')
        }),
    )


# ============================================================
# LEDGER
# ============================================================
class LedgerLineInline(admin.TabularInline):
    model = LedgerLine
    extra = 1
    fields = ['account', 'contact', 'debit', 'credit']


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = ['id', 'date', 'entry_type', 'description', 'total_amount', 'created_at']
    list_filter = ['entry_type', 'date']
    search_fields = ['description', 'reference_id']
    readonly_fields = ['total_amount', 'created_at']
    inlines = [LedgerLineInline]
    fieldsets = (
        (None, {
            'fields': ('date', 'entry_type', 'reference_id', 'description')
        }),
        ('Auto-calculated', {
            'fields': ('total_amount', 'created_at')
        }),
    )

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        total = obj.lines.aggregate(total=Sum('debit'))['total'] or Decimal('0')
        if obj.total_amount != total:
            obj.total_amount = total
            obj.save(update_fields=['total_amount'])


@admin.register(LedgerLine)
class LedgerLineAdmin(admin.ModelAdmin):
    list_display = ['ledger_entry', 'account', 'contact', 'debit', 'credit']
    list_filter = ['ledger_entry__entry_type', 'account']
    search_fields = ['account', 'contact__name']
    raw_id_fields = ['ledger_entry', 'contact']


# ============================================================
# CONTACTS
# ============================================================
@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ['name', 'contact_type', 'phone', 'email', 'user', 'opening_balance', 'current_balance']
    list_filter = ['contact_type']
    search_fields = ['name', 'company_name', 'phone', 'email', 'gstin']
    fieldsets = (
        ('Contact Type', {
            'fields': ('contact_type',)
        }),
        ('Personal/Business Info', {
            'fields': ('name', 'company_name', 'phone', 'email', 'address')
        }),
        ('GST & State', {
            'fields': ('gstin', 'state')
        }),
        ('Accounting', {
            'fields': ('opening_balance', 'notes')
        }),
        ('User Account (Customer Portal)', {
            'fields': ('user',)
        }),
    )
    readonly_fields = ['created_at']

    def current_balance(self, obj):
        return obj.balance
    current_balance.short_description = "Current Balance"


# ============================================================
# PRODUCTS
# ============================================================
@admin.register(ProductCategory)
class ProductCategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'product_count']
    search_fields = ['name']

    def product_count(self, obj):
        return obj.products.count()
    product_count.short_description = "Products"


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'category', 'hsn_code', 'selling_price', 'current_stock',
        'is_service', 'is_active', 'low_stock_badge'
    ]
    list_filter = ['is_service', 'is_active', 'category', 'unit']
    search_fields = ['name', 'hsn_code']
    readonly_fields = ['created_at']
    fieldsets = (
        ('Basic', {
            'fields': ('name', 'hsn_code', 'category', 'unit', 'is_service', 'is_active')
        }),
        ('Pricing', {
            'fields': ('purchase_price', 'selling_price', 'tax_rate')
        }),
        ('Inventory', {
            'fields': ('current_stock', 'low_stock_threshold')
        }),
        ('Meta', {
            'fields': ('created_at',)
        }),
    )
    actions = ['mark_active', 'mark_inactive']

    def low_stock_badge(self, obj):
        if obj.is_service:
            return "N/A"
        if obj.current_stock <= obj.low_stock_threshold:
            return format_html('<span style="color:red;">Low Stock ({})</span>', obj.current_stock)
        return format_html('<span style="color:green;">OK</span>')
    low_stock_badge.short_description = "Stock Status"

    def mark_active(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, f"{updated} product(s) marked as active.")
    mark_active.short_description = "Mark selected products as active"

    def mark_inactive(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, f"{updated} product(s) marked as inactive.")
    mark_inactive.short_description = "Mark selected products as inactive"


# ============================================================
# SALES INVOICE
# ============================================================
class InvoiceItemInline(admin.TabularInline):
    model = InvoiceItem
    extra = 1
    readonly_fields = ['line_total', 'tax_amount']
    fields = ['product', 'quantity', 'unit_price', 'tax_rate', 'tax_amount', 'line_total']


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = [
        'invoice_number', 'customer', 'date', 'grand_total',
        'paid_amount', 'payment_status', 'view_invoice_link'
    ]
    list_filter = ['payment_status', 'gst_type', 'date']
    search_fields = ['invoice_number', 'customer__name']
    readonly_fields = [
        'invoice_number', 'subtotal', 'tax_amount', 'grand_total',
        'paid_amount', 'balance_due', 'created_at', 'updated_at'
    ]
    inlines = [InvoiceItemInline]
    fieldsets = (
        ('Header', {
            'fields': ('invoice_number', 'customer', 'date', 'due_date')
        }),
        ('GST & Discount', {
            'fields': ('gst_type', 'discount_amount')
        }),
        ('Totals', {
            'fields': ('subtotal', 'tax_amount', 'grand_total', 'paid_amount', 'balance_due', 'payment_status')
        }),
        ('Notes', {
            'fields': ('notes',)
        }),
        ('Meta', {
            'fields': ('created_at', 'updated_at')
        }),
    )

    def view_invoice_link(self, obj):
        url = reverse('admin:accounting_invoice_change', args=[obj.pk])
        return format_html('<a href="{}">Edit</a>', url)
    view_invoice_link.short_description = "Link"

    def save_model(self, request, obj, form, change):
        obj.save()  # triggers calculate_totals, etc.
        create_or_update_invoice_ledger(obj)


@admin.register(InvoiceItem)
class InvoiceItemAdmin(admin.ModelAdmin):
    list_display = ['invoice', 'product', 'quantity', 'unit_price', 'line_total']
    list_filter = ['invoice__payment_status', 'product__is_service']
    search_fields = ['invoice__invoice_number', 'product__name']


# ============================================================
# PURCHASES
# ============================================================
class PurchaseItemInline(admin.TabularInline):
    model = PurchaseItem
    extra = 1
    readonly_fields = ['line_total', 'tax_amount']
    fields = ['product', 'quantity', 'unit_price', 'tax_rate', 'tax_amount', 'line_total']


@admin.register(Purchase)
class PurchaseAdmin(admin.ModelAdmin):
    list_display = ['purchase_number', 'vendor', 'date', 'grand_total', 'paid']
    list_filter = ['paid', 'gst_type', 'date']
    search_fields = ['purchase_number', 'vendor__name']
    readonly_fields = ['purchase_number', 'subtotal', 'tax_amount', 'grand_total', 'created_at']
    inlines = [PurchaseItemInline]
    fieldsets = (
        ('Header', {
            'fields': ('purchase_number', 'vendor', 'date')
        }),
        ('GST', {
            'fields': ('gst_type',)
        }),
        ('Totals', {
            'fields': ('subtotal', 'tax_amount', 'grand_total', 'paid')
        }),
        ('Notes', {
            'fields': ('notes',)
        }),
        ('Meta', {
            'fields': ('created_at',)
        }),
    )

    def save_model(self, request, obj, form, change):
        obj.save()
        obj.update_stock_from_items()


@admin.register(PurchaseItem)
class PurchaseItemAdmin(admin.ModelAdmin):
    list_display = ['purchase', 'product', 'quantity', 'unit_price', 'line_total']
    list_filter = ['purchase__paid']
    search_fields = ['purchase__purchase_number', 'product__name']


# ============================================================
# REPAIR JOBS
# ============================================================
class RepairPartInline(admin.TabularInline):
    model = RepairPart
    extra = 1
    readonly_fields = ['line_total']
    fields = ['product', 'quantity', 'unit_price', 'line_total']


@admin.register(RepairJob)
class RepairJobAdmin(admin.ModelAdmin):
    list_display = [
        'job_number', 'customer', 'device_model', 'status',
        'estimate_status', 'estimated_cost', 'final_amount',
        'date_in', 'invoice_link', 'approval_source', 'approval_remarks'
    ]
    list_filter = ['status', 'estimate_status', 'date_in']
    search_fields = ['job_number', 'customer__name', 'device_model', 'serial_number']
    readonly_fields = [
        'job_number', 'final_amount', 'created_at', 'updated_at',
        'estimate_approved_at', 'estimate_approved_by'
    ]
    inlines = [RepairPartInline]
    fieldsets = (
        ('Job Information', {
            'fields': ('job_number', 'customer', 'device_model', 'serial_number', 'status')
        }),
        ('Issue & Diagnosis', {
            'fields': ('issue_description', 'diagnosis_report', 'action_taken')
        }),
        ('Accessories & Condition', {
            'fields': ('accessories', 'device_condition')
        }),
        ('Estimate & Approval', {
            'fields': ('estimated_cost', 'estimate_status', 'estimate_approved_at', 'estimate_approved_by', 
                    'labour_charge', 'final_amount', 'approval_source', 'approval_remarks')  
        }),
        ('Dates & Delivery', {
            'fields': ('date_in', 'delivery_date', 'received_by', 'delivered_by')
        }),
        ('Invoice & Notes', {
            'fields': ('invoice', 'notes')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def invoice_link(self, obj):
        if obj.invoice:
            url = reverse('admin:accounting_invoice_change', args=[obj.invoice.id])
            return format_html('<a href="{}">{} <i class="bi bi-box-arrow-up-right"></i></a>', url, obj.invoice.invoice_number)
        return "-"
    invoice_link.short_description = "Invoice"

    def save_model(self, request, obj, form, change):
        if 'estimate_status' in form.changed_data and obj.estimate_status == 'approved' and not obj.estimate_approved_at:
            obj.estimate_approved_at = timezone.now()
        super().save_model(request, obj, form, change)
        obj.calculate_final_amount()


# ============================================================
# PAYMENTS
# ============================================================
@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ['date', 'direction', 'contact', 'amount', 'method', 'reference', 'linked_invoices']
    list_filter = ['direction', 'method', 'date']
    search_fields = ['contact__name', 'reference', 'description']
    readonly_fields = ['created_at']
    filter_horizontal = ['invoices']
    fieldsets = (
        (None, {
            'fields': ('direction', 'contact', 'amount', 'date', 'method', 'reference', 'description')
        }),
        ('Link to Invoices', {
            'fields': ('invoices',)
        }),
        ('Meta', {
            'fields': ('created_at',)
        }),
    )

    def linked_invoices(self, obj):
        return ", ".join([inv.invoice_number for inv in obj.invoices.all()])
    linked_invoices.short_description = "Invoices"


# ============================================================
# STOCK MOVEMENT
# ============================================================
@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = ['product', 'movement_type', 'quantity', 'reference', 'date', 'notes_preview']
    list_filter = ['movement_type', 'date']
    search_fields = ['product__name', 'reference']
    readonly_fields = ['date']

    def notes_preview(self, obj):
        return obj.notes[:50] + "..." if len(obj.notes) > 50 else obj.notes
    notes_preview.short_description = "Notes"


# ============================================================
# LEGACY TRANSACTIONS
# ============================================================
@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ['date', 'type', 'description', 'debit_account', 'credit_account', 'amount']
    list_filter = ['type', 'date']
    search_fields = ['description', 'reference_id']


# ============================================================
# NOTIFICATIONS
# ============================================================
@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ['recipient', 'title', 'notification_type', 'category', 'is_read', 'created_at']
    list_filter = ['notification_type', 'category', 'is_read', 'created_at']
    search_fields = ['recipient__username', 'recipient__email', 'title', 'message']
    readonly_fields = ['created_at']
    fieldsets = (
        (None, {
            'fields': ('recipient', 'title', 'message', 'link')
        }),
        ('Type & Category', {
            'fields': ('notification_type', 'category')
        }),
        ('Status', {
            'fields': ('is_read', 'created_at')
        }),
    )
    actions = ['mark_as_read', 'mark_as_unread']

    def mark_as_read(self, request, queryset):
        updated = queryset.update(is_read=True)
        self.message_user(request, f"{updated} notification(s) marked as read.")
    mark_as_read.short_description = "Mark selected notifications as read"

    def mark_as_unread(self, request, queryset):
        updated = queryset.update(is_read=False)
        self.message_user(request, f"{updated} notification(s) marked as unread.")
    mark_as_unread.short_description = "Mark selected notifications as unread"


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ['user', 'email_enabled', 'categories']
    list_filter = ['email_enabled']
    search_fields = ['user__username', 'user__email']
    fields = ['user', 'email_enabled', 'categories']
    
    
# ----- CONTACT MESSAGES -----
@admin.register(ContactMessage)
class ContactMessageAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'subject', 'status', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('name', 'email', 'subject', 'message')
    readonly_fields = ('created_at', 'replied_at')
    ordering = ('-created_at',)


# ============================================================
# OTP ADMIN (Email OTP Records)
# ============================================================
@admin.register(EmailOTP)
class EmailOTPAdmin(admin.ModelAdmin):
    list_display = ['email', 'otp', 'purpose', 'is_used', 'expires_at', 'created_at']
    list_filter = ['purpose', 'is_used', 'created_at']
    search_fields = ['email', 'otp']
    readonly_fields = ['created_at', 'expires_at']
    ordering = ['-created_at']
    fieldsets = (
        (None, {
            'fields': ('email', 'otp', 'purpose', 'user')
        }),
        ('Status', {
            'fields': ('is_used', 'expires_at', 'created_at')
        }),
    )