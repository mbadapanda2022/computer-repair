# accounting/context_processors.py
import re
from .models import CompanyProfile

def fix_url(url):
    """Fix malformed Cloudinary URLs (https:/ -> https://)"""
    if not url:
        return url
    # Replace 'https:/' with 'https://' (single slash to double)
    url = re.sub(r'^https:/', 'https://', url)
    url = re.sub(r'^http:/', 'http://', url)  # safety
    return url

def company_profile(request):
    company = CompanyProfile.get_instance()
    
    logo_url = fix_url(company.logo.url) if company.logo else None
    hero_url = fix_url(company.hero_image.url) if company.hero_image else None
    og_image_url = fix_url(company.og_image.url) if company.og_image else None
    
    return {
        'company': company,
        'logo_url': logo_url,      
        'hero_url': hero_url,
        'og_image_url': og_image_url,
    }