# accounting/views/auth.py
"""
Authentication views for staff + customer portal.

Features (all preserved):
─────────────────────────
✔ Unified login (Email / Phone / Username)
✔ Customer registration with OTP verification
✔ Honeypot spam protection
✔ Login rate limiting (NEW — security layer)
✔ Password reset via OTP
✔ Password reset via token (legacy)
✔ Password change (logged-in)
✔ Email change with OTP
✔ HTMX support (HX-Redirect / HX-Trigger / toast)
✔ Allauth CustomSignupView override
"""

import json
import logging
import hashlib

from allauth.account.views import SignupView

from django.contrib import messages
from django.contrib.auth import (
    authenticate, get_user_model, login, logout, update_session_auth_hash,
)
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.views import (
    PasswordResetConfirmView, PasswordResetView,
)
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from ..decorators import handle_errors
from ..forms import (
    CustomerRegistrationForm,
    CustomPasswordChangeForm,
    CustomPasswordResetForm,
)
from ..models import Contact, EmailOTP
from ..utils.otp_helpers import (
    OTP_EXPIRY_MINUTES,
    OTP_MAX_ATTEMPTS,
    OTP_RESEND_COOLDOWN_SECONDS,
    create_and_send_otp,
    verify_otp,
)
from .utils import is_htmx, redirect_to_customer, redirect_to_staff

logger = logging.getLogger(__name__)
User = get_user_model()


# ════════════════════════════════════════════════════════════
# CONSTANTS
# ════════════════════════════════════════════════════════════
AUTH_BACKEND = 'accounting.auth_backends.EmailOrPhoneBackend'

# Login rate limiting
LOGIN_MAX_ATTEMPTS = 5           # failed attempts per window
LOGIN_WINDOW_SECONDS = 300       # 5 minutes

# Session keys (centralised to avoid typos)
SK_PENDING_USER_ID = 'pending_user_id'
SK_PENDING_EMAIL = 'pending_email'
SK_OTP_PURPOSE = 'otp_purpose'
SK_RESET_USER_ID = 'reset_user_id'
SK_RESET_EMAIL = 'reset_email'
SK_PENDING_NEW_EMAIL = 'pending_new_email'


# ════════════════════════════════════════════════════════════
# HELPERS — Rate limiting + IP extraction
# ════════════════════════════════════════════════════════════
def _get_client_ip(request) -> str:
    """Extract client IP, honoring X-Forwarded-For."""
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', 'unknown')


def _rate_key(request, identifier: str) -> str:
    """Build a short cache key (hashed) for rate limiting."""
    raw = f"{_get_client_ip(request)}:{identifier.lower()}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:32]
    return f"login_rl:{digest}"


def _is_rate_limited(request, identifier: str) -> bool:
    return cache.get(_rate_key(request, identifier), 0) >= LOGIN_MAX_ATTEMPTS


def _record_failed_login(request, identifier: str) -> None:
    key = _rate_key(request, identifier)
    if cache.add(key, 1, LOGIN_WINDOW_SECONDS):
        return  # first failure — TTL set
    try:
        cache.incr(key)
    except ValueError:
        # Key expired between add and incr — recreate
        cache.set(key, 1, LOGIN_WINDOW_SECONDS)


def _clear_login_attempts(request, identifier: str) -> None:
    cache.delete(_rate_key(request, identifier))


def _clear_otp_session(request) -> None:
    """Remove all pending OTP/signup session keys."""
    for key in (SK_PENDING_USER_ID, SK_PENDING_EMAIL,
                SK_OTP_PURPOSE, SK_RESET_USER_ID,
                SK_RESET_EMAIL, SK_PENDING_NEW_EMAIL):
        request.session.pop(key, None)


