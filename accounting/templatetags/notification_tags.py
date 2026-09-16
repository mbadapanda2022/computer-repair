# accounting/templatetags/notification_tags.py
import logging
from django import template

register = template.Library()
logger = logging.getLogger(__name__)


@register.filter
def unread_count(user):
    """Return unread notification count, or empty string if 0 (badge hide)."""
    if not user or not user.is_authenticated:
        return 0
    try:
        return user.notifications.filter(is_read=False).count()
    except Exception as e:
        logger.warning(f"unread_count failed for user {user.pk}: {e}")
        return 0