# accounting/templatetags/cloudinary_filters.py
import re
from django import template

register = template.Library()

@register.filter
def fix_cloudinary_url(value):
    """
    Fix malformed Cloudinary URLs like 'https:/...' to 'https://...'
    Also ensures 'http://' is correct.
    """
    if not value:
        return value
    # Replace 'https:/' (single slash) with 'https://' (double)
    value = re.sub(r'^https:/', 'https://', value)
    value = re.sub(r'^http:/', 'http://', value)
    return value


