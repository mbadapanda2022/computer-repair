# accounting/tools_urls.py
"""Public Tech Tools URLs (namespace ``tools``, mounted at /tools/)."""
from django.urls import path
from .views import tools

app_name = 'tools'

urlpatterns = [
    path('', tools.tools_index, name='tools_index'),
    path('<slug:slug>/', tools.tools_detail, name='tools_detail'),
]
