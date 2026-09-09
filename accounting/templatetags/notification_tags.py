# accounting/templatetags/notification_tags.py

from django import template
from django.contrib.auth.models import User

register = template.Library()

@register.filter
def unread_count(user):
    if not user or not user.is_authenticated:
        return 0
    try:
        return user.notifications.filter(is_read=False).count()
    except Exception:
        return 0


