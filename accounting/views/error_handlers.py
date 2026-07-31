# accounting/views/error_handlers.py

import logging
from django.shortcuts import render
from django.http import JsonResponse, HttpResponse
from django.template import loader
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger(__name__)


def custom_404(request, exception):
    """
    Custom 404 page.
    - For HTMX requests, return a partial with error.
    - For normal requests, show full page.
    """
    if request.headers.get('HX-Request'):
        return render(request, 'errors/partials/404_htmx.html', status=404)
    return render(request, '404.html', status=404)


def custom_500(request):
    """
    Custom 500 page.
    """
    if request.headers.get('HX-Request'):
        return render(request, 'errors/partials/500_htmx.html', status=500)
    return render(request, '500.html', status=500)


def custom_403(request, exception):
    """
    Custom 403 page.
    """
    if request.headers.get('HX-Request'):
        return render(request, 'errors/partials/403_htmx.html', status=403)
    return render(request, '403.html', status=403)


def custom_400(request, exception):
    """
    Custom 400 page.
    """
    if request.headers.get('HX-Request'):
        return render(request, 'errors/partials/400_htmx.html', status=400)
    return render(request, '400.html', status=400)


@csrf_exempt
def csrf_failure(request, reason=""):
    """
    Custom CSRF failure view.
    """
    if request.headers.get('HX-Request'):
        return render(
            request,
            'errors/partials/403_htmx.html',
            {'reason': reason or 'CSRF verification failed. Please refresh and try again.'},
            status=403
        )
    return render(
        request,
        '403_csrf.html',
        {'reason': reason or 'CSRF verification failed. Please refresh and try again.'},
        status=403
    )