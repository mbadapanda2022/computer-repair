# accounting/context_processors.py

from .models import CompanyProfile

def company_profile(request):
    return {'company': CompanyProfile.get_instance()}

