# accounting/forms.py
import re
from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import (
    PasswordChangeForm,
    PasswordResetForm,
)
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.urls import reverse, reverse_lazy
from django.utils import timezone

from .models import (
    BankAccount,
    BankTransaction,
    CompanyProfile,
    Contact,
    ContactMessage,
    FAQ,
    Invoice,
    InvoiceItem,
    Payment,
    Product,
    ProductCategory,
    Purchase,
    PurchaseItem,
    RepairJob,
    RepairPart,
    RepairService,
    Service,
    StockMovement,
    Testimonial,
)

User = get_user_model()


# ============================================================
# BOOTSTRAP WIDGET FACTORIES
# ------------------------------------------------------------
# IMPORTANT: These are FUNCTIONS, not shared instances.
# Django widgets are mutable — sharing one instance across many
# forms causes attribute leakage (e.g. HTMX attrs from one form
# bleeding into another). Always call these: BS_TEXT(), BS_SELECT()
# ============================================================
def BS_TEXT(**kwargs):
    return forms.TextInput(attrs={'class': 'form-control', **kwargs})


def BS_EMAIL(**kwargs):
    return forms.EmailInput(attrs={'class': 'form-control', **kwargs})


def BS_PASSWORD(**kwargs):
    return forms.PasswordInput(attrs={'class': 'form-control', **kwargs})


def BS_TEXTAREA(**kwargs):
    return forms.Textarea(attrs={'class': 'form-control', 'rows': 3, **kwargs})


def BS_SELECT(**kwargs):
    return forms.Select(attrs={'class': 'form-select', **kwargs})


def BS_DATE(**kwargs):
    return forms.DateInput(attrs={'class': 'form-control', 'type': 'date', **kwargs})


def BS_DATETIME(**kwargs):
    return forms.DateTimeInput(
        attrs={'class': 'form-control', 'type': 'datetime-local', **kwargs}
    )


def BS_NUMBER(**kwargs):
    return forms.NumberInput(
        attrs={'class': 'form-control', 'step': '0.01', **kwargs}
    )


def BS_CLEARABLE_FILE(**kwargs):
    return forms.ClearableFileInput(attrs={'class': 'form-control', **kwargs})


def BS_CHECKBOX(**kwargs):
    return forms.CheckboxInput(attrs={'class': 'form-check-input', **kwargs})


# ============================================================
# HTMX Validation Mixin
# ============================================================
class HTMXValidationMixin:
    def add_htmx_validation(self, validate_url, field_names=None, include_id_field=None):
        if field_names is None:
            field_names = self.fields.keys()
        instance_id = None
        if self.instance and self.instance.pk:
            instance_id = self.instance.pk

        for name in field_names:
            if name not in self.fields:
                continue
            field = self.fields[name]
            widget = field.widget
            if not isinstance(widget, forms.HiddenInput):
                attrs = {
                    'hx-get': validate_url,
                    'hx-trigger': 'blur change',
                    'hx-target': f'#field-{name}',
                    'hx-swap': 'outerHTML',
                }
                if instance_id and include_id_field:
                    attrs['hx-include'] = f'[name="{name}"],[name="{include_id_field}"]'
                else:
                    attrs['hx-include'] = f'[name="{name}"]'
                widget.attrs.update(attrs)

        if instance_id and include_id_field and include_id_field not in self.fields:
            self.fields[include_id_field] = forms.IntegerField(
                widget=forms.HiddenInput(),
                initial=instance_id,
                required=False,
            )


# ============================================================
# CUSTOMER REGISTRATION FORM
# ============================================================
class CustomerRegistrationForm(forms.ModelForm):
    # Honeypot field — bots fill this, real users leave empty
    website = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'style': 'display: none !important;',
            'tabindex': '-1',
            'autocomplete': 'off',
            'aria-hidden': 'true',
        }),
        label="Website (Leave empty)",
    )

    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Choose a username',
            'hx-get': reverse_lazy('accounting:validate_register_field'),
            'hx-trigger': 'blur, keyup changed delay:500ms',
            'hx-target': '#field-username',
            'hx-swap': 'innerHTML',
            'hx-include': '[name="username"]',
        }),
        label="Username",
        required=True,
    )

    full_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Full Name',
            'hx-get': reverse_lazy('accounting:validate_register_field'),
            'hx-trigger': 'blur, keyup changed delay:500ms',
            'hx-target': '#field-full_name',
            'hx-swap': 'innerHTML',
            'hx-include': '[name="full_name"]',
        }),
        label="Full Name",
        required=True,
    )

    email = forms.EmailField(
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'placeholder': 'Email Address',
            'hx-get': reverse_lazy('accounting:validate_register_field'),
            'hx-trigger': 'blur, keyup changed delay:500ms',
            'hx-target': '#field-email',
            'hx-swap': 'innerHTML',
            'hx-include': '[name="email"]',
        }),
        label="Email",
        required=True,
    )

    phone = forms.CharField(
        max_length=15,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': '10-digit Mobile Number (Optional)',
            'hx-get': reverse_lazy('accounting:validate_register_field'),
            'hx-trigger': 'blur, keyup changed delay:500ms',
            'hx-target': '#field-phone',
            'hx-swap': 'innerHTML',
            'hx-include': '[name="phone"]',
        }),
        label="Phone (Optional)",
    )

    password1 = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Password (min 8 chars)',
        }),
        label="Password",
        required=True,
    )

    password2 = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Confirm Password',
        }),
        label="Confirm Password",
        required=True,
    )

    class Meta:
        model = User
        fields = ['username', 'email']

    # ============================================================
    # CLEAN METHODS
    # ============================================================

    def clean_website(self):
        website = self.cleaned_data.get('website')
        if website:
            raise ValidationError("Spam detected. This field should be empty.")
        return website

    def clean_username(self):
        username = (self.cleaned_data.get('username') or '').strip()
        if not username:
            raise ValidationError("Username is required.")
        if len(username) < 3:
            raise ValidationError("Username must be at least 3 characters long.")
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError("This username is already taken.")
        return username

    def clean_full_name(self):
        name = (self.cleaned_data.get('full_name') or '').strip()
        if not name or len(name) < 2:
            raise ValidationError("Full name is required (min 2 chars).")
        return name

    def clean_email(self):
        email = (self.cleaned_data.get('email') or '').strip().lower()
        if not email:
            raise ValidationError("Email is required.")
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("This email is already registered.")
        return email

    def clean_phone(self):
        """
        Phone is optional. If provided:
          - normalize to digits
          - keep last 10 digits (Indian mobile)
          - must start with 6, 7, 8, or 9
          - must be unique across active Contacts
        """
        phone = (self.cleaned_data.get('phone') or '').strip()
        if not phone:
            return ''

        phone_clean = ''.join(filter(str.isdigit, phone))
        if len(phone_clean) < 10:
            raise ValidationError(
                "Enter a valid 10-digit mobile number (or leave blank)."
            )
        phone_clean = phone_clean[-10:]

        if not phone_clean.startswith(('6', '7', '8', '9')):
            raise ValidationError(
                "Enter a valid Indian mobile number starting with 6-9."
            )

        if Contact.objects.filter(phone=phone_clean).exists():
            raise ValidationError("This phone number is already registered.")

        return phone_clean

    def clean_password1(self):
        p1 = self.cleaned_data.get('password1')
        if p1 and len(p1) < 8:
            raise ValidationError("Password must be at least 8 characters long.")
        return p1

    def clean_password2(self):
        return self.cleaned_data.get('password2')

    def clean(self):
        cleaned_data = super().clean()
        p1 = cleaned_data.get('password1')
        p2 = cleaned_data.get('password2')
        if p1 and p2 and p1 != p2:
            self.add_error('password2', "Passwords do not match.")
        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data['password1'])
        user.first_name = self.cleaned_data['full_name']
        if commit:
            with transaction.atomic():
                user.save()
                Contact.objects.create(
                    user=user,
                    name=self.cleaned_data['full_name'],
                    email=self.cleaned_data['email'],
                    phone=self.cleaned_data.get('phone') or '',
                    contact_type='customer',
                )
        return user


