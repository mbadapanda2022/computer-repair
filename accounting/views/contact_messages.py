# accounting/views/contact_messages.py

import json
import logging
from django.shortcuts import render, get_object_or_404
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_protect
from django.db.models import Q
from django.utils import timezone

from ..models import ContactMessage
from .utils import is_htmx, htmx_response
from ..decorators import handle_errors

logger = logging.getLogger(__name__)


@handle_errors(default_redirect='accounting:message_list')
def message_list(request):
    """
    Display list of contact messages with filters.
    Supports HTMX partial updates.
    """
    status = request.GET.get('status', '')
    search = request.GET.get('search', '')

    contact_messages = ContactMessage.objects.all()

    if status:
        contact_messages = contact_messages.filter(status=status)

    if search:
        contact_messages = contact_messages.filter(
            Q(name__icontains=search) |
            Q(email__icontains=search) |
            Q(subject__icontains=search) |
            Q(message__icontains=search)
        )

    context = {
        'contact_messages': contact_messages,
        'status_filter': status,
        'search_query': search,
    }

    # HTMX request → return only the table body
    if is_htmx(request):
        html = render_to_string('messages/_list_content.html', context, request=request)
        return HttpResponse(html)

    # Full page load → return the whole page
    return render(request, 'messages/list.html', context)


def message_detail(request, pk):
    """Show message detail in modal."""
    msg = get_object_or_404(ContactMessage, pk=pk)
    return render(request, 'messages/_detail.html', {'message': msg})


@csrf_protect
@handle_errors(default_redirect='accounting:message_list')
def message_mark_status(request, pk):
    """Mark message status (read, replied, spam)."""
    msg = get_object_or_404(ContactMessage, pk=pk)
    new_status = request.POST.get('status')
    
    if new_status in dict(ContactMessage.STATUS_CHOICES):
        msg.status = new_status
        if new_status == 'replied':
            msg.replied_at = timezone.now()
        msg.save(update_fields=['status', 'replied_at'])

    html = render_to_string('messages/_row.html', {'message': msg}, request=request)
    response = HttpResponse(html)
    response['HX-Trigger'] = json.dumps({
        'showToast': {'level': 'success', 'message': f'Message marked as {new_status}.'}
    })
    return response


@csrf_protect
@handle_errors(default_redirect='accounting:message_list')
def message_delete(request, pk):
    """Delete a contact message."""
    msg = get_object_or_404(ContactMessage, pk=pk)
    msg.delete()
    response = HttpResponse()
    response['HX-Trigger'] = json.dumps({
        'showToast': {'level': 'success', 'message': 'Message deleted.'}
    })
    return response


def message_unread_count(request):
    """Return unread count as plain text (for badge)."""
    count = ContactMessage.objects.filter(status='new').count()
    return HttpResponse(str(count))


