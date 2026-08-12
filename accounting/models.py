from decimal import Decimal
from django.db import models, transaction
from django.conf import settings
from django.db.models import F, Sum, Q
from django.core.validators import MinValueValidator, RegexValidator
from django.utils import timezone
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.contrib.auth.models import User
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
import cloudinary.uploader
from cloudinary_storage.storage import MediaCloudinaryStorage
from .validators import validate_image_file_extension, validate_image_binary
from accounting.image_processor import process_uploaded_image


# ============================================================
# 1. COMPANY & SETTINGS 
# ============================================================

class CompanyProfile(models.Model):
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
    invoice_prefix = models.CharField(max_length=10, default="INV", help_text="e.g., INV, REP, PUR")
    invoice_start_number = models.PositiveIntegerField(default=1)
    default_tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=18.00,
        help_text="Default GST rate in %"
    )
    financial_year_start = models.DateField(default=timezone.now)
    state = models.CharField(max_length=100, blank=True, help_text="Home state for GST (CGST/SGST)")

    # ----- FIELDS FOR LANDING PAGE -----
    tagline = models.CharField(
        max_length=255,
        blank=True,
        default="Expert chip-level repair, sales, and service for all brands.",
        help_text="Short tagline for hero section"
    )
    hero_image = models.ImageField(
        upload_to='company_hero/',
        blank=True,
        null=True,
        help_text="Upload hero background or main image (recommended size: 1200x600)"
    )
    about_text = models.TextField(
        blank=True,
        help_text="About the company (used in footer or about section)"
    )
    google_map_embed = models.TextField(
        blank=True,
        help_text="Paste the full Google Maps iframe embed code"
    )
    working_hours = models.CharField(
        max_length=100,
        blank=True,
        default="Mon–Sat: 10:00 AM – 8:00 PM",
        help_text="Display working hours"
    )
    facebook_url = models.URLField(blank=True, help_text="Facebook page URL")
    blog_url = models.URLField(blank=True, help_text="blog page url")
    instagram_url = models.URLField(blank=True, help_text="Instagram profile URL")
    youtube_url = models.URLField(blank=True, help_text="YouTube channel URL")
    whatsapp_number = models.CharField(
        max_length=20,
        blank=True,
        help_text="WhatsApp number with country code (e.g., +919876543210)"
    )
    google_review_link = models.URLField(blank=True, help_text="Google Review URL (e.g., https://search.google.com/local/reviews?placeid=...)")
    
    # ----- SEO FIELDS -----
    meta_title = models.CharField(
        max_length=70,
        blank=True,
        help_text="SEO Title (max 70 chars). If blank, uses company name."
    )
    meta_description = models.CharField(
        max_length=160,
        blank=True,
        help_text="SEO Description (max 160 chars). Used in search results."
    )
    meta_keywords = models.CharField(
        max_length=200,
        blank=True,
        help_text="Comma-separated keywords (e.g., laptop repair, printer repair, computer service)"
    )
    og_image = models.ImageField(
        upload_to='og_images/',
        blank=True,
        null=True,
        help_text="Open Graph image (recommended: 1200x630) for social sharing"
    )

    class Meta:
        verbose_name_plural = "Company Profile"

    def save(self, *args, **kwargs):
        from django.core.files.uploadedfile import UploadedFile

        if self.logo and hasattr(self.logo, 'file') and isinstance(self.logo.file, UploadedFile):
            self.logo = process_uploaded_image(self.logo.file)  

        if self.hero_image and hasattr(self.hero_image, 'file') and isinstance(self.hero_image.file, UploadedFile):
            self.hero_image = process_uploaded_image(self.hero_image.file)

        if self.og_image and hasattr(self.og_image, 'file') and isinstance(self.og_image.file, UploadedFile):
            self.og_image = process_uploaded_image(self.og_image.file)

        super().save(*args, **kwargs)

    @classmethod
    def get_instance(cls):
        obj, created = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return self.name
    

# ============================================================
# 16. SERVICES (Dynamic Services for Landing Page)
# ============================================================

class Service(models.Model):
    title = models.CharField(max_length=200, help_text="Service title (e.g., Chip-Level Repair)")
    description = models.TextField(help_text="Short description of the service")
    icon = models.CharField(
        max_length=50,
        blank=True,
        help_text="Bootstrap icon class (e.g., bi-tools, bi-display, bi-printer). See https://icons.getbootstrap.com/"
    )
    image = models.ImageField(
        upload_to='services/',
        blank=True,
        null=True,
        help_text="Optional image (overrides icon if provided)"
    )
    order = models.PositiveIntegerField(default=0, help_text="Display order (lower = first)")
    is_active = models.BooleanField(default=True, help_text="Show on landing page")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['order', 'created_at']
        verbose_name = "Service"
        verbose_name_plural = "Services"
        

    def __str__(self):
        return self.title
    
    
    def save(self, *args, **kwargs):
        from django.core.files.uploadedfile import UploadedFile

        if self.image and hasattr(self.image, 'file') and isinstance(self.image.file, UploadedFile):
            self.image = process_uploaded_image(self.image.file)
        super().save(*args, **kwargs)

    def get_icon_html(self):
        """Returns HTML for icon or image"""
        if self.image:
            return f'<img src="{self.image.url}" alt="{self.title}" style="height:60px; width:auto;">'
        elif self.icon:
            return f'<i class="bi {self.icon}"></i>'
        return '<i class="bi bi-box"></i>'  


# ============================================================
# 15. TESTIMONIALS (Customer Reviews)
# ============================================================

