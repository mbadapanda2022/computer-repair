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


@register.filter
def cloudinary_fill(value, size='880x600'):
    """
    Inject a smart-crop transform into a Cloudinary delivery URL so cards
    get one consistent aspect ratio at an optimized size (q_auto/f_auto).
    Non-Cloudinary URLs are returned unchanged.
    """
    if not value:
        return value
    s = str(value)
    m = re.match(r'^(https?://res\.cloudinary\.com/[^/]+/image/upload/)(.+)$', s)
    if not m:
        return s
    try:
        w, h = size.lower().split('x', 1)
        w, h = w.strip(), h.strip()
    except ValueError:
        w, h = '880', '600'
    return (m.group(1)
            + 'c_fill,g_auto,w_%s,h_%s,q_auto,f_auto/' % (w, h)
            + m.group(2))


