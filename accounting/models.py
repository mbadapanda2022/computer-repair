# accounting/models.py

from decimal import Decimal
import json
import logging
from datetime import datetime, time, timedelta, date
from django.db import models, transaction, IntegrityError
from django.conf import settings
from django.db.models import F, Sum, Q, Max
from django.core.validators import MinValueValidator, RegexValidator
from django.db.models.signals import post_save, post_delete, pre_save, m2m_changed
from django.dispatch import receiver
from django.contrib.auth.models import User
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError

from django.utils import timezone
def to_aware_datetime(value):
    """
    Convert a date OR naive/aware datetime to timezone-aware datetime.

    StockMovement.date is a DateTimeField. Passing a DateField value
    raises: RuntimeWarning: received a naive datetime while time zone
    support is active. This normalises the value.
    """
    if value is None:
        return timezone.now()
    if isinstance(value, datetime):
        if timezone.is_naive(value):
            return timezone.make_aware(
                value, timezone.get_current_timezone()
            )
        return value
    if isinstance(value, date):
        return timezone.make_aware(
            datetime.combine(value, time.min),
            timezone.get_current_timezone(),
        )
    return timezone.now()

logger = logging.getLogger(__name__)

# --- CONSTANTS ---
MONEY_ZERO = Decimal('0.00')
TAX_PRECISION = Decimal('0.01')
POSITIVE_VALIDATOR = [MinValueValidator(MONEY_ZERO)]


# ============================================================
# 0. BASE MODELS (Soft Delete & Audit)
# ============================================================

class SoftDeleteQuerySet(models.QuerySet):
    def delete(self):
        from django.utils import timezone
        # Models listed here MUST be deleted per-instance so that
        # post_save / post_delete signals fire (which handle ledger
        # cleanup, stock reversal, and balance recalculation).
        #
        # Without this, `qs.update(is_deleted=True)` bypasses signals
        # and leaves orphan ledger entries behind.
        if self.model.__name__ in (
            'Contact',
            'StockMovement',
            'InvoiceItem',
            'PurchaseItem',
            'CreditNoteItem',
            'RepairPart',
            'RepairService',
            'RepairJob',
            'Payment',
            'Purchase',
            'Invoice',
            'CreditNote',
        ):
            count = 0
            for obj in self.filter(is_deleted=False):
                obj.delete()   # calls instance delete() → runs custom logic
                count += 1
            label = self.model._meta.label
            return (count, {label: count})

        # ── Default: fast bulk soft delete (safe for models without ledger) ──
        qs = self.filter(is_deleted=False)
        count = qs.count()
        if count:
            qs.update(
                is_deleted=True,
                deleted_at=timezone.now(),
            )
        label = self.model._meta.label
        return (count, {label: count})

    def hard_delete(self):
        """Permanent delete — bypasses soft delete."""
        return super().delete()

    def restore(self):
        """Restore all soft-deleted records in this queryset."""
        return self.filter(is_deleted=True).update(
            is_deleted=False,
            deleted_at=None,
            deleted_by=None,
        )

class SoftDeleteManager(models.Manager):
    """Manager that excludes soft-deleted records by default."""

    def get_queryset(self):
        return SoftDeleteQuerySet(self.model, using=self._db).filter(is_deleted=False)

    def all_with_deleted(self):
        return SoftDeleteQuerySet(self.model, using=self._db)

    def deleted_only(self):
        return SoftDeleteQuerySet(self.model, using=self._db).filter(is_deleted=True)


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
    all_objects = models.Manager()  
    
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
    prefix = models.CharField(max_length=10, default="INV", unique=True)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Invoice Counter"
        verbose_name_plural = "Invoice Counters"

    @classmethod
    def get_next_number(cls, prefix):
        from django.db import transaction

        with transaction.atomic():
            try:
                counter = cls.objects.select_for_update().get(prefix=prefix)
            except cls.DoesNotExist:
                # Pehli baar – create with locking
                counter = cls.objects.create(prefix=prefix, last_number=0)
                # Refresh with lock
                counter = cls.objects.select_for_update().get(pk=counter.pk)

            counter.last_number = (counter.last_number or 0) + 1
            counter.save(update_fields=['last_number'])
            return counter.last_number


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
        ('credit_note', 'Credit Note'),
        ('bank_manual', 'Bank Transaction (Manual)'),
    )
    JOURNAL_TYPES = (
        # Professional types (shown in form)
        ('discount_allowed', 'Discount Allowed'),
        ('discount_received', 'Discount Received'),
        ('advance_received', 'Advance Received'),
        ('advance_paid', 'Advance Paid'),
        ('general', 'General Journal'),
        # Legacy types (backward compat)
        ('discount', 'Discount (Legacy)'),
        ('payment', 'Payment (Legacy)'),
        ('receipt', 'Receipt (Legacy)'),
    )
    date = models.DateField(default=timezone.now)
    entry_type = models.CharField(max_length=20, choices=ENTRY_TYPE)
    journal_type = models.CharField(max_length=20, choices=JOURNAL_TYPES, blank=True, null=True)
    reference_id = models.PositiveIntegerField(blank=True, null=True)
    description = models.CharField(max_length=200)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)

    bank_account = models.ForeignKey(
        'BankAccount',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ledger_entries',
        help_text="Specific bank account used (journal / bank_manual entries).",
    )
    bank_transaction = models.OneToOneField(
        'BankTransaction',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ledger_entry',
        help_text="Originating bank transaction (bank_manual entries).",
    )
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
        # Partial updates (e.g. save(update_fields=['bank_account']))
        # don't change debit/credit balance — skip expensive validation.
        if kwargs.get('update_fields'):
            super().save(*args, **kwargs)
            return
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.entry_type} - {self.description} (₹{self.total_amount})"


