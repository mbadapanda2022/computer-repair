# accounting/admin.py
from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from django.db.models import Sum, Count
from django.contrib import messages
from django.utils import timezone
from decimal import Decimal
from django.utils.html import format_html
from django.contrib.auth.models import User
from django.contrib.auth.admin import UserAdmin
from .models import (
    CompanyProfile, LedgerEntry, LedgerLine, Contact,
    ProductCategory, Product, Invoice, InvoiceItem,
    Purchase, PurchaseItem, RepairJob, RepairPart,
    Payment, StockMovement,
    Notification, NotificationPreference, ContactMessage, 
    FAQ, Testimonial, Service, EmailOTP,
    BankAccount, BankTransaction, PaymentAllocation, AdvanceAdjustment,
    Account, AccountGroup, AuditLog, RepairStatusLog, 
    BlogCategory, BlogTag, BlogPost
)

from .models import sync_invoice_ledger

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

        # 3b. REPAIR DEFAULTS
        ('Repair Defaults', {
            'fields': ('default_labour_product', 'default_service_tax_rate'),
            'description': (
                'Labour/service lines ke liye defaults. '
                '`default_labour_product` = Repair Labour ke liye service '
                'product (naam-string lookup ki jagah). '
                '`default_service_tax_rate` khali = product ka apna rate.'
            ),
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
    list_display = ['customer_name', 'rating', 'source', 'order', 'is_active', 'created_at']
    list_filter = ['rating', 'is_active', 'source', 'created_at']
    search_fields = ['customer_name', 'review_text', 'designation']
    list_editable = ['order', 'is_active', 'rating']
    readonly_fields = ['source', 'google_review_id', 'author_photo_url', 'review_url', 'reviewed_at']
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
        ('Google Sync (read-only)', {
            'fields': ('source', 'google_review_id', 'reviewed_at', 'author_photo_url', 'review_url'),
            'classes': ('collapse',),
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
    fields = ['account', 'contact', 'subledger_type', 'debit', 'credit']


@admin.register(LedgerLine)
class LedgerLineAdmin(admin.ModelAdmin):
    list_display = ['ledger_entry', 'account', 'contact', 'subledger_type', 'debit', 'credit']
    list_filter = ['ledger_entry__entry_type', 'account', 'subledger_type']
    search_fields = ['account__name', 'contact__name']
    raw_id_fields = ['ledger_entry', 'contact']


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
        ('Bank (journal / manual entries)', {
            'fields': ('bank_account', 'bank_transaction'),
            'classes': ('collapse',),
        }),
        ('Auto-calculated', {
            'fields': ('total_amount', 'created_at')
        }),
    )

    def save_related(self, request, form, formsets, change):
        """
        Called AFTER inline LedgerLines are saved.
        Recompute total_amount from the final set of lines so the
        displayed total is never stale.
        """
        super().save_related(request, form, formsets, change)
        obj = form.instance
        total = obj.lines.aggregate(total=Sum('debit'))['total'] or Decimal('0')
        if obj.total_amount != total:
            obj.total_amount = total
            obj.save(update_fields=['total_amount'])


# ============================================================
# CONTACTS
# ============================================================

@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'contact_type', 'phone', 'email', 'user',
        'opening_balance', 'receivable_display', 'payable_display', 'net_balance_display'
    ]
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
        ('Opening Balance', {
            'fields': ('opening_balance', 'opening_balance_date')
        }),
        ('Balances (Auto-calculated)', {
            'fields': ('receivable_balance', 'payable_balance', 'advance_balance', 'balance'),
            'classes': ('collapse',),
            'description': 'These are recalculated automatically from ledger entries.'
        }),
        ('Notes', {
            'fields': ('notes',)
        }),
        ('User Account (Customer Portal)', {
            'fields': ('user',)
        }),
    )
    readonly_fields = [
        'created_at',
        'receivable_balance',
        'payable_balance',
        'advance_balance',
        'balance',
    ]

    def receivable_display(self, obj):
        color = 'red' if obj.receivable_balance > 0 else 'gray'
        return format_html(
            '<span style="color:{};">Rs.{}</span>',
            color, obj.receivable_balance
        )
    receivable_display.short_description = "Receivable"

    def payable_display(self, obj):
        color = 'orange' if obj.payable_balance > 0 else 'gray'
        return format_html(
            '<span style="color:{};">Rs.{}</span>',
            color, obj.payable_balance
        )
    payable_display.short_description = "Payable"

    def net_balance_display(self, obj):
        color = 'green' if obj.balance >= 0 else 'red'
        return format_html(
            '<strong style="color:{};">Rs.{}</strong>',
            color, obj.balance
        )
    net_balance_display.short_description = "Net Position"

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
        'name', 'category', 'hsn_code', 'selling_price',
        'current_stock', 'low_stock_badge', 'is_service', 'is_active',
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
        # Auto-fill discount approver when a discount is applied
        if obj.discount_amount and obj.discount_amount > 0:
            if not obj.discount_approved_by:
                obj.discount_approved_by = request.user
        super().save_model(request, obj, form, change)


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
    fields = [
        'product', 'quantity', 'unit_price', 'tax_rate',
        'tax_amount', 'line_total', 'is_office_use'
    ]


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
        ('GST & Charges', {
            'fields': ('gst_type', 'discount_amount', 'freight_charge')
        }),
        ('Totals', {
            'fields': ('subtotal', 'tax_amount', 'grand_total', 'paid')
        }),
        ('Discount Audit Trail', {
            'fields': ('discount_note', 'discount_date', 'discount_type', 'discount_approved_by'),
            'classes': ('collapse',)
        }),
        ('Notes', {
            'fields': ('notes',)
        }),
        ('Meta', {
            'fields': ('created_at',)
        }),
    )
    
    def save_model(self, request, obj, form, change):
        # Auto-fill discount approver when a discount is applied
        if obj.discount_amount and obj.discount_amount > 0:
            if not obj.discount_approved_by:
                obj.discount_approved_by = request.user
        super().save_model(request, obj, form, change)

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


