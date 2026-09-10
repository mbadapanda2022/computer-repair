# accounting/models.py

from decimal import Decimal
import json
import logging
from django.db import models, transaction, IntegrityError
from django.conf import settings
from django.db.models import F, Sum, Q, Max
from django.core.validators import MinValueValidator, RegexValidator
from django.utils import timezone
from django.db.models.signals import post_save, post_delete, pre_save, m2m_changed
from django.dispatch import receiver
from django.contrib.auth.models import User
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError

logger = logging.getLogger(__name__)

# --- CONSTANTS ---
MONEY_ZERO = Decimal('0.00')
TAX_PRECISION = Decimal('0.01')
POSITIVE_VALIDATOR = [MinValueValidator(MONEY_ZERO)]


# ============================================================
# 0. BASE MODELS (Soft Delete & Audit)
# ============================================================

class SoftDeleteManager(models.Manager):
    """Manager that excludes soft-deleted records by default."""
    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)
    
    def all_with_deleted(self):
        return super().get_queryset()
    
    def deleted_only(self):
        return super().get_queryset().filter(is_deleted=True)


class SoftDeleteModel(models.Model):
    """
    Abstract base model for soft delete functionality.
    """
    is_deleted = models.BooleanField(default=False, help_text="Soft delete flag")
    deleted_at = models.DateTimeField(null=True, blank=True, help_text="When was this record deleted")
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='%(class)s_deleted',
        help_text="Who deleted this record"
    )
    
    objects = SoftDeleteManager()
    all_objects = models.Manager()  # Includes deleted records
    
    class Meta:
        abstract = True
    
    def soft_delete(self, user=None):
        """Soft delete this record."""
        self.is_deleted = True
        self.deleted_at = timezone.now()
        if user:
            self.deleted_by = user
        self.save(update_fields=['is_deleted', 'deleted_at', 'deleted_by'])
    
    def restore(self):
        """Restore a soft-deleted record."""
        self.is_deleted = False
        self.deleted_at = None
        self.deleted_by = None
        self.save(update_fields=['is_deleted', 'deleted_at', 'deleted_by'])
    
    def delete(self, *args, **kwargs):
        """Override delete to perform soft delete by default."""
        if not self.is_deleted:
            self.soft_delete()



class AuditLog(models.Model):
    ACTION_CHOICES = (
        ('CREATE', 'Create'),
        ('UPDATE', 'Update'),
        ('DELETE', 'Delete'),
        ('SOFT_DELETE', 'Soft Delete'),
        ('RESTORE', 'Restore'),
    )
    
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    content_object = GenericForeignKey('content_type', 'object_id')
    
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    changes = models.JSONField(default=dict, help_text="JSON diff of changes")
    timestamp = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    
    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['content_type', 'object_id']),
            models.Index(fields=['timestamp']),
            models.Index(fields=['action']),
        ]
    
    def __str__(self):
        return f"{self.action} on {self.content_type} #{self.object_id} by {self.user}"


# ============================================================
# 1. CHART OF ACCOUNTS (COA)
# ============================================================

class AccountGroup(models.Model):
    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(max_length=10, unique=True, help_text="e.g., 1 for Assets, 2 for Liabilities")
    parent = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='children')
    is_active = models.BooleanField(default=True)
    
    class Meta:
        ordering = ['code']
        verbose_name = "Account Group"
        verbose_name_plural = "Account Groups"
    
    def __str__(self):
        return f"{self.code} - {self.name}"


class Account(models.Model):
    ACCOUNT_TYPES = (
        ('asset', 'Asset'),
        ('liability', 'Liability'),
        ('equity', 'Equity'),
        ('income', 'Income/Revenue'),
        ('expense', 'Expense'),
        ('contra_asset', 'Contra Asset'),
    )
    
    code = models.CharField(max_length=20, unique=True, help_text="e.g., 1010 for Cash, 4010 for Sales Revenue")
    name = models.CharField(max_length=100)
    account_type = models.CharField(max_length=15, choices=ACCOUNT_TYPES)
    group = models.ForeignKey(AccountGroup, on_delete=models.PROTECT, related_name='accounts')
    parent = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='children')
    
    # Default Tax Rate for this account (if applicable, e.g., GST on Sales Revenue)
    default_tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    
    is_active = models.BooleanField(default=True)
    is_system = models.BooleanField(default=False, help_text="System accounts cannot be deleted")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['code']
        verbose_name = "Account"
        verbose_name_plural = "Accounts"
    
    def __str__(self):
        return f"{self.code} - {self.name} ({self.get_account_type_display()})"
    
    def save(self, *args, **kwargs):
        if self.is_system and self.pk:
            # Don't allow changing system account names/codes
            old = Account.objects.get(pk=self.pk)
            if old.is_system and (old.code != self.code or old.name != self.name):
                raise ValidationError("System accounts cannot be renamed.")
        super().save(*args, **kwargs)


# Constants for Account Group mapping
GROUP_NAMES = {
    '1': 'Assets',
    '2': 'Liabilities',
    '3': 'Equity',
    '4': 'Income',
    '5': 'Expenses',
}

# Helper function to get or create accounts (used in ledger sync)
def get_account(code, name, account_type, group_code):
    """Get or create an Account by code, creating AccountGroup if needed."""
    group_name = GROUP_NAMES.get(group_code, name)  
    group, _ = AccountGroup.objects.get_or_create(
        code=group_code,
        defaults={'name': group_name}
    )
    account, _ = Account.objects.get_or_create(
        code=code,
        defaults={
            'name': name,
            'account_type': account_type,
            'group': group,
            'is_system': True,
        }
    )
    return account


# ============================================================
# 2. INVOICE COUNTER (Race Condition Safe)
# ============================================================

class InvoiceCounter(models.Model):
    prefix = models.CharField(max_length=10, default="INV")
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Invoice Counter"
        verbose_name_plural = "Invoice Counters"

    @classmethod
    def get_next_number(cls, prefix="INV"):
        max_retries = 5
        for attempt in range(max_retries):
            try:
                with transaction.atomic():
                    counter, created = cls.objects.select_for_update().get_or_create(prefix=prefix)
                    next_num = counter.last_number + 1
                    counter.last_number = next_num
                    counter.save(update_fields=['last_number'])
                    return next_num
            except IntegrityError:
                continue
        raise Exception("Failed to generate unique number after multiple attempts")

    def __str__(self):
        return f"{self.prefix} - {self.last_number:04d}"


# ============================================================
# 3. COMPANY & SETTINGS
# ============================================================