# ============================================================
# CUSTOM PASSWORD RESET FORM
# ============================================================
class CustomPasswordResetForm(PasswordResetForm):
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'placeholder': 'Enter your registered email',
        }),
        label="Email",
    )


# ============================================================
# CUSTOM PASSWORD CHANGE FORM
# ============================================================
class CustomPasswordChangeForm(PasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({'class': 'form-control'})


# ============================================================
# CONTACT MESSAGE FORM
# ============================================================
class ContactMessageForm(forms.ModelForm):
    # Honeypot field
    website = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'style': 'display: none !important;',
            'tabindex': '-1',
            'autocomplete': 'off',
            'aria-hidden': 'true',
        }),
        label="Website (Leave empty)",
    )

    class Meta:
        model = ContactMessage
        fields = ['name', 'email', 'phone', 'subject', 'message']
        widgets = {
            'phone': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Optional 10-digit mobile number',
            }),
        }

    def clean_name(self):
        name = (self.cleaned_data.get('name') or '').strip()
        if len(name) < 2:
            raise ValidationError("Name must be at least 2 characters.")
        return name

    def clean_phone(self):
        phone = (self.cleaned_data.get('phone') or '').strip()
        if not phone:
            return ''

        phone_clean = ''.join(filter(str.isdigit, phone))
        if len(phone_clean) < 10 or len(phone_clean) > 15:
            raise ValidationError("Enter a valid phone number (10-15 digits).")
        if len(phone_clean) == 10:
            return phone_clean
        return phone_clean[-10:]

    def clean_subject(self):
        subject = (self.cleaned_data.get('subject') or '').strip()
        if len(subject) < 3:
            raise ValidationError("Subject must be at least 3 characters.")
        return subject

    def clean_message(self):
        message = (self.cleaned_data.get('message') or '').strip()
        if len(message) < 10:
            raise ValidationError("Message must be at least 10 characters.")
        return message

    def clean_website(self):
        website = self.cleaned_data.get('website')
        if website:
            raise ValidationError("Spam detected. This field should be empty.")
        return website


# ============================================================
# 1. COMPANY SETTINGS
# ============================================================
class CompanyProfileForm(forms.ModelForm):
    class Meta:
        model = CompanyProfile
        fields = [
            'name', 'address', 'phone', 'email', 'gstin',
            'logo', 'invoice_prefix', 'invoice_start_number',
            'default_tax_rate', 'financial_year_start', 'state',
            'tagline', 'hero_image', 'about_text',
            'google_map_embed', 'working_hours',
            'facebook_url', 'instagram_url', 'youtube_url',
            'whatsapp_number', 'google_review_link',
            'meta_title', 'meta_description', 'meta_keywords', 'og_image',
        ]
        widgets = {
            'name': BS_TEXT(),
            'address': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'phone': BS_TEXT(),
            'email': BS_EMAIL(),
            'gstin': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': '22AAAAA0000A1Z5',
            }),
            'logo': forms.ClearableFileInput(attrs={
                'class': 'form-control',
                'accept': 'image/jpeg,image/png,image/webp',
            }),
            'invoice_prefix': BS_TEXT(),
            'default_tax_rate': forms.NumberInput(attrs={
                'class': 'form-control', 'step': '0.01',
            }),
            'financial_year_start': BS_DATE(),
            'state': BS_TEXT(),
            'tagline': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Short tagline for hero section',
            }),
            'hero_image': forms.ClearableFileInput(attrs={
                'class': 'form-control',
                'accept': 'image/jpeg,image/png,image/webp',
            }),
            'about_text': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 4,
                'placeholder': 'About the company',
            }),
            'google_map_embed': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 4,
                'placeholder': 'Paste iframe code',
            }),
            'working_hours': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g., Mon-Sat: 10:00 AM - 8:00 PM',
            }),
            'facebook_url': forms.URLInput(attrs={
                'class': 'form-control',
                'placeholder': 'https://facebook.com/yourpage',
            }),
            'instagram_url': forms.URLInput(attrs={
                'class': 'form-control',
                'placeholder': 'https://instagram.com/yourprofile',
            }),
            'youtube_url': forms.URLInput(attrs={
                'class': 'form-control',
                'placeholder': 'https://youtube.com/yourchannel',
            }),
            'whatsapp_number': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': '+919876543210',
            }),
            'google_review_link': forms.URLInput(attrs={
                'class': 'form-control',
                'placeholder': 'https://g.page/r/CSroVuEHgpaJEAE/review',
            }),
            'meta_title': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Max 70 characters',
            }),
            'meta_description': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Max 160 characters',
            }),
            'meta_keywords': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Comma separated keywords',
            }),
            'og_image': forms.ClearableFileInput(attrs={
                'class': 'form-control',
                'accept': 'image/jpeg,image/png,image/webp',
            }),
        }
        help_texts = {
            'gstin': 'Leave blank to disable GST features',
            'invoice_prefix': 'e.g., INV, REP, PUR',
            'default_tax_rate': 'Default GST rate for new products',
            'tagline': 'Short tagline displayed in the hero section',
            'hero_image': 'Upload hero image (recommended: 1200x600)',
            'about_text': 'Brief description of the company (used in footer)',
            'google_map_embed': 'Full iframe code from Google Maps',
            'working_hours': 'Display working hours (e.g., Mon-Sat: 10 AM - 8 PM)',
            'facebook_url': 'Full URL to your Facebook page',
            'instagram_url': 'Full URL to your Instagram profile',
            'youtube_url': 'Full URL to your YouTube channel',
            'whatsapp_number': 'WhatsApp number with country code (e.g., +919876543210)',
            'meta_title': 'Page Title (max 70 chars). Leave blank to use company name.',
            'meta_description': 'Meta Description (max 160 chars). Used in search results.',
            'meta_keywords': 'Comma separated keywords for SEO.',
            'og_image': 'Social Media Sharing Image (Recommended: 1200x630).',
        }

    def clean_phone(self):
        phone = self.cleaned_data.get('phone')
        if phone:
            if not re.match(r'^\+?\d{10,15}$', phone):
                raise ValidationError(
                    "Enter a valid phone number (10-15 digits, optional +)."
                )
        return phone

    def clean_whatsapp_number(self):
        number = self.cleaned_data.get('whatsapp_number')
        if number:
            clean = number.replace(' ', '').replace('+', '')
            if not clean.isdigit():
                raise ValidationError("WhatsApp number must contain only digits.")
            if len(clean) < 10 or len(clean) > 15:
                raise ValidationError(
                    "WhatsApp number must be between 10 and 15 digits."
                )
        return number

    def clean_logo(self):
        logo = self.cleaned_data.get('logo')
        if logo and hasattr(logo, 'name') and logo.name:
            if 'logo' in self.files:
                from .validators import (
                    validate_image_binary,
                    validate_image_file_extension,
                )
                from .image_processor import process_uploaded_image
                validate_image_file_extension(logo)
                validate_image_binary(logo)
                logo = process_uploaded_image(logo, max_size=(400, 400))
        return logo

    def clean_hero_image(self):
        hero = self.cleaned_data.get('hero_image')
        if hero and hasattr(hero, 'file') and hero.name:
            if 'hero_image' in self.files:
                from .validators import (
                    validate_image_binary,
                    validate_image_file_extension,
                )
                validate_image_file_extension(hero)
                validate_image_binary(hero)
        return hero


