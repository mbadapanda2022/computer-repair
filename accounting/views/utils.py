# accounting/views/utils.py
import json
import logging

from django.shortcuts import render, redirect
from django.http import HttpResponse, JsonResponse
from django.urls import reverse, NoReverseMatch
from django.db import connections
from django.db.utils import OperationalError

logger = logging.getLogger(__name__)


# ============================================================
# HTMX DETECTION
# ============================================================
def is_htmx(request):
    """
    Check if the request is an HTMX request.
    Returns True if the 'HX-Request' header is present and 'true'.
    """
    return request.headers.get('HX-Request', '').lower() == 'true'


# ============================================================
# NAMESPACE-BASED REDIRECTS (With Fallback)
# ============================================================
def redirect_to_staff(view_name, *args, **kwargs):
    """
    Redirect to a staff view (accounting namespace).
    Safe: Returns home if the URL name doesn't exist.
    """
    try:
        return redirect(f'accounting:{view_name}', *args, **kwargs)
    except NoReverseMatch as e:
        logger.error(f"Redirect to staff '{view_name}' failed: {e}. Falling back to home.")
        return redirect('home')


def redirect_to_customer(view_name, *args, **kwargs):
    """
    Redirect to a customer view (customer namespace).
    Safe: Returns home if the URL name doesn't exist.
    """
    try:
        return redirect(f'customer:{view_name}', *args, **kwargs)
    except NoReverseMatch as e:
        logger.error(f"Redirect to customer '{view_name}' failed: {e}. Falling back to home.")
        return redirect('home')


# ============================================================
# HTMX RESPONSE (With Merged Headers)
# ============================================================
def htmx_response(
    request,
    template,
    context=None,
    toast=None,
    redirect_url=None,
    close_modal=False,
    status=200,
    extra_headers=None
):
    """
    Standardized HTMX response with:
    - Toast notifications
    - Modal close
    - Redirect
    - Merged HX-Trigger headers (does not overwrite existing)
    """
    if context is None:
        context = {}
    
    response = render(request, template, context, status=status)

    # Build trigger data
    trigger_data = {}
    
    # Merge existing HX-Trigger if present
    existing_trigger = response.get('HX-Trigger')
    if existing_trigger:
        try:
            if isinstance(existing_trigger, str):
                existing_trigger = json.loads(existing_trigger)
            if isinstance(existing_trigger, dict):
                trigger_data.update(existing_trigger)
        except json.JSONDecodeError:
            pass

    # Add toast
    if toast:
        trigger_data['showToast'] = {
            'level': toast.get('level', 'info'),
            'message': toast.get('message', ''),
            'title': toast.get('title', 'Notification'),
            'link': toast.get('link', '')
        }
    
    # Add modal close
    if close_modal:
        trigger_data['closeModal'] = ''

    # Set HX-Trigger if we have data
    if trigger_data:
        response['HX-Trigger'] = json.dumps(trigger_data)

    # Handle redirect (HX-Redirect takes precedence)
    if redirect_url:
        response['HX-Redirect'] = redirect_url

    # Extra headers
    if extra_headers:
        for key, value in extra_headers.items():
            response[key] = value

    return response


# ============================================================
# TOAST-ONLY RESPONSE (No HTML, just a toast)
# ============================================================
def toast_only_response(toast, status=200):
    """
    Return an HTTP response that only triggers a toast notification,
    without swapping any content.

    IMPORTANT: `HX-Reswap: none` prevents the empty response from being
    swapped into the target. Without it, an empty response targeting
    `#mainModalContent` would replace the modal body with nothing,
    leaving the backdrop visible but the modal blank.
    """
    response = HttpResponse(status=status)
    response['HX-Reswap'] = 'none'
    response['HX-Trigger'] = json.dumps({
        'showToast': {
            'level': toast.get('level', 'info'),
            'message': toast.get('message', ''),
            'title': toast.get('title', 'Notification'),
            'link': toast.get('link', '')
        }
    })
    return response


# ============================================================
# HEALTH CHECK (For Render & Supabase Keep-Alive)
# ============================================================
def health_check(request):
    """
    Simple health check endpoint to keep both Render and Supabase awake.
    Returns JSON status of database connection.
    
    Usage: Add to urls.py -> path('health/', health_check, name='health_check')
    Then setup cron-job.org or similar to hit this URL every 10-15 minutes.
    """
    try:
        # Try to connect to the database to ensure Supabase is awake
        connections['default'].cursor()
        return JsonResponse({
            'status': 'ok',
            'database': 'connected',
            'server': 'active',
            'message': 'All systems operational.'
        }, status=200)
    except OperationalError as e:
        logger.error(f"Health check failed: {e}")
        # Public endpoint — DB error text (host/user/db fragments) never
        # jaana chahiye, detail sirf log me.
        return JsonResponse({
            'status': 'error',
            'database': 'disconnected',
            'server': 'active',
            'message': 'Database unavailable.'
        }, status=500)
    except Exception as e:
        logger.error(f"Unexpected health check error: {e}")
        return JsonResponse({
            'status': 'error',
            'database': 'unknown',
            'server': 'active',
            'message': 'Health check failed.'
        }, status=500)
        
        
# ============================================================
# HTMX FIELD-VALIDATION RESPONSE (escaped)
# ============================================================
from django.utils.html import escape


def htmx_field_error_response(field_name, errors=()):
    """
    HTMX field-validation partial. `field_name` request GET se aata hai —
    bina escape ka HTML me jodna reflected XSS deta hai, isliye hamesha
    yahi helper use karein.
    """
    field_name = escape(field_name)
    if errors:
        err_html = ''.join(
            f'<div><i class="bi bi-exclamation-circle me-1"></i>{escape(err)}</div>'
            for err in errors
        )
        html = f'<div id="field-{field_name}" class="invalid-feedback d-block">{err_html}</div>'
    else:
        html = f'<div id="field-{field_name}" class="invalid-feedback"></div>'
    return HttpResponse(html)