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


def handle_errors(default_redirect=None, htmx_template=None):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            try:
                return view_func(request, *args, **kwargs)
            except Exception as e:
                logger.exception(f"Error in {view_func.__name__}: {e}")

                if settings.DEBUG:
                    user_error_msg = str(e)
                else:
                    user_error_msg = "An unexpected error occurred. Please try again later."

                status_code = 500
                if hasattr(e, 'status_code'):
                    status_code = e.status_code
                elif isinstance(e, (ValueError, TypeError, KeyError)):
                    status_code = 400

                if is_htmx(request):
                    context = {'error': user_error_msg}
                    if htmx_template:
                        return render(request, htmx_template, context, status=status_code)

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