# ============================================================
# SERVICE FORM
# ============================================================
class ServiceForm(forms.ModelForm):
    class Meta:
        model = Service
        fields = ['title', 'description', 'icon', 'image', 'order', 'is_active']
        widgets = {
            'title': BS_TEXT(),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'icon': BS_TEXT(),
            'image': forms.ClearableFileInput(attrs={
                'class': 'form-control',
                'accept': 'image/jpeg,image/png,image/webp',
            }),
            'order': forms.NumberInput(attrs={'class': 'form-control'}),
            'is_active': BS_CHECKBOX(),
        }
        help_texts = {
            'icon': 'Bootstrap icon class (e.g., bi-tools, bi-display).',
            'order': 'Lower numbers appear first.',
        }


# ============================================================
# TESTIMONIAL FORM
# ============================================================
class TestimonialForm(forms.ModelForm):
    class Meta:
        model = Testimonial
        fields = [
            'customer_name', 'customer_photo', 'designation', 'company_name',
            'review_text', 'rating', 'order', 'is_active',
        ]
        widgets = {
            'customer_name': BS_TEXT(),
            'customer_photo': forms.ClearableFileInput(attrs={
                'class': 'form-control',
                'accept': 'image/jpeg,image/png,image/webp',
            }),
            'designation': BS_TEXT(),
            'company_name': BS_TEXT(),
            'review_text': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
            'rating': forms.Select(attrs={'class': 'form-select'}),
            'order': forms.NumberInput(attrs={'class': 'form-control'}),
            'is_active': BS_CHECKBOX(),
        }
        help_texts = {
            'customer_photo': 'Optional photo of the customer.',
            'rating': '1 to 5 stars.',
            'order': 'Lower numbers appear first.',
        }


# ============================================================
# FAQ FORM
# ============================================================
class FAQForm(forms.ModelForm):
    class Meta:
        model = FAQ
        fields = ['question', 'answer', 'order', 'is_active']
        widgets = {
            'question': BS_TEXT(),
            'answer': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
            'order': forms.NumberInput(attrs={'class': 'form-control'}),
            'is_active': BS_CHECKBOX(),
        }
        help_texts = {
            'order': 'Lower numbers appear first.',
        }


