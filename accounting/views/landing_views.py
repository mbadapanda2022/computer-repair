# accounting/views/landing_views.py
import json
import logging
from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse
from django.forms import modelform_factory
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods
from django.contrib.admin.views.decorators import staff_member_required
from django.urls import reverse
from django.contrib import messages

from ..models import *
from ..forms import ContactMessageForm, ServiceForm, TestimonialForm, FAQForm
from .utils import is_htmx, htmx_response
from ..decorators import handle_errors
from ..utils.notification_helpers import send_notification_to_staff
from ..google_reviews import maybe_background_refresh, sync_google_reviews, is_configured as google_reviews_configured


logger = logging.getLogger(__name__)


# ============================================================
# LANDING PAGE (HOME)
# ============================================================

def landing(request):
    """Public landing page with company info, services, testimonials, FAQs."""
    maybe_background_refresh()
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
        if request.POST.get('website', '').strip():
            logger.warning(f"🔥 Honeypot triggered from IP {request.META.get('REMOTE_ADDR')}")
            html = '<div class="alert alert-success">Thank you! Your message has been sent.</div>'
            response = HttpResponse(html)
            response['HX-Trigger'] = json.dumps({
                'showToast': {'level': 'success', 'message': 'Message sent successfully!'}
            })
            return response
        
        if form.is_valid():
            message = form.save()
            
            send_notification_to_staff(
                title=f"New Contact Message from {message.name}",
                message=f"Subject: {message.subject}\nMessage: {message.message[:100]}...",
                link=reverse('accounting:message_list'),
                notif_type='info',
                category='general',
                send_email=True  
            )
            
            html = '<div class="alert alert-success">Thank you! Your message has been sent.</div>'
            response = HttpResponse(html)
            response['HX-Trigger'] = json.dumps({
                'showToast': {'level': 'success', 'message': 'Message sent successfully!'}
            })
            return response
        else:
            html = render_to_string('landing/partials/_contact_form.html', {'form': form}, request=request)
            return HttpResponse(html, status=200)
    else:
        form = ContactMessageForm()
        return render(request, 'landing/partials/_contact_form.html', {'form': form})


# ============================================================
# REAL-TIME FIELD VALIDATION (HTMX) + HONEYPOT BYPASS
# ============================================================
ALLOWED_FIELDS = {'name', 'email', 'phone', 'subject', 'message'}
def validate_contact_field(request):
    field_name = request.GET.get('field')
    if field_name not in ALLOWED_FIELDS:
        return HttpResponse("", status=200)

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
# PRIVACY POLICY & COOKIE POLICY
# ============================================================

def privacy_policy(request):
    return render(request, 'landing/privacy_policy.html', {})

def cookie_policy(request):
    return render(request, 'landing/cookie_policy.html')


# ============================================================
# DEBUG: Cloudinary Info
# ============================================================
@staff_member_required
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


# ============================================================
# ============================================================
# LANDING PAGE MANAGEMENT (HTMX CRUD) – STAFF ONLY
# ============================================================
# ============================================================


# ---------- SERVICES ----------

@staff_member_required
def service_list_partial(request):
    """Return partial HTML for service list (for HTMX refresh)"""
    services = Service.objects.all().order_by('order', 'created_at')
    html = render_to_string('landing/partials/_service_list.html', {'services': services}, request=request)
    return HttpResponse(html)


@staff_member_required
def service_create(request):
    """Create a new service via HTMX modal"""
    if request.method == 'POST':
        form = ServiceForm(request.POST, request.FILES)
        if form.is_valid():
            service = form.save()
            if is_htmx(request):
                services = Service.objects.all().order_by('order', 'created_at')
                html = render_to_string('landing/partials/_service_list.html', {'services': services}, request=request)
                response = HttpResponse(html)
                response['HX-Trigger'] = json.dumps({
                    'showToast': {'level': 'success', 'message': f'Service "{service.title}" created successfully.'},
                    'closeModal': ''
                })
                return response
            messages.success(request, f'Service "{service.title}" created successfully.')
            return redirect('accounting:dashboard')
    else:
        form = ServiceForm()
    
    return render(request, 'landing/partials/_service_form.html', {'form': form, 'action': 'Add'})


@staff_member_required
def service_edit(request, pk):
    """Edit an existing service via HTMX modal"""
    service = get_object_or_404(Service, pk=pk)
    if request.method == 'POST':
        form = ServiceForm(request.POST, request.FILES, instance=service)
        if form.is_valid():
            form.save()
            if is_htmx(request):
                services = Service.objects.all().order_by('order', 'created_at')
                html = render_to_string('landing/partials/_service_list.html', {'services': services}, request=request)
                response = HttpResponse(html)
                response['HX-Trigger'] = json.dumps({
                    'showToast': {'level': 'success', 'message': f'Service "{service.title}" updated successfully.'},
                    'closeModal': ''
                })
                return response
            messages.success(request, f'Service "{service.title}" updated successfully.')
            return redirect('accounting:dashboard')
    else:
        form = ServiceForm(instance=service)
    
    return render(request, 'landing/partials/_service_form.html', {'form': form, 'action': 'Edit', 'service': service})


