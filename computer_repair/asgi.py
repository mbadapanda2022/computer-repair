import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from django.urls import path, re_path

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'computer_repair.settings')

# Django ASGI Application Load करें
django_asgi_app = get_asgi_application()

# ✅ EventStream को ASGI Compatible तरीके से Import करें
from django_eventstream import views as eventstream_views

application = ProtocolTypeRouter({
    "http": URLRouter([
        # ✅ Django EventStream का Correct View Use करें
        path("events/", eventstream_views.events, name="events"),
        re_path(r"", django_asgi_app),
    ]),
})