# ============================================================
# 2. CONTACTS
# ============================================================
class ContactForm(forms.ModelForm, HTMXValidationMixin):
    class Meta:
        model = Contact
        fields = [
            'contact_type', 'name', 'company_name', 'phone',
            'email', 'address', 'gstin', 'state',
            'opening_balance', 'opening_balance_date', 'notes',
        ]
        widgets = {
            'contact_type': BS_SELECT(),
            'name': BS_TEXT(),
            'company_name': BS_TEXT(),
            'phone': BS_TEXT(),
            'email': BS_EMAIL(),
            'address': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'gstin': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'GSTIN',
            }),
            'state': BS_TEXT(),
            'opening_balance': BS_NUMBER(),
            'opening_balance_date': BS_DATE(),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
        }
        labels = {
            'opening_balance': 'Opening Balance (Rs.)',
            'opening_balance_date': 'Opening Balance Date',
        }
        help_texts = {
            'opening_balance': 'Customer: +ve = owes you, -ve = advance',
            'opening_balance_date': 'Leave blank for today',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if not self.instance.pk and 'opening_balance_date' in self.fields:
            if not self.initial.get('opening_balance_date'):
                self.fields['opening_balance_date'].initial = timezone.now().date()

        self.add_htmx_validation(
            validate_url=reverse('accounting:validate_accounting_contact_field'),
            include_id_field='contact_id',
        )

    def clean_phone(self):
        phone = (self.cleaned_data.get('phone') or '').strip()
        if not phone:
            return ''

        phone_clean = ''.join(filter(str.isdigit, phone))
        if len(phone_clean) < 10:
            raise ValidationError("Phone number must contain at least 10 digits.")
        phone_clean = phone_clean[-10:]

        qs = Contact.objects.filter(phone=phone_clean)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError("This phone number is already in use.")

        return phone_clean

    def clean_opening_balance(self):
        balance = self.cleaned_data.get('opening_balance') or Decimal('0')

        if self.instance and self.instance.pk:
            try:
                original = Contact.objects.get(pk=self.instance.pk).opening_balance
            except Contact.DoesNotExist:
                original = Decimal('0')

            if balance != original:
                has_transactions = (
                    self.instance.sales_invoices.exists()
                    or self.instance.purchases.exists()
                    or self.instance.payments.exists()
                    or self.instance.repair_jobs.exists()
                    or self.instance.ledger_lines.exclude(
                        ledger_entry__entry_type='opening'
                    ).exists()
                )

                if has_transactions:
                    raise ValidationError(
                        "Opening balance cannot be changed after transactions exist. "
                        "Please use a Journal Entry to adjust the balance instead."
                    )

        return balance


# ============================================================
# 3. PRODUCT CATEGORY
# ============================================================
class ProductCategoryForm(forms.ModelForm):
    class Meta:
        model = ProductCategory
        fields = ['name', 'description']
        widgets = {
            'name': BS_TEXT(),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }

    def clean_name(self):
        name = (self.cleaned_data.get('name') or '').strip()
        if not name:
            raise ValidationError("Category name is required.")
        if ProductCategory.objects.filter(name__iexact=name).exists():
            raise ValidationError("A category with this name already exists.")
        return name


# ============================================================
# 4. PRODUCT
# ============================================================
class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = [
            'name', 'hsn_code', 'category', 'unit',
            'purchase_price', 'selling_price', 'current_stock',
            'low_stock_threshold', 'tax_rate', 'is_service', 'is_active',
        ]
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'hsn_code': forms.TextInput(attrs={'class': 'form-control'}),
            'category': forms.Select(attrs={'class': 'form-select'}),
            'unit': forms.Select(attrs={'class': 'form-select'}),
            'purchase_price': forms.NumberInput(attrs={
                'class': 'form-control', 'step': '0.01',
            }),
            'selling_price': forms.NumberInput(attrs={
                'class': 'form-control', 'step': '0.01',
            }),
            'current_stock': forms.NumberInput(attrs={
                'class': 'form-control', 'step': 'any',
            }),
            'low_stock_threshold': forms.NumberInput(attrs={'class': 'form-control'}),
            'tax_rate': forms.NumberInput(attrs={
                'class': 'form-control', 'step': '0.01',
            }),
            'is_service': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['current_stock'].required = False
        self.fields['low_stock_threshold'].required = False
        self.fields['current_stock'].widget.attrs['placeholder'] = '0 (for service)'
        self.fields['low_stock_threshold'].widget.attrs['placeholder'] = '0 (for service)'

        # ── GST rate: model default (18%) hardcoded thi, CompanyProfile ka
        # configurable `default_tax_rate` use karte hain. Naye product par
        # company ka rate pre-fill hota hai; har product apna rate rakh sakta
        # hai (edited value override karti hai).
        if not self.instance.pk:
            try:
                company = CompanyProfile.get_instance()
                self.fields['tax_rate'].initial = (
                    company.default_tax_rate
                    if company.default_tax_rate is not None
                    else Decimal('0')
                )
            except Exception:                       # pragma: no cover
                self.fields['tax_rate'].initial = Decimal('0')
        self.fields['tax_rate'].help_text = (
            "GST % for this product/service. Company default se pre-filled "
            "hai — zaroorat ho to badal dein."
        )

    def clean_current_stock(self):
        is_service = self.cleaned_data.get('is_service')
        value = self.cleaned_data.get('current_stock')
        if is_service:
            return Decimal('0')
        if value is None or value == '':
            return Decimal('0')
        try:
            val = Decimal(value)
            if val < 0:
                raise ValidationError("Stock cannot be negative.")
            return val
        except ValidationError:
            raise
        except Exception:
            raise ValidationError("Enter a valid number.")

    def clean_low_stock_threshold(self):
        is_service = self.cleaned_data.get('is_service')
        value = self.cleaned_data.get('low_stock_threshold')
        if is_service:
            return 0
        if value is None or value == '':
            return 5
        try:
            val = int(value)
            if val < 0:
                raise ValidationError("Threshold cannot be negative.")
            return val
        except ValidationError:
            raise
        except Exception:
            raise ValidationError("Enter a valid integer.")

    def clean(self):
        cleaned_data = super().clean()
        is_service = cleaned_data.get('is_service', False)
        purchase_price = cleaned_data.get('purchase_price') or Decimal('0')
        selling_price = cleaned_data.get('selling_price') or Decimal('0')

        if is_service:
            cleaned_data['current_stock'] = Decimal('0')
            cleaned_data['low_stock_threshold'] = 0
        else:
            if purchase_price < 0:
                self.add_error('purchase_price', "Purchase price cannot be negative.")
            if selling_price < 0:
                self.add_error('selling_price', "Selling price cannot be negative.")
            if selling_price < purchase_price:
                self.add_error(
                    'selling_price',
                    "Selling price should not be less than purchase price.",
                )

        return cleaned_data


# ============================================================
# 5. SALES INVOICE
# ============================================================
class InvoiceForm(forms.ModelForm):
    class Meta:
        model = Invoice
        fields = [
            'customer', 'date', 'due_date', 'gst_type',
            'discount_amount', 'discount_type', 'discount_note',
            'discount_date', 'notes',
        ]
        widgets = {
            'customer': BS_SELECT(),
            'date': BS_DATE(),
            'due_date': BS_DATE(),
            'gst_type': BS_SELECT(),
            'discount_amount': BS_NUMBER(),
            'discount_type': BS_SELECT(),
            'discount_note': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'discount_date': BS_DATE(),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }

    def clean_discount_amount(self):
        disc = self.cleaned_data.get('discount_amount') or Decimal('0')
        if disc < 0:
            raise ValidationError("Discount amount cannot be negative.")
        return disc


class InvoiceItemForm(forms.ModelForm):
    class Meta:
        model = InvoiceItem
        fields = ['product', 'quantity', 'unit_price', 'tax_rate']
        widgets = {
            'product': forms.Select(attrs={
                'class': 'form-select',
                'hx-get': reverse_lazy('accounting:get_product_price'),
                'hx-trigger': 'change',
                'hx-target': '#id_unit_price, #id_tax_rate',
                'hx-swap': 'outerHTML',
            }),
            'quantity': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': '0.01', 'step': 'any',
                'placeholder': 'Quantity',
            }),
            'unit_price': forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.01',
                'id': 'id_unit_price',
            }),
            'tax_rate': forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.01',
                'id': 'id_tax_rate',
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['product'].queryset = Product.objects.filter(is_active=True)

    def clean_quantity(self):
        qty = self.cleaned_data.get('quantity')
        if qty is not None and qty <= 0:
            raise ValidationError("Quantity must be greater than zero.")
        return qty

    def clean_unit_price(self):
        price = self.cleaned_data.get('unit_price')
        if price is not None and price < 0:
            raise ValidationError("Unit price cannot be negative.")
        return price


# ============================================================
# 6. PURCHASE INVOICE
# ============================================================
class PurchaseForm(forms.ModelForm, HTMXValidationMixin):
    class Meta:
        model = Purchase
        fields = [
            'vendor', 'date', 'gst_type',
            'discount_amount', 'freight_charge',
            'discount_type', 'discount_note', 'discount_date',
            'notes',
        ]
        widgets = {
            'vendor': BS_SELECT(),
            'date': BS_DATE(),
            'gst_type': BS_SELECT(),
            'discount_amount': BS_NUMBER(),
            'freight_charge': BS_NUMBER(),
            'discount_type': BS_SELECT(),
            'discount_note': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'discount_date': BS_DATE(),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_htmx_validation(
            validate_url=reverse('accounting:validate_purchase_field'),
            include_id_field='purchase_id',
        )

    def clean_discount_amount(self):
        disc = self.cleaned_data.get('discount_amount') or Decimal('0')
        if disc < 0:
            raise ValidationError("Discount amount cannot be negative.")
        return disc

    def clean_freight_charge(self):
        freight = self.cleaned_data.get('freight_charge') or Decimal('0')
        if freight < 0:
            raise ValidationError("Freight charge cannot be negative.")
        return freight


class PurchaseItemForm(forms.ModelForm):
    class Meta:
        model = PurchaseItem
        fields = ['product', 'quantity', 'unit_price', 'tax_rate', 'is_office_use']
        widgets = {
            'product': forms.Select(attrs={
                'class': 'form-select',
                'hx-get': reverse_lazy('accounting:get_product_price'),
                'hx-trigger': 'change',
                'hx-target': '#id_unit_price, #id_tax_rate',
                'hx-swap': 'outerHTML',
            }),
            'quantity': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': '0.01', 'step': 'any',
            }),
            'unit_price': forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.01',
                'id': 'id_unit_price',
            }),
            'tax_rate': forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.01',
                'id': 'id_tax_rate',
            }),
            'is_office_use': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['product'].queryset = Product.objects.filter(is_service=False)

    def clean_quantity(self):
        qty = self.cleaned_data.get('quantity')
        if qty is not None and qty <= 0:
            raise ValidationError("Quantity must be greater than zero.")
        return qty

    def clean_unit_price(self):
        price = self.cleaned_data.get('unit_price')
        if price is not None and price < 0:
            raise ValidationError("Unit price cannot be negative.")
        return price