class LedgerLine(SoftDeleteModel):
    SUBLEDGER_CHOICES = (
        ('receivable', 'Receivable'),
        ('payable', 'Payable'),
    )
    ledger_entry = models.ForeignKey(LedgerEntry, on_delete=models.CASCADE, related_name='lines')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='ledger_lines')
    contact = models.ForeignKey('Contact', on_delete=models.SET_NULL, null=True, blank=True, related_name='ledger_lines')
    subledger_type = models.CharField(
        max_length=10,
        choices=SUBLEDGER_CHOICES,
        blank=True,
        default='',
        db_index=True,
        help_text="For contact-linked lines: receivable (customer side) or payable (vendor side)",
    )
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

    # Account codes that belong to each subledger side
    RECEIVABLE_ACCOUNT_CODES = frozenset({'1011'})   # Customer Receivable
    PAYABLE_ACCOUNT_CODES = frozenset({'2011'})      # Vendor Payable

    def save(self, *args, **kwargs):
        # Auto-detect subledger_type from account code if contact is set
        if self.contact_id and not self.subledger_type:
            try:
                code = self.account.code
                if code in self.RECEIVABLE_ACCOUNT_CODES:
                    self.subledger_type = 'receivable'
                elif code in self.PAYABLE_ACCOUNT_CODES:
                    self.subledger_type = 'payable'
            except Account.DoesNotExist:
                pass

        super().save(*args, **kwargs)

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
    opening_balance = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Opening balance: Customer (+ve = owes you, -ve = advance), Vendor (+ve = you owe, -ve = advance)"
    )
    opening_balance_date = models.DateField(
        null=True, blank=True,
        help_text="Date when opening balance was recorded (defaults to today)"
    )
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    receivable_balance = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Net amount this party owes us (positive = they owe us)"
    )
    payable_balance = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Net amount we owe this party (positive = we owe them)"
    )
    advance_balance = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text="Net advance balance (Customer: positive = advance received, Vendor: positive = advance paid)"
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='customer_contact'
    )

    class Meta:
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(
                fields=['phone'],
                condition=Q(phone__isnull=False, is_deleted=False),
                name='unique_phone_active',
            ),
        ]
        indexes = [
            models.Index(fields=['name']),
            models.Index(fields=['contact_type']),
            models.Index(fields=['balance']),
            models.Index(fields=['is_deleted']),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_contact_type_display()})"

    # ════════════════════════════════════════════════════════════
    # BALANCE CALCULATIONS
    # ════════════════════════════════════════════════════════════

    def recalc_balance(self):
        recv = self.ledger_lines.filter(
            subledger_type='receivable',
            ledger_entry__is_deleted=False,
        ).aggregate(
            dr=Sum('debit'), cr=Sum('credit')
        )
        recv_dr = recv['dr'] or Decimal('0')
        recv_cr = recv['cr'] or Decimal('0')
        receivable = (recv_dr - recv_cr).quantize(TAX_PRECISION)

        pay = self.ledger_lines.filter(
            subledger_type='payable',
            ledger_entry__is_deleted=False,
        ).aggregate(
            dr=Sum('debit'), cr=Sum('credit')
        )
        pay_dr = pay['dr'] or Decimal('0')
        pay_cr = pay['cr'] or Decimal('0')
        payable = (pay_cr - pay_dr).quantize(TAX_PRECISION)

        # Net balance - backward compatible with existing logic
        if self.contact_type in ('customer', 'both'):
            new_bal = (receivable - payable).quantize(TAX_PRECISION)
        else:
            new_bal = payable

        Contact.objects.filter(pk=self.pk).update(
            receivable_balance=receivable,
            payable_balance=payable,
            balance=new_bal,
        )
        self.receivable_balance = receivable
        self.payable_balance = payable
        self.balance = new_bal

    def recalc_advance_balance(self):
        """
        Recalculate advance balance from advance payments.

        - Customer-only: net advance received (positive = customer has credit).
        - Vendor-only:   net advance paid    (positive = we have credit).
        - Both:          net position (received_balance - paid_balance).
        """
        def _net_advance(direction):
            total = self.payments.filter(
                is_advance=True,
                direction=direction,
            ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

            settled = AdvanceAdjustment.objects.filter(
                payment__contact=self,
                payment__is_advance=True,
                payment__direction=direction,
            ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

            return (total - settled).quantize(TAX_PRECISION)

        if self.contact_type == 'customer':
            new_advance = _net_advance('received')
        elif self.contact_type == 'vendor':
            new_advance = _net_advance('paid')
        else:  # 'both'
            received = _net_advance('received')
            paid = _net_advance('paid')
            new_advance = (received - paid).quantize(TAX_PRECISION)

        self.advance_balance = new_advance
        self.save(update_fields=['advance_balance'])

    # ════════════════════════════════════════════════════════════
    # SOFT DELETE OVERRIDE — also anonymize linked User
    # ════════════════════════════════════════════════════════════
    def soft_delete(self, user=None):
        from django.db import transaction as _tx
        with _tx.atomic():
            linked = self.user
            if linked and not linked.is_staff:
                uid = linked.id
                # Only anonymize once (idempotent)
                if not linked.username.startswith('deleted_user_'):
                    linked.is_active = False
                    linked.email = f'deleted_{uid}@deleted.local'
                    linked.username = f'deleted_user_{uid}'
                    linked.first_name = ''
                    linked.last_name = ''
                    linked.set_unusable_password()
                    linked.save(update_fields=[
                        'is_active', 'email', 'username',
                        'first_name', 'last_name', 'password',
                    ])
                    logger.info(
                        "Anonymized User#%s linked to Contact#%s",
                        uid, self.pk,
                    )

            super().soft_delete(user)

    def restore(self):
        """
        Restore this Contact.

        Note: User credentials are NOT auto-restored — because the original
        email/username may have been reused by a new registration. Staff
        must manually re-enable the User via the admin panel if needed.
        """
        super().restore()

    # ════════════════════════════════════════════════════════════
    # SAVE
    # ════════════════════════════════════════════════════════════
    def save(self, *args, **kwargs):
        # Normalize phone: keep digits only, store last 10 (Indian mobile).
        if self.phone is not None:
            phone_clean = ''.join(filter(str.isdigit, str(self.phone)))
            if not phone_clean:
                self.phone = None
            elif len(phone_clean) >= 10:
                self.phone = phone_clean[-10:]
            else:
                self.phone = phone_clean

        # Auto-fill opening balance date if not provided
        if self.opening_balance and not self.opening_balance_date:
            self.opening_balance_date = timezone.now().date()

        super().save(*args, **kwargs)

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
        if invoice.pk and not invoice.items.exists():
            for entry in LedgerEntry.objects.filter(
                entry_type='sales', reference_id=invoice.pk,
            ):
                for line in list(entry.lines.all()):
                    line.delete()
                entry.delete()
            return
        
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
                contact=invoice.customer,
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
    
    def get_gst_breakup(self):
        """
        Return GST breakdown based on invoice type.
        - interstate: IGST
        - intrastate: CGST + SGST (split equally)
        - regular / non_gst: no split
        """
        tax = self.tax_amount or Decimal('0')

        if self.gst_type == 'interstate':
            return {
                'total_tax': tax,
                'igst': tax,
                'cgst': Decimal('0'),
                'sgst': Decimal('0'),
            }
        elif self.gst_type == 'intrastate':
            half = (tax / 2).quantize(TAX_PRECISION)
            return {
                'total_tax': tax,
                'igst': Decimal('0'),
                'cgst': half,
                'sgst': tax - half,
            }
        else:
            return {
                'total_tax': tax,
                'igst': Decimal('0'),
                'cgst': Decimal('0'),
                'sgst': Decimal('0'),
            }
            
    # ════════════════════════════════════════════════════════════
    # OVERDUE & SHARE HELPERS 
    # ════════════════════════════════════════════════════════════

    @property
    def is_overdue(self):
        """True if unpaid/partial and past due_date."""
        if self.payment_status == 'paid':
            return False
        if not self.due_date:
            return False
        return self.due_date < timezone.now().date()

    @property
    def days_overdue(self):
        """Days past due_date (0 if not overdue)."""
        if not self.is_overdue:
            return 0
        return (timezone.now().date() - self.due_date).days

    @property
    def linked_repair(self):
        # Prefer value cached by the view (bulk-prefetched)
        if '_linked_repair_cache' in self.__dict__:
            return self.__dict__['_linked_repair_cache']
        # Auto-cache on first access — prevents repeated DB queries
        # when the template accesses this property multiple times
        # (e.g., invoice_detail.html uses it 4-5 times).
        self.__dict__['_linked_repair_cache'] = (
            RepairJob.objects.filter(invoice=self).first()
        )
        return self.__dict__['_linked_repair_cache']

    @linked_repair.setter
    def linked_repair(self, value):
        """Allow views to attach a prefetched RepairJob instance."""
        self.__dict__['_linked_repair_cache'] = value

    @property
    def whatsapp_share_url(self):
        """Return a wa.me deep-link URL to share this invoice summary."""
        if not self.customer or not self.customer.phone:
            return None
        phone = ''.join(filter(str.isdigit, str(self.customer.phone)))
        if len(phone) == 10:
            phone = '91' + phone
        elif len(phone) < 10:
            return None

        from urllib.parse import quote
        parts = [
            f"Hi {self.customer.name},",
            f"Your invoice {self.invoice_number} from A1 Computer Solutions.",
            f"Date: {self.date.strftime('%d %b %Y')}",
            f"Total: Rs.{self.grand_total:.2f}",
            f"Due: Rs.{self.balance_due:.2f}" if self.balance_due > 0 else "Status: PAID",
        ]
        if self.due_date and self.balance_due > 0:
            parts.append(f"Due Date: {self.due_date.strftime('%d %b %Y')}")
        text = "\n".join(parts)
        return f"https://wa.me/{phone}?text={quote(text)}"
    
    
    # ════════════════════════════════════════════════════════════
    # CREDIT NOTE HELPERS
    # ════════════════════════════════════════════════════════════

    @property
    def credit_note_total(self):
        """Total amount credited against this invoice (active CNs only)."""
        return self.credit_notes.aggregate(
            total=Sum('total_amount')
        )['total'] or Decimal('0')

    @property
    def net_amount(self):
        """Grand total minus credits — the 'real' invoice value."""
        return (self.grand_total - self.credit_note_total).quantize(TAX_PRECISION)

    @property
    def is_fully_returned(self):
        """True if credits >= grand_total."""
        return self.credit_note_total >= self.grand_total

    @property
    def has_credit_notes(self):
        return self.credit_notes.exists()

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
        # NOTE: start value Decimal('0') is MANDATORY here. sum() on an
        # empty iterable returns the *start* value; without an explicit
        # Decimal start it returns int 0, and .quantize() below would
        # crash with AttributeError when the last item is removed.
        self.subtotal = sum(
            (item.quantity * item.unit_price for item in items),
            Decimal('0'),
        )
        self.tax_amount = sum(
            (item.tax_amount for item in items),
            Decimal('0'),
        )
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
    
    # ── Repair linkage (prevents stock double-deduction) ──
    repair_part = models.ForeignKey(
        'RepairPart',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='invoice_items',
        help_text="RepairPart this item was sourced from (audit trail).",
    )
    stock_already_deducted = models.BooleanField(
        default=False,
        help_text="True for items whose stock was already deducted "
                  "(e.g. from repair parts). Prevents double deduction.",
    )

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

        # ── Stock movement: skip if already deducted by repair part ──
        if (not self.is_deleted
                and not self.product.is_service
                and not self.stock_already_deducted):

            StockMovement.all_objects.update_or_create(
                source_content_type=ContentType.objects.get_for_model(self),
                source_object_id=self.pk,
                defaults={
                    'product': self.product,
                    'movement_type': 'sale_out',
                    'quantity': (-Decimal(self.quantity)).quantize(TAX_PRECISION),
                    'date': to_aware_datetime(self.invoice.date),
                    'is_deleted': False,
                    'deleted_at': None,
                    'deleted_by': None,
                }
            )

        if self.invoice:
            self.invoice.calculate_totals()
            total_advance = self.invoice.advance_adjustments.aggregate(
                total=Sum('amount')
            )['total'] or Decimal('0')
            total_paid = self.invoice.paid_amount + total_advance
            self.invoice.balance_due = (
                self.invoice.grand_total - total_paid
            ).quantize(TAX_PRECISION)
            if self.invoice.balance_due <= 0:
                self.invoice.payment_status = 'paid'
            elif total_paid > 0 and self.invoice.balance_due < self.invoice.grand_total:
                self.invoice.payment_status = 'partial'
            else:
                self.invoice.payment_status = 'unpaid'

            self.invoice.save(update_fields=[
                'subtotal', 'tax_amount', 'grand_total',
                'balance_due', 'payment_status',
            ])

            if self.invoice.customer:
                self.invoice.customer.recalc_balance()
    
    def delete(self, *args, **kwargs):
        # ── Reverse stock ONLY if this item deducted it ──
        if (not self.product.is_service
                and not self.stock_already_deducted):

            StockMovement.objects.filter(
                source_content_type=ContentType.objects.get_for_model(self),
                source_object_id=self.pk
            ).delete()

        invoice = self.invoice
        super().delete(*args, **kwargs)

        if invoice:
            invoice.calculate_totals()
            total_advance = invoice.advance_adjustments.aggregate(
                total=Sum('amount')
            )['total'] or Decimal('0')
            total_paid = invoice.paid_amount + total_advance
            invoice.balance_due = (
                invoice.grand_total - total_paid
            ).quantize(TAX_PRECISION)

            if invoice.balance_due <= 0:
                invoice.payment_status = 'paid'
            elif total_paid > 0 and invoice.balance_due < invoice.grand_total:
                invoice.payment_status = 'partial'
            else:
                invoice.payment_status = 'unpaid'

            invoice.save(update_fields=[
                'subtotal', 'tax_amount', 'grand_total',
                'balance_due', 'payment_status',
            ])

            # ── Re-sync the ledger ──
            sync_invoice_ledger(invoice)

            if invoice.customer:
                invoice.customer.recalc_balance()
                invoice.customer.recalc_advance_balance()


# ============================================================
# 10. PURCHASE INVOICE (With Soft Delete & Advance Support)
# ============================================================

def sync_purchase_ledger(purchase):
    """
    Enterprise Purchase Ledger Sync with Freight, Discount, Advance, and Office Use.
    Now handles unsaved entry gracefully.
    """
    with transaction.atomic():
        if purchase.pk and not purchase.items.exists():
            for entry in LedgerEntry.objects.filter(
                entry_type='purchase', reference_id=purchase.pk,
            ):
                for line in list(entry.lines.all()):
                    line.delete()
                entry.delete()
            return
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
        # NOTE: Purchase model does not currently have `advance_adjustments` relation
        # (only Invoice has it). This block is defensive so it doesn't crash if the
        # relation is absent — which is the current case.
        total_advance_settled = Decimal('0')
        if hasattr(purchase, 'advance_adjustments'):
            total_advance_settled = (
                purchase.advance_adjustments.aggregate(total=Sum('amount'))['total']
                or Decimal('0')
            )
        if total_advance_settled > 0:
            advance_account = get_account('1014', 'Advance to Vendor', 'asset', '1')

            # Debit Vendor Payable (reduce liability)
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=vendor_account,
                contact=purchase.vendor,
                debit=total_advance_settled,
                credit=0
            )

            # Credit Advance to Vendor (reduce asset)
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=advance_account,
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
        
    def delete(self, *args, **kwargs):
        """
        Override soft-delete to also clean up ledger entries + items.

        Without this, deleting a purchase via Django admin (bulk action)
        or any other path leaves orphan ledger entries behind, which
        corrupt statements and contact balances.
        """
        if self.is_deleted:
            return

        # Clean up ledger entries for this purchase
        for entry in LedgerEntry.objects.filter(
            entry_type='purchase',
            reference_id=self.pk,
        ):
            # Delete lines first (per-instance so signals fire)
            for line in list(entry.lines.all()):
                line.delete()
            entry.delete()

        # Delete items (reverses stock via StockMovement.delete)
        for item in list(self.items.all()):
            item.delete()

        # Finally soft-delete the purchase header
        super().delete(*args, **kwargs)

        # Refresh vendor balance after cleanup
        if self.vendor_id:
            try:
                self.vendor.recalc_balance()
            except Exception:
                pass
        
    def save(self, *args, **kwargs):
        from django.db import IntegrityError, transaction

        if not self.purchase_number:
            max_attempts = 5
            last_error = None
            saved = False

            for attempt in range(max_attempts):
                next_num = InvoiceCounter.get_next_number("PUR")
                self.purchase_number = f"PUR-{next_num:04d}"

                try:
                    with transaction.atomic():
                        super().save(*args, **kwargs)
                    saved = True
                    break
                except IntegrityError as e:
                    last_error = e
                    if 'purchase_number' in str(e).lower() and attempt < max_attempts - 1:
                        logger.warning(
                            f"purchase_number collision on attempt {attempt + 1}, "
                            f"got {self.purchase_number}, retrying..."
                        )
                        continue
                    raise

            if not saved:
                raise IntegrityError(
                    f"Unable to generate unique purchase_number after {max_attempts} attempts: {last_error}"
                )
        else:
            super().save(*args, **kwargs)
            
        update_fields = kwargs.get('update_fields')
        SYNC_TRIGGERS = {
            'subtotal', 'tax_amount', 'grand_total',
            'discount_amount', 'freight_charge',
        }
        if update_fields is None:
            should_sync = True
        else:
            should_sync = bool(SYNC_TRIGGERS & set(update_fields))

        if should_sync and self.pk and self.items.exists():
            sync_purchase_ledger(self)
            if self.vendor:
                self.vendor.recalc_balance()


    def calculate_totals(self):
        items = self.items.all()
        # NOTE: start value Decimal('0') is MANDATORY — same reason as
        # Invoice.calculate_totals(). Prevents int-vs-Decimal crash when
        # the last item is removed.
        self.subtotal = sum(
            (item.quantity * item.unit_price for item in items),
            Decimal('0'),
        )
        self.tax_amount = sum(
            (item.tax_amount for item in items),
            Decimal('0'),
        )
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

        if not self.is_deleted and not self.product.is_service and not self.is_office_use:
            StockMovement.all_objects.update_or_create(
                source_content_type=ContentType.objects.get_for_model(self),
                source_object_id=self.pk,
                defaults={
                    'product': self.product,
                    'movement_type': 'purchase_in',
                    'quantity': Decimal(self.quantity).quantize(TAX_PRECISION),
                    'date': to_aware_datetime(self.purchase.date),
                    'is_deleted': False,
                    'deleted_at': None,
                    'deleted_by': None,
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
            purchase.save(update_fields=[
                'subtotal', 'tax_amount', 'grand_total',
            ])

            # ── Re-sync the ledger (save() may skip it) ──
            sync_purchase_ledger(purchase)

            if purchase.vendor:
                purchase.vendor.recalc_balance()
                
# ============================================================
# 9.1 SALES RETURNS / CREDIT NOTES
# ============================================================

def sync_credit_note_ledger(credit_note):
    """
    Double-entry ledger for credit notes.

    Reverses the original invoice's effect:
      Original invoice:  Dr Customer  /  Cr Sales + Cr GST
      Credit note:       Dr Sales Return + Dr GST  /  Cr Customer
    """
    with transaction.atomic():
        if credit_note.pk and not credit_note.items.exists():
            for entry in LedgerEntry.objects.filter(
                entry_type='credit_note', reference_id=credit_note.pk,
            ):
                for line in list(entry.lines.all()):
                    line.delete()
                entry.delete()
            return
        
        entry, created = LedgerEntry.objects.get_or_create(
            entry_type='credit_note',
            reference_id=credit_note.id,
            defaults={
                'date': credit_note.date,
                'description': f"Credit Note {credit_note.credit_note_number} "
                               f"(against {credit_note.invoice.invoice_number})",
                'total_amount': credit_note.total_amount,
            }
        )

        if not created:
            entry.date = credit_note.date
            entry.description = (
                f"Credit Note {credit_note.credit_note_number} "
                f"(against {credit_note.invoice.invoice_number})"
            )
            entry.total_amount = credit_note.total_amount
            entry.save()

        # Clear old lines (idempotent)
        LedgerLine.objects.filter(ledger_entry=entry).delete()

        sales_return = get_account('4020', 'Sales Returns', 'income', '4')
        gst_payable = get_account('2010', 'GST Payable', 'liability', '2')
        customer_account = get_account('1011', 'Customer Receivable', 'asset', '1')

        # Dr Sales Returns (contra-revenue)
        if credit_note.subtotal > 0:
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=sales_return,
                debit=credit_note.subtotal,
                credit=0,
            )

        # Dr GST Payable (reverse liability)
        if credit_note.tax_amount > 0:
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=gst_payable,
                debit=credit_note.tax_amount,
                credit=0,
            )

        # Cr Customer Receivable (reduce balance)
        LedgerLine.objects.create(
            ledger_entry=entry,
            account=customer_account,
            contact=credit_note.customer,
            debit=0,
            credit=credit_note.total_amount,
        )

        entry.full_clean()


class CreditNote(SoftDeleteModel):
    """
    Sales Return / Credit Note against an existing Invoice.
    Supports full return, partial return, and value-only adjustments.
    """

    REASON_CHOICES = (
        ('sales_return', 'Sales Return (Defective/Damaged)'),
        ('rate_difference', 'Rate Difference'),
        ('excess_quantity', 'Excess Quantity Supplied'),
        ('post_sale_discount', 'Post-Sale Discount'),
        ('order_cancelled', 'Order Cancelled'),
        ('gst_correction', 'GST / Invoice Correction'),
        ('other', 'Other'),
    )

    REFUND_METHOD = (
        ('cash', 'Cash Refund'),
        ('bank', 'Bank Refund'),
        ('credit', 'Credit to Account (Adjust in Next Invoice)'),
        ('none', 'No Refund — Stock Only'),
    )

    credit_note_number = models.CharField(max_length=50, unique=True, editable=False)
    invoice = models.ForeignKey(
        Invoice, on_delete=models.PROTECT,
        related_name='credit_notes',
    )
    customer = models.ForeignKey(
        Contact, on_delete=models.PROTECT,
        related_name='credit_notes',
    )
    date = models.DateField(default=timezone.now)
    reason = models.CharField(max_length=30, choices=REASON_CHOICES)
    notes = models.TextField(blank=True)

    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)

    refund_method = models.CharField(max_length=10, choices=REFUND_METHOD, default='credit')
    is_stock_return = models.BooleanField(
        default=True,
        help_text="True for physical return (stock auto-added). False for value-only.",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='credit_notes_created',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-created_at']
        indexes = [
            models.Index(fields=['credit_note_number']),
            models.Index(fields=['invoice']),
            models.Index(fields=['customer', 'date']),
        ]

    def __str__(self):
        return f"CN {self.credit_note_number} — {self.customer.name}"

    def save(self, *args, **kwargs):
        from django.db import IntegrityError, transaction

        if not self.credit_note_number:
            max_attempts = 5
            last_error = None
            saved = False
            for attempt in range(max_attempts):
                next_num = InvoiceCounter.get_next_number("CN")
                self.credit_note_number = f"CN-{next_num:04d}"
                try:
                    with transaction.atomic():
                        super().save(*args, **kwargs)
                    saved = True
                    break
                except IntegrityError as e:
                    last_error = e
                    if 'credit_note_number' in str(e).lower() and attempt < max_attempts - 1:
                        continue
                    raise
            if not saved:
                raise IntegrityError(
                    f"Could not generate unique CN number: {last_error}"
                )
        else:
            super().save(*args, **kwargs)

        # Sync ledger + customer balance (skip on partial update_fields saves)
        update_fields = kwargs.get('update_fields')
        if update_fields is None and self.pk and self.items.exists():
            try:
                self.calculate_totals()
                super().save(update_fields=[
                    'subtotal', 'tax_amount', 'total_amount'
                ])
                sync_credit_note_ledger(self)
                if self.customer:
                    self.customer.recalc_balance()
            except Exception as e:
                logger.error(
                    f"CN ledger sync failed for {self.credit_note_number}: {e}",
                    exc_info=True,
                )

    def calculate_totals(self):
        """Sum up items to compute header totals."""
        items = self.items.all()
        self.subtotal = sum(
            (item.quantity_returned * item.unit_price for item in items),
            Decimal('0'),
        ).quantize(TAX_PRECISION)
        self.tax_amount = sum(
            (item.tax_amount for item in items),
            Decimal('0'),
        ).quantize(TAX_PRECISION)
        self.total_amount = (self.subtotal + self.tax_amount).quantize(TAX_PRECISION)
        return self.total_amount

    @property
    def whatsapp_share_url(self):
        if not self.customer or not self.customer.phone:
            return None
        phone = ''.join(filter(str.isdigit, str(self.customer.phone)))
        if len(phone) == 10:
            phone = '91' + phone
        elif len(phone) < 10:
            return None
        from urllib.parse import quote
        text = "\n".join([
            f"Hi {self.customer.name},",
            f"Credit Note {self.credit_note_number} from A1 Computer Solutions.",
            f"Against Invoice: {self.invoice.invoice_number}",
            f"Date: {self.date.strftime('%d %b %Y')}",
            f"Amount: Rs.{self.total_amount:.2f}",
            f"Reason: {self.get_reason_display()}",
        ])
        return f"https://wa.me/{phone}?text={quote(text)}"


class CreditNoteItem(SoftDeleteModel):
    """Line items for a credit note."""

    credit_note = models.ForeignKey(
        CreditNote, on_delete=models.CASCADE,
        related_name='items',
    )
    invoice_item = models.ForeignKey(
        InvoiceItem, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='credit_items',
    )
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity_returned = models.DecimalField(
        max_digits=10, decimal_places=2, default=1,
        validators=[MinValueValidator(Decimal('0.01'))],
    )
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, validators=POSITIVE_VALIDATOR)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)

    @transaction.atomic
    def save(self, *args, **kwargs):
        # Auto-fill tax from product if empty
        if not self.tax_rate and self.product:
            self.tax_rate = self.product.tax_rate

        line_amount = self.quantity_returned * self.unit_price
        self.tax_amount = ((line_amount * self.tax_rate) / 100).quantize(TAX_PRECISION)
        self.line_total = (line_amount + self.tax_amount).quantize(TAX_PRECISION)

        super().save(*args, **kwargs)

        # Stock IN — only if physical return and product is not service
        if (not self.is_deleted
                and self.credit_note.is_stock_return
                and not self.product.is_service):
            StockMovement.all_objects.update_or_create(
                source_content_type=ContentType.objects.get_for_model(self),
                source_object_id=self.pk,
                defaults={
                    'product': self.product,
                    'movement_type': 'return_in',
                    'quantity': Decimal(self.quantity_returned).quantize(TAX_PRECISION),
                    'date': to_aware_datetime(self.credit_note.date),
                    'reference': f"CN {self.credit_note.credit_note_number}",
                    'is_deleted': False,
                    'deleted_at': None,
                    'deleted_by': None,
                },
            )

        # Refresh header totals
        if self.credit_note:
            self.credit_note.calculate_totals()
            CreditNote.objects.filter(pk=self.credit_note.pk).update(
                subtotal=self.credit_note.subtotal,
                tax_amount=self.credit_note.tax_amount,
                total_amount=self.credit_note.total_amount,
            )

    def delete(self, *args, **kwargs):
        """Reverse stock effect + re-sync ledger after delete."""
        if not self.product.is_service:
            for sm in StockMovement.objects.filter(
                source_content_type=ContentType.objects.get_for_model(self),
                source_object_id=self.pk,
            ):
                sm.delete()

        cn = self.credit_note
        super().delete(*args, **kwargs)

        if cn:
            cn.calculate_totals()
            CreditNote.objects.filter(pk=cn.pk).update(
                subtotal=cn.subtotal,
                tax_amount=cn.tax_amount,
                total_amount=cn.total_amount,
            )

            # ── Re-sync the ledger (update() bypassed save()) ──
            sync_credit_note_ledger(cn)

            if cn.customer:
                cn.customer.recalc_balance()