class CompanyProfile(SoftDeleteModel):
    name = models.CharField(max_length=200)
    address = models.TextField(blank=True)
    phone = models.CharField(
        max_length=20,
        null=True,
        blank=True,
        validators=[RegexValidator(r'^\+?\d{10,15}$', message="Enter a valid phone number.")]
    )
    email = models.EmailField(blank=True)
    gstin = models.CharField(max_length=15, blank=True, help_text="Leave blank to disable GST")
    logo = models.ImageField(upload_to='company_logo/', blank=True, null=True)
    invoice_prefix = models.CharField(max_length=10, default="INV")
    invoice_start_number = models.PositiveIntegerField(default=1, help_text="Starting number for the next invoice (auto-incremented)")
    default_tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=18.00, validators=POSITIVE_VALIDATOR)
    financial_year_start = models.DateField(default=timezone.now)
    state = models.CharField(max_length=100, blank=True)
    tagline = models.CharField(max_length=255, blank=True, default="Expert chip-level repair, sales, and service for all brands.")
    hero_image = models.ImageField(upload_to='company_hero/', blank=True, null=True)
    about_text = models.TextField(blank=True)
    google_map_embed = models.TextField(blank=True)
    working_hours = models.CharField(max_length=100, blank=True, default="Mon–Sat: 10:00 AM – 8:00 PM")
    facebook_url = models.URLField(blank=True)
    blog_url = models.URLField(blank=True)
    instagram_url = models.URLField(blank=True)
    youtube_url = models.URLField(blank=True)
    whatsapp_number = models.CharField(max_length=20, blank=True)
    google_review_link = models.URLField(blank=True)
    meta_title = models.CharField(max_length=70, blank=True)
    meta_description = models.CharField(max_length=160, blank=True)
    meta_keywords = models.CharField(max_length=200, blank=True)
    og_image = models.ImageField(upload_to='og_images/', blank=True, null=True)

    class Meta:
        verbose_name_plural = "Company Profile"

    def clean(self):
        if not self.pk and CompanyProfile.objects.exists():
            raise ValidationError("Only one company profile can exist.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    @classmethod
    def get_instance(cls):
        obj = cls.objects.first()
        if not obj:
            obj = cls.objects.create(name="My Company")
        return obj

    def __str__(self):
        return self.name


# ============================================================
# 4. SERVICES, TESTIMONIALS, FAQ (Unchanged, just use SoftDelete)
# ============================================================

class Service(SoftDeleteModel):
    title = models.CharField(max_length=200)
    description = models.TextField()
    icon = models.CharField(max_length=50, blank=True)
    image = models.ImageField(upload_to='services/', blank=True, null=True)
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['order', 'created_at']

    def __str__(self):
        return self.title


class Testimonial(SoftDeleteModel):
    RATING_CHOICES = [(i, '⭐ ' * i + str(i) + ' Star' + ('s' if i>1 else '')) for i in range(1,6)]
    customer_name = models.CharField(max_length=100)
    customer_photo = models.ImageField(upload_to='testimonials/', blank=True, null=True)
    designation = models.CharField(max_length=100, blank=True)
    company_name = models.CharField(max_length=100, blank=True)
    review_text = models.TextField()
    rating = models.PositiveSmallIntegerField(choices=RATING_CHOICES, default=5)
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['order', '-created_at']

    def __str__(self):
        return self.customer_name


class FAQ(SoftDeleteModel):
    question = models.CharField(max_length=300)
    answer = models.TextField()
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['order', 'created_at']

    def __str__(self):
        return self.question


# ============================================================
# 5. LEDGER (Double-Entry) – Updated with Account FK
# ============================================================

class LedgerEntry(SoftDeleteModel):
    ENTRY_TYPE = (
        ('sales', 'Sales'), ('purchase', 'Purchase'), ('payment', 'Payment'),
        ('receipt', 'Receipt'), ('contra', 'Contra'), ('journal', 'Journal'),
        ('opening', 'Opening Balance'),
        ('advance_received', 'Advance Received'),
        ('advance_paid', 'Advance Paid'),
        ('discount', 'Discount Adjustment'),
    )
    JOURNAL_TYPES = (
        ('discount', 'Discount'), ('advance_received', 'Advance Received'),
        ('advance_paid', 'Advance Paid'), ('payment', 'Payment to Vendor'),
        ('receipt', 'Receipt from Customer'), ('general', 'General Journal'),
    )
    date = models.DateField(default=timezone.now)
    entry_type = models.CharField(max_length=20, choices=ENTRY_TYPE)
    journal_type = models.CharField(max_length=20, choices=JOURNAL_TYPES, blank=True, null=True)
    reference_id = models.PositiveIntegerField(blank=True, null=True)
    description = models.CharField(max_length=200)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date']
        indexes = [
            models.Index(fields=['entry_type', 'reference_id']),
            models.Index(fields=['date']),
            models.Index(fields=['journal_type']),
        ]

    def clean(self):
        # If this is a new unsaved entry, skip validation (lines will be added later)
        if not self.pk:
            return
        total_debit = self.lines.aggregate(Sum('debit'))['debit__sum'] or Decimal('0')
        total_credit = self.lines.aggregate(Sum('credit'))['credit__sum'] or Decimal('0')
        if total_debit.quantize(TAX_PRECISION) != total_credit.quantize(TAX_PRECISION):
            raise ValidationError(f"Debit ({total_debit}) and Credit ({total_credit}) must be equal.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.entry_type} - {self.description} (₹{self.total_amount})"


class LedgerLine(SoftDeleteModel):
    ledger_entry = models.ForeignKey(LedgerEntry, on_delete=models.CASCADE, related_name='lines')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='ledger_lines')
    contact = models.ForeignKey('Contact', on_delete=models.SET_NULL, null=True, blank=True, related_name='ledger_lines')
    debit = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    credit = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)

    
    class Meta:
        indexes = [
            models.Index(
                fields=['account'],
                name='accounting__account_f344c8_idx',
            ),
            models.Index(
                fields=['contact'],
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(debit__gte=0),
                name='ledgerline_debit_positive',
            ),
            models.CheckConstraint(
                condition=Q(credit__gte=0),
                name='ledgerline_credit_positive',
            ),
        ]

    def __str__(self):
        return f"{self.account.name} Dr:{self.debit} Cr:{self.credit}"


# ============================================================
# 6. CONTACTS (Customer/Vendor) – SoftDelete added
# ============================================================

class Contact(SoftDeleteModel):
    CONTACT_TYPE = (('customer', 'Customer'), ('vendor', 'Vendor'), ('both', 'Both'))
    contact_type = models.CharField(max_length=10, choices=CONTACT_TYPE)
    name = models.CharField(max_length=200)
    company_name = models.CharField(max_length=200, blank=True)
    phone = models.CharField(max_length=20, null=True, blank=True, validators=[RegexValidator(r'^\+?\d{10,15}$')])
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    gstin = models.CharField(max_length=15, blank=True)
    state = models.CharField(max_length=100, blank=True)
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    advance_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="Net advance balance (Customer: positive = advance received, Vendor: positive = advance paid)")
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='customer_contact')

    class Meta:
        ordering = ['name']
        constraints = [models.UniqueConstraint(fields=['phone'], condition=Q(phone__isnull=False), name='unique_phone_non_null')]
        indexes = [models.Index(fields=['name']), models.Index(fields=['contact_type']), models.Index(fields=['balance'])]

    def __str__(self):
        return f"{self.name} ({self.get_contact_type_display()})"

    def recalc_balance(self):
        dr = self.ledger_lines.aggregate(total=Sum('debit'))['total'] or Decimal('0')
        cr = self.ledger_lines.aggregate(total=Sum('credit'))['total'] or Decimal('0')
        if self.contact_type in ('customer', 'both'):
            new_bal = dr - cr
        else:
            new_bal = cr - dr
        Contact.objects.filter(pk=self.pk).update(balance=new_bal.quantize(TAX_PRECISION))
        self.balance = new_bal.quantize(TAX_PRECISION)

    def recalc_advance_balance(self):
        """Recalculate advance balance from advance payment entries."""
        if self.contact_type in ('customer', 'both'):
            total_advance = self.payments.filter(is_advance=True, direction='received').aggregate(total=Sum('amount'))['total'] or Decimal('0')
            total_settled = AdvanceAdjustment.objects.filter(payment__contact=self, payment__is_advance=True).aggregate(total=Sum('amount'))['total'] or Decimal('0')
            self.advance_balance = (total_advance - total_settled).quantize(TAX_PRECISION)
        else:
            total_advance = self.payments.filter(is_advance=True, direction='paid').aggregate(total=Sum('amount'))['total'] or Decimal('0')
            total_settled = AdvanceAdjustment.objects.filter(payment__contact=self, payment__is_advance=True).aggregate(total=Sum('amount'))['total'] or Decimal('0')
            self.advance_balance = (total_advance - total_settled).quantize(TAX_PRECISION)
        self.save(update_fields=['advance_balance'])


# ============================================================
# 7. INVENTORY & STOCK
# ============================================================

class ProductCategory(SoftDeleteModel):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "Product Categories"
        ordering = ['name']

    def __str__(self):
        return self.name


class Product(SoftDeleteModel):
    UNIT_CHOICES = (('pcs', 'Pieces'), ('kg', 'Kilograms'), ('ltr', 'Litres'), ('hrs', 'Hours'), ('nos', 'Numbers'))
    name = models.CharField(max_length=200)
    hsn_code = models.CharField(max_length=10, blank=True)
    category = models.ForeignKey(ProductCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name='products')
    unit = models.CharField(max_length=10, choices=UNIT_CHOICES, default='pcs')
    purchase_price = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    selling_price = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    current_stock = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    low_stock_threshold = models.PositiveIntegerField(default=5)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=18.00, validators=POSITIVE_VALIDATOR)
    is_service = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        indexes = [models.Index(fields=['name']), models.Index(fields=['is_active'])]

    def __str__(self):
        return f"{self.name} ({self.hsn_code or 'N/A'})"

    def low_stock_alert(self):
        if self.is_service:
            return False
        return self.current_stock <= self.low_stock_threshold