# ════════════════════════════════════════════════════════════
# 1. UNIFIED LOGIN
# ════════════════════════════════════════════════════════════
def unified_login_view(request):
    """
    Single login page for staff + customer.
    Accepts: email / phone / username.
    Supports HTMX (returns HX-Redirect for SPA-like nav).
    """
    # Already logged in? Redirect by role
    if request.user.is_authenticated:
        return (
            redirect_to_staff('dashboard')
            if request.user.is_staff
            else redirect_to_customer('customer_dashboard')
        )

    htmx = is_htmx(request)

    if request.method == 'POST':
        username = (request.POST.get('username') or '').strip()

        # ── Rate limit guard ──────────────────────────────
        if _is_rate_limited(request, username):
            msg = (
                f"Too many failed login attempts. "
                f"Please try again after {LOGIN_WINDOW_SECONDS // 60} minutes."
            )
            logger.warning(
                "Login rate-limited | input=%s | ip=%s",
                username[:2] + '***', _get_client_ip(request),
            )
            if htmx:
                response = render(
                    request, 'auth/partials/_login_form.html',
                    {'form': AuthenticationForm()},
                )
                response['HX-Trigger'] = json.dumps({
                    'showToast': {'level': 'danger', 'message': msg},
                })
                return response
            messages.error(request, msg)
            return render(request, 'auth/unified_login.html',
                          {'form': AuthenticationForm()})

        # ── Form validation ───────────────────────────────
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = authenticate(
                request,
                username=form.cleaned_data['username'],
                password=form.cleaned_data['password'],
            )
            if user is not None:
                # ---- SUCCESS ----
                _clear_login_attempts(request, username)
                login(request, user, backend=AUTH_BACKEND)
                logger.info(
                    "User logged in | user=%s | staff=%s | ip=%s",
                    user.username, user.is_staff, _get_client_ip(request),
                )
                messages.success(
                    request,
                    f"Welcome back, {user.get_full_name() or user.username}!",
                )
                return _redirect_after_login(request, user, htmx)

            # ---- Wrong credentials ----
            _record_failed_login(request, username)

        # ---- Form invalid OR auth failed ----
        if htmx:
            response = render(
                request, 'auth/partials/_login_form.html', {'form': form},
            )
            errors = []
            for field_errors in form.errors.values():
                errors.extend(field_errors)
            msg = ' '.join(errors) or 'Invalid email/mobile or password.'
            response['HX-Trigger'] = json.dumps({
                'showToast': {'level': 'danger', 'message': msg},
            })
            return response
    else:
        form = AuthenticationForm()

    return render(request, 'auth/unified_login.html', {'form': form})


def _redirect_after_login(request, user, htmx):
    """Role-based redirect (staff -> dashboard, customer -> portal)."""
    target = (
        reverse('accounting:dashboard') if user.is_staff
        else reverse('customer:customer_dashboard')
    )
    if htmx:
        response = HttpResponse()
        response['HX-Redirect'] = target
        return response
    return redirect(target)


# ════════════════════════════════════════════════════════════
# 2. CUSTOMER REGISTRATION (with OTP)
# ════════════════════════════════════════════════════════════
@handle_errors(default_redirect='home')
def register_view(request):
    """Register a new customer account (inactive until OTP verified)."""
    if request.user.is_authenticated:
        return (
            redirect('accounting:dashboard') if request.user.is_staff
            else redirect('customer:customer_dashboard')
        )

    htmx = is_htmx(request)

    if request.method == 'POST':
        # ── Honeypot: silent success for bots ────────────
        if (request.POST.get('website') or '').strip():
            logger.warning(
                "Honeypot triggered on register | ip=%s",
                _get_client_ip(request),
            )
            return _registration_success_response(
                request, htmx, user=None,
                message='🎉 Registration successful! Please check your email for OTP.',
            )

        form = CustomerRegistrationForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.is_active = False
            user.save()

            Contact.objects.create(
                user=user,
                name=form.cleaned_data['full_name'],
                email=form.cleaned_data['email'],
                phone=form.cleaned_data.get('phone', ''),
                contact_type='customer',
            )

            # Try sending OTP (never crash registration on email failure)
            try:
                otp_sent = create_and_send_otp(user, user.email, 'signup')
            except Exception:
                logger.exception("OTP send failed during registration")
                otp_sent = False

            # Set session for verify step
            request.session[SK_PENDING_USER_ID] = user.id
            request.session[SK_PENDING_EMAIL] = user.email
            request.session[SK_OTP_PURPOSE] = 'signup'

            if otp_sent:
                messages.success(
                    request,
                    f"Welcome {user.first_name}! OTP sent to your email.",
                )
                return _registration_success_response(
                    request, htmx, user=user,
                    message=f'🎉 Welcome {user.first_name}! Check your email for OTP.',
                    title='Registration Successful',
                    level='success',
                )
            else:
                messages.warning(
                    request,
                    "Registration successful, but OTP could not be sent. "
                    "Please try resending OTP.",
                )
                return _registration_success_response(
                    request, htmx, user=user,
                    message='Account created, but OTP email failed. '
                            'Please click "Resend OTP".',
                    title='Registration Partial',
                    level='warning',
                )

        # ---- Form errors ----
        if htmx:
            response = render(
                request, 'auth/partials/_register_form.html', {'form': form},
            )
            response['HX-Trigger'] = json.dumps({
                'showToast': {
                    'level': 'danger',
                    'message': 'Please correct the errors below.',
                },
            })
            return response
        return render(request, 'auth/register.html', {'form': form})

    # GET request
    form = CustomerRegistrationForm()
    template = (
        'auth/partials/_register_form.html' if htmx
        else 'auth/register.html'
    )
    return render(request, template, {'form': form})