# ============================================================
# 11. REPAIR JOBS
# ============================================================

class RepairJob(SoftDeleteModel):
    STATUS_CHOICES = (
        ('pending',    'Pending (Awaiting Receipt)'),
        ('received',   'Received at Shop'),
        ('diagnosis',  'Diagnosis'),
        ('repairing',  'Repairing'),
        ('ready',      'Ready for Delivery'),
        ('delivered',  'Delivered'),
        ('cancelled',  'Cancelled'),
    )
    ESTIMATE_STATUS_CHOICES = (
        ('pending',  'Pending Approval'),
        ('approved', 'Approved'),
        ('on_hold',  'On Hold'),
        ('rejected', 'Rejected'),
    )

    job_number = models.CharField(max_length=50, unique=True, editable=False)
    customer = models.ForeignKey(
        Contact,
        on_delete=models.PROTECT,
        related_name='repair_jobs',
        limit_choices_to={'contact_type__in': ['customer', 'both']},
    )
    device_model = models.CharField(max_length=200)
    serial_number = models.CharField(max_length=100, blank=True)
    issue_description = models.TextField()
    accessories = models.TextField(blank=True, null=True)
    device_condition = models.TextField(blank=True, null=True)
    action_taken = models.TextField(blank=True)
    diagnosis_report = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='pending')

    # ---------- Estimate flow ----------
    estimate_status = models.CharField(max_length=10, choices=ESTIMATE_STATUS_CHOICES, default='pending')
    estimate_approved_at = models.DateTimeField(null=True, blank=True)
    estimate_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_estimates',
    )
    approval_source = models.CharField(
        max_length=30,
        blank=True,
        null=True,
        choices=[
            ('portal', 'Customer Portal'),
            ('staff_phone', 'Staff (Phone Call)'),
            ('staff_inperson', 'Staff (In-Person)'),
            ('staff_whatsapp', 'Staff (WhatsApp)'),
        ],
    )
    approval_remarks = models.TextField(blank=True, null=True)

    # ---------- Amounts ----------
    estimated_cost = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True, validators=POSITIVE_VALIDATOR)
    final_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)
    labour_charge = models.DecimalField(max_digits=10, decimal_places=2, default=0, validators=POSITIVE_VALIDATOR)

    # ---------- Personnel ----------
    received_by = models.CharField(max_length=100, blank=True, null=True)
    delivered_by = models.CharField(max_length=100, blank=True, null=True)

    # ---------- Timeline tracking ----------
    date_in = models.DateField(default=timezone.now, help_text="Legacy: date of first entry into system")
    submitted_at = models.DateTimeField(null=True, blank=True, help_text="Auto: when customer submitted via online portal")
    received_at = models.DateField(null=True, blank=True, help_text="Staff: when device physically arrived at shop")
    received_remarks = models.TextField(blank=True, null=True, help_text="Staff: device condition at reception")
    ready_at = models.DateField(null=True, blank=True, help_text="Auto: when repair was marked Ready for Delivery")
    delivered_at = models.DateTimeField(null=True, blank=True, help_text="Auto audit: when status changed to Delivered")

    # ---------- Delivery details (staff-editable) ----------
    delivery_date = models.DateField(blank=True, null=True, help_text="Staff: actual date device was handed over")
    delivered_to_name = models.CharField(max_length=200, blank=True, null=True)
    delivered_to_phone = models.CharField(max_length=20, blank=True, null=True)
    delivered_to_designation = models.CharField(max_length=100, blank=True, null=True)
    delivery_remarks = models.TextField(blank=True, null=True)

    # ---------- Related ----------
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
    
    # ════════════════════════════════════════════════════════════
    # AMOUNT PROPERTIES (computed, not stored)
    # ════════════════════════════════════════════════════════════

    @property
    def parts_total(self):
        """Sum of active parts' line_total (pre-tax)."""
        result = self.parts.aggregate(total=Sum('line_total'))['total']
        return (result or Decimal('0')).quantize(TAX_PRECISION)
    
    @property
    def services_total(self):
        """Sum of active repair services' line_total (pre-tax)."""
        result = self.services.aggregate(total=Sum('line_total'))['total']
        return (result or Decimal('0')).quantize(TAX_PRECISION)

    @property
    def base_amount(self):
        """
        Pre-tax total = parts + services (+ legacy labour_charge).
        This is what `final_amount` field stores.
        """
        legacy = self.labour_charge or Decimal('0')
        return (
            self.parts_total + self.services_total + legacy
        ).quantize(TAX_PRECISION)

    @property
    def invoiced_amount(self):
        """
        Actual invoiced grand_total (if invoice exists). None otherwise.
        Uses select_related('invoice') — no extra DB hit in lists.
        """
        if not self.invoice_id:
            return None
        if not getattr(self, 'invoice', None):
            return None
        return self.invoice.grand_total

    @property
    def display_amount(self):
        """
        Amount to display in list/detail views.

        Priority:
          1. Invoice exists  → actual invoiced amount (what customer pays)
          2. Otherwise       → base amount (pre-tax estimate)
        """
        invoiced = self.invoiced_amount
        if invoiced is not None:
            return invoiced
        return self.final_amount or Decimal('0')

    @property
    def is_invoiced(self):
        """Convenience flag for templates."""
        return self.invoice_id is not None

    @property
    def can_be_invoiced(self):
        return not self.invoice and self.status in ('ready', 'delivered')

    def calculate_final_amount(self):
        parts_total = self.parts.aggregate(total=Sum('line_total'))['total'] or Decimal('0')
        services_total = self.services.aggregate(total=Sum('line_total'))['total'] or Decimal('0')
        legacy_labour = self.labour_charge or Decimal('0')
        self.final_amount = (
            parts_total + services_total + legacy_labour
        ).quantize(TAX_PRECISION)
        self.save(update_fields=['final_amount'])
        return self.final_amount
    
    def save(self, *args, **kwargs):
        from django.db import IntegrityError, transaction

        is_new = self.pk is None

        # ============================================================
        # AUTO-SYNC date_in
        # - Agar received_at set hai → date_in = received_at (staff / received customer)
        # - Warna submitted_at.date() (customer pending)
        # ============================================================
        if self.received_at:
            self.date_in = self.received_at
        elif self.submitted_at:
            self.date_in = self.submitted_at.date()

        # ============================================================
        # NEW RECORD PATH (race-safe job_number)
        # ============================================================
        if is_new:
            max_attempts = 5
            last_error = None

            for attempt in range(max_attempts):
                next_num = InvoiceCounter.get_next_number("REP")
                self.job_number = f"REP-{next_num:04d}"

                try:
                    with transaction.atomic():
                        super().save(*args, **kwargs)
                    break
                except IntegrityError as e:
                    last_error = e
                    if 'job_number' in str(e).lower() and attempt < max_attempts - 1:
                        logger.warning(
                            f"job_number collision on attempt {attempt + 1}, "
                            f"got {self.job_number}, retrying..."
                        )
                        continue
                    raise

            return

        # ============================================================
        # EXISTING RECORD PATH
        # ============================================================
        old_status = None
        try:
            old_status = RepairJob.objects.get(pk=self.pk).status
        except RepairJob.DoesNotExist:
            pass

        # Auto-stamp timeline dates on status change
        if old_status and old_status != self.status:
            now_dt = timezone.now()
            today = now_dt.date()

            if self.status == 'received' and not self.received_at:
                self.received_at = today
                # Mirror to date_in
                self.date_in = self.received_at

            if self.status == 'ready' and not self.ready_at:
                self.ready_at = today

            if self.status == 'delivered':
                if not self.delivered_at:
                    self.delivered_at = now_dt
                if not self.delivery_date:
                    self.delivery_date = today

        # Recalculate final_amount — parts + services + legacy labour
        if self.pk:
            parts_total = self.parts.aggregate(
                total=Sum('line_total')
            )['total'] or Decimal('0')
            services_total = self.services.aggregate(
                total=Sum('line_total')
            )['total'] or Decimal('0')
            legacy_labour = self.labour_charge or Decimal('0')
            self.final_amount = (
                parts_total + services_total + legacy_labour
            ).quantize(TAX_PRECISION)

        # Ensure auto-stamped fields are included in update_fields
        update_fields = kwargs.get('update_fields')
        if update_fields:
            extra = []
            if 'date_in' not in update_fields:
                extra.append('date_in')
            if old_status and old_status != self.status:
                for f in ['received_at', 'ready_at', 'delivered_at', 'delivery_date']:
                    if getattr(self, f, None) and f not in update_fields:
                        extra.append(f)
            if extra:
                kwargs['update_fields'] = list(update_fields) + extra

        super().save(*args, **kwargs)

        # Notifications on status change
        if old_status and old_status != self.status:
            try:
                from django.urls import reverse
                from accounting.utils.notification_helpers import send_notification_to_contact
                send_notification_to_contact(
                    self.customer,
                    title=f"Repair Status Updated: {self.job_number}",
                    message=f"Your repair for {self.device_model} is now {self.get_status_display()}.",
                    link=reverse('customer:customer_repair_detail', args=[self.pk]),
                    notif_type='info',
                    category='repairs',
                    send_email=False,
                )
            except Exception as notif_error:
                logger.error(f"Notification error for job {self.job_number}: {notif_error}")
                
                
    def delete(self, *args, **kwargs):
        """
        Delete repair AND its parts/services (reversing stock via RepairPart.delete).

        Safety: Cannot delete a repair that has an invoice linked.
        """
        if self.invoice_id:
            raise ValidationError(
                "Cannot delete a repair job that has a linked invoice. "
                "Please delete the invoice first."
            )
        # Delete services first (no stock impact, but clean refs)
        for service in list(self.services.all()):
            service.delete()
        # Delete parts (reverses stock)
        for part in list(self.parts.all()):
            part.delete()
        super().delete(*args, **kwargs)

   
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

        if not self.is_deleted and not self.product.is_service:
            StockMovement.all_objects.update_or_create(
                source_content_type=ContentType.objects.get_for_model(self),
                source_object_id=self.pk,
                defaults={
                    'product': self.product,
                    'movement_type': 'repair_out',
                    'quantity': (-Decimal(self.quantity)).quantize(TAX_PRECISION),
                    'date': to_aware_datetime(self.repair_job.date_in),
                    'is_deleted': False,
                    'deleted_at': None,
                    'deleted_by': None,
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
# REPAIR SERVICE (No quantity, no stock — flat charge)
# ============================================================

class RepairService(SoftDeleteModel):
    """
    Service charge attached to a repair job — NO physical stock.

    Examples: Repair Labour, Data Recovery, Software Loading, Diagnostics.
    Uses a Product with is_service=True. Charges a flat amount (no quantity).

    Stock impact: NONE — service items never touch inventory.
    """
    repair_job = models.ForeignKey(
        RepairJob,
        on_delete=models.CASCADE,
        related_name='services',
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        limit_choices_to={'is_service': True},
        help_text="Service product (is_service=True).",
    )
    amount = models.DecimalField(
        max_digits=12, decimal_places=2,
        validators=POSITIVE_VALIDATOR,
    )
    description = models.CharField(
        max_length=200, blank=True,
        help_text="Optional note (e.g., 'Printer head cleaning').",
    )
    line_total = models.DecimalField(
        max_digits=12, decimal_places=2,
        editable=False,
        validators=POSITIVE_VALIDATOR,
    )

    class Meta:
        ordering = ['id']
        indexes = [
            models.Index(fields=['repair_job']),
        ]

    def __str__(self):
        return f"{self.product.name} — ₹{self.amount}"

    def save(self, *args, **kwargs):
        if not self.amount and self.product:
            self.amount = self.product.selling_price or Decimal('0')
        self.line_total = (Decimal(self.amount)).quantize(TAX_PRECISION)
        super().save(*args, **kwargs)
        self.repair_job.calculate_final_amount()

    def delete(self, *args, **kwargs):
        repair_job = self.repair_job
        super().delete(*args, **kwargs)
        if repair_job:
            repair_job.calculate_final_amount()
            
            
# ============================================================
# BANK TRANSACTION ↔ LEDGER SYNC (Manual entries only)
# ============================================================

# Manual-source counter-account mapping
_BANK_SOURCE_ACCOUNTS = {
    # source_type → (code, name, type, group_code)
    'interest':     ('4012', 'Interest Income', 'income', '4'),
    'bank_charge':  ('5014', 'Bank Charges', 'expense', '5'),
    # 'transfer', 'manual', 'payment_received', 'payment_made' → Suspense
}
_BANK_SUSPENSE = ('1020', 'Bank Suspense', 'asset', '1')


def sync_bank_transaction_ledger(txn):
    """
    Create/refresh a LedgerEntry mirror for a MANUAL BankTransaction.

    SKIPPED (no ledger sync — by design):
      - Transactions with a linked Payment (`txn.payment_id`)
        → the Payment already writes its own ledger entry.
      - Transactions with source_type in
        {'payment_received', 'payment_made'} — these are always
        auto-created by Payment, so a duplicate would double-count.
      - Soft-deleted rows.

    ACCOUNT MAPPING:
      deposit + interest     → Dr Bank (1010) / Cr Interest Income (4012)
      withdrawal + bank_charge → Dr Bank Charges (5014) / Cr Bank (1010)
      everything else        → Dr/Cr Bank ↔ Dr/Cr Bank Suspense (1020)

    Idempotent — safe to call multiple times.
    """
    if not txn or txn.is_deleted:
        return
    if txn.payment_id:
        return
    if txn.source_type in ('payment_received', 'payment_made'):
        return
    if not txn.bank_account_id:
        return

    with transaction.atomic():
        entry, _created = LedgerEntry.objects.get_or_create(
            entry_type='bank_manual',
            reference_id=txn.pk,
            defaults={
                'date': txn.date,
                'description': txn.description or txn.get_source_type_display(),
                'total_amount': txn.amount,
                'bank_account': txn.bank_account,
                'bank_transaction': txn,
            },
        )
        # Always refresh (date/amount/desc may have changed)
        entry.date = txn.date
        entry.description = txn.description or txn.get_source_type_display()
        entry.total_amount = txn.amount
        entry.bank_account = txn.bank_account
        entry.bank_transaction = txn
        entry.save()

        # Clear old lines — idempotent
        LedgerLine.objects.filter(ledger_entry=entry).delete()

        bank_ledger = get_account('1010', 'Bank Account', 'asset', '1')

        if txn.transaction_type == 'deposit':
            code, name, atype, grp = _BANK_SOURCE_ACCOUNTS.get(
                txn.source_type, _BANK_SUSPENSE
            )
            counter = get_account(code, name, atype, grp)
            LedgerLine.objects.create(
                ledger_entry=entry, account=bank_ledger,
                debit=txn.amount, credit=0,
            )
            LedgerLine.objects.create(
                ledger_entry=entry, account=counter,
                debit=0, credit=txn.amount,
            )
        else:  # withdrawal
            code, name, atype, grp = _BANK_SOURCE_ACCOUNTS.get(
                txn.source_type, _BANK_SUSPENSE
            )
            counter = get_account(code, name, atype, grp)
            LedgerLine.objects.create(
                ledger_entry=entry, account=counter,
                debit=txn.amount, credit=0,
            )
            LedgerLine.objects.create(
                ledger_entry=entry, account=bank_ledger,
                debit=0, credit=txn.amount,
            )

        entry.full_clean()


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

    def delete(self, *args, **kwargs):
        """
        Soft-delete the transaction AND remove its mirror LedgerEntry
        (if it was a manual entry). Without this, soft-deleted bank
        transactions would leave orphan ledger lines that skewed
        Trial Balance and Chart of Accounts.
        """
        if self.is_deleted:
            return

        # Remove ledger mirror (if exists) — per-instance so signals fire
        for entry in LedgerEntry.objects.filter(
            entry_type='bank_manual',
            reference_id=self.pk,
        ):
            for line in list(entry.lines.all()):
                line.delete()
            entry.delete()

        super().delete(*args, **kwargs)

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
        old_amount = None
        if not is_new:
            # Capture previous amount so we can detect a decrease
            try:
                old_amount = Payment.all_objects.get(pk=self.pk).amount
            except Payment.DoesNotExist:
                old_amount = None

        with transaction.atomic():
            super().save(*args, **kwargs)
            if is_new:
                self.create_ledger_entry()
                self.create_bank_transaction()
            else:
                self.update_ledger_entry()
                self.update_bank_transaction()

                # ── Auto-trim overallocations when amount decreases ──
                # Without this, editing a payment to a smaller value leaves
                # allocations exceeding the new amount → invoices show
                # paid_amount > grand_total and status flags go wrong.
                if old_amount is not None and self.amount < old_amount:
                    self._trim_allocations_to_amount()

            # Update contact balances
            if self.contact:
                self.contact.recalc_balance()
                self.contact.recalc_advance_balance()

            # Update invoice statuses
            self.update_invoices()

    def _trim_allocations_to_amount(self):
        """
        Trim allocations (and advance adjustments) from the newest first
        until the total allocated ≤ payment.amount.

        Called automatically when Payment.amount decreases on edit.
        """
        allocations = list(
            self.allocations.select_related('invoice').order_by('-id')
        )
        total = sum(a.amount for a in allocations)

        for alloc in allocations:
            if total <= self.amount:
                break
            excess = total - self.amount
            if alloc.amount <= excess:
                # Remove this allocation entirely — HARD delete so the
                # (payment, invoice) unique constraint is freed for reuse.
                total -= alloc.amount
                alloc.hard_delete()
            else:
                # Partial trim — reduce this allocation's amount
                alloc.amount = (alloc.amount - excess).quantize(TAX_PRECISION)
                alloc.save(update_fields=['amount'])
                total -= excess

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
        
        new_entry_type = 'payment'
        if self.is_advance:
            new_entry_type = 'advance_received' if self.direction == 'received' else 'advance_paid'

        entry.entry_type = new_entry_type
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
        """
        Soft delete the payment and clean up ALL related records.

        Bug #1 fix (Payments audit): bulk soft-delete skips per-instance
        post_save / post_delete signals, so `sync_payment_allocation`
        and `sync_advance_adjustment` never fire. As a result,
        invoice.paid_amount / balance_due / payment_status stayed
        stale after deleting a payment. We now capture the affected
        invoice IDs up-front and recalculate them manually.

        Bonus: LedgerLines belonging to this payment's ledger entries
        were being orphaned (bulk delete on LedgerEntry didn't touch
        the lines). We now delete the lines first, per-instance, so
        their post_save signals recalc contact balances.
        """
        if self.is_deleted:
            return

        # ── Capture affected invoice IDs BEFORE deleting anything ──
        allocation_invoice_ids = list(
            self.allocations.values_list('invoice_id', flat=True)
        )
        advance_invoice_ids = list(
            AdvanceAdjustment.objects
            .filter(payment=self)
            .values_list('invoice_id', flat=True)
        )
        affected_invoice_ids = set(allocation_invoice_ids) | set(advance_invoice_ids)

        # ── Capture ledger entry IDs (so we can clean their lines too) ──
        ledger_entry_ids = list(
            LedgerEntry.objects.filter(
                reference_id=self.id,
                entry_type__in=['payment', 'advance_received', 'advance_paid'],
            ).values_list('id', flat=True)
        )

        # ── Delete child records ──
        self.allocations.all().delete()
        AdvanceAdjustment.objects.filter(payment=self).delete()

        if hasattr(self, 'bank_transaction'):
            self.bank_transaction.delete()

        # ── Delete ledger LINES first (per-instance → signals fire),
        #    then the ledger ENTRIES themselves ──
        for line in list(LedgerLine.objects.filter(ledger_entry_id__in=ledger_entry_ids)):
            line.delete()
        LedgerEntry.objects.filter(id__in=ledger_entry_ids).delete()

        # ── Recalculate affected invoices (now that allocations are gone) ──
        for inv in Invoice.objects.filter(pk__in=affected_invoice_ids):
            total_paid = inv.payment_allocations.aggregate(
                total=Sum('amount')
            )['total'] or Decimal('0')
            total_advance = inv.advance_adjustments.aggregate(
                total=Sum('amount')
            )['total'] or Decimal('0')
            inv.paid_amount = total_paid + total_advance
            inv.balance_due = (inv.grand_total - inv.paid_amount).quantize(TAX_PRECISION)
            if inv.balance_due <= 0:
                inv.payment_status = 'paid'
            elif inv.paid_amount > 0 and inv.balance_due < inv.grand_total:
                inv.payment_status = 'partial'
            else:
                inv.payment_status = 'unpaid'
            inv.save(update_fields=['paid_amount', 'balance_due', 'payment_status'])

        # ── Recalculate contact balances ──
        if self.contact:
            self.contact.recalc_balance()
            self.contact.recalc_advance_balance()

        # ── Finally, soft delete the payment itself ──
        self.soft_delete()

    def __str__(self):
        return f"{self.direction} - {self.contact.name} - ₹{self.amount}"


# ────────────────────────────────────────────────────────────
# Audit Trail — Production Grade
# ────────────────────────────────────────────────────────────
# Design:
#   • pre_save  → capture old state (for diff)
#   • post_save → log CREATE / UPDATE / SOFT_DELETE / RESTORE
#                 - skips noise-only saves (e.g. updated_at only)
#                 - captures real user via thread-local (audit.py)
#   • post_delete → log hard DELETE
# ────────────────────────────────────────────────────────────

_AUDITED_MODELS = (Invoice, Purchase, Payment, Contact, Product)


@receiver(pre_save)
def _audit_pre_save(sender, instance, **kwargs):
    """Snapshot the current DB state so post_save can diff it."""
    if sender not in _AUDITED_MODELS:
        return
    if not instance.pk:
        instance._audit_old = None
        return
    try:
        instance._audit_old = sender.all_objects.get(pk=instance.pk)
    except sender.DoesNotExist:
        instance._audit_old = None


@receiver(post_save)
def _audit_post_save(sender, instance, created, **kwargs):
    """Write an AuditLog row for CREATE / UPDATE / SOFT_DELETE / RESTORE."""
    if sender not in _AUDITED_MODELS:
        return

    # Local imports — avoid circular import at module load
    from .audit import (
        build_change_diff,
        get_current_ip,
        get_current_user,
        get_current_user_agent,
    )

    old = getattr(instance, '_audit_old', None)

    # ── Determine action ────────────────────────────────
    if created:
        action = 'CREATE'
        changes = {}
    else:
        was_deleted = bool(old and old.is_deleted)
        is_deleted = bool(getattr(instance, 'is_deleted', False))

        if is_deleted and not was_deleted:
            action = 'SOFT_DELETE'
            changes = {}
        elif was_deleted and not is_deleted:
            action = 'RESTORE'
            changes = {}
        else:
            action = 'UPDATE'
            changes = build_change_diff(old, instance)
            if not changes:
                # No meaningful business field changed — skip noise row
                return

    try:
        AuditLog.objects.create(
            content_type=ContentType.objects.get_for_model(instance),
            object_id=instance.pk,
            action=action,
            user=get_current_user(),
            changes=changes,
            ip_address=get_current_ip(),
            user_agent=get_current_user_agent(),
        )
    except Exception:
        logger.exception(
            "Audit log write failed | model=%s | pk=%s",
            sender._meta.label, instance.pk,
        )


@receiver(post_delete)
def _audit_post_delete(sender, instance, **kwargs):
    """Write AuditLog for HARD deletes only (soft deletes don't fire post_delete)."""
    if sender not in _AUDITED_MODELS:
        return

    from .audit import (
        get_current_ip,
        get_current_user,
        get_current_user_agent,
    )

    try:
        AuditLog.objects.create(
            content_type=ContentType.objects.get_for_model(instance),
            object_id=instance.pk,
            action='DELETE',
            user=get_current_user(),
            changes={},
            ip_address=get_current_ip(),
            user_agent=get_current_user_agent(),
        )
    except Exception:
        logger.exception(
            "Audit log delete write failed | model=%s | pk=%s",
            sender._meta.label, instance.pk,
        )


# ────────────────────────────────────────────────────────────
# Bank Transaction → Ledger mirror (manual entries)
# ────────────────────────────────────────────────────────────

@receiver(post_save, sender=BankTransaction)
def _sync_bank_txn_ledger_on_save(sender, instance, **kwargs):
    """Auto-create/refresh the mirror LedgerEntry for manual bank txns."""
    if instance.is_deleted:
        return
    try:
        sync_bank_transaction_ledger(instance)
    except Exception:
        logger.exception(
            "Bank txn ledger sync failed | txn_id=%s", instance.pk,
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
    """
    Opening balance ledger entry management.
    
    Smart behavior:
    - Naya Contact -> create entry
    - Existing Contact, opening_balance changed -> delete old + create new
    - Existing Contact, opening_balance same -> kuch mat karo (skip)
    - opening_balance = 0 -> delete entry
    """
    # Original value DB se lo (agar existing contact hai)
    original_balance = Decimal('0')
    is_new = not instance.pk  # post_save me pk hamesha hoga, isliye created check
    
    # Purani opening entry dhundo
    existing_entry = LedgerEntry.objects.filter(
        entry_type='opening',
        reference_id=instance.id,
    ).first()
    
    # Agar entry pehle se hai aur amount same hai -> kuch nahi karna
    if existing_entry and existing_entry.total_amount == abs(instance.opening_balance or Decimal('0')):
        # Sirf description update karo (name change hua ho sakta hai)
        new_description = f"Opening balance for {instance.name}"
        if existing_entry.description != new_description:
            existing_entry.description = new_description
            existing_entry.save(update_fields=['description'])
        return
    
    # Amount change hua hai YA entry nahi hai -> delete + recreate
    if existing_entry:
        existing_entry.delete()
    
    # Agar opening_balance 0 hai -> kuch nahi banana
    if not instance.opening_balance or instance.opening_balance == 0:
        instance.recalc_balance()
        return
    
    # Naya entry banao
    abs_bal = abs(instance.opening_balance)
    entry_date = instance.opening_balance_date or timezone.now().date()
    entry = LedgerEntry.objects.create(
        date=entry_date,
        entry_type='opening',
        reference_id=instance.id,
        description=f"Opening balance for {instance.name}",
        total_amount=abs_bal,
    )
    
    if instance.contact_type in ('customer', 'both'):
        customer_acc = get_account('1011', 'Customer Receivable', 'asset', '1')
        opening_acc = get_account('3010', 'Opening Balance Equity', 'equity', '3')
        if instance.opening_balance > 0:
            # Customer owes us
            LedgerLine.objects.create(ledger_entry=entry, account=customer_acc, contact=instance,
                                       debit=instance.opening_balance, credit=0)
            LedgerLine.objects.create(ledger_entry=entry, account=opening_acc,
                                       debit=0, credit=instance.opening_balance)
        else:
            # Customer paid us in advance
            LedgerLine.objects.create(ledger_entry=entry, account=customer_acc, contact=instance,
                                       debit=0, credit=abs_bal)
            LedgerLine.objects.create(ledger_entry=entry, account=opening_acc,
                                       debit=abs_bal, credit=0)
    else:  # vendor
        vendor_acc = get_account('2011', 'Vendor Payable', 'liability', '2')
        opening_acc = get_account('3010', 'Opening Balance Equity', 'equity', '3')
        if instance.opening_balance > 0:
            # We owe vendor
            LedgerLine.objects.create(ledger_entry=entry, account=vendor_acc, contact=instance,
                                       debit=0, credit=instance.opening_balance)
            LedgerLine.objects.create(ledger_entry=entry, account=opening_acc,
                                       debit=instance.opening_balance, credit=0)
        else:
            # Vendor owes us
            LedgerLine.objects.create(ledger_entry=entry, account=vendor_acc, contact=instance,
                                       debit=abs_bal, credit=0)
            LedgerLine.objects.create(ledger_entry=entry, account=opening_acc,
                                       debit=0, credit=abs_bal)
    
    instance.recalc_balance()


@receiver(post_save, sender=User)
def create_notification_preferences(sender, instance, created, **kwargs):
    if created:
        NotificationPreference.objects.create(user=instance, categories={})
        
        
# ════════════════════════════════════════════════════════════
# AUTO-CLEANUP — Ledger on document soft-delete
# ════════════════════════════════════════════════════════════
# Why: When documents are soft-deleted via admin bulk action or
# QuerySet.update(), the instance delete() method is bypassed.
# This leaves orphan ledger entries behind, which corrupt
# statements and contact balances.
#
# These signals detect a soft-delete transition (False → True)
# and clean up the linked ledger entries + recalculate contact.

_LEDGER_CLEANUP_MAP = {
    'Payment': (
        ['payment', 'advance_received', 'advance_paid'],
        'contact',
    ),
    'Purchase': (
        ['purchase'],
        'vendor',
    ),
    'Invoice': (
        ['sales'],
        'customer',
    ),
    'CreditNote': (
        ['credit_note'],
        'customer',
    ),
}


@receiver(pre_save)
def _capture_was_deleted_flag(sender, instance, **kwargs):
    """Snapshot is_deleted before save so post_save can detect transition."""
    if sender.__name__ not in _LEDGER_CLEANUP_MAP:
        return
    if not instance.pk:
        instance._was_is_deleted = False
        return
    try:
        old = sender.all_objects.get(pk=instance.pk)
        instance._was_is_deleted = old.is_deleted
    except sender.DoesNotExist:
        instance._was_is_deleted = False


@receiver(post_save)
def _cleanup_ledger_on_soft_delete(sender, instance, created, **kwargs):
    """On fresh soft-delete, remove linked ledger entries and recalc contact."""
    if sender.__name__ not in _LEDGER_CLEANUP_MAP:
        return
    if created:
        return

    was_deleted = getattr(instance, '_was_is_deleted', False)
    is_deleted = getattr(instance, 'is_deleted', False)
    if not is_deleted or was_deleted:
        return  # not a fresh False→True transition

    entry_types, contact_field = _LEDGER_CLEANUP_MAP[sender.__name__]

    try:
        with transaction.atomic():
            # Purchase → also reverse stock via item delete
            if sender.__name__ in ('Purchase', 'Invoice', 'CreditNote'):
                for item in list(instance.items.all()):
                    item.delete()

            # Delete ledger entries (lines first, then entry)
            entries = LedgerEntry.all_objects.filter(
                reference_id=instance.pk,
                entry_type__in=entry_types,
            )
            for entry in list(entries):
                for line in list(entry.lines.all()):
                    line.delete()
                entry.delete()

            # Recalc the related contact
            contact = getattr(instance, contact_field, None)
            if contact is not None:
                try:
                    contact.recalc_balance()
                    contact.recalc_advance_balance()
                except Exception:
                    logger.exception(
                        "Contact recalc failed after %s soft-delete | id=%s",
                        sender.__name__, instance.pk,
                    )
    except Exception:
        logger.exception(
            "Ledger cleanup failed after %s soft-delete | id=%s",
            sender.__name__, instance.pk,
        )


@receiver(post_save, sender=LedgerLine)
@receiver(post_delete, sender=LedgerLine)
def sync_contact_balance_on_ledger_line_change(sender, instance, **kwargs):
    if instance.contact:
        instance.contact.recalc_balance()