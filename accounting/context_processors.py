# accounting/context_processors.py
from .models import CompanyProfile

def company_profile(request):
    company = CompanyProfile.get_instance()
    
    logo_url = company.logo.url if company.logo else None
    hero_url = company.hero_image.url if company.hero_image else None
    og_image_url = company.og_image.url if company.og_image else None
    
    return {
        'company': company,
        'logo_url': logo_url,      
        'hero_url': hero_url,
        'og_image_url': og_image_url, 
    }