class Testimonial(models.Model):
    RATING_CHOICES = [
        (1, '⭐ 1 Star'),
        (2, '⭐ 2 Stars'),
        (3, '⭐ 3 Stars'),
        (4, '⭐ 4 Stars'),
        (5, '⭐ 5 Stars'),
    ]

    customer_name = models.CharField(max_length=100)
    customer_photo = models.ImageField(
        upload_to='testimonials/',
        blank=True,
        null=True,
        help_text="Optional profile photo"
    )
    designation = models.CharField(max_length=100, blank=True, help_text="e.g., Business Owner, Student")
    company_name = models.CharField(max_length=100, blank=True, help_text="e.g., Google, Microsoft")
    review_text = models.TextField(help_text="Customer's feedback")
    rating = models.PositiveSmallIntegerField(choices=RATING_CHOICES, default=5)
    order = models.PositiveIntegerField(default=0, help_text="Display order (lower = first)")
    is_active = models.BooleanField(default=True, help_text="Show on landing page")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['order', '-created_at']
        verbose_name = "Testimonial"
        verbose_name_plural = "Testimonials"
        
        
    def save(self, *args, **kwargs):
        from django.core.files.uploadedfile import UploadedFile

        if self.customer_photo and hasattr(self.customer_photo, 'file') and isinstance(self.customer_photo.file, UploadedFile):
            self.customer_photo = process_uploaded_image(self.customer_photo.file)
        super().save(*args, **kwargs)
        
    def __str__(self):
        return self.customer_name

    def get_star_display(self):
        return '⭐' * self.rating
    


# ============================================================
# 14. FAQ (Frequently Asked Questions)
# ============================================================

class FAQ(models.Model):
    question = models.CharField(max_length=300, help_text="The question")
    answer = models.TextField(help_text="The answer to the question")
    order = models.PositiveIntegerField(default=0, help_text="Display order (lower numbers appear first)")
    is_active = models.BooleanField(default=True, help_text="Show on the landing page")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['order', 'created_at']
        verbose_name = "FAQ"
        verbose_name_plural = "FAQs"

    def __str__(self):
        return self.question

# ============================================================
# 2. LEDGER (Double-Entry)
# ============================================================


class LedgerEntry(models.Model):
    ENTRY_TYPE = (
        ('sales', 'Sales'),
        ('purchase', 'Purchase'),
        ('payment', 'Payment'),
        ('receipt', 'Receipt'),
        ('contra', 'Contra'),
        ('journal', 'Journal'),
        ('opening', 'Opening Balance'),
    )
    
    JOURNAL_TYPES = (
        ('discount', 'Discount'),
        ('advance_received', 'Advance Received'),
        ('advance_paid', 'Advance Paid'),
        ('payment', 'Payment to Vendor'),
        ('receipt', 'Receipt from Customer'),
        ('general', 'General Journal'),
    )
    
    date = models.DateField(default=timezone.now)
    entry_type = models.CharField(max_length=12, choices=ENTRY_TYPE)
    journal_type = models.CharField(max_length=20, choices=JOURNAL_TYPES, blank=True, null=True, help_text="Specific type for journal entries")
    reference_id = models.PositiveIntegerField(blank=True, null=True)
    description = models.CharField(max_length=200)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date']
        indexes = [
            models.Index(fields=['entry_type', 'reference_id']),
            models.Index(fields=['date']),
            models.Index(fields=['journal_type']),  
        ]

    def __str__(self):
        return f"{self.entry_type} - {self.description} (₹{self.total_amount})"


class LedgerLine(models.Model):
    ledger_entry = models.ForeignKey(LedgerEntry, on_delete=models.CASCADE, related_name='lines')
    account = models.CharField(max_length=100)
    contact = models.ForeignKey(
        'Contact',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ledger_lines'
    )
    debit = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    credit = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        indexes = [
            models.Index(fields=['account']),
            models.Index(fields=['contact']),
        ]

    def __str__(self):
        return f"{self.account} Dr:{self.debit} Cr:{self.credit}"


# ============================================================
# 3. CONTACTS (Customers & Vendors)
# ============================================================

class Contact(models.Model):
    CONTACT_TYPE = (
        ('customer', 'Customer'),
        ('vendor', 'Vendor'),
        ('both', 'Both'),
    )
    contact_type = models.CharField(max_length=10, choices=CONTACT_TYPE)
    name = models.CharField(max_length=200)
    company_name = models.CharField(max_length=200, blank=True)
    phone = models.CharField(
        max_length=20,
        null=True,
        blank=True,
        help_text="Must be unique. Leave blank if unknown.",
        validators=[RegexValidator(r'^\+?\d{10,15}$', message="Enter a valid phone number.")]
    )
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    gstin = models.CharField(max_length=15, blank=True)
    state = models.CharField(max_length=100, blank=True, help_text="For IGST/CGST+SGST determination")
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='customer_contact'
    )

    class Meta:
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(
                fields=['phone'],
                condition=Q(phone__isnull=False),
                name='unique_phone_non_null'
            )
        ]
        indexes = [
            models.Index(fields=['name']),
            models.Index(fields=['contact_type']),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_contact_type_display()})"

    @property
    def balance(self):
        dr = self.ledger_lines.filter(debit__gt=0).aggregate(total=Sum('debit'))['total'] or Decimal('0')
        cr = self.ledger_lines.filter(credit__gt=0).aggregate(total=Sum('credit'))['total'] or Decimal('0')
        if self.contact_type in ('customer', 'both'):
            return dr - cr
        return cr - dr


# ============================================================
# 4. INVENTORY
# ============================================================

