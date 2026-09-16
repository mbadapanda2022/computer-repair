# accounting/views/notifications.py
"""
Staff notification views.

Uses HTMX polling (not SSE) — Render free-tier friendly.
"""

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from ..decorators import handle_errors
from ..models import Notification
from ..utils.notification_helpers import get_unread_count
from .utils import is_htmx, redirect_to_staff

logger = logging.getLogger(__name__)

PAGE_SIZE = 20
DROPDOWN_LIMIT = 10


# ════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════
def _base_queryset(request):
    """Ordered queryset — Meta.ordering already handles -created_at."""
    return request.user.notifications.all()


def _render_list_partial(request, page_obj=None):
    if page_obj is None:
        paginator = Paginator(_base_queryset(request), PAGE_SIZE)
        page_obj = paginator.get_page(request.GET.get('page', 1))

    return render(request, 'notifications/partials/_notification_items.html', {
        'page_obj': page_obj,
        'unread_count': get_unread_count(request.user),
    })


def _render_dropdown_partial(request):
    notifications = list(_base_queryset(request)[:DROPDOWN_LIMIT])
    return render(request, 'notifications/partials/dropdown.html', {
        'notifications': notifications,
        'unread_count': get_unread_count(request.user),
    })


def _htmx_partial_response(request):
    """
    Return the appropriate partial based on which container triggered
    the request. Falls back to the list partial.
    """
    target = request.headers.get('HX-Target', '').lower()
    if 'dropdown' in target:
        return _render_dropdown_partial(request)
    return _render_list_partial(request)


# ════════════════════════════════════════════════════════════
# LIST
# ════════════════════════════════════════════════════════════
@login_required
@handle_errors(default_redirect='accounting:dashboard')
def notification_list(request):
    paginator = Paginator(_base_queryset(request), PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get('page'))

    if is_htmx(request):
        return _render_list_partial(request, page_obj)

    return render(request, 'notifications/list.html', {
        'page_obj': page_obj,
        'unread_count': get_unread_count(request.user),
    })


# ════════════════════════════════════════════════════════════
# DROPDOWN
# ════════════════════════════════════════════════════════════
@login_required
def notification_dropdown(request):
    try:
        return _render_dropdown_partial(request)
    except Exception:
        logger.exception("Dropdown render failed | user=%s", request.user.id)
        return HttpResponse(
            '<div class="dropdown-item text-danger">Error loading</div>',
            status=500,
        )


# ════════════════════════════════════════════════════════════
# MARK READ (SINGLE)
# ════════════════════════════════════════════════════════════
@login_required
@require_http_methods(["POST"])
@handle_errors(default_redirect='accounting:notification_list')
def mark_as_read(request, pk):
    notification = get_object_or_404(
        Notification, pk=pk, recipient=request.user,
    )
    if not notification.is_read:
        notification.is_read = True
        notification.save(update_fields=['is_read'])

    if is_htmx(request):
        return _htmx_partial_response(request)

    messages.success(request, "Marked as read.")
    return redirect_to_staff('notification_list')


# ════════════════════════════════════════════════════════════
# MARK ALL READ
# ════════════════════════════════════════════════════════════
@login_required
@require_http_methods(["POST"])
@handle_errors(default_redirect='accounting:notification_list')
def mark_all_read(request):
    count = request.user.notifications.filter(is_read=False).update(is_read=True)
    logger.info("User %s marked %s notifications read", request.user.id, count)

    if is_htmx(request):
        return _htmx_partial_response(request)

    messages.success(request, f"{count} marked read.")
    return redirect_to_staff('notification_list')


# ════════════════════════════════════════════════════════════
# DELETE (SINGLE)
# ════════════════════════════════════════════════════════════
@login_required
@require_http_methods(["DELETE"])
@handle_errors(default_redirect='accounting:notification_list')
def delete_notification(request, pk):
    notification = get_object_or_404(
        Notification, pk=pk, recipient=request.user,
    )
    notification.delete()   # soft delete via SoftDeleteModel
    logger.info("User %s deleted notification %s", request.user.id, pk)

    if is_htmx(request):
        return _htmx_partial_response(request)

    messages.success(request, "Notification deleted.")
    return redirect_to_staff('notification_list')


# ════════════════════════════════════════════════════════════
# DELETE ALL
# ════════════════════════════════════════════════════════════
@login_required
@require_http_methods(["DELETE"])
@handle_errors(default_redirect='accounting:notification_list')
def delete_all_notifications(request):
    # SoftDeleteQuerySet.delete() handles soft delete per-row
    qs = request.user.notifications.all()
    count = qs.count()
    qs.delete()
    logger.info("User %s deleted all %s notifications", request.user.id, count)

    if is_htmx(request):
        return _htmx_partial_response(request)

    messages.success(request, "All notifications deleted.")
    return redirect_to_staff('notification_list')


# ════════════════════════════════════════════════════════════
# UNREAD COUNT ENDPOINTS
# ════════════════════════════════════════════════════════════
@login_required
def get_unread_count_json(request):
    return JsonResponse({'count': get_unread_count(request.user)})


@login_required
def unread_count_text(request):
    """Return count as text, empty string if 0 (for badge hide)."""
    count = get_unread_count(request.user)
    if count == 0:
        return HttpResponse('')   
    return HttpResponse(str(count))