# accounting/views/auth.py
import json
import logging
from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import PasswordResetView, PasswordResetConfirmView
from django.urls import reverse_lazy, reverse
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.views.decorators.http import require_http_methods
from django.db import transaction
from django.contrib.auth.models import User
from django.utils import timezone

# Allauth imports for Custom Signup View
from allauth.account.views import SignupView
from allauth.account import app_settings as allauth_settings

from ..models import Contact, EmailOTP
from ..forms import (
    CustomerRegistrationForm,
    CustomPasswordResetForm,
    CustomPasswordChangeForm,
)
from .utils import redirect_to_customer, redirect_to_staff
from ..utils.otp_helpers import create_and_send_otp, verify_otp
from ..decorators import handle_errors

logger = logging.getLogger(__name__)


# ============================================================
# 1. UNIFIED LOGIN (Staff + Customer) – Phone/Email/Username
# ============================================================
# यह View आपके Custom Backend का उपयोग करता है, जो Phone, Email, Username – तीनों से Login करता है।
# Allauth के Login View को Override करने के लिए इस View को /accounts/login/ पर Map करें (यदि चाहें)।
# ============================================================

def unified_login_view(request):
    if request.user.is_authenticated:
        return redirect_to_staff('dashboard') if request.user.is_staff else redirect_to_customer('customer_dashboard')

    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            username = form.cleaned_data.get('username')
            password = form.cleaned_data.get('password')
            user = authenticate(request, username=username, password=password)
            if user is not None:
                login(request, user)
                logger.info(f"User {user.username} logged in.")
                messages.success(request, f"Welcome back, {user.get_full_name() or user.username}!")
                
                if request.headers.get('HX-Request'):
                    response = HttpResponse()
                    if user.is_staff:
                        response['HX-Redirect'] = reverse('accounting:dashboard')
                    else:
                        response['HX-Redirect'] = reverse('customer:customer_dashboard')
                    return response
                
                return redirect_to_staff('dashboard') if user.is_staff else redirect_to_customer('customer_dashboard')
            else:
                messages.error(request, "Invalid email/mobile or password.")
                if request.headers.get('HX-Request'):
                    response = render(request, 'auth/partials/_login_form.html', {'form': form}, status=400)
                    response['HX-Trigger'] = json.dumps({
                        'showToast': {
                            'level': 'danger',
                            'message': 'Invalid email/mobile or password.'
                        }
                    })
                    return response
        else:
            if request.headers.get('HX-Request'):
                response = render(request, 'auth/partials/_login_form.html', {'form': form}, status=400)
                errors = []
                for field, field_errors in form.errors.items():
                    for err in field_errors:
                        errors.append(err)
                error_message = ' '.join(errors) if errors else 'Please correct the errors below.'
                response['HX-Trigger'] = json.dumps({
                    'showToast': {
                        'level': 'danger',
                        'message': error_message
                    }
                })
                return response
    else:
        form = AuthenticationForm()

    return render(request, 'auth/unified_login.html', {'form': form})


