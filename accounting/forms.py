import re
from decimal import Decimal
from django import forms
from django.contrib.auth.forms import AuthenticationForm, PasswordResetForm, PasswordChangeForm
from django.contrib.auth import get_user_model
from django.contrib.auth.models import User
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone
from django.db import transaction

from .models import *
User = get_user_model()

# ============================================================
# BOOTSTRAP WIDGETS
# ============================================================
BS_TEXT = forms.TextInput(attrs={'class': 'form-control'})
BS_EMAIL = forms.EmailInput(attrs={'class': 'form-control'})
BS_PASSWORD = forms.PasswordInput(attrs={'class': 'form-control'})
BS_TEXTAREA = forms.Textarea(attrs={'class': 'form-control', 'rows': 3})
BS_SELECT = forms.Select(attrs={'class': 'form-select'})
BS_DATE = forms.DateInput(attrs={'class': 'form-control', 'type': 'date'})
BS_DATETIME = forms.DateTimeInput(attrs={'class': 'form-control', 'type': 'datetime-local'})
BS_NUMBER = forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'})
BS_CLEARABLE_FILE = forms.ClearableFileInput(attrs={'class': 'form-control'})
BS_CHECKBOX = forms.CheckboxInput(attrs={'class': 'form-check-input'})


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
                required=False
            )


# ============================================================
# CUSTOMER REGISTRATION FORM
# ============================================================

class CustomerRegistrationForm(forms.ModelForm):
    # ============================================================
    # HONEYPOT FIELD - Bots isko fill karenge, real users nahi
    # ============================================================
    website = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'style': 'display: none !important;',
            'tabindex': '-1',
            'autocomplete': 'off',
            'aria-hidden': 'true',
        }),
        label="Website (Leave empty)"
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
        required=True
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
        required=True
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
        required=True
    )

    # Phone – optional
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
        label="Phone (Optional)"
    )

    password1 = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Password (min 8 chars)',
        }),
        label="Password",
        required=True
    )

    password2 = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Confirm Password',
        }),
        label="Confirm Password",
        required=True
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
        username = self.cleaned_data.get('username', '').strip()
        if not username:
            raise ValidationError("Username is required.")
        if len(username) < 3:
            raise ValidationError("Username must be at least 3 characters long.")
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError("This username is already taken.")
        return username

    def clean_full_name(self):
        name = self.cleaned_data.get('full_name', '').strip()
        if not name or len(name) < 2:
            raise ValidationError("Full name is required (min 2 chars).")
        return name

    def clean_email(self):
        email = self.cleaned_data.get('email', '').strip().lower()
        if not email:
            raise ValidationError("Email is required.")
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("This email is already registered.")
        return email

    # Modified clean_phone – अब Optional, और Uniqueness Check सिर्फ तब होगा जब Phone दिया हो
    def clean_phone(self):
        phone = self.cleaned_data.get('phone', '').strip()
        if not phone:
            return ''   # Optional – empty allowed

        # Normalize: keep only digits, and take last 10 digits (Indian mobile)
        phone_clean = ''.join(filter(str.isdigit, phone))
        if len(phone_clean) < 10:
            raise ValidationError("Enter a valid 10-digit mobile number (or leave blank).")
        phone_clean = phone_clean[-10:]   # Ensure 10 digits

        # Indian mobile number must start with 6,7,8,9
        if not phone_clean.startswith(('6', '7', '8', '9')):
            raise ValidationError("Enter a valid Indian mobile number starting with 6-9.")

        # Uniqueness check – only if phone is provided
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
                    phone=self.cleaned_data.get('phone', ''),
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
            'placeholder': 'Enter your registered email'
        }),
        label="Email"
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
# CONTACT MESSAGE FORM (single definition)
# ============================================================
class ContactMessageForm(forms.ModelForm):
    # ============================================================
    # HONEYPOT FIELD - Bots isko fill karenge, real users nahi
    # ============================================================
    website = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'style': 'display: none !important;',
            'tabindex': '-1',
            'autocomplete': 'off',
            'aria-hidden': 'true',
        }),
        label="Website (Leave empty)"
    )

    class Meta:
        model = ContactMessage
        fields = ['name', 'email', 'phone', 'subject', 'message']  
        widgets = {
            'phone': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Optional 10-digit mobile number'
            }),
        }

    def clean_name(self):
        name = self.cleaned_data.get('name')
        if len(name.strip()) < 2:
            raise ValidationError("Name must be at least 2 characters.")
        return name.strip()

    def clean_phone(self):
        phone = self.cleaned_data.get('phone', '').strip()
        if phone:
            # Validate phone format (optional, so only if provided)
            phone_clean = ''.join(filter(str.isdigit, phone))
            if len(phone_clean) < 10 or len(phone_clean) > 15:
                raise ValidationError("Enter a valid phone number (10-15 digits).")
            if len(phone_clean) == 10:
                return phone_clean
            elif len(phone_clean) > 10:
                # if longer, keep last 10
                return phone_clean[-10:]
        return phone

    def clean_subject(self):
        subject = self.cleaned_data.get('subject')
        if len(subject.strip()) < 3:
            raise ValidationError("Subject must be at least 3 characters.")
        return subject.strip()

    def clean_message(self):
        message = self.cleaned_data.get('message')
        if len(message.strip()) < 10:
            raise ValidationError("Message must be at least 10 characters.")
        return message.strip()

    def clean_website(self):
        """HONEYPOT: Agar ye field filled hai toh spam samjho."""
        website = self.cleaned_data.get('website')
        if website:
            raise ValidationError("Spam detected. This field should be empty.")
        return website