def _registration_success_response(request, htmx, user, message,
                                    title='Welcome!', level='success'):
    """Common success-response for register + honeypot."""
    target = reverse('accounting:verify_otp')
    if htmx:
        response = HttpResponse()
        response['HX-Redirect'] = target
        response['HX-Trigger'] = json.dumps({
            'showToast': {'level': level, 'message': message, 'title': title},
        })
        return response
    return redirect(target)


# ════════════════════════════════════════════════════════════
# 3. LOGOUT
# ════════════════════════════════════════════════════════════
@require_POST
def unified_logout_view(request):
    if request.user.is_authenticated:
        logger.info("User logged out | user=%s", request.user.username)
        logout(request)
        messages.info(request, "You have been logged out.")
    return redirect('home')


# ════════════════════════════════════════════════════════════
# 4. PASSWORD RESET (Token-based — legacy)
# ════════════════════════════════════════════════════════════
class CustomPasswordResetView(PasswordResetView):
    form_class = CustomPasswordResetForm
    template_name = 'auth/password_reset.html'
    email_template_name = 'auth/password_reset_email.html'
    subject_template_name = 'auth/password_reset_subject.txt'
    success_url = reverse_lazy('accounting:password_reset_done')

    def form_valid(self, form):
        messages.success(
            self.request,
            "Password reset email sent. Please check your inbox.",
        )
        return super().form_valid(form)

    def form_invalid(self, form):
        messages.error(
            self.request,
            "Email not found. Please check and try again.",
        )
        return super().form_invalid(form)


class CustomPasswordResetConfirmView(PasswordResetConfirmView):
    template_name = 'auth/password_reset_confirm.html'
    success_url = reverse_lazy('accounting:password_reset_complete')

    def form_valid(self, form):
        messages.success(
            self.request,
            "Your password has been reset successfully. You can now login.",
        )
        return super().form_valid(form)


# ════════════════════════════════════════════════════════════
# 5. PASSWORD CHANGE (logged-in)
# ════════════════════════════════════════════════════════════
@login_required
@handle_errors(default_redirect='home')
def password_change_view(request):
    htmx = is_htmx(request)

    if request.method == 'POST':
        form = CustomPasswordChangeForm(user=request.user, data=request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)
            logger.info(
                "Password changed | user=%s | ip=%s",
                user.username, _get_client_ip(request),
            )
            messages.success(request, "Your password has been changed successfully.")
            if htmx:
                response = HttpResponse()
                target = (
                    reverse('accounting:dashboard') if user.is_staff
                    else reverse('customer:customer_dashboard')
                )
                response['HX-Redirect'] = target
                return response
            return (
                redirect_to_staff('dashboard') if user.is_staff
                else redirect_to_customer('customer_dashboard')
            )

        if htmx:
            return render(
                request, 'auth/partials/_password_change_form.html',
                {'form': form}, status=400,
            )
    else:
        form = CustomPasswordChangeForm(user=request.user)

    return render(request, 'auth/password_change.html', {'form': form})