# ============================================================
# 2. CUSTOMER REGISTRATION (with OTP) 
# ============================================================
@handle_errors(default_redirect='home')
def register_view(request):
    if request.user.is_authenticated:
        if request.user.is_staff:
            return redirect('accounting:dashboard')
        else:
            return redirect('customer:customer_dashboard')

    if request.method == 'POST':
        form = CustomerRegistrationForm(request.POST)

        # HONEYPOT CHECK
        if request.POST.get('website', '').strip():
            logger.warning(f"🔥 Honeypot triggered on registration from IP {request.META.get('REMOTE_ADDR')}")
            if request.headers.get('HX-Request'):
                response = HttpResponse()
                response['HX-Trigger'] = json.dumps({
                    'showToast': {
                        'level': 'success',
                        'message': '🎉 Registration successful! Please check your email for OTP.',
                        'title': 'Welcome!'
                    }
                })
                response['HX-Redirect'] = reverse('accounting:verify_otp')
                return response
            return redirect('accounting:verify_otp')

        if form.is_valid():
            # Step 1: Save User (Inactive)
            user = form.save(commit=False)
            user.is_active = False
            user.save()
            logger.info(f"🔥 User saved: {user.username} (ID: {user.id})")

            # Step 2: Create Contact manually (because we used commit=False)
            Contact.objects.create(
                user=user,
                name=form.cleaned_data['full_name'],
                email=form.cleaned_data['email'],
                phone=form.cleaned_data['phone'],
                contact_type='customer'
            )
            logger.info(f"✅ Contact created for user: {user.username}")

            # Step 3: Try to send OTP 
            otp_sent = False
            try:
                otp_sent = create_and_send_otp(user, user.email, 'signup')
            except Exception as e:
                logger.error(f"🔥 OTP sending failed: {e}")

            if otp_sent:
                request.session['pending_user_id'] = user.id
                request.session['pending_email'] = user.email
                request.session['otp_purpose'] = 'signup'

                messages.success(request, f"Welcome {user.first_name}! OTP sent to your email.")
                if request.headers.get('HX-Request'):
                    response = HttpResponse()
                    response['HX-Redirect'] = reverse('accounting:verify_otp')
                    response['HX-Trigger'] = json.dumps({
                        'showToast': {
                            'level': 'success',
                            'message': f'🎉 Welcome {user.first_name}! Check your email for OTP.',
                            'title': 'Registration Successful'
                        }
                    })
                    return response
                return redirect('accounting:verify_otp')
            else:
                request.session['pending_user_id'] = user.id
                request.session['pending_email'] = user.email
                request.session['otp_purpose'] = 'signup'

                messages.warning(request, "Registration successful, but OTP could not be sent. Please try resending OTP.")
                if request.headers.get('HX-Request'):
                    response = HttpResponse()
                    response['HX-Redirect'] = reverse('accounting:verify_otp')
                    response['HX-Trigger'] = json.dumps({
                        'showToast': {
                            'level': 'warning',
                            'message': 'Account created, but OTP email failed. Please click "Resend OTP" to try again.',
                            'title': 'Registration Partial'
                        }
                    })
                    return response
                return redirect('accounting:verify_otp')
        else:
            if request.headers.get('HX-Request'):
                response = render(request, 'auth/partials/_register_form.html', {'form': form}, status=400)
                response['HX-Trigger'] = json.dumps({
                    'showToast': {
                        'level': 'danger',
                        'message': 'Please correct the errors below.'
                    }
                })
                return response
            return render(request, 'auth/register.html', {'form': form})
    else:
        form = CustomerRegistrationForm()
        if request.headers.get('HX-Request'):
            return render(request, 'auth/partials/_register_form.html', {'form': form})
        return render(request, 'auth/register.html', {'form': form})

# ============================================================
# 3. UNIFIED LOGOUT
# ============================================================

def unified_logout_view(request):
    if request.user.is_authenticated:
        logout(request)
        messages.info(request, "You have been logged out.")
    return redirect('home')


# ============================================================
# 4. PASSWORD RESET (Token-based – Legacy) – यदि आप Allauth Reset का उपयोग करते हैं तो इसकी ज़रूरत नहीं
# ============================================================

class CustomPasswordResetView(PasswordResetView):
    form_class = CustomPasswordResetForm
    template_name = 'auth/password_reset.html'
    email_template_name = 'auth/password_reset_email.html'
    subject_template_name = 'auth/password_reset_subject.txt'
    success_url = reverse_lazy('accounting:password_reset_done')

    def form_valid(self, form):
        messages.success(self.request, "Password reset email sent. Please check your inbox.")
        return super().form_valid(form)
    
    def form_invalid(self, form):
        messages.error(self.request, "Email not found. Please check and try again.")
        return super().form_invalid(form)


class CustomPasswordResetConfirmView(PasswordResetConfirmView):
    template_name = 'auth/password_reset_confirm.html'
    success_url = reverse_lazy('accounting:password_reset_complete')

    def form_valid(self, form):
        messages.success(self.request, "Your password has been reset successfully. You can now login.")
        return super().form_valid(form)


# ============================================================
# 5. PASSWORD CHANGE (Logged-in)
# ============================================================

@login_required
@handle_errors(default_redirect='home')
def password_change_view(request):
    if request.method == 'POST':
        form = CustomPasswordChangeForm(user=request.user, data=request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)
            messages.success(request, "Your password has been changed successfully.")
            if request.headers.get('HX-Request'):
                response = HttpResponse()
                if request.user.is_staff:
                    response['HX-Redirect'] = reverse('accounting:dashboard')
                else:
                    response['HX-Redirect'] = reverse('customer:customer_dashboard')
                return response
            return redirect_to_staff('dashboard') if request.user.is_staff else redirect_to_customer('customer_dashboard')
        else:
            if request.headers.get('HX-Request'):
                return render(request, 'auth/partials/_password_change_form.html', {'form': form}, status=400)
    else:
        form = CustomPasswordChangeForm(user=request.user)
    
    return render(request, 'auth/password_change.html', {'form': form})


# ============================================================
# 6. HTMX REAL-TIME VALIDATION
# ============================================================