# ============================================================
# 1. COMPANY SETTINGS (UPDATED with accept attributes)
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
            'facebook_url', 'instagram_url', 'youtube_url', 'whatsapp_number', 'google_review_link',
            'meta_title', 'meta_description', 'meta_keywords', 'og_image'
        ]
        widgets = {
            'name': BS_TEXT,
            'address': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'phone': BS_TEXT,
            'email': BS_EMAIL,
            'gstin': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '22AAAAA0000A1Z5'}),
            'logo': forms.ClearableFileInput(attrs={
                'class': 'form-control',
                'accept': 'image/jpeg,image/png,image/webp'
            }),
            'invoice_prefix': BS_TEXT,
            'default_tax_rate': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'financial_year_start': BS_DATE,
            'state': BS_TEXT,
            'tagline': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Short tagline for hero section'}),
            'hero_image': forms.ClearableFileInput(attrs={
                'class': 'form-control',
                'accept': 'image/jpeg,image/png,image/webp'
            }),
            'about_text': forms.Textarea(attrs={'class': 'form-control', 'rows': 4, 'placeholder': 'About the company'}),
            'google_map_embed': forms.Textarea(attrs={'class': 'form-control', 'rows': 4, 'placeholder': 'Paste iframe code'}),
            'working_hours': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., Mon–Sat: 10:00 AM – 8:00 PM'}),
            'facebook_url': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://facebook.com/yourpage'}),
            'instagram_url': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://instagram.com/yourprofile'}),
            'youtube_url': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://youtube.com/yourchannel'}),
            'whatsapp_number': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '+919876543210'}),
            'google_review_link': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://g.page/r/CSroVuEHgpaJEAE/review'}),
            'meta_title': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Max 70 characters'}),
            'meta_description': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Max 160 characters'}),
            'meta_keywords': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Comma separated keywords'}),
            'og_image': forms.ClearableFileInput(attrs={'class': 'form-control', 'accept': 'image/jpeg,image/png,image/webp'}),
        }
        help_texts = {
            'gstin': 'Leave blank to disable GST features',
            'invoice_prefix': 'e.g., INV, REP, PUR',
            'default_tax_rate': 'Default GST rate for new products',
            'tagline': 'Short tagline displayed in the hero section',
            'hero_image': 'Upload hero image (recommended: 1200x600)',
            'about_text': 'Brief description of the company (used in footer)',
            'google_map_embed': 'Full iframe code from Google Maps (e.g., <iframe src="..."></iframe>)',
            'working_hours': 'Display working hours (e.g., Mon–Sat: 10 AM – 8 PM)',
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
                raise ValidationError("Enter a valid phone number (10-15 digits, optional +).")
        return phone

    def clean_whatsapp_number(self):
        number = self.cleaned_data.get('whatsapp_number')
        if number:
            clean = number.replace(' ', '').replace('+', '')
            if not clean.isdigit():
                raise ValidationError("WhatsApp number must contain only digits.")
            if len(clean) < 10 or len(clean) > 15:
                raise ValidationError("WhatsApp number must be between 10 and 15 digits.")
        return number
    
    def clean_logo(self):
        logo = self.cleaned_data.get('logo')
        if logo and hasattr(logo, 'file') and logo.name:
            if 'logo' in self.files:
                from .validators import validate_image_file_extension, validate_image_binary
                validate_image_file_extension(logo)
                validate_image_binary(logo)
        return logo

    def clean_hero_image(self):
        hero = self.cleaned_data.get('hero_image')
        if hero and hasattr(hero, 'file') and hero.name:
            if 'hero_image' in self.files:
                from .validators import validate_image_file_extension, validate_image_binary
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
            'title': BS_TEXT,
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'icon': BS_TEXT,
            'image': forms.ClearableFileInput(attrs={
                'class': 'form-control',
                'accept': 'image/jpeg,image/png,image/webp'
            }),
            'order': forms.NumberInput(attrs={'class': 'form-control'}),
            'is_active': BS_CHECKBOX,
        }
        help_texts = {
            'icon': 'Bootstrap icon class (e.g., bi-tools, bi-display). See <a href="https://icons.getbootstrap.com/" target="_blank">Bootstrap Icons</a>.',
            'order': 'Lower numbers appear first.',
        }


