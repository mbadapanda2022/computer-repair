# accounting/views/landing_views.py

import json
import logging
from django.shortcuts import render, redirect
from django.http import HttpResponse
from django.forms import modelform_factory
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_protect
from django.urls import reverse

from ..models import *
from ..forms import ContactMessageForm
from .utils import is_htmx, htmx_response
from ..decorators import handle_errors
from ..utils.notification_helpers import send_notification_to_staff

logger = logging.getLogger(__name__)


# ============================================================
# LANDING PAGE (HOME)
# ============================================================

def landing(request):
    """Public landing page with company info, services, testimonials, FAQs."""
    company = CompanyProfile.get_instance()
    faqs = FAQ.objects.filter(is_active=True).order_by('order', 'created_at')
    testimonials = Testimonial.objects.filter(is_active=True).order_by('order', '-created_at')
    services = Service.objects.filter(is_active=True).order_by('order', 'created_at') 

    meta = {
        'title': company.meta_title or f"{company.name} – Laptop & Printer Repair",
        'description': company.meta_description or company.about_text or "Expert chip-level repair...",
        'keywords': company.meta_keywords or "laptop repair, printer repair, computer service...",
        'og_image': company.og_image.url if company.og_image else None,
        'og_url': request.build_absolute_uri('/'),
    }

    context = {
        'company': company,
        'faqs': faqs,
        'testimonials': testimonials,
        'services': services,   
        'meta': meta,
    }
    return render(request, 'landing/landing.html', context)


# ============================================================
# CONTACT MESSAGE SUBMISSION (HTMX) + HONEYPOT
# ============================================================

@csrf_protect
@handle_errors(default_redirect='home')
def contact_message(request):
    """Handle contact form submission via HTMX with real-time staff notification."""
    if request.method == 'POST':
        form = ContactMessageForm(request.POST)
        
        # ============================================================
        # HONEYPOT CHECK - Bot Detection (Silent Fail)
        # ============================================================
        # clean_website() already raises ValidationError if filled.
        # But we want to give fake success to bots.
        # Check if honeypot field is filled (even before form.is_valid)
        if request.POST.get('website', '').strip():
            logger.warning(f"🔥 Honeypot triggered from IP {request.META.get('REMOTE_ADDR')}")
            # Fake success response — bot ko confuse karo
            html = '<div class="alert alert-success">Thank you! Your message has been sent.</div>'
            response = HttpResponse(html)
            response['HX-Trigger'] = json.dumps({
                'showToast': {'level': 'success', 'message': 'Message sent successfully!'}
            })
            return response
        
        if form.is_valid():
            message = form.save()
            
            # Send Real-time Notification to staff
            send_notification_to_staff(
                title=f"New Contact Message from {message.name}",
                message=f"Subject: {message.subject}\nMessage: {message.message[:100]}...",
                link=reverse('accounting:message_list'),
                notif_type='info',
                category='general',
                send_email=True  
            )
            
            # Return success message and toast trigger
            html = '<div class="alert alert-success">Thank you! Your message has been sent.</div>'
            response = HttpResponse(html)
            response['HX-Trigger'] = json.dumps({
                'showToast': {'level': 'success', 'message': 'Message sent successfully!'}
            })
            return response
        else:
            # Return the form with errors
            html = render_to_string('landing/partials/_contact_form.html', {'form': form}, request=request)
            return HttpResponse(html, status=200)
    else:
        form = ContactMessageForm()
        return render(request, 'landing/partials/_contact_form.html', {'form': form})


# ============================================================
# REAL-TIME FIELD VALIDATION (HTMX) + HONEYPOT BYPASS
# ============================================================

def validate_contact_field(request):
    field_name = request.GET.get('field')
    if not field_name:
        return HttpResponse("Invalid field", status=400)

    # Honeypot field ko skip karo
    if field_name == 'website':
        return HttpResponse("")

    value = request.GET.get(field_name, '')
    ContactValidationForm = modelform_factory(ContactMessage, form=ContactMessageForm, fields=[field_name])
    form = ContactValidationForm(data={field_name: value})

    errors = form.errors.get(field_name, [])
    html = render_to_string('landing/partials/_field_errors.html', {
        'field': field_name,
        'errors': errors,
        'value': value
    }, request=request)
    return HttpResponse(html)


# ============================================================
# PRIVACY POLICY PAGE
# ============================================================

def privacy_policy(request):
    return render(request, 'landing/privacy_policy.html', {})

def cookie_policy(request):
    return render(request, 'landing/cookie_policy.html')


def debug_cloudinary(request):
    import cloudinary
    from accounting.models import CompanyProfile
    c = CompanyProfile.get_instance()
    data = {
        'cloud_name': cloudinary.config().cloud_name,
        'api_key': cloudinary.config().api_key,
        'logo_url': c.logo.url if c.logo else None,
        'hero_url': c.hero_image.url if c.hero_image else None,
    }
    return render(request, 'debug.html', {'data': data})


