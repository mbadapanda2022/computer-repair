# accounting/middleware.py

import logging
from django.urls import resolve, Resolver404, reverse
from django.shortcuts import redirect
from django.contrib.auth.views import redirect_to_login
from django.conf import settings

logger = logging.getLogger(__name__)

class AccessControlMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

        # Public Paths – Login, Register, Password Reset 
        self.public_paths = {
            '/',
            '/home/',
            '/login/',             
            '/register/',           
            '/logout/',
            '/admin/',
            '/admin/login/',
            '/settings/',
            '/privacy-policy/',
            '/contact-message/',
            '/validate-contact-field/',
            '/password-reset/',
            '/password-reset/done/',
            '/password-reset/complete/',
            '/auth/validate-login-field/',
            '/auth/validate-register-field/',
            '/verify-otp/',          
            '/resend-otp/',          
            '/password-reset-otp/',  
            '/reset-password-set/', 
        }

        # Public Prefixes 
        self.public_prefixes = (
            '/password-reset/',
            '/auth/validate-',
            '/events/',
            '/api/',
            '/login/',             
            '/register/', 
            '/verify-otp/',       
            '/resend-otp/',         
            '/reset-password-set/',        
        )

    def __call__(self, request):
        # 1. Static/Media
        if request.path.startswith(settings.STATIC_URL) or request.path.startswith(settings.MEDIA_URL):
            return self.get_response(request)

        # 2. Django Admin Login
        if request.path.startswith('/admin/login/'):
            return self.get_response(request)

        # 3. Explicit Public Paths Check
        if request.path in self.public_paths:
            return self.get_response(request)

        # 4. Public Prefixes Check
        for prefix in self.public_prefixes:
            if request.path.startswith(prefix):
                return self.get_response(request)

        # 5. Resolve URL Namespace
        try:
            resolved = resolve(request.path)
            namespace = resolved.namespace
        except Resolver404:
            return self.get_response(request)

        # 6. Not Authenticated → Redirect to Login
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), login_url=settings.LOGIN_URL)

        # 7. Customer → Allowed
        if namespace == 'customer':
            return self.get_response(request)

        # 8. Staff (accounting namespace) → Only Staff
        if namespace == 'accounting':
            if request.user.is_staff:
                return self.get_response(request)
            try:
                return redirect('customer:customer_dashboard')
            except:
                return redirect('home')

        # 9. Django Admin → Only Staff
        if request.path.startswith('/admin/'):
            if request.user.is_staff:
                return self.get_response(request)
            try:
                return redirect('customer:customer_dashboard')
            except:
                return redirect('home')

        # 10. Any other path → Allow
        return self.get_response(request)
    
    
