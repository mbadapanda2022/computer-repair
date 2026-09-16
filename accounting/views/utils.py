# accounting/views/utils.py
import json
import logging
from functools import wraps
from urllib.parse import urlparse

from django.shortcuts import render, redirect
from django.http import HttpResponse, JsonResponse
from django.urls import reverse, NoReverseMatch
from django.conf import settings
from django.contrib import messages
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
# SAFE REDIRECT (Prevents Infinite Loops & Open Redirects)
# ============================================================
def safe_redirect(default_url='home', fallback_url='home', request=None):
    """
    Returns a safe redirect URL.
    If request is provided, it validates HTTP_REFERER to prevent open redirects.
    """
    if not request:
        return default_url

    referer = request.META.get('HTTP_REFERER')
    if not referer:
        return default_url

    try:
        parsed_referer = urlparse(referer)
        parsed_host = urlparse(request.build_absolute_uri('/'))
        
        # Only allow redirects to our own domain
        if parsed_referer.netloc == parsed_host.netloc:
            # Prevent redirect loops: don't redirect to the same page
            current_path = request.path
            if parsed_referer.path != current_path:
                return referer
    except Exception as e:
        logger.warning(f"Error parsing referer: {e}")
    
    return default_url


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
# STANDARD TEMPLATE RENDER
# ============================================================
def render_partial(request, template, context=None):
    """
    Render a template (simple wrapper for render).
    """
    if context is None:
        context = {}
    return render(request, template, context)


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
# JSON RESPONSE (For APIs / Non-HTMX endpoints)
# ============================================================
def json_response(data, status=200, toast=None):
    """
    Return JSON response with optional toast.
    """
    response = JsonResponse(data, status=status)
    if toast:
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
# ERROR RESPONSE (JSON)
# ============================================================
def error_response(message, status=400, toast=None):
    """
    Return JSON error response with optional toast.
    """
    toast_data = toast or {'level': 'danger', 'message': message, 'title': 'Error'}
    response = JsonResponse({'error': message}, status=status)
    response['HX-Trigger'] = json.dumps({
        'showToast': {
            'level': toast_data.get('level', 'danger'),
            'message': toast_data.get('message', message),
            'title': toast_data.get('title', 'Error'),
            'link': toast_data.get('link', '')
        }
    })
    return response


# ============================================================
# SAFE REDIRECT WITH RESPONSE (For Views)
# ============================================================
def safe_redirect_response(request, default_url='home'):
    """
    Returns an HTTP redirect response to a safe URL.
    Prevents open redirect vulnerabilities.
    """
    redirect_url = safe_redirect(default_url, default_url, request)
    return redirect(redirect_url)


# ============================================================
# DECORATOR: HTMX REQUIRED
# ============================================================
def htmx_required(view_func):
    """
    Decorator to ensure the request is an HTMX request.
    Returns 400 error if not HTMX.
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not is_htmx(request):
            logger.warning(f"Non-HTMX request to HTMX-only view: {request.path}")
            return HttpResponse(
                "This view requires HTMX. Please use the website normally.",
                status=400
            )
        return view_func(request, *args, **kwargs)
    return wrapper


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
        return JsonResponse({
            'status': 'error',
            'database': 'disconnected',
            'server': 'active',
            'message': str(e)
        }, status=500)
    except Exception as e:
        logger.error(f"Unexpected health check error: {e}")
        return JsonResponse({
            'status': 'error',
            'database': 'unknown',
            'server': 'active',
            'message': str(e)
        }, status=500)