class StockMovement(SoftDeleteModel):
    MOVEMENT_TYPE = (
        ('purchase_in', 'Purchase In'), ('sale_out', 'Sale Out'),
        ('repair_out', 'Repair Usage Out'), ('return_in', 'Return In'),
        ('adjustment', 'Stock Adjustment'),
    )
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='stock_movements')
    movement_type = models.CharField(max_length=15, choices=MOVEMENT_TYPE)
    quantity = models.DecimalField(max_digits=12, decimal_places=2, help_text="Positive for stock-in, negative for stock-out")
    
    source_content_type = models.ForeignKey(ContentType, on_delete=models.SET_NULL, null=True, blank=True)
    source_object_id = models.PositiveIntegerField(null=True, blank=True)
    source_object = GenericForeignKey('source_content_type', 'source_object_id')
    
    reference = models.CharField(max_length=200, blank=True, help_text="Legacy or manual reference")
    date = models.DateTimeField(default=timezone.now)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-date']
        indexes = [
            models.Index(fields=['product']),
            models.Index(fields=['movement_type']),
            models.Index(fields=['source_content_type', 'source_object_id']),
        ]
        constraints = [
            models.UniqueConstraint(fields=['source_content_type', 'source_object_id'], name='unique_stock_movement_source')
        ]

    def __str__(self):
        return f"{self.product.name} {self.get_movement_type_display()} ({self.quantity})"

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        old_qty = Decimal('0')
        if not is_new:
            try:
                old_qty = StockMovement.objects.get(pk=self.pk).quantity
            except StockMovement.DoesNotExist:
                pass

        super().save(*args, **kwargs)

        delta = self.quantity - old_qty
        if delta != 0 and not self.product.is_service:
            Product.objects.filter(pk=self.product_id).update(current_stock=F('current_stock') + delta)

    def delete(self, *args, **kwargs):
        """Soft delete and reverse stock effect."""
        if not self.is_deleted:
            if not self.product.is_service:
                Product.objects.filter(pk=self.product_id).update(
                    current_stock=F('current_stock') - self.quantity
                )
            self.soft_delete()


# ============================================================
# 8. ADVANCE PAYMENT & SETTLEMENT
# ============================================================

class AdvanceAdjustment(SoftDeleteModel):
    payment = models.ForeignKey('Payment', on_delete=models.CASCADE, related_name='advance_adjustments')
    invoice = models.ForeignKey('Invoice', on_delete=models.CASCADE, related_name='advance_adjustments')
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=POSITIVE_VALIDATOR)
    date = models.DateField(default=timezone.now)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [['payment', 'invoice']]
        indexes = [models.Index(fields=['payment', 'invoice'])]

    def __str__(self):
        return f"Advance {self.payment.id} -> Invoice {self.invoice.invoice_number}: ₹{self.amount}"


# ============================================================
# 9. SALES INVOICE (With Soft Delete & Advance Support)
# ============================================================

def sync_invoice_ledger(invoice):
    """
    Enterprise Discount + Advance Adjustment Support
    """
    with transaction.atomic():
        entry, created = LedgerEntry.objects.get_or_create(
            entry_type='sales',
            reference_id=invoice.id,
            defaults={
                'date': invoice.date,
                'description': f"Invoice {invoice.invoice_number}",
                'total_amount': invoice.grand_total
            }
        )
        
        if not created:
            entry.date = invoice.date
            entry.description = f"Invoice {invoice.invoice_number}"
            entry.total_amount = invoice.grand_total
            entry.save()
        
        entry.lines.all().delete()
        
        gross_amount = invoice.subtotal + invoice.tax_amount
        discount = invoice.discount_amount or Decimal('0.00')
        
        # Get account objects using helper
        sales_revenue = get_account('4010', 'Sales Revenue', 'income', '4')
        gst_payable = get_account('2010', 'GST Payable', 'liability', '2')
        discount_allowed = get_account('5010', 'Discount Allowed', 'expense', '5')
        customer_account = get_account('1011', 'Customer Receivable', 'asset', '1')
        
        # 1. Debit: Customer (Gross Amount)
        LedgerLine.objects.create(
            ledger_entry=entry,
            account=customer_account,
            contact=invoice.customer,
            debit=gross_amount,
            credit=0
        )
        
        # 2. Credit: Sales Revenue
        if invoice.subtotal > 0:
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=sales_revenue,
                debit=0,
                credit=invoice.subtotal
            )
        
        # 3. Credit: GST Payable
        if invoice.tax_amount > 0:
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=gst_payable,
                debit=0,
                credit=invoice.tax_amount
            )
        
        # 4. Discount Adjustment (if any)
        if discount > 0:
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=discount_allowed,
                debit=discount,
                credit=0
            )
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=customer_account,
                contact=invoice.customer,
                debit=0,
                credit=discount
            )
        
        # 5. Advance Adjustment (if any)
        total_advance_settled = invoice.advance_adjustments.aggregate(total=Sum('amount'))['total'] or Decimal('0')
        if total_advance_settled > 0:
            advance_account = get_account('1012', 'Advance from Customer', 'liability', '2')
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=advance_account,
                debit=total_advance_settled,
                credit=0
            )
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=customer_account,
                contact=invoice.customer,
                debit=0,
                credit=total_advance_settled
            )
        
        # Validate entry
        entry.full_clean()