# ============================================================
# 7. REPAIR JOBS
# ============================================================
class RepairJobForm(forms.ModelForm, HTMXValidationMixin):
    """
    Repair form for staff. Staff can edit all timeline dates,
    delivery details, and status.
    """
    class Meta:
        model = RepairJob
        fields = [
            'customer', 'device_model', 'serial_number',
            'issue_description', 'diagnosis_report', 'action_taken',
            'accessories', 'device_condition',
            'status',
            'received_at', 'ready_at', 'delivery_date',
            'received_by', 'received_remarks',
            'delivered_by',
            'delivered_to_name',
            'delivered_to_phone',
            'delivered_to_designation',
            'delivery_remarks',
            'estimated_cost',
            'notes',
        ]
        widgets = {
            'customer': BS_SELECT(),
            'device_model': BS_TEXT(),
            'serial_number': BS_TEXT(),
            'issue_description': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
            'diagnosis_report': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 3,
                'placeholder': 'E.g., Hard drive bad sectors, RAM loose, etc.',
            }),
            'action_taken': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 3,
                'placeholder': 'Describe what was done...',
            }),
            'accessories': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 2,
                'placeholder': 'E.g., Adaptor, Bag, CD',
            }),
            'device_condition': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 2,
                'placeholder': 'E.g., Battery missing, Hard disk removed',
            }),
            'status': forms.Select(attrs={'class': 'form-select'}),
            'received_at': BS_DATE(),
            'ready_at': BS_DATE(),
            'delivery_date': BS_DATE(),
            'received_by': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Staff name who received device',
            }),
            'received_remarks': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 2,
                'placeholder': 'Device condition at reception (e.g., scratched screen, missing charger)',
            }),
            'delivered_by': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Staff name who delivered',
            }),
            'delivered_to_name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Recipient name (if different from customer)',
            }),
            'delivered_to_phone': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Recipient phone number',
            }),
            'delivered_to_designation': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g., Driver, Accountant, Office Boy',
            }),
            'delivery_remarks': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 2,
                'placeholder': 'Any special delivery remarks...',
            }),
            'estimated_cost': BS_NUMBER(),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
        }
        labels = {
            'received_at': 'Received at Shop (date)',
            'ready_at': 'Ready for Delivery (date)',
            'delivery_date': 'Delivered to Customer (date)',
            'received_by': 'Received By (staff name)',
            'received_remarks': 'Device Condition at Reception',
            'delivered_by': 'Delivered By (staff name)',
            'delivered_to_name': 'Handed Over To (name)',
            'delivered_to_phone': 'Recipient Phone',
            'delivered_to_designation': 'Recipient Designation',
            'delivery_remarks': 'Delivery Remarks',
        }

    def clean_customer(self):
        customer = self.cleaned_data.get('customer')

        if self.instance and self.instance.pk and self.instance.invoice_id:
            try:
                original = RepairJob.objects.get(pk=self.instance.pk).customer
            except RepairJob.DoesNotExist:
                original = None

            if original and customer != original:
                raise ValidationError(
                    "Customer cannot be changed after an invoice is generated. "
                    "Please delete the invoice first."
                )
        return customer

    def clean_estimated_cost(self):
        cost = self.cleaned_data.get('estimated_cost')
        if cost is not None and cost < 0:
            raise ValidationError("Estimated cost cannot be negative.")
        return cost

    def clean(self):
        cleaned_data = super().clean()
        status = cleaned_data.get('status')
        delivery_date = cleaned_data.get('delivery_date')

        if status == 'delivered' and not delivery_date:
            self.add_error(
                'delivery_date',
                "Delivery date is required when status is 'Delivered'.",
            )

        # ── STATE MACHINE: transition validation (professional flow) ──
        # Status ab sirf valid transitions se hi badal sakta hai. Purane
        # behaviour me koi bhi status kisi bhi status se set ho sakta tha
        # (pending → delivered bhi) — ab model ka transition table check
        # hota hai aur saaf error milta hai.
        if status and self.instance and self.instance.pk:
            old_status = (
                RepairJob.all_objects.filter(pk=self.instance.pk)
                .values_list('status', flat=True).first()
            )
            if old_status and old_status != status and not \
                    RepairJob.can_transition_to(old_status, status):
                allowed = sorted(RepairJob.allowed_next_statuses(old_status))
                self.add_error(
                    'status',
                    f"'{RepairJob.status_label(old_status)}' se "
                    f"'{RepairJob.status_label(status)}' allowed nahi. "
                    f"Allowed next: "
                    f"{', '.join(RepairJob.status_label(a) for a in allowed) or 'none (terminal state)'}.",
                )

        return cleaned_data

    def save(self, commit=True):
        """
        Aapka purana form-save — PLUS: status change ab state machine se
        hota hai (transition guards + timeline dates + immutable history +
        customer notification + cancel par stock reversal).

        Kyun zaroori hai: staff ke liye status dropdown sababse natural UX
        hai, lekin seedha save hone se saare guards bypass ho jate the. Ab
        dropdown bhi `change_status()` se guzarta hai.

        Steps:
          1. Baaki fields (status ke bina) save karo
          2. Status ko purani value par wapas lao (taaki guard na toote)
          3. `change_status()` se transition lagao (guards + history + notify)
        """
        instance = super().save(commit=False)

        new_status = self.cleaned_data.get('status')
        status_changed = bool(
            self.instance.pk and new_status
            and new_status != self._status_before_save
        )

        if commit:
            if status_changed:
                # 1+2. Non-status fields save karo (status purani value par)
                instance.status = self._status_before_save
                instance.save()
                # 3. Ab transition state machine se lagao
                instance.change_status(
                    new_status,
                    by=self._actor,
                    remarks=(self.cleaned_data.get('status_remarks')
                             or 'Status changed via repair form'),
                )
            else:
                instance.save()

        return instance

    def __init__(self, *args, **kwargs):
        """
        Batch 1 (purana): HTMX live-validation + optional fields.
        Batch 2 (naya)  : `actor=` (request.user) — audit history ke liye.
        """
        self._actor = kwargs.pop('actor', None)
        super().__init__(*args, **kwargs)

        field_names = [f for f in self.fields.keys() if f != 'status']
        self.add_htmx_validation(
            validate_url=reverse('accounting:validate_repair_field'),
            field_names=field_names,
            include_id_field='repair_id',
        )

        for field in [
            'delivered_to_name', 'delivered_to_phone',
            'delivered_to_designation', 'delivery_remarks',
            'received_at', 'ready_at', 'delivery_date',
            'received_by', 'received_remarks', 'delivered_by',
        ]:
            if field in self.fields:
                self.fields[field].required = False

        self._status_before_save = getattr(self.instance, 'status', None)


