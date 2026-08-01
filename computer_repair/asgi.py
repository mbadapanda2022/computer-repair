import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from django.urls import path, re_path

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'computer_repair.settings')

# Django ASGI Application Load करें
django_asgi_app = get_asgi_application()

# ✅ EventStream को सही ASGI Wrapper के साथ Import करें
from django_eventstream import views as eventstream_views
from django_eventstream.channels import AsgiHandler

application = ProtocolTypeRouter({
    "http": URLRouter([
        # ✅ Django EventStream का सही ASGI Handler Use करें
        path("events/", AsgiHandler.as_view()(eventstream_views.events), name="events"),
        re_path(r"", django_asgi_app),
    ]),
})