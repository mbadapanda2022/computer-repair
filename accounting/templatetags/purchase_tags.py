# accounting/templatetags/purchase_tags.py
"""
Template tags for customer portal purchase visibility.
"""
from django import template
from ..models import Purchase

register = template.Library()


@register.simple_tag
def has_purchases(user):
    """
    Return True if the logged-in customer user is linked to a Contact
    that has at least one purchase recorded (as vendor).

    Safe for staff / anonymous / users without a linked contact.
    Runs a single EXISTS query — negligible cost.
    """
    if not user or not user.is_authenticated or user.is_staff:
        return False

    try:
        contact = user.customer_contact
    except Exception:
        # RelatedObjectDoesNotExist (User has no linked Contact)
        return False

    if not contact or getattr(contact, 'is_deleted', False):
        return False

    return Purchase.objects.filter(vendor=contact).exists()