# ============================================================
# TESTIMONIAL FORM
# ============================================================
class TestimonialForm(forms.ModelForm):
    class Meta:
        model = Testimonial
        fields = ['customer_name', 'customer_photo', 'designation', 'company_name', 
                  'review_text', 'rating', 'order', 'is_active']
        widgets = {
            'customer_name': BS_TEXT,
            'customer_photo': forms.ClearableFileInput(attrs={
                'class': 'form-control',
                'accept': 'image/jpeg,image/png,image/webp'
            }),
            'designation': BS_TEXT,
            'company_name': BS_TEXT,
            'review_text': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
            'rating': forms.Select(attrs={'class': 'form-select'}),
            'order': forms.NumberInput(attrs={'class': 'form-control'}),
            'is_active': BS_CHECKBOX,
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
            'question': BS_TEXT,
            'answer': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
            'order': forms.NumberInput(attrs={'class': 'form-control'}),
            'is_active': BS_CHECKBOX,
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
            'opening_balance', 'notes'
        ]
        widgets = {
            'contact_type': BS_SELECT,
            'name': BS_TEXT,
            'company_name': BS_TEXT,
            'phone': BS_TEXT,
            'email': BS_EMAIL,
            'address': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'gstin': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'GSTIN'}),
            'state': BS_TEXT,
            'opening_balance': BS_NUMBER,
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_htmx_validation(
            validate_url=reverse('accounting:validate_contact_field'),  
            include_id_field='contact_id'
        )

    def clean_phone(self):
        phone = self.cleaned_data.get('phone')
        if phone:
            if not re.match(r'^\+?\d+$', phone):
                raise ValidationError("Phone number must contain only digits (and optional leading '+').")
            digits_only = phone.lstrip('+')
            if len(digits_only) < 10 or len(digits_only) > 15:
                raise ValidationError("Phone number must be between 10 and 15 digits.")
            qs = Contact.objects.filter(phone=phone)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise ValidationError("This phone number is already in use.")
        return phone

    def clean_opening_balance(self):
        balance = self.cleaned_data.get('opening_balance', Decimal('0'))
        return balance


