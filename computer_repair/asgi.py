import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from django.urls import path, re_path
from whitenoise import WhiteNoise

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'computer_repair.settings')

# Django ASGI Application Load करें
django_asgi_app = get_asgi_application()

# ✅ Whitenoise को ASGI के साथ Wrap करें (Static/Media Files के लिए)
application = WhiteNoise(django_asgi_app)

# ✅ EventStream Routing (यह Whitenoise को Override नहीं करेगा)
from django_eventstream import views as eventstream_views

application = ProtocolTypeRouter({
    "http": URLRouter([
        path("events/", eventstream_views.events, name="events"),
        re_path(r"", application),
    ]),
})