# ════════════════════════════════════════════════════════════
# 6. HTMX REAL-TIME FIELD VALIDATION
# ════════════════════════════════════════════════════════════
@require_http_methods(["GET"])
def validate_register_field(request):
    """HTMX validation for the registration form fields."""
    field = request.GET.get('field')
    value = request.GET.get(field, '')

    # Honeypot field — silently ignore
    if field == 'website' or not field:
        return HttpResponse("")

    # Build full data dict so cross-field rules work
    data = {f: request.GET.get(f, '') for f in CustomerRegistrationForm.base_fields}
    data[field] = value

    form = CustomerRegistrationForm(data)
    form.is_valid()
    errors = form.errors.get(field, [])

    html = render_to_string('auth/partials/_field_errors.html', {
        'field': field, 'errors': errors, 'value': value,
    })
    return HttpResponse(html)


@require_http_methods(["GET"])
def validate_login_field(request):
    """HTMX validation for login form (currently only checks empty)."""
    field = request.GET.get('field')
    value = (request.GET.get(field, '') or '').strip()
    errors = []

    if field == 'username' and not value:
        errors.append('Email or Mobile number is required.')

    html = render_to_string('auth/partials/_field_errors.html', {
        'field': field, 'errors': errors, 'value': value,
    })
    return HttpResponse(html)


# ════════════════════════════════════════════════════════════
# 7. OTP VERIFY / RESEND
# ════════════════════════════════════════════════════════════
@handle_errors(default_redirect='home')
def verify_otp_view(request):
    """
    Verify the OTP sent during signup or password reset.
    On success: activates user (signup) or forwards to reset-password page.
    """
    user_id = request.session.get(SK_PENDING_USER_ID)
    email = request.session.get(SK_PENDING_EMAIL)
    purpose = request.session.get(SK_OTP_PURPOSE)

    if not user_id or not email or not purpose:
        messages.error(request, "Invalid OTP verification session. Please try again.")
        return redirect('home')

    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        _clear_otp_session(request)
        messages.error(request, "User not found.")
        return redirect('home')

    htmx = is_htmx(request)
    ctx = {'email': email, 'purpose': purpose}

    if request.method == 'POST':
        otp = (request.POST.get('otp') or '').strip()

        if not otp or len(otp) != 6 or not otp.isdigit():
            return _otp_error_response(
                request, htmx, ctx,
                'Please enter a valid 6-digit OTP.',
            )

        # Brute-force guard: OTP ke lifetime me limited wrong attempts.
        # Bina iske 6-digit OTP (10^9 combinations... 10^6) ko 10 minute
        # ki window me parallel requests se todha ja sakta hai.
        attempts_key = f'otp_verify_attempts:{purpose}:{email.lower()}'
        if cache.get(attempts_key, 0) >= OTP_MAX_ATTEMPTS:
            logger.warning(
                "OTP verify blocked (too many attempts) | purpose=%s",
                purpose,
            )
            return _otp_error_response(
                request, htmx, ctx,
                'Too many wrong attempts. Please request a new OTP.',
                status=429,
            )

        verified_user = verify_otp(email, otp, purpose)

        if verified_user and verified_user.id == user.id:
            cache.delete(attempts_key)

            # ── SIGNUP VERIFICATION ──────────────────────
            if purpose == 'signup':
                user.is_active = True
                user.save(update_fields=['is_active'])
                _clear_otp_session(request)
                login(request, user, backend=AUTH_BACKEND)
                logger.info("Signup OTP verified | user=%s", user.username)

                messages.success(request, "Email verified! Welcome aboard.")
                msg = (
                    f'🎉 Welcome {user.first_name or user.username}! '
                    f'Your account is verified.'
                )
                if htmx:
                    response = HttpResponse()
                    response['HX-Redirect'] = reverse('customer:customer_dashboard')
                    response['HX-Trigger'] = json.dumps({
                        'showToast': {
                            'level': 'success', 'message': msg,
                            'title': 'Registration Complete',
                        },
                    })
                    return response
                return redirect('customer:customer_dashboard')

            # ── PASSWORD RESET VERIFICATION ──────────────
            elif purpose == 'reset_password':
                # Clear signup keys first, THEN set reset keys (order matters)
                _clear_otp_session(request)
                request.session[SK_RESET_USER_ID] = user.id
                request.session[SK_RESET_EMAIL] = email

                if htmx:
                    response = HttpResponse()
                    response['HX-Redirect'] = reverse('accounting:reset_password_set')
                    return response
                return redirect('accounting:reset_password_set')

            # ── UNKNOWN PURPOSE (defensive) ──────────────
            else:
                logger.error(
                    "verify_otp_view called with unknown purpose=%s", purpose,
                )
                _clear_otp_session(request)
                messages.error(request, "Invalid OTP purpose. Please try again.")
                return redirect('home')

        # ---- Wrong/expired OTP ----
        try:
            cache.incr(attempts_key)
        except ValueError:
            cache.set(attempts_key, 1, OTP_EXPIRY_MINUTES * 60)
        return _otp_error_response(
            request, htmx, ctx,
            'Invalid or expired OTP. Please try again.',
        )

    # GET
    return render(request, 'auth/verify_otp.html', ctx)