class ProductCategory(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "Product Categories"
        ordering = ['name']

    def __str__(self):
        return self.name


class Product(models.Model):
    UNIT_CHOICES = (
        ('pcs', 'Pieces'),
        ('kg', 'Kilograms'),
        ('ltr', 'Litres'),
        ('hrs', 'Hours (Service)'),
        ('nos', 'Numbers'),
    )
    name = models.CharField(max_length=200)
    hsn_code = models.CharField(max_length=10, blank=True, help_text="HSN/SAC code for GST")
    category = models.ForeignKey(
        ProductCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='products'
    )
    unit = models.CharField(max_length=10, choices=UNIT_CHOICES, default='pcs')
    purchase_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    selling_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    current_stock = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Physical stock count (non-service items)"
    )
    low_stock_threshold = models.PositiveIntegerField(default=5)
    tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=18.00,
        help_text="Default GST rate for this product"
    )
    is_service = models.BooleanField(default=False, help_text="Service items (labour) don't affect stock")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['name']),
            models.Index(fields=['is_active']),
        ]

    def __str__(self):
        return f"{self.name} ({self.hsn_code or 'N/A'})"

    def low_stock_alert(self):
        if self.is_service:
            return False
        return self.current_stock <= self.low_stock_threshold


# ============================================================
# 5. SALES INVOICE
# ============================================================

class Invoice(models.Model):
    GST_TYPE = (
        ('regular', 'Regular (B2B)'),
        ('non_gst', 'Non-GST'),
        ('interstate', 'Inter-state (IGST)'),
        ('intrastate', 'Intra-state (CGST+SGST)'),
    )
    PAYMENT_STATUS = (
        ('paid', 'Paid'),
        ('partial', 'Partially Paid'),
        ('unpaid', 'Unpaid'),
    )

    invoice_number = models.CharField(max_length=50, unique=True, editable=False)
    customer = models.ForeignKey(
        Contact,
        on_delete=models.PROTECT,
        related_name='sales_invoices',
        limit_choices_to={'contact_type__in': ['customer', 'both']}
    )
    date = models.DateField(default=timezone.now)
    due_date = models.DateField(blank=True, null=True)
    gst_type = models.CharField(max_length=12, choices=GST_TYPE, default='regular')
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Flat discount, not percentage"
    )
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    grand_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    balance_due = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_status = models.CharField(max_length=10, choices=PAYMENT_STATUS, default='unpaid')
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date', '-invoice_number']
        indexes = [
            models.Index(fields=['invoice_number']),
            models.Index(fields=['customer', 'payment_status']),
        ]

    def __str__(self):
        return f"Invoice {self.invoice_number} - {self.customer.name}"

    def save(self, *args, **kwargs):
        if not self.invoice_number:
            prefix = CompanyProfile.get_instance().invoice_prefix or "INV"
            last = Invoice.objects.order_by('-id').first()
            next_num = 1
            if last:
                try:
                    next_num = int(last.invoice_number.split('-')[-1]) + 1
                except (ValueError, IndexError):
                    next_num = last.id + 1
            self.invoice_number = f"{prefix}-{next_num:04d}"

        self.calculate_totals()
        self.balance_due = self.grand_total - self.paid_amount
        if self.balance_due <= 0:
            self.payment_status = 'paid'
        elif self.paid_amount > 0 and self.balance_due < self.grand_total:
            self.payment_status = 'partial'
        else:
            self.payment_status = 'unpaid'

        super().save(*args, **kwargs)

    def calculate_totals(self):
        if self.pk is None:
            self.subtotal = Decimal('0')
            self.tax_amount = Decimal('0')
            self.grand_total = Decimal('0')
            return

        items = self.items.all()
        self.subtotal = sum(item.quantity * item.unit_price for item in items)
        self.tax_amount = sum(item.tax_amount for item in items)
        discounted_subtotal = self.subtotal - self.discount_amount
        if discounted_subtotal < 0:
            discounted_subtotal = Decimal('0')
        self.grand_total = discounted_subtotal + self.tax_amount

    def update_paid_amount(self):
        total = self.payments_received.aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
        self.paid_amount = total
        self.save(update_fields=['paid_amount', 'balance_due', 'payment_status'])

    def get_gst_breakup(self):
        items = self.items.all()
        tax_total = Decimal('0')
        cgst = Decimal('0')
        sgst = Decimal('0')
        igst = Decimal('0')
        for item in items:
            tax = item.tax_amount
            tax_total += tax
            if self.gst_type == 'intrastate':
                cgst += tax / 2
                sgst += tax / 2
            elif self.gst_type == 'interstate':
                igst += tax
        return {'total_tax': tax_total, 'cgst': cgst, 'sgst': sgst, 'igst': igst}

    def update_stock_from_items(self):
        StockMovement.objects.filter(reference=self.invoice_number).delete()
        for item in self.items.all():
            if not item.product.is_service:
                StockMovement.objects.create(
                    product=item.product,
                    movement_type='sale_out',
                    quantity=-item.quantity,
                    reference=self.invoice_number,
                    date=self.date
                )
        for product in Product.objects.filter(is_service=False):
            total_qty = StockMovement.objects.filter(product=product).aggregate(total=Sum('quantity'))['total'] or 0
            product.current_stock = total_qty
            product.save(update_fields=['current_stock'])


