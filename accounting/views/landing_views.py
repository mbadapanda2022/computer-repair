# accounting/views/landing_views.py
import io
import json
import logging

from django.core.cache import cache
from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse
from django.forms import modelform_factory
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods, require_GET
from django.contrib.admin.views.decorators import staff_member_required
from django.urls import reverse
from django.contrib import messages

from ..models import *
from ..forms import (BookRepairForm, ContactMessageForm, ServiceForm,
                     TestimonialForm, FAQForm)
from .utils import is_htmx
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
    latest_posts = (
        BlogPost.objects.published()
        .select_related('author', 'category')[:3]
    )

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
        'latest_posts': latest_posts,
        'meta': meta,
        'track_token': request.GET.get('token', ''),
        'track_err': request.GET.get('err', ''),
        'track_job': request.GET.get('job', ''),
        'track_mobile': request.GET.get('mobile', ''),
    }
    return render(request, 'landing/landing.html', context)


# ============================================================
# CONTACT MESSAGE SUBMISSION (HTMX) + HONEYPOT
# ============================================================

def _client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', 'unknown')


def _throttle(key_prefix, request, limit, window):
    """True if caller exceeded `limit` hits in `window` seconds (per IP)."""
    import hashlib
    ip_hash = hashlib.sha256(f'{_client_ip(request)}|{key_prefix}'.encode()).hexdigest()[:24]
    key = f'{key_prefix}_{ip_hash}'
    hits = cache.get(key, 0)
    if hits >= limit:
        return True
    cache.set(key, hits + 1, timeout=window)
    return False


CONTACT_SUCCESS_HTML = (
    '<div id="contactFormContainer" class="alert alert-success">'
    'Thank you! Your message has been sent.</div>'
)


@csrf_protect
@handle_errors(default_redirect='home')
def contact_message(request):
    """Handle contact form submission via HTMX with real-time staff notification."""
    if request.method == 'POST':
        # Spam/abuse throttle: max 3 messages per 10 minutes per IP.
        # (Each message also sends email to all staff, so this protects
        # both the DB queue and the SMTP relay.)
        if _throttle('contact_rl', request, limit=3, window=600):
            html = ('<div id="contactFormContainer" class="alert alert-warning">'
                    'Too many messages from your connection. Please try again '
                    'after 10 minutes, or call us directly.</div>')
            return HttpResponse(html, status=429)

        form = ContactMessageForm(request.POST)
        
        # ============================================================
        # HONEYPOT CHECK - Bot Detection (Silent Fail)
        # ============================================================
        if request.POST.get('website', '').strip():
            logger.warning(f"🔥 Honeypot triggered from IP {request.META.get('REMOTE_ADDR')}")
            response = HttpResponse(CONTACT_SUCCESS_HTML)
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
            
            html = CONTACT_SUCCESS_HTML
            response = HttpResponse(html)
            response['HX-Trigger'] = json.dumps({
                'showToast': {'level': 'success', 'message': 'Message sent successfully!'}
            })
            return response
        else:
            # Partial ka root element container-div ke andar render karo —
            # warna outerHTML swap container ka id mita deta hai aur
            # ek validation-error ke baad form ka target toot jata tha.
            inner = render_to_string('landing/partials/_contact_form.html', {'form': form}, request=request)
            html = f'<div id="contactFormContainer">{inner}</div>'
            return HttpResponse(html, status=200)
    else:
        form = ContactMessageForm()
        return render(request, 'landing/partials/_contact_form.html', {'form': form})


# ============================================================
# PUBLIC REPAIR BOOKING (/book-repair/)
# ============================================================