@staff_member_required
@require_http_methods(["DELETE"])
def service_delete(request, pk):
    """Delete a service via HTMX (with confirm)"""
    service = get_object_or_404(Service, pk=pk)
    title = service.title
    service.delete()
    if is_htmx(request):
        services = Service.objects.all().order_by('order', 'created_at')
        html = render_to_string('landing/partials/_service_list.html', {'services': services}, request=request)
        response = HttpResponse(html)
        response['HX-Trigger'] = json.dumps({
            'showToast': {'level': 'success', 'message': f'Service "{title}" deleted successfully.'}
        })
        return response
    messages.success(request, f'Service "{title}" deleted successfully.')
    return redirect('accounting:dashboard')


# ---------- TESTIMONIALS ----------

@staff_member_required
def testimonial_list_partial(request):
    """Return partial HTML for testimonial list"""
    testimonials = Testimonial.objects.all().order_by('order', '-created_at')
    html = render_to_string('landing/partials/_testimonial_list.html', {'testimonials': testimonials}, request=request)
    return HttpResponse(html)


@staff_member_required
def testimonial_create(request):
    """Create a new testimonial via HTMX modal"""
    if request.method == 'POST':
        form = TestimonialForm(request.POST, request.FILES)
        if form.is_valid():
            testimonial = form.save()
            if is_htmx(request):
                testimonials = Testimonial.objects.all().order_by('order', '-created_at')
                html = render_to_string('landing/partials/_testimonial_list.html', {'testimonials': testimonials}, request=request)
                response = HttpResponse(html)
                response['HX-Trigger'] = json.dumps({
                    'showToast': {'level': 'success', 'message': f'Testimonial from "{testimonial.customer_name}" created.'},
                    'closeModal': ''
                })
                return response
            messages.success(request, f'Testimonial from "{testimonial.customer_name}" created.')
            return redirect('accounting:dashboard')
    else:
        form = TestimonialForm()
    return render(request, 'landing/partials/_testimonial_form.html', {'form': form, 'action': 'Add'})


@staff_member_required
def testimonial_edit(request, pk):
    """Edit an existing testimonial via HTMX modal"""
    testimonial = get_object_or_404(Testimonial, pk=pk)
    if request.method == 'POST':
        form = TestimonialForm(request.POST, request.FILES, instance=testimonial)
        if form.is_valid():
            form.save()
            if is_htmx(request):
                testimonials = Testimonial.objects.all().order_by('order', '-created_at')
                html = render_to_string('landing/partials/_testimonial_list.html', {'testimonials': testimonials}, request=request)
                response = HttpResponse(html)
                response['HX-Trigger'] = json.dumps({
                    'showToast': {'level': 'success', 'message': 'Testimonial updated successfully.'},
                    'closeModal': ''
                })
                return response
            messages.success(request, 'Testimonial updated successfully.')
            return redirect('accounting:dashboard')
    else:
        form = TestimonialForm(instance=testimonial)
    return render(request, 'landing/partials/_testimonial_form.html', {'form': form, 'action': 'Edit', 'testimonial': testimonial})


@staff_member_required
@require_http_methods(["DELETE"])
def testimonial_delete(request, pk):
    """Delete a testimonial via HTMX"""
    testimonial = get_object_or_404(Testimonial, pk=pk)
    name = testimonial.customer_name
    testimonial.delete()
    if is_htmx(request):
        testimonials = Testimonial.objects.all().order_by('order', '-created_at')
        html = render_to_string('landing/partials/_testimonial_list.html', {'testimonials': testimonials}, request=request)
        response = HttpResponse(html)
        response['HX-Trigger'] = json.dumps({
            'showToast': {'level': 'success', 'message': f'Testimonial from "{name}" deleted.'}
        })
        return response
    messages.success(request, f'Testimonial from "{name}" deleted.')
    return redirect('accounting:dashboard')


