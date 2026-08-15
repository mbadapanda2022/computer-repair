# accoutings/auth_backends.py
import logging
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model
from django.db.models import Q

User = get_user_model()
logger = logging.getLogger(__name__)

# Masking Utility
def mask_username(username):
    if not username or len(username) <= 4:
        return "****"
    return f"{username[:2]}****{username[-2:]}"

class EmailOrPhoneBackend(ModelBackend):
    """
    Authenticate against email OR phone number (via Contact).
    Production-ready with safe logging.
    """
    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None or password is None:
            return None

        login_input = username.strip()
        user = None

        # 1. Try Email (case-insensitive)
        try:
            user = User.objects.get(email__iexact=login_input)
        except User.DoesNotExist:
            pass

        # 2. Try Username (fallback)
        if not user:
            try:
                user = User.objects.get(username=login_input)
            except User.DoesNotExist:
                pass

        # 3. Try Phone (via Contact) - सुरक्षित normalization
        if not user:
            try:
                from .models import Contact
                phone_clean = ''.join(filter(str.isdigit, login_input))
                # अगर 10 digits से कम है तो कोशिश न करें
                if len(phone_clean) >= 10:
                    # DB में हमेशा last 10 digits स्टोर होते हैं (हमने save() में यह सुनिश्चित किया है)
                    normalized_phone = phone_clean[-10:]
                    contact = Contact.objects.get(phone=normalized_phone)
                    user = contact.user
            except (Contact.DoesNotExist, AttributeError):
                pass

        # Authentication check
        if user and user.check_password(password) and self.user_can_authenticate(user):
            # सिर्फ सफल लॉगिन पर ही Full username Log करें (जो की सुरक्षित है)
            logger.info(f"User {user.username} authenticated successfully from IP {request.META.get('REMOTE_ADDR')}")
            return user

        # ❌ असफल लॉगिन पर Masked username + IP Log करें
        logger.warning(f"Failed login attempt from IP {request.META.get('REMOTE_ADDR')} for user: {mask_username(login_input)}")
        return None
    
    
