# accounting/decorators.py
import json
import logging
from functools import wraps
from django.shortcuts import render, redirect
from django.contrib import messages
from django.http import HttpResponse
from django.conf import settings
from .views.utils import is_htmx

logger = logging.getLogger(__name__)


def handle_errors(default_redirect=None, htmx_template=None, fallback_form=None):
    """
    Production-ready error handler decorator.
    - SECURE: Never exposes tracebacks to clients in production.
    - SAFE: Prevents infinite redirect loops.
    - UTILIZES: fallback_form if provided.
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            try:
                return view_func(request, *args, **kwargs)
            except Exception as e:
                # 1. Log the full error with traceback (server side only)
                logger.exception(f"Error in {view_func.__name__}: {e}")

                # 2. Sanitize error for client (Never show raw str(e) in production!)
                if settings.DEBUG:
                    user_error_msg = str(e)
                else:
                    # In production, show generic message to avoid data leakage
                    user_error_msg = "An unexpected error occurred. Please try again later."

                # 3. Determine the error status code (default to 500)
                status_code = 500
                if hasattr(e, 'status_code'):
                    status_code = e.status_code
                elif isinstance(e, (ValueError, TypeError, KeyError)):
                    status_code = 400  # Client-side bad input

                # 4. Handle HTMX requests
                if is_htmx(request):
                    context = {'error': user_error_msg}
                    
                    # If fallback_form is provided, instantiate it to re-render form with errors
                    if fallback_form:
                        context['form'] = fallback_form(request.POST or None)
                    
                    if htmx_template:
                        return render(request, htmx_template, context, status=status_code)
                    
                    # Default HTMX response (Toast notification)
                    response = HttpResponse(
                        f'<div class="alert alert-danger alert-dismissible fade show" role="alert">'
                        f'  {user_error_msg}'
                        f'  <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>'
                        f'</div>',
                        status=status_code
                    )
                    response['HX-Trigger'] = json.dumps({
                        'showToast': {
                            'level': 'danger',
                            'message': user_error_msg
                        }
                    })
                    return response

                # 5. Handle Non-HTMX (standard) requests
                messages.error(request, user_error_msg)
                
                # 6. Safe redirect (Prevents infinite loops)
                redirect_to = default_redirect or 'home'
                referer = request.META.get('HTTP_REFERER')
                if referer:
                    # Ensure the referer is from our own domain to prevent open redirects
                    try:
                        from urllib.parse import urlparse
                        parsed_referer = urlparse(referer)
                        parsed_host = urlparse(request.build_absolute_uri('/'))
                        if parsed_referer.netloc == parsed_host.netloc:
                            redirect_to = referer
                    except Exception:
                        pass
                
                return redirect(redirect_to)
        return wrapper
    return decorator