def _otp_error_response(request, htmx, ctx, message, status=400):
    if htmx:
        response = render(request, 'auth/verify_otp.html', ctx, status=status)
        response['HX-Trigger'] = json.dumps({
            'showToast': {'level': 'danger', 'message': message},
        })
        return response
    messages.error(request, message)
    return render(request, 'auth/verify_otp.html', ctx)


@handle_errors(default_redirect='home')
def resend_otp_view(request):
    """Resend OTP with a 60-second cooldown per (email, purpose)."""
    user_id = request.session.get(SK_PENDING_USER_ID)
    email = request.session.get(SK_PENDING_EMAIL)
    purpose = request.session.get(SK_OTP_PURPOSE)

    if not user_id or not email or not purpose:
        messages.error(request, "Session expired. Please start again.")
        return redirect('home')

    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        _clear_otp_session(request)
        messages.error(request, "User not found.")
        return redirect('home')

    htmx = is_htmx(request)

    # ── Cooldown check ────────────────────────────────
    last_otp = (
        EmailOTP.objects
        .filter(email=email, purpose=purpose)
        .order_by('-created_at')
        .first()
    )
    if last_otp:
        elapsed = (timezone.now() - last_otp.created_at).total_seconds()
        if elapsed < OTP_RESEND_COOLDOWN_SECONDS:
            wait = OTP_RESEND_COOLDOWN_SECONDS - int(elapsed)
            msg = f"Please wait {wait} seconds before requesting a new OTP."
            if htmx:
                response = HttpResponse()
                response['HX-Trigger'] = json.dumps({
                    'showToast': {'level': 'warning', 'message': msg},
                })
                response['HX-Redirect'] = reverse('accounting:verify_otp')
                return response
            messages.warning(request, msg)
            return redirect('accounting:verify_otp')

    # ── Send fresh OTP ────────────────────────────────
    try:
        success = create_and_send_otp(user, email, purpose)
    except Exception:
        logger.exception("Resend OTP failed")
        success = False

    if success:
        msg = "A new OTP has been sent to your email."
        messages.success(request, msg)
        if htmx:
            response = HttpResponse()
            response['HX-Trigger'] = json.dumps({
                'showToast': {
                    'level': 'success', 'message': msg, 'title': 'OTP Resent',
                },
            })
            response['HX-Redirect'] = reverse('accounting:verify_otp')
            return response
    else:
        msg = "Failed to send OTP. Please try again later."
        messages.error(request, msg)
        if htmx:
            response = HttpResponse()
            response['HX-Trigger'] = json.dumps({
                'showToast': {'level': 'danger', 'message': msg},
            })
            response['HX-Redirect'] = reverse('accounting:verify_otp')
            return response

    return redirect('accounting:verify_otp')


