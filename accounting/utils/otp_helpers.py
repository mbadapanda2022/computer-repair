import random
import logging
from django.utils import timezone
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.conf import settings
from django.contrib.auth.models import User
from ..models import EmailOTP

logger = logging.getLogger(__name__)

def generate_otp():
    """Generate a 6-digit secure OTP."""
    return f"{random.randint(100000, 999999)}"

def send_otp_email(user, email, otp, purpose):
    """Send OTP via HTML email."""
    purpose_display = "Signup Verification" if purpose == 'signup' else "Password Reset"
    subject = f"Your OTP for {purpose_display}"
    context = {
        'user': user,
        'email': email,
        'otp': otp,
        'purpose': purpose_display,
        'expiry_minutes': 10,
    }
    try:
        html_message = render_to_string('auth/email_otp.html', context)
        plain_message = f"Your OTP for {purpose_display} is: {otp}. It expires in 10 minutes."
        
        send_mail(
            subject,
            plain_message,
            settings.DEFAULT_FROM_EMAIL,
            [email],
            html_message=html_message,
            fail_silently=False,
        )
        logger.info(f"OTP sent to {email} for {purpose}")
        return True
    except Exception as e:
        logger.error(f"Failed to send OTP to {email}: {e}")
        return False

def create_and_send_otp(user, email, purpose):
    """
    Create OTP record, delete old unused ones, and send email.
    Returns True if sent successfully, else False.
    """
    # 1. Cleanup old expired/unused OTPs for this email & purpose
    EmailOTP.objects.filter(
        email=email,
        purpose=purpose,
        is_used=False,
        expires_at__lt=timezone.now()
    ).delete()
    
    # 2. Generate new OTP
    otp = generate_otp()
    expires_at = timezone.now() + timezone.timedelta(minutes=10)
    
    # 3. Save to DB
    otp_record = EmailOTP.objects.create(
        user=user,
        email=email,
        otp=otp,
        purpose=purpose,
        expires_at=expires_at,
    )
    
    # 4. Send email
    sent = send_otp_email(user, email, otp, purpose)
    if not sent:
        # Rollback OTP record if email fails
        otp_record.delete()
        return False
    return True

def verify_otp(email, otp, purpose):
    """
    Verify OTP. Returns User object if valid, else None.
    Marks OTP as used upon successful verification.
    """
    try:
        otp_record = EmailOTP.objects.get(
            email=email,
            otp=otp,
            purpose=purpose,
            is_used=False,
            expires_at__gte=timezone.now()
        )
        # Mark as used
        otp_record.is_used = True
        otp_record.save()
        return otp_record.user
    except EmailOTP.DoesNotExist:
        return None

# dummy change
