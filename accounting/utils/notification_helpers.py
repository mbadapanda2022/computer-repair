# accounting/utils/notification_helpers.py
import logging
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.conf import settings
from django.template.loader import render_to_string
from django_eventstream import send_event
from ..models import Notification

logger = logging.getLogger(__name__)

def send_notification(user, title, message, link=None, notif_type='info', category='general', send_email=False):
    """
    🛡️ PRODUCTION-READY: Sends notification with FULL error handling.
    Agar Redis/SSE fail ho jaye, toh bhi User ko koi Error nahi dikhegi.
    """
    if not user or not user.is_authenticated:
        logger.warning(f"Attempted to send notification to unauthenticated user: {user}")
        return None

    try:
        # 1. Database mein Notification Save karein
        notif = Notification.objects.create(
            recipient=user,
            title=title,
            message=message,
            link=link,
            notification_type=notif_type,
            category=category
        )
        
        # 2. SSE Updates (Badge, Refresh, Toast) – कोशिश करें, अगर Fail हो तो सिर्फ Log करें
        try:
            stream = f'notif-{user.id}'
            count = user.notifications.filter(is_read=False).count()
            
            send_event(stream, 'badge', {'count': count})
            send_event(stream, 'refresh', {})
            send_event(stream, 'toast', {
                'title': title,
                'message': message,
                'type': notif_type,  # success, warning, error, info
                'link': link
            })
            logger.debug(f"SSE sent successfully to user {user.id} on stream {stream}")
        except Exception as sse_error:
            # ✅ यहाँ SSE Fail होने पर भी App Crash नहीं होगा!
            logger.error(f"🔥 SSE/Redis failed for user {user.id}: {sse_error}", exc_info=True)

        # 3. Email भेजें (अगर Option On है)
        if send_email and user.email:
            try:
                send_notification_email(user, title, message, link)
            except Exception as email_error:
                logger.error(f"🔥 Email failed for user {user.id}: {email_error}", exc_info=True)

        return notif

    except Exception as db_error:
        # अगर DB में Notification Save भी Fail हो जाए (बहुत ही कम संभावना)
        logger.error(f"🔥 Critical: Notification DB save failed for user {user.id}: {db_error}", exc_info=True)
        return None


def send_notification_to_staff(title, message, link=None, notif_type='info', category='general', send_email=False):
    """Send notification to ALL staff users."""
    for user in User.objects.filter(is_staff=True):
        send_notification(user, title, message, link, notif_type, category, send_email)


def send_notification_to_customer(customer_contact, title, message, link=None, notif_type='info', category='general', send_email=False):
    """Send notification to a specific customer (using their User account)."""
    if customer_contact and customer_contact.user:
        send_notification(customer_contact.user, title, message, link, notif_type, category, send_email)


def send_notification_sse(user):
    """
    🛡️ SAFE SSE Update: Only sends 'badge' and 'refresh' to update the UI.
    Agar fail ho, toh sirf Log karein.
    """
    if not user or not user.is_authenticated:
        return
    try:
        stream = f'notif-{user.id}'
        count = user.notifications.filter(is_read=False).count()
        send_event(stream, 'badge', {'count': count})
        send_event(stream, 'refresh', {})
    except Exception as e:
        # ✅ Silent Fail – User को कोई Error नहीं दिखेगी
        logger.error(f"🔥 SSE refresh failed for user {user.id}: {e}", exc_info=True)


def send_notification_email(user, title, message, link):
    """Email sending with its own internal error handling."""
    try:
        subject = f"[A1 Computer] {title}"
        context = {'user': user, 'title': title, 'message': message, 'link': link}
        html_message = render_to_string('notifications/email_notification.html', context)
        send_mail(
            subject, 
            message, 
            settings.DEFAULT_FROM_EMAIL, 
            [user.email], 
            html_message=html_message,
            fail_silently=True  # Django's built-in safety
        )
    except Exception as e:
        logger.error(f"🔥 Email send failed for {user.email}: {e}", exc_info=True)
        
        
