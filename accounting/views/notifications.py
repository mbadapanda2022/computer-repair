# accounting/views/notifications.py
import json
import logging
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse
from django.core.paginator import Paginator
from django.views.decorators.http import require_http_methods
from django.contrib import messages
from ..models import Notification
from .utils import is_htmx, redirect_to_staff
from ..decorators import handle_errors

logger = logging.getLogger(__name__)


# ============================================================
# HELPERS
# ============================================================
def render_notification_partial(request, paginator_page=None):
    if paginator_page is None:
        notifications = request.user.notifications.all()
        paginator = Paginator(notifications, 20)
        page = request.GET.get('page', 1)
        page_obj = paginator.get_page(page)
    else:
        page_obj = paginator_page
    unread_count = request.user.notifications.filter(is_read=False).count()
    return render(request, 'notifications/partials/_notification_items.html', {
        'page_obj': page_obj,
        'unread_count': unread_count,
    })


def render_dropdown_partial(request):
    notifications = request.user.notifications.all()[:10]
    unread_count = request.user.notifications.filter(is_read=False).count()
    return render(request, 'notifications/partials/dropdown.html', {
        'notifications': notifications,
        'unread_count': unread_count,
    })


# ============================================================
# LIST
# ============================================================
@login_required
def notification_list(request):
    try:
        notifications = request.user.notifications.all()
        paginator = Paginator(notifications, 20)
        page = request.GET.get('page')
        page_obj = paginator.get_page(page)
        unread_count = request.user.notifications.filter(is_read=False).count()

        if is_htmx(request):
            return render(request, 'notifications/partials/_notification_items.html', {
                'page_obj': page_obj,
                'unread_count': unread_count,
            })
        return render(request, 'notifications/list.html', {
            'page_obj': page_obj,
            'unread_count': unread_count,
        })
    except Exception as e:
        logger.error(f"Notification list error: {e}")
        messages.error(request, "Unable to load notifications.")
        return redirect_to_staff('dashboard')


# ============================================================
# DROPDOWN
# ============================================================
@login_required
def notification_dropdown(request):
    try:
        return render_dropdown_partial(request)
    except Exception:
        return HttpResponse('<div class="dropdown-item text-danger">Error loading</div>', status=500)


# ============================================================
# MARK READ (SINGLE)
# ============================================================
@login_required
@require_http_methods(["POST"])
@handle_errors(default_redirect='accounting:notification_list')
def mark_as_read(request, pk):
    notification = get_object_or_404(Notification, pk=pk, recipient=request.user)
    notification.is_read = True
    notification.save()

    if is_htmx(request):
        target = request.headers.get('HX-Target', '')
        if 'dropdown' in target.lower():
            return render_dropdown_partial(request)
        return render_notification_partial(request)

    messages.success(request, "Marked as read.")
    return redirect_to_staff('notification_list')


# ============================================================
# MARK ALL READ
# ============================================================
@login_required
@require_http_methods(["POST"])
@handle_errors(default_redirect='accounting:notification_list')
def mark_all_read(request):
    count = request.user.notifications.filter(is_read=False).update(is_read=True)
    logger.info(f"User {request.user.id} marked {count} notifications as read")

    if is_htmx(request):
        target = request.headers.get('HX-Target', '')
        if 'dropdown' in target.lower():
            return render_dropdown_partial(request)
        return render_notification_partial(request)

    messages.success(request, f"{count} marked read.")
    return redirect_to_staff('notification_list')


# ============================================================
# DELETE SINGLE
# ============================================================
@login_required
@require_http_methods(["DELETE"])
@handle_errors(default_redirect='accounting:notification_list')
def delete_notification(request, pk):
    notification = get_object_or_404(Notification, pk=pk, recipient=request.user)
    notification.delete()
    logger.info(f"User {request.user.id} deleted notification {pk}")

    if is_htmx(request):
        target = request.headers.get('HX-Target', '')
        if 'dropdown' in target.lower():
            return render_dropdown_partial(request)
        return render_notification_partial(request)

    messages.success(request, "Notification deleted.")
    return redirect_to_staff('notification_list')


# ============================================================
# DELETE ALL
# ============================================================
@login_required
@require_http_methods(["DELETE"])
@handle_errors(default_redirect='accounting:notification_list')
def delete_all_notifications(request):
    count = request.user.notifications.count()
    request.user.notifications.all().delete()
    logger.info(f"User {request.user.id} deleted all {count} notifications")

    if is_htmx(request):
        target = request.headers.get('HX-Target', '')
        if 'dropdown' in target.lower():
            return render_dropdown_partial(request)
        return render_notification_partial(request)

    messages.success(request, "All deleted.")
    return redirect_to_staff('notification_list')


# ============================================================
# UNREAD COUNT (JSON)
# ============================================================
@login_required
def get_unread_count(request):
    try:
        count = request.user.notifications.filter(is_read=False).count()
        return JsonResponse({'count': count})
    except Exception as e:
        logger.error(f"Error getting unread count: {e}")
        return JsonResponse({'count': 0}, status=500)


# ============================================================
# UNREAD COUNT (PLAIN TEXT)
# ============================================================
@login_required
def unread_count_text(request):
    return HttpResponse(str(request.user.notifications.filter(is_read=False).count()))