class InvoiceItem(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    description = models.CharField(max_length=200, blank=True)
    quantity = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=1,
        validators=[MinValueValidator(Decimal('0.01'))]
    )
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        indexes = [
            models.Index(fields=['invoice', 'product']),
        ]

    @transaction.atomic
    def save(self, *args, **kwargs):
        is_new = self.pk is None
        old_qty = 0
        if not is_new:
            old = InvoiceItem.objects.get(pk=self.pk)
            old_qty = old.quantity

        if not self.unit_price:
            self.unit_price = self.product.selling_price

        if self.invoice and self.invoice.gst_type == 'non_gst':
            self.tax_rate = Decimal('0')
            self.tax_amount = Decimal('0')
        else:
            if not self.tax_rate:
                self.tax_rate = self.product.tax_rate
            line_amount = self.quantity * self.unit_price
            self.tax_amount = (line_amount * self.tax_rate) / 100

        line_amount = self.quantity * self.unit_price
        self.line_total = line_amount + self.tax_amount

        super().save(*args, **kwargs)

        if not self.product.is_service:
            delta_qty = self.quantity - old_qty
            Product.objects.filter(pk=self.product_id).update(current_stock=F('current_stock') - delta_qty)
            StockMovement.objects.create(
                product=self.product,
                movement_type='sale_out',
                quantity=-delta_qty,
                reference=self.invoice.invoice_number,
                date=self.invoice.date
            )

        if self.invoice:
            self.invoice.save()

    @transaction.atomic
    def delete(self, *args, **kwargs):
        if not self.product.is_service:
            Product.objects.filter(pk=self.product_id).update(current_stock=F('current_stock') + self.quantity)
            StockMovement.objects.filter(
                reference=self.invoice.invoice_number,
                product=self.product
            ).delete()
        invoice = self.invoice
        super().delete(*args, **kwargs)
        if invoice:
            invoice.save()

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"


# ============================================================
# 6. PURCHASE INVOICE
# ============================================================

class Purchase(models.Model):
    PURCHASE_GST_TYPE = (
        ('regular', 'Regular'),
        ('non_gst', 'Non-GST'),
        ('interstate', 'Inter-state'),
        ('intrastate', 'Intra-state'),
    )
    purchase_number = models.CharField(max_length=50, unique=True, editable=False)
    vendor = models.ForeignKey(
        Contact,
        on_delete=models.PROTECT,
        related_name='purchases',
        limit_choices_to={'contact_type__in': ['vendor', 'both']}
    )
    date = models.DateField(default=timezone.now)
    gst_type = models.CharField(max_length=12, choices=PURCHASE_GST_TYPE, default='regular')
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    grand_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    paid = models.BooleanField(default=False)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date']
        indexes = [
            models.Index(fields=['purchase_number']),
            models.Index(fields=['vendor']),
        ]

    def save(self, *args, **kwargs):
        if not self.purchase_number:
            last = Purchase.objects.order_by('-id').first()
            next_num = 1
            if last:
                try:
                    next_num = int(last.purchase_number.split('-')[-1]) + 1
                except (ValueError, IndexError):
                    next_num = last.id + 1
            self.purchase_number = f"PUR-{next_num:04d}"
        if self.pk:
            self.calculate_totals()
        super().save(*args, **kwargs)

    def calculate_totals(self):
        items = self.items.all()
        self.subtotal = sum(item.quantity * item.unit_price for item in items)
        self.tax_amount = sum(item.tax_amount for item in items)
        self.grand_total = self.subtotal + self.tax_amount

    def update_stock_from_items(self):
        StockMovement.objects.filter(reference=self.purchase_number).delete()
        for item in self.items.all():
            if not item.product.is_service:
                StockMovement.objects.create(
                    product=item.product,
                    movement_type='purchase_in',
                    quantity=item.quantity,
                    reference=self.purchase_number,
                    date=self.date
                )
        for product in Product.objects.filter(is_service=False):
            total_qty = StockMovement.objects.filter(product=product).aggregate(total=Sum('quantity'))['total'] or 0
            product.current_stock = total_qty
            product.save(update_fields=['current_stock'])

    def __str__(self):
        return f"Purchase {self.purchase_number} from {self.vendor.name}"


class PurchaseItem(models.Model):
    purchase = models.ForeignKey(Purchase, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    description = models.CharField(max_length=200, blank=True)
    quantity = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=1,
        validators=[MinValueValidator(Decimal('0.01'))]
    )
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    @transaction.atomic
    def save(self, *args, **kwargs):
        is_new = self.pk is None
        old_qty = 0
        if not is_new:
            old = PurchaseItem.objects.get(pk=self.pk)
            old_qty = old.quantity

        if not self.unit_price:
            self.unit_price = self.product.purchase_price
        if not self.tax_rate:
            self.tax_rate = self.product.tax_rate
        line_amount = self.quantity * self.unit_price
        self.tax_amount = (line_amount * self.tax_rate) / 100
        self.line_total = line_amount + self.tax_amount

        super().save(*args, **kwargs)

        if not self.product.is_service:
            delta_qty = self.quantity - old_qty
            Product.objects.filter(pk=self.product_id).update(current_stock=F('current_stock') + delta_qty)
            StockMovement.objects.create(
                product=self.product,
                movement_type='purchase_in',
                quantity=delta_qty,
                reference=self.purchase.purchase_number,
                date=self.purchase.date
            )

        if self.purchase:
            self.purchase.save()

    @transaction.atomic
    def delete(self, *args, **kwargs):
        if not self.product.is_service:
            Product.objects.filter(pk=self.product_id).update(current_stock=F('current_stock') - self.quantity)
            StockMovement.objects.filter(
                reference=self.purchase.purchase_number,
                product=self.product
            ).delete()
        purchase = self.purchase
        super().delete(*args, **kwargs)
        if purchase:
            purchase.save()

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"


# ============================================================
# 7. REPAIR JOBS
# ============================================================