# ════════════════════════════════════════════════════════════
# 8. PASSWORD RESET via OTP
# ════════════════════════════════════════════════════════════
@handle_errors(default_redirect='home')
def password_reset_otp_request(request):
    """Step 1: user submits email -> OTP sent."""
    if request.method == 'POST':
        email = (request.POST.get('email') or '').strip().lower()
        if not email:
            messages.error(request, "Please enter your email address.")
            return render(request, 'auth/password_reset_otp.html')

        # Lookup — same message whether user exists or not (privacy)
        users = User.objects.filter(email__iexact=email)
        if users.count() != 1:
            messages.info(
                request,
                "If an account with this email exists, we've sent an OTP.",
            )
            return render(request, 'auth/password_reset_otp.html')

        user = users.first()

        try:
            success = create_and_send_otp(user, email, 'reset_password')
        except Exception:
            logger.exception("Password reset OTP send failed")
            success = False

        if success:
            request.session[SK_PENDING_USER_ID] = user.id
            request.session[SK_PENDING_EMAIL] = email
            request.session[SK_OTP_PURPOSE] = 'reset_password'

            msg = "OTP sent to your email. Please verify."
            if is_htmx(request):
                response = HttpResponse()
                response['HX-Redirect'] = reverse('accounting:verify_otp')
                response['HX-Trigger'] = json.dumps({
                    'showToast': {'level': 'success', 'message': msg},
                })
                return response

            messages.success(request, msg)
            return redirect('accounting:verify_otp')

        messages.error(request, "Unable to send OTP. Please try again later.")
        return render(request, 'auth/password_reset_otp.html')

    return render(request, 'auth/password_reset_otp.html')


@handle_errors(default_redirect='home')
def reset_password_set_view(request):
    """Step 3: set new password after OTP verified. HTMX-aware."""
    user_id = request.session.get(SK_RESET_USER_ID)
    email = request.session.get(SK_RESET_EMAIL)

    if not user_id or not email:
        messages.error(request, "Invalid session. Please request reset again.")
        return redirect('home')

    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        _clear_otp_session(request)
        messages.error(request, "User not found.")
        return redirect('home')

    ctx = {'email': email}
    htmx = is_htmx(request)

    if request.method == 'POST':
        p1 = request.POST.get('password1')
        p2 = request.POST.get('password2')

        # ---- Validation errors ----
        error_msg = None
        if not p1 or not p2:
            error_msg = "Both password fields are required."
        elif p1 != p2:
            error_msg = "Passwords do not match."
        else:
            try:
                validate_password(p1, user=user)
            except ValidationError as e:
                error_msg = ' '.join(e.messages)

        if error_msg:
            if htmx:
                response = render(
                    request, 'auth/reset_password_set.html', ctx, status=400,
                )
                response['HX-Trigger'] = json.dumps({
                    'showToast': {'level': 'danger', 'message': error_msg},
                })
                return response
            messages.error(request, error_msg)
            return render(request, 'auth/reset_password_set.html', ctx)

        # ---- SUCCESS ----
        user.set_password(p1)
        user.save(update_fields=['password'])
        logger.info("Password reset completed | user=%s", user.username)

        _clear_otp_session(request)
        success_msg = "Password reset successfully! Please login with your new password."

        if htmx:
            response = HttpResponse()
            response['HX-Redirect'] = reverse('accounting:login')
            response['HX-Trigger'] = json.dumps({
                'showToast': {
                    'level': 'success',
                    'message': success_msg,
                    'title': 'Password Changed',
                },
            })
            return response

        messages.success(request, success_msg)
        return redirect('accounting:login')

    return render(request, 'auth/reset_password_set.html', ctx)


# ════════════════════════════════════════════════════════════
# 9. ALLAUTH CUSTOM SIGNUP VIEW (kept for backward compat)
# ════════════════════════════════════════════════════════════
class CustomSignupView(SignupView):
    """
    Allauth signup override — routes through our OTP flow.
    Kept for backward compatibility; the standard /register/ uses
    register_view above.
    """
    form_class = CustomerRegistrationForm
    success_url = reverse_lazy('accounting:verify_otp')

    def form_valid(self, form):
        user = form.save(commit=False)
        user.is_active = False
        user.save()

        Contact.objects.create(
            user=user,
            name=form.cleaned_data.get('full_name', user.username),
            email=form.cleaned_data.get('email', ''),
            phone=form.cleaned_data.get('phone', ''),
            contact_type='customer',
        )

        try:
            create_and_send_otp(user, user.email, 'signup')
        except Exception:
            logger.exception("OTP send failed in CustomSignupView")

        self.request.session[SK_PENDING_USER_ID] = user.id
        self.request.session[SK_PENDING_EMAIL] = user.email
        self.request.session[SK_OTP_PURPOSE] = 'signup'

        return redirect(self.success_url)