class Invoice(SoftDeleteModel):
    GST_TYPE = (('regular', 'Regular (B2B)'), ('non_gst', 'Non-GST'), ('interstate', 'Inter-state (IGST)'), ('intrastate', 'Intra-state (CGST+SGST)'))
    PAYMENT_STATUS = (('paid', 'Paid'), ('partial', 'Partially Paid'), ('unpaid', 'Unpaid'))
    
    DISCOUNT_TYPE_CHOICES = (
        ('early_payment', 'Early Payment Discount'),
        ('volume', 'Volume/Bulk Discount'),
        ('seasonal', 'Seasonal Sale'),
        ('damage', 'Damaged Goods'),
        ('staff', 'Staff Discount'),
        ('other', 'Other'),
    )

    invoice_number = models.CharField(max_length=50, unique=True, editable=False)
    customer = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name='sales_invoices', limit_choices_to={'contact_type__in': ['customer', 'both']})
    date = models.DateField(default=timezone.now)
    due_date = models.DateField(blank=True, null=True)
    gst_type = models.CharField(max_length=12, choices=GST_TYPE, default='regular')
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    grand_total = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    balance_due = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    payment_status = models.CharField(max_length=10, choices=PAYMENT_STATUS, default='unpaid')
    notes = models.TextField(blank=True)
    
    # Discount Audit Trail
    discount_note = models.TextField(blank=True, help_text="Reason for discount (e.g., Early payment, Bulk order)")
    discount_date = models.DateField(null=True, blank=True, help_text="When was discount approved/given")
    discount_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_sales_discounts',
        help_text="Staff/Admin who approved this discount"
    )
    discount_type = models.CharField(
        max_length=20,
        blank=True,
        choices=DISCOUNT_TYPE_CHOICES,
        help_text="Type/Reason for discount"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date', '-invoice_number']
        indexes = [
            models.Index(fields=['invoice_number']),
            models.Index(fields=['customer', 'payment_status']),
            models.Index(fields=['discount_type']),
            models.Index(fields=['discount_date']),
        ]

    def __str__(self):
        return f"Invoice {self.invoice_number} - {self.customer.name}"

    def save(self, *args, **kwargs):
        if not self.invoice_number:
            prefix = CompanyProfile.get_instance().invoice_prefix or "INV"
            next_num = InvoiceCounter.get_next_number(prefix)
            self.invoice_number = f"{prefix}-{next_num:04d}"

        is_new = self.pk is None
        super().save(*args, **kwargs)

        if self.pk and self.items.exists():
            self.calculate_totals()
            total_advance = self.advance_adjustments.aggregate(total=Sum('amount'))['total'] or Decimal('0')
            total_paid = self.paid_amount + total_advance
            self.balance_due = (self.grand_total - total_paid).quantize(TAX_PRECISION)
            if self.balance_due <= 0:
                self.payment_status = 'paid'
            elif total_paid > 0 and self.balance_due < self.grand_total:
                self.payment_status = 'partial'
            else:
                self.payment_status = 'unpaid'
            super().save(update_fields=['subtotal', 'tax_amount', 'grand_total', 'balance_due', 'payment_status'])

            sync_invoice_ledger(self)
            if self.customer:
                self.customer.recalc_balance()
                self.customer.recalc_advance_balance()

    def calculate_totals(self):
        if not self.pk:
            return
        items = self.items.all()
        self.subtotal = sum(item.quantity * item.unit_price for item in items)
        self.tax_amount = sum(item.tax_amount for item in items)
        discounted_subtotal = max(Decimal('0'), self.subtotal - self.discount_amount)
        self.grand_total = (discounted_subtotal + self.tax_amount).quantize(TAX_PRECISION)
        self.subtotal = self.subtotal.quantize(TAX_PRECISION)
        self.tax_amount = self.tax_amount.quantize(TAX_PRECISION)


class InvoiceItem(SoftDeleteModel):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    description = models.CharField(max_length=200, blank=True)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=1, validators=[MinValueValidator(Decimal('0.01'))])
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, validators=POSITIVE_VALIDATOR)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)

    class Meta:
        indexes = [models.Index(fields=['invoice', 'product'])]

    @transaction.atomic
    def save(self, *args, **kwargs):
        if not self.unit_price:
            self.unit_price = self.product.selling_price

        if self.invoice and self.invoice.gst_type == 'non_gst':
            self.tax_rate = Decimal('0')
            self.tax_amount = Decimal('0')
        else:
            if not self.tax_rate:
                self.tax_rate = self.product.tax_rate
            line_amount = self.quantity * self.unit_price
            self.tax_amount = ((line_amount * self.tax_rate) / 100).quantize(TAX_PRECISION)

        line_amount = self.quantity * self.unit_price
        self.line_total = (line_amount + self.tax_amount).quantize(TAX_PRECISION)

        super().save(*args, **kwargs)

        if not self.product.is_service:
            StockMovement.objects.update_or_create(
                source_content_type=ContentType.objects.get_for_model(self),
                source_object_id=self.pk,
                defaults={
                    'product': self.product,
                    'movement_type': 'sale_out',
                    'quantity': (-self.quantity).quantize(TAX_PRECISION),
                    'date': self.invoice.date
                }
            )

        if self.invoice:
            self.invoice.calculate_totals()
            # Recalculate balance and payment status
            total_advance = self.invoice.advance_adjustments.aggregate(total=Sum('amount'))['total'] or Decimal('0')
            total_paid = self.invoice.paid_amount + total_advance
            self.invoice.balance_due = (self.invoice.grand_total - total_paid).quantize(TAX_PRECISION)
            if self.invoice.balance_due <= 0:
                self.invoice.payment_status = 'paid'
            elif total_paid > 0 and self.invoice.balance_due < self.invoice.grand_total:
                self.invoice.payment_status = 'partial'
            else:
                self.invoice.payment_status = 'unpaid'
            
            self.invoice.save(update_fields=['subtotal', 'tax_amount', 'grand_total', 'balance_due', 'payment_status'])
            
            if self.invoice.customer:
                self.invoice.customer.recalc_balance()
    
    def delete(self, *args, **kwargs):
        if not self.product.is_service:
            StockMovement.objects.filter(
                source_content_type=ContentType.objects.get_for_model(self),
                source_object_id=self.pk
            ).delete()
        invoice = self.invoice
        super().delete(*args, **kwargs)
        if invoice:
            invoice.calculate_totals()
            total_advance = invoice.advance_adjustments.aggregate(total=Sum('amount'))['total'] or Decimal('0')
            total_paid = invoice.paid_amount + total_advance
            invoice.balance_due = (invoice.grand_total - total_paid).quantize(TAX_PRECISION)
            if invoice.balance_due <= 0:
                invoice.payment_status = 'paid'
            elif total_paid > 0 and invoice.balance_due < invoice.grand_total:
                invoice.payment_status = 'partial'
            else:
                invoice.payment_status = 'unpaid'
            
            invoice.save(update_fields=['subtotal', 'tax_amount', 'grand_total', 'balance_due', 'payment_status'])
            
            if invoice.customer:
                invoice.customer.recalc_balance()


# ============================================================
# 10. PURCHASE INVOICE (With Soft Delete & Advance Support)
# ============================================================

def sync_purchase_ledger(purchase):
    """
    Enterprise Purchase Ledger Sync with Freight, Discount, Advance, and Office Use.
    Now handles unsaved entry gracefully.
    """
    with transaction.atomic():
        # ===== STEP 1: Ensure Purchase is Saved =====
        if not purchase.pk:
            purchase.save()

        # ===== STEP 2: Get or Create Ledger Entry =====
        entry, created = LedgerEntry.objects.get_or_create(
            entry_type='purchase',
            reference_id=purchase.id,
            defaults={
                'date': purchase.date,
                'description': f"Purchase {purchase.purchase_number}",
                'total_amount': purchase.grand_total
            }
        )

        # Force Save (just in case get_or_create didn't save properly)
        entry.save()

        # ===== STEP 3: Delete Old Lines =====
        LedgerLine.objects.filter(ledger_entry=entry).delete()

        # ===== STEP 4: Create New Lines =====
        # Freight / Carriage Inward
        if purchase.freight_charge > 0:
            freight_account = get_account('5012', 'Carriage Inward', 'expense', '5')
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=freight_account,
                debit=purchase.freight_charge,
                credit=0
            )

        gross_amount = purchase.subtotal + purchase.tax_amount + purchase.freight_charge
        discount = purchase.discount_amount or Decimal('0.00')

        # Get accounts
        purchases_account = get_account('5011', 'Purchases', 'expense', '5')
        gst_input = get_account('1013', 'GST Input', 'asset', '1')
        vendor_account = get_account('2011', 'Vendor Payable', 'liability', '2')
        discount_received = get_account('4011', 'Discount Received', 'income', '4')

        # Debit: Purchases / Office Expenses (Item-wise)
        for item in purchase.items.all():
            if item.is_office_use:
                office_expense_account = get_account('5013', 'Office Expenses', 'expense', '5')
                LedgerLine.objects.create(
                    ledger_entry=entry,
                    account=office_expense_account,
                    debit=item.quantity * item.unit_price,
                    credit=0
                )
            else:
                LedgerLine.objects.create(
                    ledger_entry=entry,
                    account=purchases_account,
                    debit=item.quantity * item.unit_price,
                    credit=0
                )

        # Debit: GST Input
        if purchase.tax_amount > 0:
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=gst_input,
                debit=purchase.tax_amount,
                credit=0
            )

        # Credit: Vendor (Gross)
        LedgerLine.objects.create(
            ledger_entry=entry,
            account=vendor_account,
            contact=purchase.vendor,
            debit=0,
            credit=gross_amount
        )

        # Discount Adjustment (if any from vendor)
        if discount > 0:
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=vendor_account,
                contact=purchase.vendor,
                debit=discount,
                credit=0
            )
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=discount_received,
                debit=0,
                credit=discount
            )

        # Advance Adjustment (if any)
        total_advance_settled = purchase.advance_adjustments.aggregate(total=Sum('amount'))['total'] or Decimal('0')
        if total_advance_settled > 0:
            advance_account = get_account('1014', 'Advance to Vendor', 'asset', '1')
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=advance_account,
                credit=total_advance_settled,
                debit=0
            )
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=vendor_account,
                contact=purchase.vendor,
                debit=0,
                credit=total_advance_settled
            )

        # Validate entry
        entry.full_clean()


