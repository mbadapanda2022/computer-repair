# accounting/utils/notification_helpers.py
import logging
import re
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.conf import settings
from django.template.loader import render_to_string
from django.urls import resolve
from django.urls.exceptions import Resolver404
from django.contrib.contenttypes.models import ContentType
from django.apps import apps
from ..models import Notification

logger = logging.getLogger(__name__)
User = get_user_model()


def safe_send_event(stream, event_type, data):
    try:
        from django_eventstream import send_event
        send_event(stream, event_type, data)
    except Exception as e:
        logger.debug(f"SSE event failed (ignored): {e}")


def _extract_content_type_and_id(link):
    """
    Link se (content_type, object_id) nikalne ki koshish.
    Returns tuple (content_type or None, object_id or None).
    """
    content_type = None
    object_id = None

    if not link:
        return content_type, object_id

    # ---- 1. Django URL resolver se try karo ----
    try:
        resolved = resolve(link)
        if 'pk' in resolved.kwargs:
            pk = resolved.kwargs['pk']
            view_func = resolved.func
            if hasattr(view_func, 'view_class'):
                model = getattr(view_func.view_class, 'model', None)
                if model and hasattr(model, '_meta'):
                    content_type = ContentType.objects.get_for_model(model)
                    object_id = pk
    except (Resolver404, AttributeError, KeyError, ValueError):
        pass

    # ---- 2. Agar resolve se nahi mila -> regex fallback ----
    if not content_type:
        # NOTE: URLs plural/singular dono handle karte hain
        # Format: (regex_pattern, app_label, model_name)
        patterns = [
            (r'/repairs?/(\d+)/', 'accounting', 'repairjob'),
            (r'/sales/(\d+)/', 'accounting', 'invoice'),
            (r'/invoices?/(\d+)/', 'accounting', 'invoice'),
            (r'/purchases?/(\d+)/', 'accounting', 'purchase'),
            (r'/contacts?/(\d+)/', 'accounting', 'contact'),
            (r'/payments?/(\d+)/', 'accounting', 'payment'),
        ]
        for pattern, app_label, model_name in patterns:
            match = re.search(pattern, link)
            if match:
                pk = int(match.group(1))
                try:
                    model = apps.get_model(app_label, model_name)
                    content_type = ContentType.objects.get_for_model(model)
                    object_id = pk
                except LookupError:
                    pass
                break

    return content_type, object_id


def send_notification(user, title, message, link=None, notif_type='info',
                      category='general', send_email=False):
    if not user or not user.is_authenticated:
        return None

    content_type, object_id = _extract_content_type_and_id(link)

    try:
        notif = Notification.objects.create(
            recipient=user,
            title=title,
            message=message,
            link=link,
            notification_type=notif_type,
            category=category,
            content_type=content_type,
            object_id=object_id,
        )

        stream = f'notif-{user.id}'
        count = user.notifications.filter(is_read=False).count()
        safe_send_event(stream, 'badge', {'count': count})
        safe_send_event(stream, 'refresh', {})
        safe_send_event(stream, 'toast', {
            'title': title,
            'message': message,
            'type': notif_type,
            'link': link,
        })

        if send_email and user.email:
            try:
                subject = f"[A1 Computer] {title}"
                context = {
                    'user': user,
                    'title': title,
                    'message': message,
                    'link': link,
                }
                html_message = render_to_string(
                    'notifications/email_notification.html', context
                )
                send_mail(
                    subject,
                    message,
                    settings.DEFAULT_FROM_EMAIL,
                    [user.email],
                    html_message=html_message,
                    fail_silently=True,
                )
            except Exception as e:
                logger.error(f"Email failed for {user.email}: {e}")

        return notif

    except Exception as e:
        logger.error(f"Notification DB save failed for user {user.id}: {e}")
        return None


def send_notification_to_staff(title, message, link=None, notif_type='info',
                                category='general', send_email=False):
    for user in User.objects.filter(is_staff=True):
        send_notification(user, title, message, link, notif_type, category, send_email)


def send_notification_to_customer(customer_contact, title, message, link=None,
                                   notif_type='info', category='general',
                                   send_email=False):
    if customer_contact and customer_contact.user:
        send_notification(customer_contact.user, title, message, link,
                          notif_type, category, send_email)


def send_notification_sse(user):
    """Sirf badge count aur refresh event bhejo (DB me kuch save nahi)."""
    if not user or not user.is_authenticated:
        return
    try:
        stream = f'notif-{user.id}'
        count = user.notifications.filter(is_read=False).count()
        safe_send_event(stream, 'badge', {'count': count})
        safe_send_event(stream, 'refresh', {})
    except Exception as e:
        logger.debug(f"SSE refresh skipped: {e}")


# ============================================================
# Aliases for backward compatibility
# ============================================================
send_notification_to_contact = send_notification_to_customer
send_notification_to_all_staff = send_notification_to_staff