class RepairJob(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('diagnosis', 'Diagnosis'),
        ('repairing', 'Repairing'),
        ('ready', 'Ready for Delivery'),
        ('delivered', 'Delivered'),
        ('cancelled', 'Cancelled'),
    )
    ESTIMATE_STATUS_CHOICES = (
        ('pending', 'Pending Approval'),
        ('approved', 'Approved'),
        ('on_hold', 'On Hold'),
        ('rejected', 'Rejected'),
    )

    job_number = models.CharField(max_length=50, unique=True, editable=False)
    customer = models.ForeignKey(
        Contact,
        on_delete=models.PROTECT,
        related_name='repair_jobs',
        limit_choices_to={'contact_type__in': ['customer', 'both']}
    )
    device_model = models.CharField(max_length=200)
    serial_number = models.CharField(max_length=100, blank=True)
    issue_description = models.TextField()
    accessories = models.TextField(blank=True, null=True, help_text="Items received with device")
    device_condition = models.TextField(blank=True, null=True, help_text="Physical condition")
    action_taken = models.TextField(blank=True, help_text="What work was done")
    diagnosis_report = models.TextField(blank=True, null=True, help_text="Technician's diagnosis")
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='pending')
    estimate_status = models.CharField(
        max_length=10,
        choices=ESTIMATE_STATUS_CHOICES,
        default='pending',
        help_text="Customer approval status of the estimate"
    )
    estimate_approved_at = models.DateTimeField(null=True, blank=True)
    estimate_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_estimates',
        help_text="User who approved the estimate"
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
        help_text="How was the estimate approved?"
    )
    approval_remarks = models.TextField(
        blank=True, 
        help_text="Staff notes for verbal approvals (e.g., Customer called and agreed)"
    )
    estimated_cost = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    final_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    labour_charge = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text="Service/Labour charge"
    )
    received_by = models.CharField(max_length=100, blank=True, null=True)
    delivered_by = models.CharField(max_length=100, blank=True, null=True)
    date_in = models.DateField(default=timezone.now)
    delivery_date = models.DateField(blank=True, null=True)

    # Delivery Recipient Details
    delivered_to_name = models.CharField(
        max_length=200,
        blank=True,
        null=True,
        help_text="Name of the person who received the device (if different from customer)"
    )
    delivered_to_phone = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        help_text="Phone number of the recipient"
    )
    delivered_to_designation = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Designation/Role of the recipient (e.g., Driver, Accountant, Office Boy)"
    )
    delivery_remarks = models.TextField(
        blank=True,
        null=True,
        help_text="Any special remarks during delivery (e.g., Device condition at delivery, Signature remarks)"
    )

    invoice = models.OneToOneField(Invoice, on_delete=models.SET_NULL, blank=True, null=True)
    notes = models.TextField(blank=True)
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
    
    def save(self, *args, **kwargs):
            is_new = self.pk is None
    
            if is_new:
                last = RepairJob.objects.order_by('-id').first()
                next_num = 1
                if last:
                    try:
                        next_num = int(last.job_number.split('-')[-1]) + 1
                    except (ValueError, IndexError):
                        next_num = last.id + 1
                self.job_number = f"REP-{next_num:04d}"
    
            old_status = None
            if not is_new:
                try:
                    old_status = RepairJob.objects.get(pk=self.pk).status
                except RepairJob.DoesNotExist:
                    pass
    
            if self.status not in ['delivered', 'cancelled']:
                if self.status == 'pending' and self.diagnosis_report:
                    self.status = 'diagnosis'
                elif self.status == 'repairing' and self.action_taken:
                    self.status = 'ready'
    
            if self.pk:
                parts_total = self.parts.aggregate(total=Sum('line_total'))['total'] or Decimal('0')
                self.final_amount = parts_total + self.labour_charge
    
            super().save(*args, **kwargs)
    
            # =============================================
            # NOTIFICATION LOGIC (With Full Error Handling)
            # =============================================
            if old_status and old_status != self.status:
                from django.urls import reverse
                from accounting.utils.notification_helpers import send_notification_to_customer, send_notification_sse
                from django.contrib.auth.models import User
                import logging
                logger = logging.getLogger(__name__)
    
                try:
                    # Notification to Customer
                    send_notification_to_customer(
                        self.customer,
                        title=f"Repair Status Updated: {self.job_number}",
                        message=f"Your repair for {self.device_model} is now {self.get_status_display()}.",
                        link=reverse('customer:customer_repair_detail', args=[self.pk]),
                        notif_type='info',
                        category='repairs',
                        send_email=False
                    )
    
                    for staff in User.objects.filter(is_staff=True):
                        send_notification_sse(staff)
                        
                except Exception as notif_error:
                    logger.error(f"🔥 Notification failed during RepairJob.save() for job {self.job_number}: {notif_error}", exc_info=True)



    def calculate_final_amount(self):
        parts_total = sum(part.quantity * part.unit_price for part in self.parts.all())
        self.final_amount = parts_total + self.labour_charge
        self.save(update_fields=['final_amount'])


class RepairPart(models.Model):
    repair_job = models.ForeignKey(RepairJob, on_delete=models.CASCADE, related_name='parts')
    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        limit_choices_to={'is_service': False}
    )
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    line_total = models.DecimalField(max_digits=12, decimal_places=2, editable=False)

    def save(self, *args, **kwargs):
        if not self.unit_price:
            self.unit_price = self.product.selling_price
        self.line_total = self.quantity * self.unit_price
        super().save(*args, **kwargs)
        self.repair_job.calculate_final_amount()

    def __str__(self):
        return f"{self.product.name} x {self.quantity} for Job {self.repair_job.job_number}"


# ============================================================
# 8. PAYMENTS
# ============================================================