class RepairStatusTransitionForm(forms.Form):
    """
    Status transition ka dedicated form (HTMX buttons ke liye).

    Model ka state machine hi asli guard hai — ye form sirf:
      • valid options offer karta hai,
      • remarks (audit ke liye) collect karta hai,
      • aur `change_status()` call karta hai.
    """
    status = forms.ChoiceField(
        choices=(), required=True, widget=BS_SELECT(),
        label='Naya Status',
    )
    remarks = forms.CharField(
        required=False, max_length=300, widget=BS_TEXTAREA(rows=2),
        label='Remarks / Reason',
        help_text='Audit trail me save hoga (customer ko nahi dikhta).',
    )
    force = forms.BooleanField(
        required=False, widget=BS_CHECKBOX(), label='Force (manager override)',
        help_text='Sirf tab jab workflow ke bahar jaana zaroori ho — audit me '
                  'FORCED mark ho jata hai.',
    )

    def __init__(self, *args, job=None, allow_force=False, **kwargs):
        self.job = job
        super().__init__(*args, **kwargs)
        self._allow_force = allow_force

        if job is not None:
            display = dict(job.STATUS_CHOICES)
            # Sirf wo transitions jo normal flow me allowed hain (force-only
            # wale — jaise delivered→repairing reopen — yahan nahi aate).
            allowed = sorted(job.allowed_next_statuses(job.status))
            self.allowed = allowed
            self.fields['status'].choices = (
                [('', '— Select —')] + [(c, display.get(c, c)) for c in allowed]
            )
        else:
            self.allowed = []
            self.fields['status'].choices = [('', '— Select —')]

        if not allow_force:
            self.fields.pop('force', None)

    @property
    def has_transitions(self):
        return bool(self.allowed) and self.job is not None \
            and not self.job.is_locked

    def clean_status(self):
        value = self.cleaned_data.get('status')
        if not value or self.job is None:
            return value
        if not RepairJob.can_transition_to(self.job.status, value):
            allowed = sorted(RepairJob.allowed_next_statuses(self.job.status))
            raise ValidationError(
                f"'{RepairJob.status_label(self.job.status)}' se "
                f"'{RepairJob.status_label(value)}' allowed nahi. "
                f"Allowed next: {', '.join(allowed) or 'none (terminal state)'}."
            )
        return value

    def clean(self):
        cleaned = super().clean()
        target = cleaned.get('status')
        forced = bool(cleaned.get('force'))
        if self.job is not None and target and not forced:
            try:
                self.job._status_guards(self.job.status, target, force=False)
            except ValidationError as exc:
                self.add_error('status', exc.messages[0])
        return cleaned

    def save(self, by=None):
        if not self.is_valid():
            raise ValidationError('Form invalid hai — pehle errors theek karein.')
        return self.job.change_status(
            self.cleaned_data['status'],
            by=by,
            remarks=self.cleaned_data.get('remarks', ''),
            force=bool(self.cleaned_data.get('force')),
        )


class RepairPartForm(forms.ModelForm):
    class Meta:
        model = RepairPart
        fields = ['product', 'quantity', 'unit_price']
        widgets = {
            'product': forms.Select(attrs={
                'class': 'form-select',
                'hx-get': reverse_lazy('accounting:get_product_price'),
                'hx-trigger': 'change',
                'hx-target': '#id_unit_price',
                'hx-swap': 'outerHTML',
            }),
            'quantity': forms.NumberInput(attrs={
                'class': 'form-control', 'min': '1', 'step': '1',
            }),
            'unit_price': forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.01',
                'id': 'id_unit_price',
            }),
        }

    def __init__(self, *args, repair_job=None, **kwargs):
        """
        `repair_job=` optional hai — diya jaye to stock availability is job
        ke hisaab se check hoti hai (job par pehle se lage parts count hote
        hain, warna jhoota "insufficient stock" error aata tha).

        NOTE: `self.instance` sirf `super().__init__()` ke BAAD milta hai,
        isliye instance fallback wahan se lete hain.
        """
        self.repair_job = repair_job
        super().__init__(*args, **kwargs)
        if self.repair_job is None:
            self.repair_job = getattr(self.instance, 'repair_job', None)
        # Repair parts must be PHYSICAL products only.
        self.fields['product'].queryset = Product.objects.filter(
            is_active=True,
            is_service=False,
        )

    def clean_quantity(self):
        qty = self.cleaned_data.get('quantity')
        if qty is not None and qty <= 0:
            raise ValidationError("Quantity must be at least 1.")
        return qty

    def clean_unit_price(self):
        price = self.cleaned_data.get('unit_price')
        if price is not None and price <= 0:
            raise ValidationError("Unit price must be greater than zero.")
        return price

    def clean(self):
        """
        Business guards (model layer ke saath consistent):

        1. Invoiced job → saaf error (view tak jaane se pehle)
        2. Stock availability — `current_stock + is job par pehle se lage`
           ke hisaab se, aur message me exact available qty
        """
        cleaned = super().clean()
        product = cleaned.get('product')
        qty = cleaned.get('quantity')

        if not (product and qty):
            return cleaned

        # 1. Invoiced job lock (model bhi rokta hai, par form me saaf message)
        if self.repair_job is not None and getattr(self.repair_job, 'invoice_id', None):
            raise ValidationError(
                f"Is job ka invoice ban chuka hai — part add/change nahi ho "
                f"sakta. Pehle invoice handle karein."
            )

        if product.is_service:
            self.add_error(
                'product',
                f"'{product.name}' ek SERVICE product hai — ise "
                f"'Add Service' se add karein, part ke roop me nahi.",
            )
            return cleaned

        # 2. Stock availability — is job par pehle se lage parts ko count karo
        already = Decimal('0')
        if self.repair_job is not None and self.repair_job.pk:
            already = (
                RepairPart.objects
                .filter(repair_job=self.repair_job, product=product,
                        is_deleted=False)
                .exclude(pk=self.instance.pk)
                .aggregate(total=Sum('quantity'))['total']
                or Decimal('0')
            )
        available = (product.current_stock or Decimal('0')) + Decimal(str(already))
        if Decimal(qty) > available:
            self.add_error(
                'quantity',
                f"Stock kam hai: '{product.name}' me {available} available "
                f"hai (aap {qty} maang rahe hain). Purchase karein ya "
                f"quantity kam karein.",
            )
        return cleaned


class RepairServiceForm(forms.ModelForm):
    """
    Form to add a Service charge to a repair job.
    Uses Product (is_service=True) — flat amount, no quantity.
    """
    class Meta:
        model = RepairService
        fields = ['product', 'amount', 'description']
        widgets = {
            'product': forms.Select(attrs={
                'class': 'form-select',
                'required': 'required',
            }),
            'amount': forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.01',
                'min': '0',
                'placeholder': '0.00',
            }),
            'description': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Optional note (e.g., Printer head cleaning)',
            }),
        }
        labels = {
            'product': 'Service',
            'amount': 'Amount (₹)',
            'description': 'Description',
        }

    def __init__(self, *args, repair_job=None, **kwargs):
        self.repair_job = repair_job
        super().__init__(*args, **kwargs)
        if self.repair_job is None:
            self.repair_job = getattr(self.instance, 'repair_job', None)
        self.fields['product'].queryset = Product.objects.filter(
            is_service=True,
            is_active=True,
        ).order_by('name')
        self.fields['product'].empty_label = "— Select Service —"
        self.fields['amount'].required = True

    def clean_amount(self):
        amount = self.cleaned_data.get('amount')
        if amount is None:
            raise ValidationError("Amount is required.")
        if amount < 0:
            raise ValidationError("Amount cannot be negative.")
        return amount

    def clean_product(self):
        """Service line me sirf SERVICE product (is_service=True)."""
        product = self.cleaned_data.get('product')
        if product is not None and not product.is_service:
            raise ValidationError(
                f"'{product.name}' ek physical product hai — ise "
                f"'Add Part' se add karein, service ke roop me nahi."
            )
        return product

    def clean(self):
        cleaned = super().clean()
        if self.repair_job is not None and getattr(self.repair_job, 'invoice_id', None):
            raise ValidationError(
                "Is job ka invoice ban chuka hai — service add/change nahi ho "
                "sakta. Pehle invoice handle karein."
            )
        return cleaned