class Purchase(SoftDeleteModel):
    PURCHASE_GST_TYPE = (('regular', 'Regular'), ('non_gst', 'Non-GST'), ('interstate', 'Inter-state'), ('intrastate', 'Intra-state'))
    
    DISCOUNT_TYPE_CHOICES = (
        ('early_payment', 'Early Payment Discount (Vendor)'),
        ('volume', 'Volume/Bulk Discount (Vendor)'),
        ('seasonal', 'Seasonal Sale (Vendor)'),
        ('damage', 'Damaged Goods (Vendor)'),
        ('trade', 'Trade Discount'),
        ('other', 'Other'),
    )
    
    purchase_number = models.CharField(max_length=50, unique=True, editable=False)
    vendor = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name='purchases', limit_choices_to={'contact_type__in': ['vendor', 'both']})
    date = models.DateField(default=timezone.now)
    gst_type = models.CharField(max_length=12, choices=PURCHASE_GST_TYPE, default='regular')
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    freight_charge = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR, help_text="Transport / Carriage Inward charges")
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    grand_total = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    paid = models.BooleanField(default=False)
    notes = models.TextField(blank=True)
    
    # Discount Audit Trail
    discount_note = models.TextField(blank=True, help_text="Reason for discount from vendor")
    discount_date = models.DateField(null=True, blank=True, help_text="When was discount approved/given by vendor")
    discount_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_purchase_discounts',
        help_text="Staff/Admin who recorded this discount"
    )
    discount_type = models.CharField(
        max_length=20,
        blank=True,
        choices=DISCOUNT_TYPE_CHOICES,
        help_text="Type/Reason for vendor discount"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date']
        indexes = [
            models.Index(fields=['purchase_number']),
            models.Index(fields=['vendor']),
            models.Index(fields=['discount_type']),
            models.Index(fields=['discount_date']),
        ]
        
    def save(self, *args, **kwargs):
        if not self.purchase_number:
            for _ in range(5):
                try:
                    next_num = InvoiceCounter.get_next_number("PUR")
                    self.purchase_number = f"PUR-{next_num:04d}"
                    super().save(*args, **kwargs)
                    return  # Success
                except IntegrityError:
                    continue
            raise IntegrityError("Unable to generate unique purchase number after retries")
        super().save(*args, **kwargs)


    def calculate_totals(self):
        items = self.items.all()
        self.subtotal = sum(item.quantity * item.unit_price for item in items)
        self.tax_amount = sum(item.tax_amount for item in items)
        discounted_subtotal = max(Decimal('0'), self.subtotal - self.discount_amount)
        self.grand_total = (discounted_subtotal + self.tax_amount + self.freight_charge).quantize(TAX_PRECISION)
        self.subtotal = self.subtotal.quantize(TAX_PRECISION)
        self.tax_amount = self.tax_amount.quantize(TAX_PRECISION)


class PurchaseItem(SoftDeleteModel):
    purchase = models.ForeignKey(Purchase, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    description = models.CharField(max_length=200, blank=True)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=1, validators=[MinValueValidator(Decimal('0.01'))])
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, validators=POSITIVE_VALIDATOR)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    is_office_use = models.BooleanField(default=False, help_text="Check if this item is for office consumption (not for resale)")

    @transaction.atomic
    def save(self, *args, **kwargs):
        if not self.unit_price:
            self.unit_price = self.product.purchase_price
        if not self.tax_rate:
            self.tax_rate = self.product.tax_rate

        line_amount = self.quantity * self.unit_price
        self.tax_amount = ((line_amount * self.tax_rate) / 100).quantize(TAX_PRECISION)
        self.line_total = (line_amount + self.tax_amount).quantize(TAX_PRECISION)

        super().save(*args, **kwargs)

        if not self.product.is_service:
            StockMovement.objects.update_or_create(
                source_content_type=ContentType.objects.get_for_model(self),
                source_object_id=self.pk,
                defaults={
                    'product': self.product,
                    'movement_type': 'purchase_in',
                    'quantity': self.quantity.quantize(TAX_PRECISION),
                    'date': self.purchase.date
                }
            )

        if self.purchase:
            self.purchase.calculate_totals()
            self.purchase.save(update_fields=['subtotal', 'tax_amount', 'grand_total'])
            if self.purchase.vendor:
                self.purchase.vendor.recalc_balance()
    
    def delete(self, *args, **kwargs):
        if not self.product.is_service and not self.is_office_use:
            StockMovement.objects.filter(
                source_content_type=ContentType.objects.get_for_model(self),
                source_object_id=self.pk
            ).delete()
        purchase = self.purchase
        super().delete(*args, **kwargs)
        if purchase:
            purchase.calculate_totals()
            purchase.save(update_fields=['subtotal', 'tax_amount', 'grand_total'])
            if purchase.vendor:
                purchase.vendor.recalc_balance()


# ============================================================
# 11. REPAIR JOBS
# ============================================================

