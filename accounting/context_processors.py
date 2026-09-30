# accounting/context_processors.py
import re

from django.core.cache import cache

from .models import CompanyProfile


def fix_url(url):
    """Fix malformed Cloudinary URLs (https:/ -> https://)"""
    if not url:
        return url
    url = re.sub(r'^https:/', 'https://', url)
    url = re.sub(r'^http:/', 'http://', url)  # safety
    return url


def company_profile(request):
    """
    Expose CompanyProfile to all templates.

    Cached for 5 minutes to avoid a DB hit on every request.
    Cache is invalidated automatically when CompanyProfile is saved
    (see CompanyProfile.save override below if added later).
    """
    company = cache.get('company_profile_singleton')
    if company is None:
        company = CompanyProfile.get_instance()
        cache.set('company_profile_singleton', company, 300)  # 5 min

    logo_url = fix_url(company.logo.url) if company.logo else None
    hero_url = fix_url(company.hero_image.url) if company.hero_image else None
    og_image_url = fix_url(company.og_image.url) if company.og_image else None

    return {
        'company': company,
        'logo_url': logo_url,
        'hero_url': hero_url,
        'og_image_url': og_image_url,
    }