# ============================================================
# REPAIR STATUS HISTORY (immutable audit trail)
# ============================================================
class RepairStatusLogInline(admin.TabularInline):
    """
    Job ke andar status history — APPEND-ONLY.

    Add/change/delete sab band: history sirf `change_status()` likhta hai.
    """
    model = RepairStatusLog
    extra = 0
    can_delete = False
    fields = ('changed_at', 'from_status', 'to_status', 'actor',
              'remarks', 'forced', 'ip_address')
    readonly_fields = fields
    ordering = ('-changed_at',)

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(RepairStatusLog)
class RepairStatusLogAdmin(admin.ModelAdmin):
    """Read-only audit log — yahan se kuch edit nahi hota."""
    list_display = ('changed_at', 'repair_job', 'from_status', 'to_status',
                    'actor', 'forced', 'ip_address')
    list_filter = ('to_status', 'forced', 'changed_at')
    search_fields = ('repair_job__job_number', 'remarks',
                     'actor__username', 'actor__first_name')
    date_hierarchy = 'changed_at'
    list_select_related = ('repair_job', 'actor')
    ordering = ('-changed_at',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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
        'estimate_approved_at', 'estimate_approved_by',
    ]
    inlines = [RepairPartInline, RepairStatusLogInline]
    actions = ['advance_status', 'mark_ready_action', 'mark_delivered_action']
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
                    'final_amount', 'approval_source', 'approval_remarks')
        }),
        ('Timeline', {
            'fields': ('submitted_at', 'received_at', 'received_remarks', 
                      'ready_at', 'delivered_at', 'date_in')
        }),
        ('Delivery Details', {
            'fields': ('delivery_date', 'received_by', 'delivered_by',
                      'delivered_to_name', 'delivered_to_phone',
                      'delivered_to_designation', 'delivery_remarks')
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
        """
        Admin se status badalna bhi STATE MACHINE se guzarta hai.

        Kyun: status sirf `change_status()` se badalna chahiye — warna
        guards, timeline dates, immutable history aur notification bypass
        ho jate hain (aur guard seedha error deta hai).

        Yahan: purana status nikaal kar, naya status `change_status()` se
        lagate hain (audit trail ke saath).
        """
        from django.core.exceptions import ValidationError as _VE

        old_status = None
        if change and obj.pk:
            old_status = (
                RepairJob.all_objects.filter(pk=obj.pk)
                .values_list('status', flat=True).first()
            )
        new_status = obj.status

        if 'estimate_status' in form.changed_data and \
                obj.estimate_status == 'approved' and not obj.estimate_approved_at:
            obj.estimate_approved_at = timezone.now()

        if old_status and new_status and old_status != new_status \
                and not obj.invoice_id:
            # Status ko purani value par lao — warna model guard raise karega
            obj.status = old_status
            super().save_model(request, obj, form, change)
            try:
                obj.change_status(
                    new_status, by=request.user,
                    remarks='Admin panel se status change',
                )
            except _VE as exc:
                self.message_user(
                    request,
                    exc.messages[0] if getattr(exc, 'messages', None) else str(exc),
                    level=messages.ERROR,
                )
        else:
            super().save_model(request, obj, form, change)

        obj.calculate_final_amount()

    # ── Bulk actions (state machine ke through) ──────────────────
    @admin.action(description="▶ Advance to next status (valid transitions only)")
    def advance_status(self, request, queryset):
        updated, failed = 0, []
        for job in queryset:
            try:
                job.advance(by=request.user,
                            remarks='Admin: advance to next status')
                updated += 1
            except Exception as exc:                          # noqa: BLE001
                failed.append(
                    f'{job.job_number}: '
                    f'{exc.messages[0] if getattr(exc, "messages", None) else exc}'
                )
        if updated:
            self.message_user(request, f'{updated} job(s) advance hue.',
                              level=messages.SUCCESS)
        for line in failed[:10]:
            self.message_user(request, line, level=messages.ERROR)

    @admin.action(description="✔ Mark Ready for Delivery")
    def mark_ready_action(self, request, queryset):
        self._bulk_transition(request, queryset, 'ready')

    @admin.action(description="🚚 Mark Delivered")
    def mark_delivered_action(self, request, queryset):
        self._bulk_transition(request, queryset, 'delivered')

    def _bulk_transition(self, request, queryset, target):
        updated, failed = 0, []
        for job in queryset:
            try:
                job.change_status(target, by=request.user,
                                  remarks=f'Admin: {target}')
                updated += 1
            except Exception as exc:                          # noqa: BLE001
                failed.append(
                    f'{job.job_number}: '
                    f'{exc.messages[0] if getattr(exc, "messages", None) else exc}'
                )
        if updated:
            self.message_user(request, f'{updated} job(s) → {target}.',
                              level=messages.SUCCESS)
        for line in failed[:10]:
            self.message_user(request, line, level=messages.ERROR)

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        # Invoiced job → status bhi lock (model guard ke saath consistent)
        if obj is not None and obj.pk and obj.invoice_id:
            fields.append('status')
        return fields

    def formfield_for_choice_field(self, db_field, request, **kwargs):
        """Status dropdown me sirf VALID next transitions (+ current)."""
        if db_field.name == 'status':
            obj_id = getattr(request.resolver_match, 'kwargs', {}).get('object_id')
            if obj_id:
                job = RepairJob.all_objects.filter(pk=obj_id).first()
                if job:
                    valid = job.allowed_next_statuses(job.status) | {job.status}
                    kwargs['choices'] = [
                        (k, v) for k, v in job.STATUS_CHOICES if k in valid
                    ]
        return super().formfield_for_choice_field(db_field, request, **kwargs)


# ============================================================
# PAYMENTS
# ============================================================
@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = [
        'date', 'direction', 'contact', 'amount', 'method',
        'bank_account', 'is_advance', 'reference', 'linked_invoices'
    ]
    list_filter = ['direction', 'method', 'is_advance', 'date', 'bank_account']
    search_fields = ['contact__name', 'reference', 'upi_ref', 'description']
    readonly_fields = ['created_at']
    fieldsets = (
        ('Payment Info', {
            'fields': ('direction', 'contact', 'amount', 'date', 'method')
        }),
        ('Bank / UPI Details', {
            'fields': ('bank_account', 'upi_ref', 'reference', 'account_name')
        }),
        ('Classification', {
            'fields': ('is_advance', 'reconciled')
        }),
        ('Discount (if any)', {
            'fields': ('discount_amount', 'discount_note', 'discount_type', 'discount_approved_by'),
            'classes': ('collapse',)
        }),
        ('Notes', {
            'fields': ('description',)
        }),
        ('Meta', {
            'fields': ('created_at',)
        }),
    )

    def linked_invoices(self, obj):
        return ", ".join([alloc.invoice.invoice_number for alloc in obj.allocations.all()])
    linked_invoices.short_description = "Invoices"
    
    def save_model(self, request, obj, form, change):
        # Auto-fill discount approver when a discount is applied
        if obj.discount_amount and obj.discount_amount > 0:
            if not obj.discount_approved_by:
                obj.discount_approved_by = request.user
        super().save_model(request, obj, form, change)


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
# BANK ACCOUNTS
# ============================================================
@admin.register(BankAccount)
class BankAccountAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'bank_name', 'account_type', 'account_number_masked',
        'opening_balance', 'current_balance_display', 'is_active'
    ]
    list_filter = ['account_type', 'is_active', 'bank_name']
    search_fields = ['name', 'bank_name', 'account_number', 'ifsc_code']
    readonly_fields = ['current_balance', 'created_at', 'updated_at']
    list_editable = ['is_active']
    fieldsets = (
        ('Basic Info', {
            'fields': ('name', 'bank_name', 'account_type', 'is_active')
        }),
        ('Account Details', {
            'fields': ('account_number', 'ifsc_code')
        }),
        ('Balances', {
            'fields': ('opening_balance', 'current_balance'),
            'description': 'Current balance is auto-calculated from transactions.'
        }),
        ('Meta', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def account_number_masked(self, obj):
        if not obj.account_number:
            return '-'
        if len(obj.account_number) <= 4:
            return obj.account_number
        return '****' + obj.account_number[-4:]
    account_number_masked.short_description = "Account #"

    def current_balance_display(self, obj):
        color = 'green' if obj.current_balance >= 0 else 'red'
        return format_html(
            '<strong style="color:{};">Rs.{}</strong>',
            color, obj.current_balance
        )
    current_balance_display.short_description = "Current Balance"


# ============================================================
# BANK TRANSACTIONS
# ============================================================
@admin.register(BankTransaction)
class BankTransactionAdmin(admin.ModelAdmin):
    list_display = [
        'date', 'bank_account', 'transaction_type_badge', 'amount_display',
        'source_type', 'linked_payment', 'reconciled', 'reference'
    ]
    list_filter = ['transaction_type', 'source_type', 'reconciled', 'bank_account', 'date']
    search_fields = ['description', 'reference', 'bank_account__name']
    readonly_fields = ['created_at']
    date_hierarchy = 'date'
    raw_id_fields = ['payment', 'invoice', 'purchase']
    fieldsets = (
        ('Transaction Info', {
            'fields': ('bank_account', 'transaction_type', 'source_type', 'amount', 'date')
        }),
        ('Reference', {
            'fields': ('description', 'reference')
        }),
        ('Linked Documents', {
            'fields': ('payment', 'invoice', 'purchase'),
            'classes': ('collapse',)
        }),
        ('Reconciliation', {
            'fields': ('reconciled',)
        }),
        ('Meta', {
            'fields': ('created_at',)
        }),
    )

    def transaction_type_badge(self, obj):
        if obj.transaction_type == 'deposit':
            return format_html(
                '<span style="color:green; font-weight:bold;">&darr; Deposit</span>'
            )
        return format_html(
            '<span style="color:red; font-weight:bold;">&uarr; Withdrawal</span>'
        )
    transaction_type_badge.short_description = "Type"

    def amount_display(self, obj):
        if obj.transaction_type == 'deposit':
            return format_html('<span style="color:green;">+Rs.{}</span>', obj.amount)
        return format_html('<span style="color:red;">-Rs.{}</span>', obj.amount)
    amount_display.short_description = "Amount"

    def linked_payment(self, obj):
        if obj.payment:
            url = reverse('admin:accounting_payment_change', args=[obj.payment.id])
            return format_html('<a href="{}">PMT-{:04d}</a>', url, obj.payment.id)
        return '-'
    linked_payment.short_description = "Payment"


# ============================================================
# PAYMENT ALLOCATIONS
# ============================================================
@admin.register(PaymentAllocation)
class PaymentAllocationAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'payment_link', 'invoice_link', 'amount',
        'payment_date', 'payment_direction'
    ]
    list_filter = ['payment__direction', 'payment__date']
    search_fields = [
        'payment__contact__name',
        'invoice__invoice_number',
        'payment__reference'
    ]
    raw_id_fields = ['payment', 'invoice']
    date_hierarchy = 'payment__date'

    def payment_link(self, obj):
        url = reverse('admin:accounting_payment_change', args=[obj.payment.id])
        return format_html(
            '<a href="{}">PMT-{:04d}</a> <small>({})</small>',
            url, obj.payment.id, obj.payment.contact.name
        )
    payment_link.short_description = "Payment"

    def invoice_link(self, obj):
        url = reverse('admin:accounting_invoice_change', args=[obj.invoice.id])
        return format_html(
            '<a href="{}">{}</a>',
            url, obj.invoice.invoice_number
        )
    invoice_link.short_description = "Invoice"

    def payment_date(self, obj):
        return obj.payment.date
    payment_date.short_description = "Date"
    payment_date.admin_order_field = 'payment__date'

    def payment_direction(self, obj):
        return obj.payment.get_direction_display()
    payment_direction.short_description = "Direction"


# ============================================================
# ADVANCE ADJUSTMENTS
# ============================================================
@admin.register(AdvanceAdjustment)
class AdvanceAdjustmentAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'date', 'payment_link', 'invoice_link',
        'amount', 'contact_name'
    ]
    list_filter = ['date']
    search_fields = [
        'payment__contact__name',
        'invoice__invoice_number',
        'notes'
    ]
    raw_id_fields = ['payment', 'invoice']
    date_hierarchy = 'date'
    readonly_fields = ['created_at']
    fieldsets = (
        ('Adjustment Info', {
            'fields': ('payment', 'invoice', 'amount', 'date')
        }),
        ('Notes', {
            'fields': ('notes',)
        }),
        ('Meta', {
            'fields': ('created_at',)
        }),
    )

    def payment_link(self, obj):
        url = reverse('admin:accounting_payment_change', args=[obj.payment.id])
        return format_html('<a href="{}">PMT-{:04d}</a>', url, obj.payment.id)
    payment_link.short_description = "Advance Payment"

    def invoice_link(self, obj):
        url = reverse('admin:accounting_invoice_change', args=[obj.invoice.id])
        return format_html('<a href="{}">{}</a>', url, obj.invoice.invoice_number)
    invoice_link.short_description = "Applied To Invoice"

    def contact_name(self, obj):
        return obj.payment.contact.name if obj.payment and obj.payment.contact else '-'
    contact_name.short_description = "Contact"


# ============================================================
# CHART OF ACCOUNTS — ACCOUNT GROUPS
# ============================================================
@admin.register(AccountGroup)
class AccountGroupAdmin(admin.ModelAdmin):
    list_display = ['code', 'name', 'parent', 'is_active', 'account_count']
    list_filter = ['is_active']
    search_fields = ['code', 'name']
    list_editable = ['is_active']
    ordering = ['code']

    def account_count(self, obj):
        return obj.accounts.count()
    account_count.short_description = "Accounts"


# ============================================================
# CHART OF ACCOUNTS — ACCOUNTS
# ============================================================
@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = [
        'code', 'name', 'account_type', 'group',
        'default_tax_rate', 'is_system', 'is_active'
    ]
    list_filter = ['account_type', 'group', 'is_system', 'is_active']
    search_fields = ['code', 'name']
    list_editable = ['is_active']
    readonly_fields = ['created_at', 'updated_at']
    ordering = ['code']
    fieldsets = (
        ('Basic Info', {
            'fields': ('code', 'name', 'account_type', 'group', 'parent')
        }),
        ('Settings', {
            'fields': ('default_tax_rate', 'is_active', 'is_system'),
            'description': 'System accounts cannot be renamed.'
        }),
        ('Meta', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


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
    
# ============================================================
# USER ADMIN (Custom – with Phone from Contact)
# ============================================================

class ContactInline(admin.StackedInline):
    """Inline for Contact model inside User admin (edit phone etc.)"""
    model = Contact
    fk_name = 'user'
    can_delete = False
    verbose_name_plural = 'Contact Info'
    fields = ('phone', 'address', 'state', 'gstin', 'contact_type')
    # Optional: make some fields readonly if needed
    # readonly_fields = ('phone',)

# Unregister default User admin
admin.site.unregister(User)

@admin.register(User)
class CustomUserAdmin(UserAdmin):
    """
    Custom User Admin to display phone number from related Contact.
    """
    list_display = (
        'username', 
        'email', 
        'first_name', 
        'last_name', 
        'is_staff', 
        'is_active', 
        'get_phone'
    )
    list_filter = ('is_staff', 'is_active')
    search_fields = ('username', 'email', 'first_name', 'last_name')
    
    # Add Contact inline to edit phone directly
    inlines = [ContactInline]
    
    def get_phone(self, obj):
        """Return phone number from related Contact, if exists."""
        try:
            return obj.customer_contact.phone
        except Contact.DoesNotExist:
            return '-'
    get_phone.short_description = 'Phone'
    get_phone.admin_order_field = 'customer_contact__phone'  # Allow ordering (if needed)
    
@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Read-mostly admin for AuditLog with bulk-delete capability."""

    list_display = (
        'timestamp',
        'action_badge',
        'content_type',
        'object_id',
        'user_display',
        'ip_address',
        'short_changes',
    )
    list_filter = (
        'action',
        'content_type',
        'timestamp',
    )
    search_fields = (
        'object_id',
        'user__username',
        'user__email',
        'ip_address',
    )
    date_hierarchy = 'timestamp'
    ordering = ('-timestamp',)
    list_per_page = 50
    show_full_result_count = True

    readonly_fields = (
        'content_type',
        'object_id',
        'action',
        'user',
        'changes_pretty',
        'timestamp',
        'ip_address',
        'user_agent',
    )

    fieldsets = (
        ('Target', {
            'fields': ('content_type', 'object_id', 'action'),
        }),
        ('Actor', {
            'fields': ('user', 'ip_address', 'user_agent'),
        }),
        ('Change Details', {
            'fields': ('changes_pretty',),
        }),
        ('Timing', {
            'fields': ('timestamp',),
        }),
    )

    # ─── Permissions ──────────────────────────────────
    def has_add_permission(self, request):
        """Logs are auto-generated; never allow manual create."""
        return False

    def has_change_permission(self, request, obj=None):
        """View-only — changes must go through normal business flows."""
        return False

    def has_delete_permission(self, request, obj=None):
        """Only superusers can delete audit logs."""
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        """Only superusers can even view logs."""
        return request.user.is_superuser

    # ─── Custom Columns ───────────────────────────────
    @admin.display(description='Action', ordering='action')
    def action_badge(self, obj):
        colors = {
            'CREATE': '#198754',       # green
            'UPDATE': '#0d6efd',       # blue
            'DELETE': '#dc3545',       # red
            'SOFT_DELETE': '#fd7e14',  # orange
            'RESTORE': '#6f42c1',      # purple
        }
        color = colors.get(obj.action, '#6c757d')
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 8px;'
            'border-radius:4px;font-size:11px;font-weight:600;">{}</span>',
            color, obj.action,
        )

    @admin.display(description='User')
    def user_display(self, obj):
        if not obj.user:
            return '—'
        name = obj.user.get_full_name() or obj.user.username
        return format_html(
            '{} <small style="color:#6c757d;">({})</small>',
            name, obj.user.username,
        )

    @admin.display(description='Changes Preview')
    def short_changes(self, obj):
        if not obj.changes:
            return '—'
        try:
            items = list(obj.changes.items())[:3]
            preview = ', '.join(f'{k}: {v}' for k, v in items)
            if len(preview) > 70:
                preview = preview[:70] + '...'
            return preview
        except Exception:
            return str(obj.changes)[:70]

    @admin.display(description='Changes')
    def changes_pretty(self, obj):
        if not obj.changes:
            return '—'
        try:
            import json
            pretty = json.dumps(obj.changes, indent=2, default=str)
            return format_html(
                '<pre style="background:#f8f9fa;padding:10px;'
                'border-radius:6px;max-height:300px;overflow:auto;'
                'font-size:12px;margin:0;">{}</pre>',
                pretty,
            )
        except Exception:
            return str(obj.changes)
    


@admin.register(BlogCategory)
class BlogCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'order', 'is_active', 'is_deleted')
    list_filter = ('is_active', 'is_deleted')
    search_fields = ('name',)
    prepopulated_fields = {'slug': ('name',)}


@admin.register(BlogTag)
class BlogTagAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug')
    search_fields = ('name',)
    prepopulated_fields = {'slug': ('name',)}


@admin.register(BlogPost)
class BlogPostAdmin(admin.ModelAdmin):
    list_display = ('title', 'author', 'category', 'status',
                    'published_at', 'views', 'reading_time', 'is_featured')
    list_filter = ('status', 'is_featured', 'category', 'tags', 'is_deleted')
    search_fields = ('title', 'excerpt', 'body')
    prepopulated_fields = {'slug': ('title',)}
    date_hierarchy = 'published_at'
    filter_horizontal = ('tags',)
    readonly_fields = ('views', 'reading_time', 'created_at', 'updated_at')
    fieldsets = (
        ('Identity', {
            'fields': ('title', 'slug', 'author', 'category', 'tags')
        }),
        ('Content', {
            'fields': ('excerpt', 'body', 'cover_image')
        }),
        ('Publication', {
            'fields': ('status', 'published_at', 'is_featured')
        }),
        ('SEO', {
            'classes': ('collapse',),
            'fields': ('meta_title', 'meta_description',
                       'meta_keywords', 'canonical_url'),
        }),
        ('Metrics', {
            'classes': ('collapse',),
            'fields': ('views', 'reading_time', 'created_at', 'updated_at'),
        }),
    )