# ============================================================
# 8. PAYMENTS
# ============================================================
class PaymentForm(forms.ModelForm):
    class Meta:
        model = Payment
        fields = [
            'direction', 'contact', 'amount', 'date', 'method',
            'bank_account', 'upi_ref', 'reference', 'description',
            'is_advance',
            'discount_amount', 'discount_type', 'discount_note',
        ]
        widgets = {
            'direction': BS_SELECT(),
            'contact': forms.Select(attrs={
                'class': 'form-select',
                'id': 'id_contact',
            }),
            'amount': BS_NUMBER(),
            'date': BS_DATE(),
            'method': BS_SELECT(),
            'bank_account': forms.Select(attrs={'class': 'form-select'}),
            'upi_ref': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'UPI TXN ID (PhonePe / GPay)',
            }),
            'reference': BS_TEXT(),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'is_advance': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'discount_amount': BS_NUMBER(),
            'discount_type': BS_SELECT(),
            'discount_note': forms.Textarea(attrs={
                'class': 'form-control',
                'rows': 2,
                'placeholder': 'Reason for discount (optional)',
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields['contact'].queryset = Contact.objects.all().order_by('name')
        self.fields['contact'].empty_label = "— Select Customer / Vendor —"
        self.fields['contact'].required = True

        self.fields['bank_account'].queryset = BankAccount.objects.filter(is_active=True)

        self.fields['discount_amount'].required = False
        self.fields['discount_type'].required = False
        self.fields['discount_note'].required = False

        if not self.instance.pk and not self.initial.get('discount_amount'):
            self.fields['discount_amount'].initial = Decimal('0.00')

    def clean_amount(self):
        amount = self.cleaned_data.get('amount')
        if amount is None or amount <= 0:
            raise ValidationError("Amount must be greater than zero.")
        return amount

    def clean_discount_amount(self):
        disc = self.cleaned_data.get('discount_amount') or Decimal('0')
        if disc < 0:
            raise ValidationError("Discount amount cannot be negative.")
        if disc > 0:
            amount = self.cleaned_data.get('amount') or Decimal('0')
            if disc >= amount:
                raise ValidationError(
                    "Discount cannot be equal to or greater than the amount."
                )
        return disc

    def clean(self):
        cleaned_data = super().clean()
        method = cleaned_data.get('method')
        bank_account = cleaned_data.get('bank_account')
        contact = cleaned_data.get('contact')
        direction = cleaned_data.get('direction')

        if method in ('bank', 'upi') and not bank_account:
            self.add_error(
                'bank_account',
                "Please select a bank account for bank/UPI payments.",
            )

        if contact and direction:
            if direction == 'received' and contact.contact_type not in ('customer', 'both'):
                self.add_error(
                    'direction',
                    f"'{contact.name}' is not a customer. Please choose 'Paid to Supplier'.",
                )
            elif direction == 'paid' and contact.contact_type not in ('vendor', 'both'):
                self.add_error(
                    'direction',
                    f"'{contact.name}' is not a vendor. Please choose 'Received from Customer'.",
                )

        return cleaned_data


# ============================================================
# 9. STOCK MOVEMENT
# ============================================================
class StockMovementForm(forms.ModelForm):
    class Meta:
        model = StockMovement
        fields = ['product', 'movement_type', 'quantity', 'reference', 'date', 'notes']
        widgets = {
            'product': BS_SELECT(),
            'movement_type': BS_SELECT(),
            'quantity': forms.NumberInput(attrs={'class': 'form-control', 'step': 'any'}),
            'reference': BS_TEXT(),
            'date': BS_DATETIME(),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }

    def clean_quantity(self):
        qty = self.cleaned_data.get('quantity')
        if qty == 0:
            raise ValidationError("Quantity cannot be zero.")
        return qty


# ============================================================
# 10. BANK ACCOUNTS & TRANSACTIONS
# ============================================================
class BankAccountForm(forms.ModelForm):
    class Meta:
        model = BankAccount
        fields = [
            'name', 'account_number', 'bank_name', 'ifsc_code',
            'account_type', 'opening_balance', 'is_active',
        ]
        widgets = {
            'name': BS_TEXT(),
            'account_number': BS_TEXT(),
            'bank_name': BS_TEXT(),
            'ifsc_code': BS_TEXT(),
            'account_type': BS_SELECT(),
            'opening_balance': BS_NUMBER(),
            'is_active': BS_CHECKBOX(),
        }

    def clean_opening_balance(self):
        balance = self.cleaned_data.get('opening_balance') or Decimal('0')
        if balance < 0:
            raise ValidationError("Opening balance cannot be negative.")
        return balance


class BankTransactionForm(forms.ModelForm):
    class Meta:
        model = BankTransaction
        fields = [
            'bank_account', 'transaction_type', 'source_type',
            'amount', 'date', 'description', 'reference',
        ]
        widgets = {
            'bank_account': forms.Select(attrs={'class': 'form-select'}),
            'transaction_type': BS_SELECT(),
            'source_type': BS_SELECT(),
            'amount': BS_NUMBER(),
            'date': BS_DATE(),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'reference': BS_TEXT(),
        }

    def clean_amount(self):
        amount = self.cleaned_data.get('amount')
        if amount is None or amount <= 0:
            raise ValidationError("Amount must be greater than zero.")
        return amount

    def clean(self):
        cleaned = super().clean()
        transaction_type = cleaned.get('transaction_type')
        source_type = cleaned.get('source_type')

        valid_sources = {
            'deposit': ['payment_received', 'transfer', 'interest', 'manual'],
            'withdrawal': ['payment_made', 'transfer', 'bank_charge', 'manual'],
        }

        if transaction_type and source_type:
            if source_type not in valid_sources.get(transaction_type, []):
                raise ValidationError(
                    f"Invalid source type '{source_type}' for {transaction_type} transaction."
                )
        return cleaned


# ============================================================
# 11. JOURNAL (Tally/Zoho style)
# ============================================================
class JournalForm(forms.Form):
    """Professional journal entry form (Tally/Zoho style)."""

    ENTRY_TYPE_CHOICES = [
        ('discount_allowed', 'Discount Allowed (given to customer)'),
        ('discount_received', 'Discount Received (from vendor)'),
        ('advance_received', 'Advance Received (from customer)'),
        ('advance_paid', 'Advance Paid (to vendor)'),
        ('general', 'General Journal (Correction / Adjustment)'),
    ]

    MONEY_ACCOUNT_CHOICES = [
        ('cash', 'Cash'),
        ('bank', 'Bank / UPI'),
    ]

    contact = forms.ModelChoiceField(
        queryset=Contact.objects.all(),
        widget=BS_SELECT(),
        label="Contact",
    )
    entry_type = forms.ChoiceField(
        choices=ENTRY_TYPE_CHOICES,
        widget=BS_SELECT(),
        label="Transaction Type",
    )
    money_account = forms.ChoiceField(
        choices=MONEY_ACCOUNT_CHOICES,
        widget=forms.RadioSelect(attrs={'class': 'form-check-input'}),
        initial='cash',
        required=True,
        label="Money Account",
    )
    bank_account = forms.ModelChoiceField(
        queryset=BankAccount.objects.filter(is_active=True),
        required=False,
        widget=BS_SELECT(),
        label="Bank Account",
        help_text="Required when Money Account = Bank/UPI",
    )
    amount = forms.DecimalField(
        max_digits=12, decimal_places=2,
        widget=BS_NUMBER(), label="Amount",
    )
    date = forms.DateField(
        widget=BS_DATE(), required=False, label="Date",
        initial=timezone.now().date,
    )
    narration = forms.CharField(
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        required=False, label="Narration",
    )

    def clean_amount(self):
        amount = self.cleaned_data.get('amount')
        if amount is not None and amount <= 0:
            raise ValidationError("Amount must be positive.")
        return amount

    def clean(self):
        cleaned_data = super().clean()
        contact = cleaned_data.get('contact')
        entry_type = cleaned_data.get('entry_type')
        money_account = cleaned_data.get('money_account')
        bank_account = cleaned_data.get('bank_account')

        if contact and entry_type:
            customer_types = ['discount_allowed', 'advance_received']
            vendor_types = ['discount_received', 'advance_paid']

            if entry_type in customer_types and contact.contact_type not in ('customer', 'both'):
                self.add_error('entry_type', "This transaction type is only for customers.")
                self.add_error('contact', "Please select a customer for this transaction.")

            if entry_type in vendor_types and contact.contact_type not in ('vendor', 'both'):
                self.add_error('entry_type', "This transaction type is only for vendors.")
                self.add_error('contact', "Please select a vendor for this transaction.")

        if money_account == 'bank' and not bank_account:
            self.add_error('bank_account', "Please select a bank account for Bank/UPI transactions.")

        if money_account == 'cash':
            cleaned_data['bank_account'] = None

        return cleaned_data


# ============================================================
# 12. CUSTOMER PORTAL FORMS
# ============================================================
class CustomerProfileForm(forms.ModelForm):
    class Meta:
        model = Contact
        fields = ['name', 'company_name', 'phone', 'address', 'state', 'gstin']
        widgets = {
            'name': BS_TEXT(),
            'company_name': BS_TEXT(),
            'phone': BS_TEXT(),
            'address': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'state': BS_TEXT(),
            'gstin': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'GSTIN',
            }),
        }
        help_texts = {
            'name': 'Your full name',
            'company_name': 'Your company name',
            'phone': 'Contact number for communication',
            'address': 'Your business or home address',
            'state': 'State (for GST purposes)',
            'gstin': 'Leave blank if not registered',
        }

    def clean_phone(self):
        phone = (self.cleaned_data.get('phone') or '').strip()
        if not phone:
            return phone

        phone_clean = ''.join(filter(str.isdigit, phone))
        if len(phone_clean) < 10:
            raise ValidationError("Phone number must contain at least 10 digits.")
        phone_clean = phone_clean[-10:]

        qs = Contact.objects.filter(phone=phone_clean)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError("This phone number is already registered by another user.")

        return phone_clean


