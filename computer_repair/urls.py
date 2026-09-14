from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from accounting.views import landing_views

# Custom Error Handlers
handler404 = 'accounting.views.error_handlers.custom_404'
handler500 = 'accounting.views.error_handlers.custom_500'
handler403 = 'accounting.views.error_handlers.custom_403'
handler400 = 'accounting.views.error_handlers.custom_400'

urlpatterns = [
    # Admin
    path('admin/', admin.site.urls),
    path('accounts/', include('allauth.urls')),

    # Public Landing Pages
    path('', landing_views.landing, name='home'),
    path('contact-message/', landing_views.contact_message, name='contact_message'),
    path('validate-contact-field/', landing_views.validate_contact_field, name='validate_contact_field'),
    path('privacy-policy/', landing_views.privacy_policy, name='privacy_policy'),
    path('cookie-policy/', landing_views.cookie_policy, name='cookie_policy'),
    path('debug/', landing_views.debug_cloudinary, name='debug_cloudinary'),

    # Customer URLs (Dashboard, Profile, etc.)
    path('customer/', include(('accounting.customer_urls', 'customer'))),

    # Staff / Accounting URLs (Login, Register, Dashboard, etc.)
    path('', include(('accounting.urls', 'accounting'))),
    path('tracking/', include('accounting.tracking_urls')),
]

# Static & Media serving in Development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    
