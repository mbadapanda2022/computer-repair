# accounting/views/demo.py
"""Demo-package pages — only active in DEMO_MODE (Windows trial build)."""

from django.conf import settings
from django.http import Http404
from django.shortcuts import render


def demo_guide(request):
    if not getattr(settings, 'DEMO_MODE', False):
        raise Http404
    from ..demo_trial import get_trial_state, TRIAL_CONTACT
    return render(request, 'demo/guide.html', {
        'trial': get_trial_state(),
        'contact': TRIAL_CONTACT,
    })