# ============================================================
# 3. PRODUCT CATEGORY
# ============================================================
class ProductCategoryForm(forms.ModelForm):
    class Meta:
        model = ProductCategory
        fields = ['name', 'description']
        widgets = {
            'name': BS_TEXT,
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }

    def clean_name(self):
        name = self.cleaned_data.get('name', '').strip()
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
            'low_stock_threshold', 'tax_rate', 'is_service', 'is_active'
        ]
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'hsn_code': forms.TextInput(attrs={'class': 'form-control'}),
            'category': forms.Select(attrs={'class': 'form-select'}),
            'unit': forms.Select(attrs={'class': 'form-select'}),
            'purchase_price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'selling_price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'current_stock': forms.NumberInput(attrs={'class': 'form-control', 'step': 'any'}),
            'low_stock_threshold': forms.NumberInput(attrs={'class': 'form-control'}),
            'tax_rate': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
            'is_service': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['current_stock'].required = False
        self.fields['low_stock_threshold'].required = False
        self.fields['current_stock'].widget.attrs['placeholder'] = '0 (for service)'
        self.fields['low_stock_threshold'].widget.attrs['placeholder'] = '0 (for service)'

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
        except:
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
        except:
            raise ValidationError("Enter a valid integer.")

    def clean(self):
        cleaned_data = super().clean()
        is_service = cleaned_data.get('is_service', False)
        purchase_price = cleaned_data.get('purchase_price', Decimal('0'))
        selling_price = cleaned_data.get('selling_price', Decimal('0'))

        if is_service:
            cleaned_data['current_stock'] = Decimal('0')
            cleaned_data['low_stock_threshold'] = 0
        else:
            if purchase_price < 0:
                self.add_error('purchase_price', "Purchase price cannot be negative.")
            if selling_price < 0:
                self.add_error('selling_price', "Selling price cannot be negative.")
            if selling_price < purchase_price:
                self.add_error('selling_price', "Selling price should not be less than purchase price.")

        return cleaned_data


# ============================================================
# 5. SALES INVOICE
# ============================================================
class InvoiceForm(forms.ModelForm):
    class Meta:
        model = Invoice
        fields = ['customer', 'date', 'due_date', 'gst_type',
                  'discount_amount', 'notes']
        widgets = {
            'customer': BS_SELECT,
            'date': BS_DATE,
            'due_date': BS_DATE,
            'gst_type': BS_SELECT,
            'discount_amount': BS_NUMBER,
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }

    def clean_discount_amount(self):
        disc = self.cleaned_data.get('discount_amount', Decimal('0'))
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
                'placeholder': 'Quantity'
            }),
            'unit_price': forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.01',
                'id': 'id_unit_price'
            }),
            'tax_rate': forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.01',
                'id': 'id_tax_rate'
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['product'].queryset = Product.objects.filter(is_active=True)

    def clean_quantity(self):
        qty = self.cleaned_data.get('quantity')
        if qty <= 0:
            raise ValidationError("Quantity must be greater than zero.")
        return qty

    def clean_unit_price(self):
        price = self.cleaned_data.get('unit_price')
        if price < 0:
            raise ValidationError("Unit price cannot be negative.")
        return price