@csrf_protect
@staff_member_required
@require_http_methods(["POST"])
def google_reviews_sync(request):
    """Manually trigger Google Business Profile reviews sync (HTMX button)."""
    if not google_reviews_configured():
        message, level = (
            'Google Places API not configured. Set GOOGLE_PLACES_API_KEY and '
            'GOOGLE_PLACE_ID in the environment.', 'warning'
        )
    else:
        stats = sync_google_reviews()
        if stats['error']:
            message, level = f"Sync failed: {stats['error']} (existing reviews kept)", 'danger'
        else:
            message, level = (
                f"Google reviews synced: {stats['created']} new, {stats['updated']} updated, "
                f"{stats['deactivated']} removed.", 'success'
            )
    if is_htmx(request):
        testimonials = Testimonial.objects.all().order_by('order', '-created_at')
        html = render_to_string('landing/partials/_testimonial_list.html', {'testimonials': testimonials}, request=request)
        response = HttpResponse(html)
        response['HX-Trigger'] = json.dumps({'showToast': {'level': level, 'message': message}})
        return response
    if level == 'success':
        messages.success(request, message)
    else:
        messages.error(request, message)
    return redirect('accounting:manage_testimonials')


# ---------- FAQS ----------

@staff_member_required
def faq_list_partial(request):
    """Return partial HTML for FAQ list"""
    faqs = FAQ.objects.all().order_by('order', 'created_at')
    html = render_to_string('landing/partials/_faq_list.html', {'faqs': faqs}, request=request)
    return HttpResponse(html)


@staff_member_required
def faq_create(request):
    """Create a new FAQ via HTMX modal"""
    if request.method == 'POST':
        form = FAQForm(request.POST)
        if form.is_valid():
            faq = form.save()
            if is_htmx(request):
                faqs = FAQ.objects.all().order_by('order', 'created_at')
                html = render_to_string('landing/partials/_faq_list.html', {'faqs': faqs}, request=request)
                response = HttpResponse(html)
                response['HX-Trigger'] = json.dumps({
                    'showToast': {'level': 'success', 'message': 'FAQ added successfully.'},
                    'closeModal': ''
                })
                return response
            messages.success(request, 'FAQ added successfully.')
            return redirect('accounting:dashboard')
    else:
        form = FAQForm()
    return render(request, 'landing/partials/_faq_form.html', {'form': form, 'action': 'Add'})


@staff_member_required
def faq_edit(request, pk):
    """Edit an existing FAQ via HTMX modal"""
    faq = get_object_or_404(FAQ, pk=pk)
    if request.method == 'POST':
        form = FAQForm(request.POST, instance=faq)
        if form.is_valid():
            form.save()
            if is_htmx(request):
                faqs = FAQ.objects.all().order_by('order', 'created_at')
                html = render_to_string('landing/partials/_faq_list.html', {'faqs': faqs}, request=request)
                response = HttpResponse(html)
                response['HX-Trigger'] = json.dumps({
                    'showToast': {'level': 'success', 'message': 'FAQ updated successfully.'},
                    'closeModal': ''
                })
                return response
            messages.success(request, 'FAQ updated successfully.')
            return redirect('accounting:dashboard')
    else:
        form = FAQForm(instance=faq)
    return render(request, 'landing/partials/_faq_form.html', {'form': form, 'action': 'Edit', 'faq': faq})


@staff_member_required
@require_http_methods(["DELETE"])
def faq_delete(request, pk):
    """Delete an FAQ via HTMX"""
    faq = get_object_or_404(FAQ, pk=pk)
    faq.delete()
    if is_htmx(request):
        faqs = FAQ.objects.all().order_by('order', 'created_at')
        html = render_to_string('landing/partials/_faq_list.html', {'faqs': faqs}, request=request)
        response = HttpResponse(html)
        response['HX-Trigger'] = json.dumps({
            'showToast': {'level': 'success', 'message': 'FAQ deleted successfully.'}
        })
        return response
    messages.success(request, 'FAQ deleted successfully.')
    return redirect('accounting:dashboard')


# ============================================================
# LANDING PAGE MANAGEMENT (STAFF ONLY – FULL PAGES)
# ============================================================

@staff_member_required
def manage_services(request):
    """Staff page to manage services (list + Add/Edit/Delete)."""
    services = Service.objects.all().order_by('order', 'created_at')
    context = {
        'services': services,
        'include_controls': True,  # Controls ko show karne ke liye
    }
    return render(request, 'landing/manage_services.html', context)


@staff_member_required
def manage_testimonials(request):
    """Staff page to manage testimonials."""
    testimonials = Testimonial.objects.all().order_by('order', '-created_at')
    context = {
        'testimonials': testimonials,
        'include_controls': True,
    }
    return render(request, 'landing/manage_testimonials.html', context)


@staff_member_required
def manage_faqs(request):
    """Staff page to manage FAQs."""
    faqs = FAQ.objects.all().order_by('order', 'created_at')
    context = {
        'faqs': faqs,
        'include_controls': True,
    }
    return render(request, 'landing/manage_faqs.html', context)