@csrf_protect
@handle_errors(default_redirect='home')
def book_repair(request):
    """Visitor books a repair → Contact (by phone) + pending RepairJob.
    Success page shows the auto job number and the login-free tracking
    link, and staff get a notification + email."""
    from ..utils.tracking import generate_tracking_url

    if request.method == 'POST':
        if _throttle('book_repair_rl', request, limit=5, window=3600):
            return render(request, 'landing/book_repair.html',
                          {'form': BookRepairForm(),
                           'throttled': True}, status=429)

        if request.POST.get('website', '').strip():
            # Bot trap: pretend success, save nothing.
            return render(request, 'landing/book_repair_thanks.html', {})

        form = BookRepairForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            contact = Contact.objects.filter(phone=cd['phone']).first()
            if contact is None:
                contact = Contact.objects.create(
                    name=cd['name'], phone=cd['phone'],
                    contact_type='customer')
            job = RepairJob.objects.create(
                customer=contact,
                device_model=cd['device_model'],
                serial_number=cd.get('serial_number') or '',
                issue_description=cd['issue_description'])
            send_notification_to_staff(
                title=f'New online repair booking: {job.job_number}',
                message=(f'{cd["name"]} ({cd["phone"]}) booked '
                         f'{cd["device_model"]}: '
                         f'{cd["issue_description"][:90]}'),
                link=reverse('accounting:repair_list'),
                notif_type='info', category='general', send_email=True)
            return render(request, 'landing/book_repair_thanks.html',
                          {'job': job,
                           'tracking_url': generate_tracking_url(job,
                                                                  request)})

        return render(request, 'landing/book_repair.html', {'form': form})

    return render(request, 'landing/book_repair.html',
                  {'form': BookRepairForm()})


# ============================================================
# REAL-TIME FIELD VALIDATION (HTMX) + HONEYPOT BYPASS
# ============================================================
ALLOWED_FIELDS = {'name', 'email', 'phone', 'subject', 'message'}
def validate_contact_field(request):
    if _throttle('contact_validate_rl', request, limit=60, window=60):
        return HttpResponse("", status=429)

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
    company = CompanyProfile.get_instance()
    meta = {
        'title': 'Privacy Policy | ' + (company.name or 'My Company'),
        'description': f'How {company.name or "My Company"} collects, uses and protects your data.',
    }
    return render(request, 'landing/privacy_policy.html', {'meta': meta})

def cookie_policy(request):
    company = CompanyProfile.get_instance()
    meta = {
        'title': 'Cookie Policy | ' + (company.name or 'My Company'),
        'description': f'Cookie usage policy for {request.get_host}.',
    }
    return render(request, 'landing/cookie_policy.html', {'meta': meta})


def terms_of_service(request):
    company = CompanyProfile.get_instance()
    meta = {
        'title': 'Terms of Service | ' + (company.name or 'My Company'),
        'description': f'Terms governing repair, sales and service work at {company.name or "My Company"}.',
    }
    return render(request, 'landing/terms_of_service.html', {'meta': meta})


def return_policy(request):
    company = CompanyProfile.get_instance()
    meta = {
        'title': 'Return & Refund Policy | ' + (company.name or 'My Company'),
        'description': 'Returns, replacements and refund process for products and repairs.',
    }
    return render(request, 'landing/return_policy.html', {'meta': meta})


# ============================================================
# SEO: robots.txt + dynamic sitemap.xml
# ============================================================

@require_GET
def robots_txt(request):
    base = request.build_absolute_uri('/').rstrip('/')
    lines = [
        'User-agent: *',
        'Allow: /$',
        'Allow: /blog/',
        'Allow: /privacy-policy/',
        'Allow: /cookie-policy/',
        'Allow: /terms-of-service/',
        'Allow: /return-policy/',
        'Disallow: /customer/',
        'Disallow: /accounts/',
        'Disallow: /admin/',
        'Disallow: /tracking/',
        'Disallow: /staff/',
        f'Sitemap: {base}/sitemap.xml',
    ]
    return HttpResponse('\n'.join(lines) + '\n', content_type='text/plain')