# ============================================================
# 6. PURCHASE INVOICE
# ============================================================
class PurchaseForm(forms.ModelForm, HTMXValidationMixin):
    class Meta:
        model = Purchase
        fields = ['vendor', 'date', 'gst_type', 'freight_charge', 'notes']
        widgets = {
            'vendor': BS_SELECT,
            'date': BS_DATE,
            'gst_type': BS_SELECT,
            'freight_charge': BS_NUMBER, 
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }

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
                'min': '0.01', 'step': 'any'
            }),
            'unit_price': forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.01',
                'id': 'id_unit_price'
            }),
            'tax_rate': forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.01',
                'id': 'id_tax_rate'
            }),
            'is_office_use': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['product'].queryset = Product.objects.filter(is_service=False)

    def clean_quantity(self):
        qty = self.cleaned_data.get('quantity')
        if qty <= 0:
            raise ValidationError("Quantity must be greater than zero.")
        return qty

    def clean_unit_price(self):
        price = self.cleaned_data.get('unit_price')
        if price < 0:
            raise ValidationError("Unit price cannot be negative.")
        return price


# ============================================================
# 7. REPAIR JOBS
# ============================================================

class RepairJobForm(forms.ModelForm, HTMXValidationMixin):
    class Meta:
        model = RepairJob
        fields = [
            'customer', 'device_model', 'serial_number',
            'issue_description', 'diagnosis_report', 'action_taken',
            'accessories', 'device_condition',
            'received_by', 
            'delivered_by', 'delivery_date',
            'delivered_to_name',
            'delivered_to_phone',
            'delivered_to_designation',
            'delivery_remarks',
            'estimated_cost', 'labour_charge',
            'status', 'date_in', 'notes'
        ]
        widgets = {
            'customer': BS_SELECT,
            'device_model': BS_TEXT,
            'serial_number': BS_TEXT,
            'issue_description': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
            'diagnosis_report': forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'E.g., Hard drive bad sectors, RAM loose, etc.'}),
            'action_taken': forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'Describe what was done...'}),
            'accessories': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'E.g., Adaptor, Bag, CD'}),
            'device_condition': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'E.g., Battery missing, Hard disk removed'}),
            'received_by': BS_TEXT,
            'delivered_by': BS_TEXT,
            'delivery_date': BS_DATE,
            'delivered_to_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Recipient name (if different from customer)'}),
            'delivered_to_phone': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Recipient phone number'}),
            'delivered_to_designation': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g., Driver, Accountant, Office Boy'}),
            'delivery_remarks': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Any special delivery remarks...'}),
            'estimated_cost': BS_NUMBER,
            'labour_charge': BS_NUMBER,
            'status': forms.Select(attrs={'class': 'form-select'}, choices=RepairJob.STATUS_CHOICES),
            'date_in': BS_DATE,
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Set default date_in if new
        if not self.instance.pk:
            self.fields['date_in'].initial = timezone.now().date()

        # Add HTMX validation to all fields except status (dynamic)
        field_names = [f for f in self.fields.keys() if f != 'status']
        self.add_htmx_validation(
            validate_url=reverse('accounting:validate_repair_field'),
            field_names=field_names,
            include_id_field='repair_id'
        )

        # Make delivery fields optional (since existing data may be null)
        # They are already optional because model has blank=True, null=True
        # But we can also set required=False explicitly for clarity
        for field in ['delivered_to_name', 'delivered_to_phone', 'delivered_to_designation', 'delivery_remarks']:
            self.fields[field].required = False

        # If status is 'delivered', we can optionally make delivery fields required
        # But we keep them optional for backward compatibility with existing data.
        # You can uncomment the following lines if you want strict validation:
        # if self.instance and self.instance.status == 'delivered':
        #     self.fields['delivered_to_name'].required = True
        #     self.fields['delivery_date'].required = True

    def clean_estimated_cost(self):
        cost = self.cleaned_data.get('estimated_cost')
        if cost is not None and cost < 0:
            raise ValidationError("Estimated cost cannot be negative.")
        return cost

    def clean_labour_charge(self):
        labour = self.cleaned_data.get('labour_charge', Decimal('0'))
        if labour < 0:
            raise ValidationError("Labour charge cannot be negative.")
        return labour

    # Optional: Cross-field validation for delivery
    def clean(self):
        cleaned_data = super().clean()
        status = cleaned_data.get('status')
        delivered_to_name = cleaned_data.get('delivered_to_name')
        delivery_date = cleaned_data.get('delivery_date')

        # If status is 'delivered', we recommend having at least a delivery date
        if status == 'delivered' and not delivery_date:
            self.add_error('delivery_date', "Delivery date is recommended when status is 'Delivered'.")
        # If delivery_to_name is filled but no phone, warn (but not error)
        if delivered_to_name and not cleaned_data.get('delivered_to_phone'):
            # You can add a warning or just ignore; we'll not add error to keep it flexible
            pass

        return cleaned_data


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
            'quantity': forms.NumberInput(attrs={'class': 'form-control', 'min': '1', 'step': '1'}),
            'unit_price': forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.01',
                'id': 'id_unit_price'
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['product'].queryset = Product.objects.filter(is_active=True)

    def clean_quantity(self):
        qty = self.cleaned_data.get('quantity')
        if qty <= 0:
            raise ValidationError("Quantity must be at least 1.")
        return qty

    def clean_unit_price(self):
        price = self.cleaned_data.get('unit_price')
        if price <= 0:
            raise ValidationError("Unit price must be greater than zero.")
        return price


