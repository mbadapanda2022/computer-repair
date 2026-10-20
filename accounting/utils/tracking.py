# accounting/utils/tracking.py
"""
Repair tracking via signed URLs — no DB storage needed.
Token contains only job_id; expires automatically via max_age.
"""
from django.core import signing
from django.urls import reverse


TRACK_SALT = 'repair-tracking-v1'
TRACK_MAX_AGE = 90 * 24 * 60 * 60  # 90 days in seconds


def generate_tracking_token(repair):
    """Create a signed token embedding the repair's pk. No DB write.

    Colons are replaced with dots: WhatsApp's linkifier truncates URLs at
    ':' so a token like "eyJ...:1xJ9:g4mN" becomes a dead 404 link for the
    customer. Base64url/hex payload chars never contain '.'.
    """
    return signing.dumps(
        {'job_id': repair.pk},
        salt=TRACK_SALT,
        compress=True,
    ).replace(':', '.')


def generate_tracking_url(repair, request=None):
    """Build absolute tracking URL (or relative if request is None)."""
    token = generate_tracking_token(repair)
    path = reverse('tracking:repair_track', args=[token])
    if request:
        return request.build_absolute_uri(path)
    return path


def verify_tracking_token(token):
    """Decode + verify. Returns job_id or None.

    Accepts both the WhatsApp-safe dot-separated tokens and legacy
    colon-separated tokens that were already sent to customers.
    """
    try:
        data = signing.loads(
            token.replace('.', ':'), salt=TRACK_SALT, max_age=TRACK_MAX_AGE)
        return data.get('job_id')
    except (signing.SignatureExpired, signing.BadSignature):
        return None