@require_http_methods(["GET"])
def validate_register_field(request):
    """
    Real-time field validation for registration form.
    Called by HTMX on blur/keyup.
    """
    field = request.GET.get('field')
    value = request.GET.get(field, '')

    # HONEYPOT: Honeypot field ko GET validation se bypass karo
    if field == 'website':
        return HttpResponse("")

    from ..forms import CustomerRegistrationForm

    # Build full data dict with all fields
    data = {}
    for f in CustomerRegistrationForm.base_fields:
        data[f] = request.GET.get(f, '')

    data[field] = value

    form = CustomerRegistrationForm(data)
    form.is_valid()  # Triggers validation on all fields
    errors = form.errors.get(field, [])

    html = render_to_string('auth/partials/_field_errors.html', {
        'field': field,
        'errors': errors,
        'value': value
    })
    return HttpResponse(html)



@require_http_methods(["GET"])
def validate_login_field(request):
    """
    Real-time validation for login form.
    Returns only the error partial (not full page).
    """
    field = request.GET.get('field')
    value = request.GET.get(field, '').strip()
    errors = []

    if field == 'username':
        if not value:
            errors.append('Email or Mobile number is required.')

    html = render_to_string('auth/partials/_field_errors.html', {
        'field': field,
        'errors': errors,
        'value': value
    })
    return HttpResponse(html)


# ============================================================
# 7. OTP VERIFICATION, RESEND, AND PASSWORD RESET (OTP-based)
# ============================================================

@handle_errors(default_redirect='home')
def verify_otp_view(request):
    user_id = request.session.get('pending_user_id')
    email = request.session.get('pending_email')
    purpose = request.session.get('otp_purpose')

    if not user_id or not email or not purpose:
        messages.error(request, "Invalid OTP verification session. Please try again.")
        return redirect('home')

    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        messages.error(request, "User not found.")
        return redirect('home')

    if request.method == 'POST':
        otp = request.POST.get('otp', '').strip()
        if not otp or len(otp) != 6:
            messages.error(request, "Please enter a valid 6-digit OTP.")
            if request.headers.get('HX-Request'):
                response = render(request, 'auth/verify_otp.html', {'email': email, 'purpose': purpose}, status=400)
                response['HX-Trigger'] = json.dumps({
                    'showToast': {
                        'level': 'danger',
                        'message': 'Please enter a valid 6-digit OTP.'
                    }
                })
                return response
            return render(request, 'auth/verify_otp.html', {'email': email, 'purpose': purpose})

        verified_user = verify_otp(email, otp, purpose)

        if verified_user and verified_user.id == user.id:
            # OTP Verified – Activate user & login
            if purpose == 'signup':
                user.is_active = True
                user.save()

                # Clean session
                request.session.pop('pending_user_id', None)
                request.session.pop('pending_email', None)
                request.session.pop('otp_purpose', None)

                # --- FIX: Set backend explicitly before login ---
                user.backend = 'accounting.auth_backends.EmailOrPhoneBackend'
                login(request, user)

                messages.success(request, "Email verified! Welcome aboard.")

                if request.headers.get('HX-Request'):
                    response = HttpResponse()
                    response['HX-Redirect'] = reverse('customer:customer_dashboard')
                    response['HX-Trigger'] = json.dumps({
                        'showToast': {
                            'level': 'success',
                            'message': f'🎉 Welcome {user.first_name or user.username}! Your account is verified.',
                            'title': 'Registration Complete'
                        }
                    })
                    return response

                return redirect('customer:customer_dashboard')

            elif purpose == 'reset_password':
                request.session['reset_user_id'] = user.id
                request.session['reset_email'] = email
                request.session.pop('pending_user_id', None)
                request.session.pop('pending_email', None)
                request.session.pop('otp_purpose', None)

                if request.headers.get('HX-Request'):
                    response = HttpResponse()
                    response['HX-Redirect'] = reverse('accounting:reset_password_set')
                    return response
                return redirect('accounting:reset_password_set')

        else:
            messages.error(request, "Invalid or expired OTP. Please try again.")
            if request.headers.get('HX-Request'):
                response = render(request, 'auth/verify_otp.html', {'email': email, 'purpose': purpose}, status=400)
                response['HX-Trigger'] = json.dumps({
                    'showToast': {
                        'level': 'danger',
                        'message': 'Invalid or expired OTP. Please try again.'
                    }
                })
                return response
            return render(request, 'auth/verify_otp.html', {'email': email, 'purpose': purpose})

    # GET request – show OTP form
    return render(request, 'auth/verify_otp.html', {'email': email, 'purpose': purpose})


