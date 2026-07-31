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
from .utils import is_htmx, htmx_response, redirect_to_staff
from ..decorators import handle_errors

logger = logging.getLogger(__name__)


# ============================================================
# HELPER: Render notification list partial (for HTMX)
# ============================================================
def render_notification_partial(request, paginator_page=None):
    """
    Renders the notification list partial for HTMX requests.
    """
    if paginator_page is None:
        notifications = request.user.notifications.all()
        paginator = Paginator(notifications, 20)
        page_number = request.GET.get('page', 1)
        page_obj = paginator.get_page(page_number)
    else:
        page_obj = paginator_page

    unread_count = request.user.notifications.filter(is_read=False).count()
    return render(request, 'notifications/partials/_notification_items.html', {
        'page_obj': page_obj,
        'unread_count': unread_count,
    })


# ============================================================
# NOTIFICATION LIST (Staff & Customer)
# ============================================================
@login_required
def notification_list(request):
    try:
        notifications = request.user.notifications.all()
        paginator = Paginator(notifications, 20)
        page_number = request.GET.get('page')
        page_obj = paginator.get_page(page_number)
        unread_count = request.user.notifications.filter(is_read=False).count()

        if request.user.is_staff:
            template = 'notifications/list.html'
        else:
            template = 'customer/notification_list.html'

        return render(request, template, {
            'page_obj': page_obj,
            'unread_count': unread_count,
        })
    except Exception as e:
        logger.error(f"Error loading notification list: {e}")
        messages.error(request, "Unable to load notifications. Please try again.")
        if request.user.is_staff:
            return redirect_to_staff('dashboard')
        else:
            return redirect('customer:customer_dashboard')


# ============================================================
# NOTIFICATION DROPDOWN (for bell icon)
# ============================================================

@login_required
def notification_dropdown(request):
    """
    HTMX endpoint to load only the dropdown content for notifications.
    Always returns the partial HTML (no redirect) to avoid full page load.
    """
    try:
        notifications = request.user.notifications.all()[:10]
        unread_count = request.user.notifications.filter(is_read=False).count()
        
        # Always render the partial – no redirect, no full page
        return render(request, 'notifications/partials/dropdown.html', {
            'notifications': notifications,
            'unread_count': unread_count,
        })
    except Exception as e:
        logger.error(f"Notification dropdown error: {e}")
        return HttpResponse(
            '<div class="dropdown-item text-danger">Error loading notifications</div>',
            status=500
        )


# ============================================================
# MARK SINGLE NOTIFICATION AS READ
# ============================================================
@login_required
@require_http_methods(["POST"])
@handle_errors(default_redirect='accounting:notification_list')
def mark_as_read(request, pk):
    notification = get_object_or_404(Notification, pk=pk, recipient=request.user)
    notification.is_read = True
    notification.save()
    logger.info(f"User {request.user.id} marked notification {pk} as read")

    if is_htmx(request):
        # Return only the partial list (not the full page)
        return render_notification_partial(request)
    messages.success(request, "Notification marked as read.")
    return redirect_to_staff('notification_list')


# ============================================================
# MARK ALL NOTIFICATIONS AS READ
# ============================================================
@login_required
@require_http_methods(["POST"])
@handle_errors(default_redirect='accounting:notification_list')
def mark_all_read(request):
    count = request.user.notifications.filter(is_read=False).update(is_read=True)
    logger.info(f"User {request.user.id} marked {count} notifications as read")

    if is_htmx(request):
        # Return the updated partial list
        return render_notification_partial(request)
    messages.success(request, f"{count} notifications marked as read.")
    return redirect_to_staff('notification_list')


# ============================================================
# DELETE SINGLE NOTIFICATION
# ============================================================
@login_required
@require_http_methods(["DELETE"])  
@handle_errors(default_redirect='accounting:notification_list')
def delete_notification(request, pk):
    notification = get_object_or_404(Notification, pk=pk, recipient=request.user)
    notification.delete()
    logger.info(f"User {request.user.id} deleted notification {pk}")

    if is_htmx(request):
        return render_notification_partial(request)
    messages.success(request, "Notification deleted.")
    return redirect_to_staff('notification_list')


# ============================================================
# DELETE ALL NOTIFICATIONS
# ============================================================
@login_required
@require_http_methods(["DELETE"])   
@handle_errors(default_redirect='accounting:notification_list')
def delete_all_notifications(request):
    count = request.user.notifications.count()
    request.user.notifications.all().delete()
    logger.info(f"User {request.user.id} deleted all {count} notifications")

    if is_htmx(request):
        return render_notification_partial(request)
    messages.success(request, f"{count} notifications deleted.")
    return redirect_to_staff('notification_list')


# ============================================================
# UNREAD COUNT (JSON)
# ============================================================
@login_required
def get_unread_count(request):
    if not request.user.is_authenticated:
        return JsonResponse({'count': 0})
    try:
        count = request.user.notifications.filter(is_read=False).count()
        return JsonResponse({'count': count})
    except Exception as e:
        logger.error(f"Error getting unread count: {e}")
        return JsonResponse({'count': 0}, status=500)


# ============================================================
# UNREAD COUNT (Plain Text)
# ============================================================
def unread_count_text(request):
    """
    Returns unread notification count as plain text.
    Safe: no redirect, always returns a number (0 if not authenticated).
    """
    if not request.user.is_authenticated:
        return HttpResponse("0")
    try:
        count = request.user.notifications.filter(is_read=False).count()
        return HttpResponse(str(count))
    except Exception as e:
        logger.error(f"Error getting unread count text: {e}")
        return HttpResponse("0")
    
    
