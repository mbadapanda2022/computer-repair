import os
from channels.routing import ProtocolTypeRouter, URLRouter
from django.urls import path, re_path
from django.core.asgi import get_asgi_application

django_asgi_app = get_asgi_application()


from django_eventstream import views as eventstream_views

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'computer_repair.settings')

application = ProtocolTypeRouter({
    "http": URLRouter([
        path("events/", eventstream_views.events, name="events"),
        re_path(r"", django_asgi_app),
    ]),
})