# ============================================================
# 8. PAYMENTS
# ============================================================

class PaymentForm(forms.ModelForm):
    contact_search = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'id': 'contact-search',
            'placeholder': 'Type customer/vendor name or phone...',
            'autocomplete': 'off'
        }),
        label="Contact"
    )

    class Meta:
        model = Payment
        fields = [
            'direction', 'contact', 'amount', 'date', 'method',
            'bank_account', 'upi_ref', 'reference', 'description',
            'is_advance'
        ]
        widgets = {
            'direction': BS_SELECT,
            'contact': forms.HiddenInput(),   # Hidden – we use contact_search instead
            'amount': BS_NUMBER,
            'date': BS_DATE,
            'method': BS_SELECT,
            'bank_account': forms.Select(attrs={'class': 'form-select'}),
            'upi_ref': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'UPI TXN ID'}),
            'reference': BS_TEXT,
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'is_advance': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['bank_account'].queryset = BankAccount.objects.filter(is_active=True)
        
        # Pre-populate contact_search if contact is set
        if self.instance and self.instance.contact_id:
            contact = self.instance.contact
            self.fields['contact_search'].initial = f"{contact.name} ({contact.phone or 'No phone'})"

        if self.data and 'contact' in self.data:
            # If contact is set via hidden field, show the name in search
            contact_id = self.data.get('contact')
            if contact_id:
                try:
                    contact = Contact.objects.get(pk=contact_id)
                    self.fields['contact_search'].initial = f"{contact.name} ({contact.phone or 'No phone'})"
                except Contact.DoesNotExist:
                    pass


    def clean_contact_search(self):
        # Validate that a contact is selected (hidden field must have a value)
        contact_id = self.cleaned_data.get('contact')
        if not contact_id:
            raise ValidationError("Please select a contact from the search results.")
        return contact_id

    def clean_amount(self):
        amount = self.cleaned_data.get('amount')
        if amount <= 0:
            raise ValidationError("Amount must be positive.")
        return amount

    def clean(self):
        cleaned_data = super().clean()
        method = cleaned_data.get('method')
        bank_account = cleaned_data.get('bank_account')
        if method in ['bank', 'upi'] and not bank_account:
            self.add_error('bank_account', "Please select a bank account for bank/UPI payments.")
        return cleaned_data