class RepairJob(SoftDeleteModel):
    STATUS_CHOICES = (('pending', 'Pending'), ('diagnosis', 'Diagnosis'), ('repairing', 'Repairing'), ('ready', 'Ready for Delivery'), ('delivered', 'Delivered'), ('cancelled', 'Cancelled'))
    ESTIMATE_STATUS_CHOICES = (('pending', 'Pending Approval'), ('approved', 'Approved'), ('on_hold', 'On Hold'), ('rejected', 'Rejected'))

    job_number = models.CharField(max_length=50, unique=True, editable=False)
    customer = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name='repair_jobs', limit_choices_to={'contact_type__in': ['customer', 'both']})
    device_model = models.CharField(max_length=200)
    serial_number = models.CharField(max_length=100, blank=True)
    issue_description = models.TextField()
    accessories = models.TextField(blank=True, null=True)
    device_condition = models.TextField(blank=True, null=True)
    action_taken = models.TextField(blank=True)
    diagnosis_report = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='pending')
    estimate_status = models.CharField(max_length=10, choices=ESTIMATE_STATUS_CHOICES, default='pending')
    estimate_approved_at = models.DateTimeField(null=True, blank=True)
    estimate_approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_estimates')
    approval_source = models.CharField(max_length=30, blank=True, null=True, choices=[('portal', 'Customer Portal'), ('staff_phone', 'Staff (Phone Call)'), ('staff_inperson', 'Staff (In-Person)'), ('staff_whatsapp', 'Staff (WhatsApp)')])
    approval_remarks = models.TextField(blank=True, null=True)
    estimated_cost = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True, validators=POSITIVE_VALIDATOR)
    final_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    labour_charge = models.DecimalField(max_digits=10, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    received_by = models.CharField(max_length=100, blank=True, null=True)
    delivered_by = models.CharField(max_length=100, blank=True, null=True)
    date_in = models.DateField(default=timezone.now)
    delivery_date = models.DateField(blank=True, null=True)
    delivered_to_name = models.CharField(max_length=200, blank=True, null=True)
    delivered_to_phone = models.CharField(max_length=20, blank=True, null=True)
    delivered_to_designation = models.CharField(max_length=100, blank=True, null=True)
    delivery_remarks = models.TextField(blank=True, null=True)
    invoice = models.OneToOneField(Invoice, on_delete=models.SET_NULL, blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date_in']
        indexes = [
            models.Index(fields=['job_number']),
            models.Index(fields=['customer', 'status']),
            models.Index(fields=['status', 'estimate_status']),
        ]

    def __str__(self):
        return f"Job {self.job_number} - {self.device_model} ({self.customer.name})"

    def calculate_final_amount(self):
        parts_total = self.parts.aggregate(total=Sum('line_total'))['total'] or Decimal('0')
        self.final_amount = (parts_total + self.labour_charge).quantize(TAX_PRECISION)
        self.save(update_fields=['final_amount'])
        return self.final_amount

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        if is_new:
            next_num = InvoiceCounter.get_next_number("REP")
            self.job_number = f"REP-{next_num:04d}"

        old_status = None
        if not is_new:
            try:
                old_status = RepairJob.objects.get(pk=self.pk).status
            except RepairJob.DoesNotExist:
                pass

        if self.pk:
            parts_total = self.parts.aggregate(total=Sum('line_total'))['total'] or Decimal('0')
            self.final_amount = (parts_total + self.labour_charge).quantize(TAX_PRECISION)

        super().save(*args, **kwargs)

        if old_status and old_status != self.status:
            try:
                from django.urls import reverse
                from accounting.utils.notification_helpers import send_notification_to_customer, send_notification_sse
                send_notification_to_customer(
                    self.customer,
                    title=f"Repair Status Updated: {self.job_number}",
                    message=f"Your repair for {self.device_model} is now {self.get_status_display()}.",
                    link=reverse('customer:customer_repair_detail', args=[self.pk]),
                    notif_type='info', category='repairs', send_email=False
                )
                for staff in User.objects.filter(is_staff=True):
                    send_notification_sse(staff)
            except Exception as notif_error:
                logger.error(f"Notification error for job {self.job_number}: {notif_error}")


class RepairPart(SoftDeleteModel):
    repair_job = models.ForeignKey(RepairJob, on_delete=models.CASCADE, related_name='parts')
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, validators=POSITIVE_VALIDATOR)
    line_total = models.DecimalField(max_digits=12, decimal_places=2, editable=False, validators=POSITIVE_VALIDATOR)

    def save(self, *args, **kwargs):
        if not self.unit_price:
            self.unit_price = self.product.selling_price
        self.line_total = (Decimal(self.quantity) * self.unit_price).quantize(TAX_PRECISION)

        super().save(*args, **kwargs)

        # Stock Movement ONLY for physical products
        if not self.product.is_service:
            StockMovement.objects.update_or_create(
                source_content_type=ContentType.objects.get_for_model(self),
                source_object_id=self.pk,
                defaults={
                    'product': self.product,
                    'movement_type': 'repair_out',
                    'quantity': (-Decimal(self.quantity)).quantize(TAX_PRECISION),
                    'date': self.repair_job.date_in
                }
            )

        self.repair_job.calculate_final_amount()
    
    def delete(self, *args, **kwargs):
        if not self.product.is_service:
            StockMovement.objects.filter(
                source_content_type=ContentType.objects.get_for_model(self),
                source_object_id=self.pk
            ).delete()
        repair_job = self.repair_job
        super().delete(*args, **kwargs)
        if repair_job:
            repair_job.calculate_final_amount()


# ============================================================
# 12. BANK ACCOUNTS & TRANSACTIONS
# ============================================================

class BankAccount(SoftDeleteModel):
    ACCOUNT_TYPES = (('savings', 'Savings Account'), ('current', 'Current Account'), ('upi', 'UPI / Wallet'))
    name = models.CharField(max_length=100)
    account_number = models.CharField(max_length=50, blank=True)
    bank_name = models.CharField(max_length=100, blank=True)
    ifsc_code = models.CharField(max_length=20, blank=True)
    account_type = models.CharField(max_length=10, choices=ACCOUNT_TYPES, default='savings')
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    current_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        indexes = [models.Index(fields=['name']), models.Index(fields=['is_active'])]

    def __str__(self):
        return f"{self.name} ({self.bank_name or self.account_type})"


class BankTransaction(SoftDeleteModel):
    TRANSACTION_TYPES = (('deposit', 'Deposit'), ('withdrawal', 'Withdrawal'))
    SOURCE_TYPES = (('payment_received', 'Payment from Customer'), ('payment_made', 'Payment to Vendor'), ('transfer', 'Transfer'), ('interest', 'Interest'), ('bank_charge', 'Bank Charge'), ('manual', 'Manual Entry'))
    bank_account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name='transactions')
    transaction_type = models.CharField(max_length=10, choices=TRANSACTION_TYPES)
    source_type = models.CharField(max_length=20, choices=SOURCE_TYPES, default='manual')
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=POSITIVE_VALIDATOR)
    date = models.DateField(default=timezone.now)
    description = models.CharField(max_length=200, blank=True)
    reference = models.CharField(max_length=100, blank=True)
    
    payment = models.OneToOneField(
        'Payment',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='bank_transaction'
    )
    invoice = models.ForeignKey('Invoice', on_delete=models.SET_NULL, null=True, blank=True)
    purchase = models.ForeignKey('Purchase', on_delete=models.SET_NULL, null=True, blank=True)
    reconciled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-created_at']
        indexes = [models.Index(fields=['bank_account', 'date']), models.Index(fields=['transaction_type'])]

    def __str__(self):
        return f"{self.get_transaction_type_display()} - {self.amount} ({self.bank_account.name})"


# ============================================================
# 13. PAYMENTS (Full Enterprise with Advance, Discount, Soft Delete)
# ============================================================

class PaymentAllocation(SoftDeleteModel):
    """Track exactly how much of a payment is applied to each invoice."""
    payment = models.ForeignKey('Payment', on_delete=models.CASCADE, related_name='allocations')
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name='payment_allocations')
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=POSITIVE_VALIDATOR)

    class Meta:
        unique_together = [['payment', 'invoice']]
        indexes = [models.Index(fields=['payment', 'invoice'])]

    def __str__(self):
        return f"{self.payment.id} -> {self.invoice.invoice_number}: ₹{self.amount}"