@handle_errors(default_redirect='home')
def resend_otp_view(request):
    user_id = request.session.get('pending_user_id')
    email = request.session.get('pending_email')
    purpose = request.session.get('otp_purpose')

    if not user_id or not email or not purpose:
        messages.error(request, "Session expired. Please start the verification process again.")
        return redirect('home')

    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        messages.error(request, "User not found.")
        return redirect('home')

    # Cooldown: 60 seconds
    last_otp = EmailOTP.objects.filter(email=email, purpose=purpose).order_by('-created_at').first()
    if last_otp:
        time_diff = (timezone.now() - last_otp.created_at).total_seconds()
        if time_diff < 60:
            messages.warning(request, f"Please wait {60 - int(time_diff)} seconds before requesting a new OTP.")
            if request.headers.get('HX-Request'):
                response = HttpResponse()
                response['HX-Trigger'] = json.dumps({
                    'showToast': {
                        'level': 'warning',
                        'message': f'Please wait {60 - int(time_diff)} seconds before requesting a new OTP.'
                    }
                })
                response['HX-Redirect'] = reverse('accounting:verify_otp')
                return response
            return redirect('accounting:verify_otp')

    success = create_and_send_otp(user, email, purpose)

    if success:
        messages.success(request, "A new OTP has been sent to your email.")
        if request.headers.get('HX-Request'):
            response = HttpResponse()
            response['HX-Trigger'] = json.dumps({
                'showToast': {
                    'level': 'success',
                    'message': 'New OTP sent to your email!',
                    'title': 'OTP Resent'
                }
            })
            response['HX-Redirect'] = reverse('accounting:verify_otp')
            return response
    else:
        messages.error(request, "Failed to send OTP. Please try again later.")
        if request.headers.get('HX-Request'):
            response = HttpResponse()
            response['HX-Trigger'] = json.dumps({
                'showToast': {
                    'level': 'danger',
                    'message': 'Failed to send OTP. Please try again later.'
                }
            })
            response['HX-Redirect'] = reverse('accounting:verify_otp')
            return response

    return redirect('accounting:verify_otp')


@handle_errors(default_redirect='home')
def password_reset_otp_request(request):
    if request.method == 'POST':
        email = request.POST.get('email', '').strip()
        if not email:
            messages.error(request, "Please enter your email address.")
            return render(request, 'auth/password_reset_otp.html')
        
        try:
            user = User.objects.get(email__iexact=email)
        except (User.DoesNotExist, User.MultipleObjectsReturned):
            users = User.objects.filter(email__iexact=email)
            if users.count() == 1:
                user = users.first()
            else:
                # Multiple users – rare case, but handle gracefully
                messages.info(request, "If an account with this email exists, we've sent an OTP.")
                return render(request, 'auth/password_reset_otp.html')
        
        success = create_and_send_otp(user, email, 'reset_password')
        
        if success:
            request.session['pending_user_id'] = user.id
            request.session['pending_email'] = email
            request.session['otp_purpose'] = 'reset_password'
            messages.success(request, "OTP sent to your email. Please verify.")
            return redirect('accounting:verify_otp')
        else:
            messages.error(request, "Unable to send OTP. Please try again later.")
            return render(request, 'auth/password_reset_otp.html')
    
    return render(request, 'auth/password_reset_otp.html')


@handle_errors(default_redirect='home')
def reset_password_set_view(request):
    user_id = request.session.get('reset_user_id')
    email = request.session.get('reset_email')
    
    if not user_id or not email:
        messages.error(request, "Invalid session. Please request password reset again.")
        return redirect('home')
    
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        messages.error(request, "User not found.")
        return redirect('home')
    
    if request.method == 'POST':
        password1 = request.POST.get('password1')
        password2 = request.POST.get('password2')
        
        if not password1 or len(password1) < 8:
            messages.error(request, "Password must be at least 8 characters.")
            return render(request, 'auth/reset_password_set.html', {'email': email})
        
        if password1 != password2:
            messages.error(request, "Passwords do not match.")
            return render(request, 'auth/reset_password_set.html', {'email': email})
        
        user.set_password(password1)
        user.save()
        
        request.session.pop('reset_user_id', None)
        request.session.pop('reset_email', None)
        
        messages.success(request, "Password reset successfully! Please login with your new password.")
        return redirect('accounting:login')
    
    return render(request, 'auth/reset_password_set.html', {'email': email})


# ============================================================
# 8. ALLAUTH CUSTOM SIGNUP VIEW (with OTP)
# ============================================================
class CustomSignupView(SignupView):
    form_class = CustomerRegistrationForm
    success_url = reverse_lazy('accounting:verify_otp')

    def form_valid(self, form):
        # Allauth Default User Creation
        response = super().form_valid(form)

        # User को Inactive करें (OTP Verify होने पर Active होगा)
        user = self.user
        user.is_active = False
        user.save()

        # OTP भेजें
        create_and_send_otp(user, user.email, 'signup')

        # Session में Data Set करें (OTP Verification View के लिए)
        self.request.session['pending_user_id'] = user.id
        self.request.session['pending_email'] = user.email
        self.request.session['otp_purpose'] = 'signup'

        return response
    
    
