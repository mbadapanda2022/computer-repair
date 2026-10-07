# accounting/decorators.py
import json
import logging
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render

from .views.utils import is_htmx

logger = logging.getLogger(__name__)


def handle_errors(default_redirect=None, htmx_template=None):
    """
    Wrap a view with centralized error handling.

    HTTP exceptions (Http404, PermissionDenied) are re-raised so Django's
    own handlers render proper 404/403 responses instead of 500s.
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            try:
                return view_func(request, *args, **kwargs)
            except (Http404, PermissionDenied):
                # Let Django produce the correct status page.
                raise
            except Exception as e:
                logger.exception("Error in %s: %s", view_func.__name__, e)

                # Raw exception details: staff-only in DEBUG, never customers
                # (schema/DB messages like "NOT NULL constraint failed" leak
                # internals and look unprofessional in the customer portal).
                if settings.DEBUG and request.user.is_staff:
                    user_error_msg = str(e)
                else:
                    user_error_msg = (
                        "An unexpected error occurred. Please try again later."
                    )

                status_code = 500
                if hasattr(e, 'status_code'):
                    status_code = e.status_code
                elif isinstance(e, (ValueError, TypeError, KeyError, ValidationError)):
                    status_code = 400

                if is_htmx(request):
                    # Attempt to render the provided template (form re-display).
                    if htmx_template:
                        try:
                            response = render(
                                request,
                                htmx_template,
                                {'error': user_error_msg},
                                status=status_code,
                            )
                            response['HX-Retarget'] = '#mainModalContent'
                            response['HX-Trigger'] = json.dumps({
                                'showToast': {
                                    'level': 'danger',
                                    'message': user_error_msg,
                                }
                            })
                            return response
                        except Exception:
                            logger.exception(
                                "handle_errors: htmx_template render failed"
                            )

                    # Generic fallback alert.
                    response = HttpResponse(
                        f'<div class="alert alert-danger alert-dismissible fade show" role="alert">'
                        f'  {user_error_msg}'
                        f'  <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>'
                        f'</div>',
                        status=status_code,
                    )
                    response['HX-Trigger'] = json.dumps({
                        'showToast': {
                            'level': 'danger',
                            'message': user_error_msg,
                        }
                    })
                    return response

                messages.error(request, user_error_msg)

                redirect_to = default_redirect or 'home'
                referer = request.META.get('HTTP_REFERER')
                if referer:
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