class Payment(models.Model):
    DIRECTION = (
        ('paid', 'Paid to Supplier'),
        ('received', 'Received from Customer'),
    )
    METHOD_CHOICES = (
        ('cash', 'Cash'),
        ('bank', 'Bank Transfer'),
        ('upi', 'UPI'),
    )
    direction = models.CharField(max_length=10, choices=DIRECTION)
    contact = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name='payments')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    date = models.DateField(default=timezone.now)
    method = models.CharField(max_length=10, choices=METHOD_CHOICES, default='cash')
    
    # 🔹 Link to actual BankAccount (instead of text choice)
    bank_account = models.ForeignKey(
        'BankAccount',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='payments',
        help_text="Select the bank/cash account used for this transaction."
    )
    
    # 🔹 UPI Reference / Transaction ID
    upi_ref = models.CharField(
        max_length=100,
        blank=True,
        help_text="UPI Transaction ID / Reference Number (if applicable)"
    )
    
    # 🔹 Reconciliation flag
    reconciled = models.BooleanField(
        default=False,
        help_text="Mark as reconciled with bank statement"
    )
    
    # Legacy fields (kept for backward compatibility, but can be removed later)
    account_name = models.CharField(max_length=50, blank=True)  # will be deprecated
    reference = models.CharField(max_length=100, blank=True)
    description = models.CharField(max_length=200, blank=True)
    invoices = models.ManyToManyField(Invoice, blank=True, related_name='payments_received')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date']
        indexes = [
            models.Index(fields=['contact', 'direction']),
            models.Index(fields=['bank_account']),
        ]

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new:
            self.create_ledger_entry()
            self.create_bank_transaction() 
        else:
            self.update_ledger_entry()
            self.update_bank_transaction()  
        self.update_invoices()

    # ----- Ledger -----
    def create_ledger_entry(self):
        entry = LedgerEntry.objects.create(
            date=self.date,
            entry_type='payment',
            reference_id=self.id,
            description=f"Payment {self.direction} from/to {self.contact.name}",
            total_amount=self.amount
        )
        if self.direction == 'received':
            LedgerLine.objects.create(ledger_entry=entry, account='Cash', debit=self.amount, credit=0)
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=f'Customer: {self.contact.name}',
                contact=self.contact,
                debit=0,
                credit=self.amount
            )
        else:
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=f'Vendor: {self.contact.name}',
                contact=self.contact,
                debit=self.amount,
                credit=0
            )
            LedgerLine.objects.create(ledger_entry=entry, account='Cash', debit=0, credit=self.amount)

    def update_ledger_entry(self):
        entry = LedgerEntry.objects.filter(reference_id=self.id, entry_type='payment').first()
        if not entry:
            self.create_ledger_entry()
            return
        entry.date = self.date
        entry.description = f"Payment {self.direction} from/to {self.contact.name}"
        entry.total_amount = self.amount
        entry.save()
        entry.lines.all().delete()
        if self.direction == 'received':
            LedgerLine.objects.create(ledger_entry=entry, account='Cash', debit=self.amount, credit=0)
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=f'Customer: {self.contact.name}',
                contact=self.contact,
                debit=0,
                credit=self.amount
            )
        else:
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=f'Vendor: {self.contact.name}',
                contact=self.contact,
                debit=self.amount,
                credit=0
            )
            LedgerLine.objects.create(ledger_entry=entry, account='Cash', debit=0, credit=self.amount)

    # ----- Bank Transaction (Auto) -----
    def create_bank_transaction(self):
        if not self.bank_account:
            return  # Skip if no bank account selected
        txn_type = 'deposit' if self.direction == 'received' else 'withdrawal'
        source_type = 'payment_received' if self.direction == 'received' else 'payment_made'
        BankTransaction.objects.create(
            bank_account=self.bank_account,
            transaction_type=txn_type,
            source_type=source_type,
            amount=self.amount,
            date=self.date,
            description=f"{self.get_direction_display()} - {self.contact.name}" + (f" ({self.description})" if self.description else ""),
            reference=self.reference or self.upi_ref,
            payment=self,
            invoice=self.invoices.first() if self.invoices.exists() else None,
            reconciled=self.reconciled,
        )

    def update_bank_transaction(self):
        # If bank account is not selected, remove any existing bank transaction
        if not self.bank_account:
            BankTransaction.objects.filter(payment=self).delete()
            return
        txn = BankTransaction.objects.filter(payment=self).first()
        if txn:
            txn.bank_account = self.bank_account
            txn.transaction_type = 'deposit' if self.direction == 'received' else 'withdrawal'
            txn.source_type = 'payment_received' if self.direction == 'received' else 'payment_made'
            txn.amount = self.amount
            txn.date = self.date
            txn.description = f"{self.get_direction_display()} - {self.contact.name}" + (f" ({self.description})" if self.description else "")
            txn.reference = self.reference or self.upi_ref
            txn.invoice = self.invoices.first() if self.invoices.exists() else None
            txn.reconciled = self.reconciled
            txn.save()
        else:
            self.create_bank_transaction()

    # ----- Invoice Update -----
    def update_invoices(self):
        if self.direction == 'received':
            for inv in self.invoices.all():
                inv.update_paid_amount()

    def delete(self, *args, **kwargs):
        # Delete associated bank transactions and ledger entries
        BankTransaction.objects.filter(payment=self).delete()
        LedgerEntry.objects.filter(reference_id=self.id, entry_type='payment').delete()
        super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.direction} - {self.contact.name} - ₹{self.amount}"


# ============================================================
# 9. BANK ACCOUNTS
# ============================================================