class Payment(SoftDeleteModel):
    DIRECTION = (('paid', 'Paid to Supplier'), ('received', 'Received from Customer'))
    METHOD_CHOICES = (('cash', 'Cash'), ('bank', 'Bank Transfer'), ('upi', 'UPI'))
    
    direction = models.CharField(max_length=10, choices=DIRECTION)
    contact = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name='payments')
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=POSITIVE_VALIDATOR)
    date = models.DateField(default=timezone.now)
    method = models.CharField(max_length=10, choices=METHOD_CHOICES, default='cash')
    bank_account = models.ForeignKey(BankAccount, on_delete=models.SET_NULL, null=True, blank=True, related_name='payments')
    upi_ref = models.CharField(max_length=100, blank=True)
    reconciled = models.BooleanField(default=False)
    account_name = models.CharField(max_length=50, blank=True)
    reference = models.CharField(max_length=100, blank=True)
    description = models.CharField(max_length=200, blank=True)
    
    is_advance = models.BooleanField(default=False, help_text="Is this an advance payment?")
    
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    discount_note = models.TextField(blank=True, help_text="Reason for discount on payment")
    discount_type = models.CharField(
        max_length=20,
        blank=True,
        choices=[
            ('early_payment', 'Early Payment Discount'),
            ('cash_discount', 'Cash Discount'),
            ('other', 'Other'),
        ]
    )
    discount_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_payment_discounts'
    )
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date']
        indexes = [
            models.Index(fields=['contact', 'direction']),
            models.Index(fields=['bank_account']),
            models.Index(fields=['is_advance']),
        ]

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        with transaction.atomic():
            super().save(*args, **kwargs)
            if is_new:
                self.create_ledger_entry()
                self.create_bank_transaction()
            else:
                self.update_ledger_entry()
                self.update_bank_transaction()
            
            # Update contact balances
            if self.contact:
                self.contact.recalc_balance()
                self.contact.recalc_advance_balance()
            
            # Update invoice statuses
            self.update_invoices()

    def _get_account_name(self):
        """Get the appropriate account for this payment."""
        if self.bank_account:
            return get_account('1010', 'Bank Account', 'asset', '1')
        return get_account('1001', 'Cash', 'asset', '1')

    def create_ledger_entry(self):
        """Create ledger entry for payment (with advance and discount support)."""
        entry = LedgerEntry.objects.create(
            date=self.date,
            entry_type='advance_received' if self.is_advance and self.direction == 'received' 
                      else 'advance_paid' if self.is_advance and self.direction == 'paid'
                      else 'payment',
            reference_id=self.id,
            description=f"{'Advance' if self.is_advance else 'Payment'} {self.direction} from/to {self.contact.name}",
            total_amount=self.amount
        )
        
        bank_acc = self._get_account_name()
        net_amount = self.amount - self.discount_amount
        
        if self.direction == 'received':
            if self.is_advance:
                LedgerLine.objects.create(ledger_entry=entry, account=bank_acc, debit=net_amount, credit=0)
                advance_acc = get_account('1012', 'Advance from Customer', 'liability', '2')
                LedgerLine.objects.create(ledger_entry=entry, account=advance_acc, contact=self.contact, debit=0, credit=net_amount)
                
                if self.discount_amount > 0:
                    discount_acc = get_account('5010', 'Discount Allowed', 'expense', '5')
                    LedgerLine.objects.create(ledger_entry=entry, account=discount_acc, debit=self.discount_amount, credit=0)
                    customer_acc = get_account('1011', 'Customer Receivable', 'asset', '1')
                    LedgerLine.objects.create(ledger_entry=entry, account=customer_acc, contact=self.contact, debit=0, credit=self.discount_amount)
            else:
                LedgerLine.objects.create(ledger_entry=entry, account=bank_acc, debit=net_amount, credit=0)
                customer_acc = get_account('1011', 'Customer Receivable', 'asset', '1')
                LedgerLine.objects.create(ledger_entry=entry, account=customer_acc, contact=self.contact, debit=0, credit=net_amount)
                
                if self.discount_amount > 0:
                    discount_acc = get_account('5010', 'Discount Allowed', 'expense', '5')
                    LedgerLine.objects.create(ledger_entry=entry, account=discount_acc, debit=self.discount_amount, credit=0)
                    LedgerLine.objects.create(ledger_entry=entry, account=customer_acc, contact=self.contact, debit=0, credit=self.discount_amount)
        else:
            if self.is_advance:
                advance_acc = get_account('1014', 'Advance to Vendor', 'asset', '1')
                LedgerLine.objects.create(ledger_entry=entry, account=advance_acc, contact=self.contact, debit=net_amount, credit=0)
                LedgerLine.objects.create(ledger_entry=entry, account=bank_acc, debit=0, credit=net_amount)
                
                if self.discount_amount > 0:
                    discount_acc = get_account('4011', 'Discount Received', 'income', '4')
                    vendor_acc = get_account('2011', 'Vendor Payable', 'liability', '2')
                    LedgerLine.objects.create(ledger_entry=entry, account=vendor_acc, contact=self.contact, debit=self.discount_amount, credit=0)
                    LedgerLine.objects.create(ledger_entry=entry, account=discount_acc, debit=0, credit=self.discount_amount)
            else:
                vendor_acc = get_account('2011', 'Vendor Payable', 'liability', '2')
                LedgerLine.objects.create(ledger_entry=entry, account=vendor_acc, contact=self.contact, debit=net_amount, credit=0)
                LedgerLine.objects.create(ledger_entry=entry, account=bank_acc, debit=0, credit=net_amount)
                
                if self.discount_amount > 0:
                    discount_acc = get_account('4011', 'Discount Received', 'income', '4')
                    LedgerLine.objects.create(ledger_entry=entry, account=vendor_acc, contact=self.contact, debit=0, credit=self.discount_amount)
                    LedgerLine.objects.create(ledger_entry=entry, account=discount_acc, debit=self.discount_amount, credit=0)

    def update_ledger_entry(self):
        """Update existing ledger entry for this payment."""
        entry = LedgerEntry.objects.filter(reference_id=self.id, entry_type__in=['payment', 'advance_received', 'advance_paid']).first()
        if not entry:
            self.create_ledger_entry()
            return
        
        entry.lines.all().delete()
        entry.date = self.date
        entry.description = f"{'Advance' if self.is_advance else 'Payment'} {self.direction} from/to {self.contact.name}"
        entry.total_amount = self.amount
        entry.save()
        
        bank_acc = self._get_account_name()
        net_amount = self.amount - self.discount_amount
        
        if self.direction == 'received':
            if self.is_advance:
                LedgerLine.objects.create(ledger_entry=entry, account=bank_acc, debit=net_amount, credit=0)
                advance_acc = get_account('1012', 'Advance from Customer', 'liability', '2')
                LedgerLine.objects.create(ledger_entry=entry, account=advance_acc, contact=self.contact, debit=0, credit=net_amount)
                if self.discount_amount > 0:
                    discount_acc = get_account('5010', 'Discount Allowed', 'expense', '5')
                    LedgerLine.objects.create(ledger_entry=entry, account=discount_acc, debit=self.discount_amount, credit=0)
                    customer_acc = get_account('1011', 'Customer Receivable', 'asset', '1')
                    LedgerLine.objects.create(ledger_entry=entry, account=customer_acc, contact=self.contact, debit=0, credit=self.discount_amount)
            else:
                LedgerLine.objects.create(ledger_entry=entry, account=bank_acc, debit=net_amount, credit=0)
                customer_acc = get_account('1011', 'Customer Receivable', 'asset', '1')
                LedgerLine.objects.create(ledger_entry=entry, account=customer_acc, contact=self.contact, debit=0, credit=net_amount)
                if self.discount_amount > 0:
                    discount_acc = get_account('5010', 'Discount Allowed', 'expense', '5')
                    LedgerLine.objects.create(ledger_entry=entry, account=discount_acc, debit=self.discount_amount, credit=0)
                    LedgerLine.objects.create(ledger_entry=entry, account=customer_acc, contact=self.contact, debit=0, credit=self.discount_amount)
        else:
            if self.is_advance:
                advance_acc = get_account('1014', 'Advance to Vendor', 'asset', '1')
                LedgerLine.objects.create(ledger_entry=entry, account=advance_acc, contact=self.contact, debit=net_amount, credit=0)
                LedgerLine.objects.create(ledger_entry=entry, account=bank_acc, debit=0, credit=net_amount)
                if self.discount_amount > 0:
                    discount_acc = get_account('4011', 'Discount Received', 'income', '4')
                    vendor_acc = get_account('2011', 'Vendor Payable', 'liability', '2')
                    LedgerLine.objects.create(ledger_entry=entry, account=vendor_acc, contact=self.contact, debit=self.discount_amount, credit=0)
                    LedgerLine.objects.create(ledger_entry=entry, account=discount_acc, debit=0, credit=self.discount_amount)
            else:
                vendor_acc = get_account('2011', 'Vendor Payable', 'liability', '2')
                LedgerLine.objects.create(ledger_entry=entry, account=vendor_acc, contact=self.contact, debit=net_amount, credit=0)
                LedgerLine.objects.create(ledger_entry=entry, account=bank_acc, debit=0, credit=net_amount)
                if self.discount_amount > 0:
                    discount_acc = get_account('4011', 'Discount Received', 'income', '4')
                    LedgerLine.objects.create(ledger_entry=entry, account=vendor_acc, contact=self.contact, debit=0, credit=self.discount_amount)
                    LedgerLine.objects.create(ledger_entry=entry, account=discount_acc, debit=self.discount_amount, credit=0)

    def create_bank_transaction(self):
        if not self.bank_account:
            return
        txn_type = 'deposit' if self.direction == 'received' else 'withdrawal'
        source_type = 'payment_received' if self.direction == 'received' else 'payment_made'
        
        first_allocation = self.allocations.first()
        linked_invoice = first_allocation.invoice if first_allocation else None
        
        BankTransaction.objects.create(
            bank_account=self.bank_account,
            transaction_type=txn_type,
            source_type=source_type,
            amount=self.amount - self.discount_amount,  # Net amount
            date=self.date,
            description=f"{self.get_direction_display()} - {self.contact.name}" + (f" ({self.description})" if self.description else ""),
            reference=self.reference or self.upi_ref,
            payment=self,
            invoice=linked_invoice,
            reconciled=self.reconciled,
        )

    def update_bank_transaction(self):
        if not self.bank_account:
            if hasattr(self, 'bank_transaction'):
                self.bank_transaction.delete()
            return
        
        first_allocation = self.allocations.first()
        linked_invoice = first_allocation.invoice if first_allocation else None
        
        if hasattr(self, 'bank_transaction'):
            txn = self.bank_transaction
            txn.bank_account = self.bank_account
            txn.transaction_type = 'deposit' if self.direction == 'received' else 'withdrawal'
            txn.source_type = 'payment_received' if self.direction == 'received' else 'payment_made'
            txn.amount = self.amount - self.discount_amount
            txn.date = self.date
            txn.description = f"{self.get_direction_display()} - {self.contact.name}" + (f" ({self.description})" if self.description else "")
            txn.reference = self.reference or self.upi_ref
            txn.invoice = linked_invoice
            txn.reconciled = self.reconciled
            txn.save()
        else:
            self.create_bank_transaction()

    def update_invoices(self):
        """Update invoice paid amounts based on PaymentAllocation and AdvanceAdjustment."""
        if self.direction == 'received':
            for allocation in self.allocations.all():
                inv = allocation.invoice
                total_paid = inv.payment_allocations.aggregate(total=Sum('amount'))['total'] or Decimal('0')
                total_advance = inv.advance_adjustments.aggregate(total=Sum('amount'))['total'] or Decimal('0')
                inv.paid_amount = total_paid + total_advance
                inv.balance_due = (inv.grand_total - inv.paid_amount).quantize(TAX_PRECISION)
                if inv.balance_due <= 0: inv.payment_status = 'paid'
                elif inv.paid_amount > 0 and inv.balance_due < inv.grand_total: inv.payment_status = 'partial'
                else: inv.payment_status = 'unpaid'
                inv.save(update_fields=['paid_amount', 'balance_due', 'payment_status'])

    def delete(self, *args, **kwargs):
        """Soft delete the payment and clean up related records."""
        if not self.is_deleted:
            # Clean up related records
            self.allocations.all().delete()
            AdvanceAdjustment.objects.filter(payment=self).delete()
            if hasattr(self, 'bank_transaction'):
                self.bank_transaction.delete()
            LedgerEntry.objects.filter(reference_id=self.id, entry_type__in=['payment', 'advance_received', 'advance_paid']).delete()
            # Now soft delete
            self.soft_delete()

    def __str__(self):
        return f"{self.direction} - {self.contact.name} - ₹{self.amount}"