# ============================================================
# 9. STOCK MOVEMENT
# ============================================================
class StockMovementForm(forms.ModelForm):
    class Meta:
        model = StockMovement
        fields = ['product', 'movement_type', 'quantity', 'reference', 'date', 'notes']
        widgets = {
            'product': BS_SELECT,
            'movement_type': BS_SELECT,
            'quantity': forms.NumberInput(attrs={'class': 'form-control', 'step': 'any'}),
            'reference': BS_TEXT,
            'date': BS_DATETIME,
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
        fields = ['name', 'account_number', 'bank_name', 'ifsc_code', 'account_type', 'opening_balance', 'is_active']
        widgets = {
            'name': BS_TEXT,
            'account_number': BS_TEXT,
            'bank_name': BS_TEXT,
            'ifsc_code': BS_TEXT,
            'account_type': BS_SELECT,
            'opening_balance': BS_NUMBER,
            'is_active': BS_CHECKBOX,
        }

    def clean_opening_balance(self):
        balance = self.cleaned_data.get('opening_balance', Decimal('0'))
        if balance < 0:
            raise ValidationError("Opening balance cannot be negative.")
        return balance


class BankTransactionForm(forms.ModelForm):
    class Meta:
        model = BankTransaction
        fields = ['bank_account', 'transaction_type', 'source_type', 'amount', 'date', 'description', 'reference']
        widgets = {
            'bank_account': forms.Select(attrs={'class': 'form-select'}),
            'transaction_type': BS_SELECT,
            'source_type': BS_SELECT,
            'amount': BS_NUMBER,
            'date': BS_DATE,
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'reference': BS_TEXT,
        }

    def clean_amount(self):
        amount = self.cleaned_data.get('amount')
        if amount <= 0:
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
                raise ValidationError(f"Invalid source type '{source_type}' for {transaction_type} transaction.")
        return cleaned


# ============================================================
# 11. JOURNAL
# ============================================================
class JournalForm(forms.Form):
    ENTRY_TYPE_CHOICES = [
        ('discount', 'Discount Allowed / Received'),
        ('advance_received', 'Advance Received (Customer)'),
        ('advance_paid', 'Advance Paid (Vendor)'),
        ('payment', 'Payment (Vendor)'),
        ('receipt', 'Receipt (Customer)'),
        ('general', 'General Journal'),
    ]
    
    contact = forms.ModelChoiceField(
        queryset=Contact.objects.all(),
        widget=BS_SELECT,
        label="Contact"
    )
    entry_type = forms.ChoiceField(
        choices=ENTRY_TYPE_CHOICES,
        widget=BS_SELECT,
        label="Transaction Type"
    )
    amount = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        widget=BS_NUMBER,
        label="Amount"
    )
    date = forms.DateField(
        widget=BS_DATE,
        required=False,
        label="Date",
        initial=timezone.now().date
    )
    narration = forms.CharField(
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        required=False,
        label="Narration"
    )

    def clean_amount(self):
        amount = self.cleaned_data.get('amount')
        if amount <= 0:
            raise ValidationError("Amount must be positive.")
        return amount

    def clean(self):
        cleaned_data = super().clean()
        contact = cleaned_data.get('contact')
        entry_type = cleaned_data.get('entry_type')

        if contact and entry_type:
            # Customer-only types
            customer_types = ['advance_received', 'receipt', 'discount']
            vendor_types = ['advance_paid', 'payment']

            if entry_type in customer_types and contact.contact_type not in ['customer', 'both']:
                self.add_error('entry_type', "This transaction type is only for customers.")
                self.add_error('contact', "Please select a customer for this transaction.")

            if entry_type in vendor_types and contact.contact_type not in ['vendor', 'both']:
                self.add_error('entry_type', "This transaction type is only for vendors.")
                self.add_error('contact', "Please select a vendor for this transaction.")
                
        return cleaned_data


