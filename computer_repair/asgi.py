import os
from django.core.asgi import get_asgi_application
from whitenoise import WhiteNoise

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'computer_repair.settings')

# Django ASGI Application Load करें
# Note: SSE/eventstream is deliberately not routed here — production runs
# WSGI (gunicorn) and notifications use HTMX polling (see notification_helpers).
django_asgi_app = get_asgi_application()

# ✅ Whitenoise को ASGI के साथ Wrap करें (Static/Media Files के लिए)
application = WhiteNoise(django_asgi_app)