# ============================================================
# 13.1 SIGNALS: Audit Log + Payment Allocation Sync
# ============================================================

@receiver(post_save, sender=Invoice)
@receiver(post_save, sender=Purchase)
@receiver(post_save, sender=Payment)
@receiver(post_save, sender=Contact)
@receiver(post_save, sender=Product)
def audit_log_save(sender, instance, created, **kwargs):
    action = 'CREATE' if created else 'UPDATE'
    AuditLog.objects.create(
        content_type=ContentType.objects.get_for_model(instance),
        object_id=instance.id,
        action=action,
        user=None,
        changes=getattr(instance, 'get_audit_changes', lambda: {})()
    )

@receiver(post_delete, sender=Invoice)
@receiver(post_delete, sender=Purchase)
@receiver(post_delete, sender=Payment)
@receiver(post_delete, sender=Contact)
@receiver(post_delete, sender=Product)
def audit_log_delete(sender, instance, **kwargs):
    AuditLog.objects.create(
        content_type=ContentType.objects.get_for_model(instance),
        object_id=instance.id,
        action='DELETE',
        user=None,
        changes={}
    )


@receiver(post_save, sender=PaymentAllocation)
@receiver(post_delete, sender=PaymentAllocation)
def sync_payment_allocation(sender, instance, **kwargs):
    if instance.payment:
        instance.payment.update_invoices()


@receiver(post_save, sender=AdvanceAdjustment)
@receiver(post_delete, sender=AdvanceAdjustment)
def sync_advance_adjustment(sender, instance, **kwargs):
    if instance.payment:
        instance.payment.update_invoices()
        if instance.payment.contact:
            instance.payment.contact.recalc_advance_balance()



# ============================================================
# 15. NOTIFICATIONS & OTP
# ============================================================

class Notification(SoftDeleteModel):
    TYPES = (('info', 'Information'), ('success', 'Success'), ('warning', 'Warning'), ('error', 'Error'))
    CATEGORIES = (('general', 'General'), ('sales', 'Sales'), ('purchases', 'Purchases'), ('repairs', 'Repairs'), ('stock', 'Stock'), ('payment', 'Payment'), ('system', 'System'))
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications')
    title = models.CharField(max_length=200)
    message = models.TextField()
    link = models.CharField(max_length=255, blank=True, null=True)
    notification_type = models.CharField(max_length=10, choices=TYPES, default='info')
    category = models.CharField(max_length=20, choices=CATEGORIES, default='general')
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, null=True, blank=True)
    object_id = models.PositiveIntegerField(null=True, blank=True)
    content_object = GenericForeignKey('content_type', 'object_id')

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['recipient', 'is_read']),
            models.Index(fields=['created_at']),
            models.Index(fields=['category']),
            models.Index(fields=['content_type', 'object_id']),
        ]

    def __str__(self):
        return f"{self.recipient.username} - {self.title}"


class NotificationPreference(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notif_prefs')
    email_enabled = models.BooleanField(default=True)
    categories = models.JSONField(default=dict)

    def __str__(self):
        return f"{self.user.username} Preferences"


class ContactMessage(SoftDeleteModel):
    STATUS_CHOICES = (('new', 'New'), ('read', 'Read'), ('replied', 'Replied'), ('spam', 'Spam'))
    name = models.CharField(max_length=200)
    email = models.EmailField()
    phone = models.CharField(max_length=20, blank=True)
    subject = models.CharField(max_length=200)
    message = models.TextField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='new')
    created_at = models.DateTimeField(auto_now_add=True)
    replied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['status']), models.Index(fields=['created_at'])]

    def __str__(self):
        return f"{self.name} - {self.subject}"


class EmailOTP(models.Model):
    PURPOSE_CHOICES = (('signup', 'Signup Verification'), ('reset_password', 'Password Reset'), ('change_email', 'Change Email'),)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True, related_name='otps')
    email = models.EmailField()
    otp = models.CharField(max_length=6)
    purpose = models.CharField(max_length=20, choices=PURPOSE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_used = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['email', 'otp']), models.Index(fields=['expires_at'])]

    def __str__(self):
        return f"{self.email} - {self.get_purpose_display()}"

    def is_valid(self):
        return not self.is_used and timezone.now() <= self.expires_at


# ============================================================
# 16. SIGNALS (Opening Balance, User Prefs, Contact Balance)
# ============================================================

@receiver(post_save, sender=Contact)
def create_or_update_opening_balance_ledger(sender, instance, **kwargs):
    """Create opening balance ledger entry for Contact."""
    LedgerEntry.objects.filter(entry_type='opening', reference_id=instance.id).delete()
    if instance.opening_balance == 0:
        return
    
    entry = LedgerEntry.objects.create(
        date=timezone.now().date(),
        entry_type='opening',
        reference_id=instance.id,
        description=f"Opening balance for {instance.name}",
        total_amount=abs(instance.opening_balance)
    )
    abs_bal = abs(instance.opening_balance)
    
    if instance.contact_type in ('customer', 'both'):
        customer_acc = get_account('1011', 'Customer Receivable', 'asset', '1')
        if instance.opening_balance > 0:
            LedgerLine.objects.create(ledger_entry=entry, account=customer_acc, contact=instance, debit=instance.opening_balance, credit=0)
            opening_acc = get_account('3010', 'Opening Balance Equity', 'equity', '3')
            LedgerLine.objects.create(ledger_entry=entry, account=opening_acc, debit=0, credit=instance.opening_balance)
        else:
            LedgerLine.objects.create(ledger_entry=entry, account=customer_acc, contact=instance, debit=0, credit=abs_bal)
            opening_acc = get_account('3010', 'Opening Balance Equity', 'equity', '3')
            LedgerLine.objects.create(ledger_entry=entry, account=opening_acc, debit=abs_bal, credit=0)
    else:  # vendor
        vendor_acc = get_account('2011', 'Vendor Payable', 'liability', '2')
        if instance.opening_balance > 0:
            LedgerLine.objects.create(ledger_entry=entry, account=vendor_acc, contact=instance, debit=0, credit=instance.opening_balance)
            opening_acc = get_account('3010', 'Opening Balance Equity', 'equity', '3')
            LedgerLine.objects.create(ledger_entry=entry, account=opening_acc, debit=instance.opening_balance, credit=0)
        else:
            LedgerLine.objects.create(ledger_entry=entry, account=vendor_acc, contact=instance, debit=abs_bal, credit=0)
            opening_acc = get_account('3010', 'Opening Balance Equity', 'equity', '3')
            LedgerLine.objects.create(ledger_entry=entry, account=opening_acc, debit=0, credit=abs_bal)
    
    instance.recalc_balance()


@receiver(post_save, sender=User)
def create_notification_preferences(sender, instance, created, **kwargs):
    if created:
        NotificationPreference.objects.create(user=instance, categories={})


@receiver(post_save, sender=LedgerLine)
@receiver(post_delete, sender=LedgerLine)
def sync_contact_balance_on_ledger_line_change(sender, instance, **kwargs):
    if instance.contact:
        instance.contact.recalc_balance()