# ============================================================
# 12. CUSTOMER PORTAL FORMS
# ============================================================
class CustomerProfileForm(forms.ModelForm):
    class Meta:
        model = Contact
        fields = ['name', 'company_name', 'phone', 'address', 'state', 'gstin']
        widgets = {
            'name': BS_TEXT,
            'company_name': BS_TEXT,
            'phone': BS_TEXT,
            'address': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'state': BS_TEXT,
            'gstin': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'GSTIN'}),
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
        phone = self.cleaned_data.get('phone')
        if phone:
            # 1. Format validation
            if not re.match(r'^\+?\d{10,15}$', phone):
                raise ValidationError("Enter a valid phone number (10-15 digits, optional +).")
            
            # 2. Normalize: remove non-digits, keep last 10 digits (Indian mobile)
            phone_clean = ''.join(filter(str.isdigit, phone))
            if len(phone_clean) >= 10:
                phone_clean = phone_clean[-10:]
            else:
                raise ValidationError("Phone number must contain at least 10 digits.")
            
            # 3. Uniqueness check (exclude self)
            qs = Contact.objects.filter(phone=phone_clean)
            if self.instance and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise ValidationError("This phone number is already registered by another user.")
            
            return phone_clean
        return phone


class CustomerRepairForm(forms.ModelForm):
    class Meta:
        model = RepairJob
        fields = ['device_model', 'serial_number', 'issue_description', 'accessories', 'device_condition']
        widgets = {
            'device_model': BS_TEXT,
            'serial_number': BS_TEXT,
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
            'hx-include': 'this'
        })
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
        })
    )
    category = forms.ModelChoiceField(
        queryset=ProductCategory.objects.all(),
        required=False,
        widget=forms.Select(attrs={
            'class': 'form-select',
            'hx-get': reverse_lazy('accounting:product_list'),  
            'hx-trigger': 'change',
            'hx-target': '#product-table',
            'hx-include': '[name=search]'
        }),
        empty_label="All Categories"
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
            'hx-include': '#filter-form'
        })
    )
    customer = forms.ModelChoiceField(
        queryset=Contact.objects.filter(contact_type__in=['customer', 'both']),
        required=False,
        widget=forms.Select(attrs={
            'class': 'form-select',
            'hx-get': reverse_lazy('accounting:invoice_list'),  
            'hx-trigger': 'change',
            'hx-target': '#invoice-table-container',
            'hx-include': '#filter-form'
        }),
        empty_label="All Customers"
    )
    status = forms.ChoiceField(
        choices=[('', 'All Status'), ('paid', 'Paid'), ('partial', 'Partial'), ('unpaid', 'Unpaid')],
        required=False,
        widget=forms.Select(attrs={
            'class': 'form-select',
            'hx-get': reverse_lazy('accounting:invoice_list'), 
            'hx-trigger': 'change',
            'hx-target': '#invoice-table-container',
            'hx-include': '#filter-form'
        })
    )
    date_from = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={
            'class': 'form-control', 'type': 'date',
            'hx-get': reverse_lazy('accounting:invoice_list'), 
            'hx-trigger': 'change',
            'hx-target': '#invoice-table-container',
            'hx-include': '#filter-form'
        })
    )
    date_to = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={
            'class': 'form-control', 'type': 'date',
            'hx-get': reverse_lazy('accounting:invoice_list'), 
            'hx-trigger': 'change',
            'hx-target': '#invoice-table-container',
            'hx-include': '#filter-form'
        })
    )

    
# ============================================================
# EMAIL CHANGE REQUEST FORM
# ============================================================

class EmailChangeRequestForm(forms.Form):
    """Form to request email change."""
    new_email = forms.EmailField(
        widget=forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'Enter new email address'}),
        label="New Email"
    )

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_new_email(self):
        email = self.cleaned_data.get('new_email').strip().lower()
        if User.objects.filter(email=email).exclude(pk=self.user.pk).exists():
            raise ValidationError("This email is already registered by another user.")
        if email == self.user.email:
            raise ValidationError("This is your current email. Please enter a different email.")
        return email