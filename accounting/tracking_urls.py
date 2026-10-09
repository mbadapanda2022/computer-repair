# accounting/tracking_urls.py
from django.urls import path
from .views import tracking


app_name = 'tracking'

urlpatterns = [
    path('repair/<str:token>/', tracking.repair_track, name='repair_track'),
    path('lookup/', tracking.repair_lookup, name='repair_lookup'),
]