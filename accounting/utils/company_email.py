# accounting/utils/company_email.py
"""Outgoing-mail sender resolution, handover-friendly.

Priority: env-configured DEFAULT_FROM_EMAIL (or EMAIL_HOST_USER) →
CompanyProfile.email entered in Staff Settings → settings fallback.
A new owner who fills Company Settings gets mail from their own address
without touching code.
"""

from django.conf import settings


def get_from_email():
    configured = getattr(settings, 'DEFAULT_FROM_EMAIL', '') or ''
    if configured:
        return configured
    from ..models import CompanyProfile
    cp = CompanyProfile.objects.first()
    if cp and cp.email:
        return cp.email
    return configured