class BankAccount(models.Model):
    ACCOUNT_TYPES = (
        ('savings', 'Savings Account'),
        ('current', 'Current Account'),
        ('upi', 'UPI / Wallet'),
    )
    name = models.CharField(max_length=100, help_text="e.g., HDFC Savings, UPI-GooglePay, etc.")
    account_number = models.CharField(max_length=50, blank=True, help_text="Account number (if applicable)")
    bank_name = models.CharField(max_length=100, blank=True)
    ifsc_code = models.CharField(max_length=20, blank=True)
    account_type = models.CharField(max_length=10, choices=ACCOUNT_TYPES, default='savings')
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    current_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['name']),
            models.Index(fields=['is_active']),
        ]

    def __str__(self):
        return f"{self.name} ({self.bank_name or self.account_type})"

    def update_balance(self):
        total_deposit = self.transactions.filter(transaction_type='deposit').aggregate(
            total=Sum('amount')
        )['total'] or Decimal('0')
        total_withdrawal = self.transactions.filter(transaction_type='withdrawal').aggregate(
            total=Sum('amount')
        )['total'] or Decimal('0')
        self.current_balance = self.opening_balance + total_deposit - total_withdrawal
        self.save(update_fields=['current_balance'])


class BankTransaction(models.Model):
    TRANSACTION_TYPES = (
        ('deposit', 'Deposit (Money In)'),
        ('withdrawal', 'Withdrawal (Money Out)'),
    )
    SOURCE_TYPES = (
        ('payment_received', 'Payment from Customer'),
        ('payment_made', 'Payment to Vendor'),
        ('transfer', 'Bank Transfer'),
        ('interest', 'Interest Earned'),
        ('bank_charge', 'Bank Charge'),
        ('manual', 'Manual Entry'),
    )
    bank_account = models.ForeignKey(
        BankAccount,
        on_delete=models.CASCADE,
        related_name='transactions'
    )
    transaction_type = models.CharField(max_length=10, choices=TRANSACTION_TYPES)
    source_type = models.CharField(max_length=20, choices=SOURCE_TYPES, default='manual')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    date = models.DateField(default=timezone.now)
    description = models.CharField(max_length=200, blank=True)
    reference = models.CharField(max_length=100, blank=True, help_text="Invoice #, Payment #, or Reference")
    # Optional links
    payment = models.ForeignKey(
        'Payment',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='bank_transactions'
    )
    invoice = models.ForeignKey('Invoice', on_delete=models.SET_NULL, null=True, blank=True)
    purchase = models.ForeignKey('Purchase', on_delete=models.SET_NULL, null=True, blank=True)
    reconciled = models.BooleanField(default=False, help_text="Matched with bank statement")   # ✅ ADD THIS
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-created_at']
        indexes = [
            models.Index(fields=['bank_account', 'date']),
            models.Index(fields=['transaction_type']),
        ]

    def __str__(self):
        return f"{self.get_transaction_type_display()} - {self.amount} ({self.bank_account.name})"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.bank_account.update_balance()

    def delete(self, *args, **kwargs):
        account = self.bank_account
        super().delete(*args, **kwargs)
        account.update_balance()


# ============================================================
# 10. STOCK MOVEMENT
# ============================================================

class StockMovement(models.Model):
    MOVEMENT_TYPE = (
        ('purchase_in', 'Purchase In'),
        ('sale_out', 'Sale Out'),
        ('repair_out', 'Repair Usage Out'),
        ('return_in', 'Return In'),
        ('adjustment', 'Stock Adjustment'),
    )
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    movement_type = models.CharField(max_length=15, choices=MOVEMENT_TYPE)
    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Positive for stock-in, negative for stock-out"
    )
    reference = models.CharField(max_length=200, blank=True)
    date = models.DateTimeField(default=timezone.now)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-date']
        indexes = [
            models.Index(fields=['product']),
            models.Index(fields=['movement_type']),
        ]

    def __str__(self):
        return f"{self.product.name} {self.get_movement_type_display()} ({self.quantity})"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        total_movement = StockMovement.objects.filter(product=self.product).aggregate(
            total=Sum('quantity')
        )['total'] or 0
        self.product.current_stock = total_movement
        self.product.save(update_fields=['current_stock'])

    def delete(self, *args, **kwargs):
        product = self.product
        super().delete(*args, **kwargs)
        total = StockMovement.objects.filter(product=product).aggregate(total=Sum('quantity'))['total'] or 0
        product.current_stock = total
        product.save(update_fields=['current_stock'])


# ============================================================
# 11. LEGACY TRANSACTIONS (kept for compatibility)
# ============================================================

class Transaction(models.Model):
    TRANSACTION_TYPE = (
        ('sale', 'Sale'),
        ('purchase', 'Purchase'),
        ('payment_received', 'Payment Received'),
        ('payment_made', 'Payment Made'),
        ('expense', 'Expense'),
        ('income', 'Income'),
        ('repair', 'Repair Job'),
    )
    date = models.DateField(default=timezone.now)
    type = models.CharField(max_length=18, choices=TRANSACTION_TYPE)
    reference_id = models.PositiveIntegerField(blank=True, null=True)
    description = models.CharField(max_length=200)
    debit_account = models.CharField(max_length=100, default="Cash")
    credit_account = models.CharField(max_length=100, default="Sales")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date']
        indexes = [
            models.Index(fields=['type']),
            models.Index(fields=['debit_account', 'credit_account']),
        ]

    def __str__(self):
        return f"{self.date} | {self.type}: {self.description} ₹{self.amount}"


# ============================================================
# 12. NOTIFICATIONS
# ============================================================

