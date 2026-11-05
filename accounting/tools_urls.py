# accounting/tools_urls.py
"""Public Tech Tools URLs (namespace ``tools``, mounted at /tools/)."""
from django.urls import path
from .views import ic_finder, tools

app_name = 'tools'

urlpatterns = [
    path('', tools.tools_index, name='tools_index'),
    # IC Finder's own endpoints come first: they are more specific than the
    # per-tool slug route below, and a technician's search must never be
    # mistaken for a tool name.
    path('ic/search/', ic_finder.ic_search, name='ic_search'),
    path('ic/pin-match/', ic_finder.ic_pin_match, name='ic_pin_match'),
    path('ic/<int:pk>/pinout/', ic_finder.ic_pinout, name='ic_pinout'),
    path('<slug:slug>/', tools.tools_detail, name='tools_detail'),
]
