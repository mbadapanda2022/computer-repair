# accounting/views/error_handlers.py
import logging
from django.shortcuts import render
from django.http import JsonResponse, HttpResponse
from django.template import loader
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger(__name__)

def get_error_template(request, template_name):
    """
    Authenticated Users (Staff) → Root Template (with Sidebar)
       Unauthenticated Users → Public Template (without Sidebar)
    """
    if request.user.is_authenticated and request.user.is_staff:
        return template_name  # e.g., '500.html' → templates/500.html
    # Unauthenticated or non-staff users → errors/ folder
    base_name = template_name.split('.')[0]  # '500' from '500.html'
    return f'errors/{base_name}_public.html'


def custom_404(request, exception):
    if request.headers.get('HX-Request'):
        return render(request, 'errors/partials/404_htmx.html', status=404)
    template = get_error_template(request, '404.html')
    return render(request, template, status=404)


def custom_500(request):
    if request.headers.get('HX-Request'):
        return render(request, 'errors/partials/500_htmx.html', status=500)
    template = get_error_template(request, '500.html')
    return render(request, template, status=500)


def custom_403(request, exception):
    if request.headers.get('HX-Request'):
        return render(request, 'errors/partials/403_htmx.html', status=403)
    template = get_error_template(request, '403.html')
    return render(request, template, status=403)


def custom_400(request, exception):
    if request.headers.get('HX-Request'):
        return render(request, 'errors/partials/400_htmx.html', status=400)
    template = get_error_template(request, '400.html')
    return render(request, template, status=400)


@csrf_exempt
def csrf_failure(request, reason=""):
    if request.headers.get('HX-Request'):
        return render(
            request,
            'errors/partials/403_htmx.html',
            {'reason': reason or 'CSRF verification failed. Please refresh and try again.'},
            status=403
        )
    if request.user.is_authenticated and request.user.is_staff:
        return render(
            request,
            '403_csrf.html',
            {'reason': reason or 'CSRF verification failed. Please refresh and try again.'},
            status=403
        )
    return render(
        request,
        'errors/403_csrf_public.html',
        {'reason': reason or 'CSRF verification failed. Please refresh and try again.'},
        status=403
    )