@require_GET
def sitemap_xml(request):
    """Dynamic sitemap: static public pages + published blog posts + categories."""
    from django.utils.xmlutils import SimplerXMLGenerator

    base = request.build_absolute_uri('/').rstrip('/')
    buf = io.StringIO()
    xml = SimplerXMLGenerator(buf, None)
    xml.startDocument()
    xml.startElement('urlset', {'xmlns': 'http://www.sitemaps.org/schemas/sitemap/0.9'})

    def add(url, changefreq, priority, lastmod=None):
        xml.startElement('url', {})
        xml.startElement('loc', {}); xml.characters(url); xml.endElement('loc')
        if lastmod:
            xml.startElement('lastmod', {})
            xml.characters(lastmod.strftime('%Y-%m-%d'))
            xml.endElement('lastmod')
        xml.startElement('changefreq', {}); xml.characters(changefreq); xml.endElement('changefreq')
        xml.startElement('priority', {}); xml.characters(priority); xml.endElement('priority')
        xml.endElement('url')

    add(base + '/', 'daily', '1.0')
    add(base + reverse('blog:post_list'), 'daily', '0.8')
    add(base + reverse('privacy_policy'), 'yearly', '0.3')
    add(base + reverse('cookie_policy'), 'yearly', '0.3')
    add(base + reverse('terms_of_service'), 'yearly', '0.3')
    add(base + reverse('return_policy'), 'yearly', '0.3')

    for post in (BlogPost.objects.published()
                 .only('slug', 'updated_at').order_by('-published_at')):
        add(base + reverse('blog:post_detail', args=[post.slug]),
            'weekly', '0.7', lastmod=post.updated_at)

    for cat in BlogCategory.objects.exclude(slug='').only('slug'):
        add(base + reverse('blog:category', args=[cat.slug]), 'weekly', '0.5')

    for tag in BlogTag.objects.exclude(slug='').only('slug'):
        add(base + reverse('blog:tag', args=[tag.slug]), 'weekly', '0.4')

    xml.endElement('urlset')
    xml.endDocument()
    return HttpResponse(buf.getvalue(), content_type='application/xml')


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
    html = render_to_string('landing/partials/_service_list.html', {'services': services, 'include_controls': True}, request=request)
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
                html = render_to_string('landing/partials/_service_list.html', {'services': services, 'include_controls': True}, request=request)
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
                html = render_to_string('landing/partials/_service_list.html', {'services': services, 'include_controls': True}, request=request)
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
        html = render_to_string('landing/partials/_service_list.html', {'services': services, 'include_controls': True}, request=request)
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
    html = render_to_string('landing/partials/_testimonial_list.html', {'testimonials': testimonials, 'include_controls': True}, request=request)
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
                html = render_to_string('landing/partials/_testimonial_list.html', {'testimonials': testimonials, 'include_controls': True}, request=request)
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
                html = render_to_string('landing/partials/_testimonial_list.html', {'testimonials': testimonials, 'include_controls': True}, request=request)
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
        html = render_to_string('landing/partials/_testimonial_list.html', {'testimonials': testimonials, 'include_controls': True}, request=request)
        response = HttpResponse(html)
        response['HX-Trigger'] = json.dumps({
            'showToast': {'level': 'success', 'message': f'Testimonial from "{name}" deleted.'}
        })
        return response
    messages.success(request, f'Testimonial from "{name}" deleted.')
    return redirect('accounting:dashboard')


# ---------- FAQS ----------

@staff_member_required
def faq_list_partial(request):
    """Return partial HTML for FAQ list"""
    faqs = FAQ.objects.all().order_by('order', 'created_at')
    html = render_to_string('landing/partials/_faq_list.html', {'faqs': faqs, 'include_controls': True}, request=request)
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
                html = render_to_string('landing/partials/_faq_list.html', {'faqs': faqs, 'include_controls': True}, request=request)
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
                html = render_to_string('landing/partials/_faq_list.html', {'faqs': faqs, 'include_controls': True}, request=request)
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
        html = render_to_string('landing/partials/_faq_list.html', {'faqs': faqs, 'include_controls': True}, request=request)
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