class Notification(models.Model):
    TYPES = (
        ('info', 'Information'),
        ('success', 'Success'),
        ('warning', 'Warning'),
        ('error', 'Error'),
    )
    CATEGORIES = (
        ('general', 'General'),
        ('sales', 'Sales'),
        ('purchases', 'Purchases'),
        ('repairs', 'Repairs'),
        ('stock', 'Stock'),
        ('payment', 'Payment'),
        ('system', 'System'),
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notifications'
    )
    title = models.CharField(max_length=200)
    message = models.TextField()
    
    link = models.CharField(max_length=255, blank=True, null=True)
    
    notification_type = models.CharField(max_length=10, choices=TYPES, default='info')
    category = models.CharField(max_length=20, choices=CATEGORIES, default='general')
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text="Model type (e.g., Invoice, RepairJob)"
    )
    object_id = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Primary Key of the related object"
    )
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
        return f"{self.recipient.username} - {self.title} ({self.notification_type})"

class NotificationPreference(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notif_prefs'
    )
    email_enabled = models.BooleanField(default=True, help_text="Email notifications on/off")
    categories = models.JSONField(default=dict, help_text="e.g., {'sales': True, 'repairs': True}")

    def __str__(self):
        return f"{self.user.username} Preferences"


# ============================================================
# SIGNALS
# ============================================================

@receiver(post_save, sender=Purchase)
def create_purchase_ledger(sender, instance, created, **kwargs):
    if created:
        entry = LedgerEntry.objects.create(
            date=instance.date,
            entry_type='purchase',
            reference_id=instance.id,
            description=f"Purchase {instance.purchase_number}",
            total_amount=instance.grand_total
        )
        LedgerLine.objects.create(
            ledger_entry=entry,
            account='Purchases',
            debit=instance.grand_total,
            credit=0
        )
        LedgerLine.objects.create(
            ledger_entry=entry,
            account=f'Vendor: {instance.vendor.name}',
            contact=instance.vendor,
            debit=0,
            credit=instance.grand_total
        )


@receiver(post_save, sender=Contact)
def create_or_update_opening_balance_ledger(sender, instance, **kwargs):
    existing_entries = LedgerEntry.objects.filter(
        entry_type='opening',
        reference_id=instance.id
    )
    if existing_entries.exists():
        existing_entries.delete()

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

    if instance.opening_balance > 0:
        if instance.contact_type in ('customer', 'both'):
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=f'Customer: {instance.name}',
                contact=instance,
                debit=instance.opening_balance,
                credit=0
            )
            LedgerLine.objects.create(
                ledger_entry=entry,
                account='Opening Balance',
                debit=0,
                credit=instance.opening_balance
            )
        else:
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=f'Vendor: {instance.name}',
                contact=instance,
                debit=0,
                credit=instance.opening_balance
            )
            LedgerLine.objects.create(
                ledger_entry=entry,
                account='Opening Balance',
                debit=instance.opening_balance,
                credit=0
            )
    else:
        if instance.contact_type in ('customer', 'both'):
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=f'Customer: {instance.name}',
                contact=instance,
                debit=0,
                credit=abs_bal
            )
            LedgerLine.objects.create(
                ledger_entry=entry,
                account='Opening Balance',
                debit=abs_bal,
                credit=0
            )
        else:
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=f'Vendor: {instance.name}',
                contact=instance,
                debit=abs_bal,
                credit=0
            )
            LedgerLine.objects.create(
                ledger_entry=entry,
                account='Opening Balance',
                debit=0,
                credit=abs_bal
            )


@receiver(post_save, sender=RepairPart)
def create_repair_stock_movements(sender, instance, created, **kwargs):
    if created:
        StockMovement.objects.create(
            product=instance.product,
            movement_type='repair_out',
            quantity=-Decimal(instance.quantity),
            reference=instance.repair_job.job_number,
            date=instance.repair_job.date_in
        )


@receiver(post_save, sender=User)
def create_notification_preferences(sender, instance, created, **kwargs):
    if created:
        NotificationPreference.objects.create(user=instance, categories={})
        
        
# ============================================================
# 13. CONTACT MESSAGES (Quick Message)
# ============================================================

class ContactMessage(models.Model):
    STATUS_CHOICES = (
        ('new', 'New'),
        ('read', 'Read'),
        ('replied', 'Replied'),
        ('spam', 'Spam'),
    )

    name = models.CharField(max_length=200)
    email = models.EmailField()
    phone = models.CharField(                  
        max_length=20,
        blank=True,
        help_text="Optional phone number for contact"
    )
    subject = models.CharField(max_length=200)
    message = models.TextField()

    status = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default='new'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    replied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['created_at']),
        ]
        verbose_name = "Contact Message"
        verbose_name_plural = "Contact Messages"

    def __str__(self):
        return f"{self.name} - {self.subject}"

    def save(self, *args, **kwargs):
        # Normalize phone if present
        if self.phone:
            cleaned = ''.join(filter(str.isdigit, self.phone))
            if cleaned.startswith('91') and len(cleaned) == 12:
                cleaned = cleaned[2:]
            if len(cleaned) >= 10:
                self.phone = cleaned[-10:]
        super().save(*args, **kwargs)

    def mark_as_read(self):
        self.status = 'read'
        self.save(update_fields=['status'])

    def mark_as_replied(self):
        self.status = 'replied'
        self.replied_at = timezone.now()
        self.save(update_fields=['status', 'replied_at'])

# ============================================================
# 17. EMAIL OTP VERIFICATION
# ============================================================

class EmailOTP(models.Model):
    PURPOSE_CHOICES = (
        ('signup', 'Signup Verification'),
        ('reset_password', 'Password Reset'),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='otps'
    )
    email = models.EmailField()
    otp = models.CharField(max_length=6)
    purpose = models.CharField(max_length=20, choices=PURPOSE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_used = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['email', 'otp']),
            models.Index(fields=['expires_at']),
        ]

    def __str__(self):
        return f"{self.email} - {self.otp} ({self.get_purpose_display()})"

    def is_valid(self):
        return not self.is_used and timezone.now() <= self.expires_at