class CustomerRepairForm(forms.ModelForm):
    class Meta:
        model = RepairJob
        fields = [
            'device_model', 'serial_number', 'issue_description',
            'accessories', 'device_condition',
        ]
        widgets = {
            'device_model': BS_TEXT(),
            'serial_number': BS_TEXT(),
            'issue_description': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
            'accessories': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'device_condition': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }


# ============================================================
# 13. FILTER FORMS
# ============================================================
class ContactFilterForm(forms.Form):
    search = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Search by name, phone, email...',
            'hx-get': reverse_lazy('accounting:contact_list'),
            'hx-trigger': 'keyup changed delay:500ms',
            'hx-target': '#contacts-table',
            'hx-include': 'this',
        }),
    )


class ProductFilterForm(forms.Form):
    search = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Search products...',
            'hx-get': reverse_lazy('accounting:product_list'),
            'hx-trigger': 'keyup changed delay:500ms',
            'hx-target': '#product-table',
        }),
    )
    category = forms.ModelChoiceField(
        queryset=ProductCategory.objects.all(),
        required=False,
        widget=forms.Select(attrs={
            'class': 'form-select',
            'hx-get': reverse_lazy('accounting:product_list'),
            'hx-trigger': 'change',
            'hx-target': '#product-table',
            'hx-include': '[name=search]',
        }),
        empty_label="All Categories",
    )


class InvoiceFilterForm(forms.Form):
    search = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Invoice # or customer',
            'hx-get': reverse_lazy('accounting:invoice_list'),
            'hx-trigger': 'keyup changed delay:500ms',
            'hx-target': '#invoice-table-container',
            'hx-include': '#filter-form',
        }),
    )
    customer = forms.ModelChoiceField(
        queryset=Contact.objects.filter(contact_type__in=['customer', 'both']),
        required=False,
        widget=forms.Select(attrs={
            'class': 'form-select',
            'hx-get': reverse_lazy('accounting:invoice_list'),
            'hx-trigger': 'change',
            'hx-target': '#invoice-table-container',
            'hx-include': '#filter-form',
        }),
        empty_label="All Customers",
    )
    status = forms.ChoiceField(
        choices=[
            ('', 'All Status'),
            ('paid', 'Paid'),
            ('partial', 'Partial'),
            ('unpaid', 'Unpaid'),
        ],
        required=False,
        widget=forms.Select(attrs={
            'class': 'form-select',
            'hx-get': reverse_lazy('accounting:invoice_list'),
            'hx-trigger': 'change',
            'hx-target': '#invoice-table-container',
            'hx-include': '#filter-form',
        }),
    )
    date_from = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={
            'class': 'form-control', 'type': 'date',
            'hx-get': reverse_lazy('accounting:invoice_list'),
            'hx-trigger': 'change',
            'hx-target': '#invoice-table-container',
            'hx-include': '#filter-form',
        }),
    )
    date_to = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={
            'class': 'form-control', 'type': 'date',
            'hx-get': reverse_lazy('accounting:invoice_list'),
            'hx-trigger': 'change',
            'hx-target': '#invoice-table-container',
            'hx-include': '#filter-form',
        }),
    )


# ============================================================
# EMAIL CHANGE REQUEST FORM
# ============================================================
class EmailChangeRequestForm(forms.Form):
    """Form to request email change."""
    new_email = forms.EmailField(
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'placeholder': 'Enter new email address',
        }),
        label="New Email",
    )

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_new_email(self):
        email = (self.cleaned_data.get('new_email') or '').strip().lower()
        if User.objects.filter(email=email).exclude(pk=self.user.pk).exists():
            raise ValidationError("This email is already registered by another user.")
        if email == self.user.email:
            raise ValidationError(
                "This is your current email. Please enter a different email."
            )
        return email