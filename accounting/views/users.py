# accounting/views/users.py
"""Dashboard user management (replaces day-to-day Django admin use).

Staff see and manage logins from /dashboard/users/. Superuser accounts are
invisible to non-superuser staff and can only be edited by a superuser.
Accounts are deactivated, never deleted, so contacts and their ledgers stay
linked.
"""

import logging
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from ..decorators import handle_errors
from ..forms import (
    StaffUserCreateForm,
    StaffUserEditForm,
    StaffUserPasswordForm,
)
from ..models import Contact, PortalClaimRequest
from ..utils.portal_linking import link_contact_to_user
from .utils import htmx_response, is_htmx, redirect_to_staff

logger = logging.getLogger(__name__)
User = get_user_model()

PAGE_SIZE = 25


# ============================================================
# HELPERS
# ============================================================
def _filters_qs(request):
    params = request.GET.copy()
    params.pop('page', None)
    return params.urlencode()


def _visible_users(request):
    """Superuser accounts are hidden from ordinary staff."""
    qs = User.objects.select_related('customer_contact')
    if not request.user.is_superuser:
        qs = qs.exclude(is_superuser=True)
    return qs


def _manageable_user(request, pk):
    """Load a target account, refusing superusers to non-superusers."""
    target = get_object_or_404(User, pk=pk)
    if target.is_superuser and not request.user.is_superuser:
        raise PermissionDenied(
            'Only an administrator can manage administrator accounts.')
    return target


def _list_context(request):
    base = _visible_users(request)
    stats = {
        'total_users': base.count(),
        'staff_users': base.filter(
            is_staff=True, is_superuser=False).count(),
        'customer_users': base.filter(is_staff=False).count(),
        'inactive_users': base.filter(is_active=False).count(),
        'pending_portal_claims': PortalClaimRequest.objects.filter(
            status='pending').count(),
    }

    search = (request.GET.get('search') or '').strip()
    role = (request.GET.get('role') or '').strip()
    status = (request.GET.get('status') or '').strip()

    qs = base
    if search:
        qs = qs.filter(
            Q(username__icontains=search)
            | Q(first_name__icontains=search)
            | Q(email__icontains=search)
            | Q(customer_contact__phone__icontains=search)
        )
    if role == 'admin':
        qs = qs.filter(is_superuser=True)
    elif role == 'staff':
        qs = qs.filter(is_staff=True, is_superuser=False)
    elif role == 'customer':
        qs = qs.filter(is_staff=False)
    if status == 'active':
        qs = qs.filter(is_active=True)
    elif status == 'inactive':
        qs = qs.filter(is_active=False)

    paginator = Paginator(qs.order_by('-date_joined', 'username'), PAGE_SIZE)
    try:
        page_obj = paginator.page(request.GET.get('page', 1))
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    filters_qs = urlencode({
        key: value for key, value in (
            ('search', search), ('role', role), ('status', status),
        ) if value
    })

    return {
        **stats,
        'users': page_obj.object_list,
        'page_obj': page_obj,
        'search': search,
        'role_filter': role,
        'status_filter': status,
        'filters_qs': filters_qs,
        'allow_admin_role': request.user.is_superuser,
        'is_htmx': is_htmx(request),
    }


def _success_response(request, message, log_msg):
    logger.info('%s | staff=%s', log_msg, request.user.username)
    if is_htmx(request):
        return htmx_response(
            request,
            'users/partials/user_table.html',
            context=_list_context(request),
            toast={'level': 'success', 'message': message},
            close_modal=True,
        )
    messages.success(request, message)
    return redirect_to_staff('user_list')


# ============================================================
# 1. USER LIST
# ============================================================
@login_required
@handle_errors(default_redirect='accounting:user_list')
def user_list(request):
    context = _list_context(request)
    if is_htmx(request):
        return render(request, 'users/partials/user_table.html', context)
    return render(request, 'users/user_list.html', context)


# ============================================================
# 2. CREATE USER
# ============================================================
@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:user_list')
def user_create(request):
    allow_admin = request.user.is_superuser
    if request.method == 'POST':
        form = StaffUserCreateForm(request.POST, allow_admin=allow_admin)
        if form.is_valid():
            user = form.save()
            label = 'Administrator' if user.is_superuser else (
                'Staff' if user.is_staff else 'Customer')
            return _success_response(
                request,
                f'{label} login "{user.username}" created.',
                f'User created role={label} target={user.pk}',
            )
        if is_htmx(request):
            return htmx_response(
                request,
                'users/user_form.html',
                context={'form': form, 'is_create': True,
                         'filters_qs': _filters_qs(request)},
                extra_headers={'HX-Retarget': '#mainModalContent'},
            )
    else:
        form = StaffUserCreateForm(allow_admin=allow_admin)
    return render(request, 'users/user_form.html', {
        'form': form,
        'is_create': True,
        'filters_qs': _filters_qs(request),
    })


# ============================================================
# 3. EDIT USER
# ============================================================
@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:user_list')
def user_edit(request, pk):
    target = _manageable_user(request, pk)
    allow_admin = request.user.is_superuser
    if request.method == 'POST':
        form = StaffUserEditForm(
            request.POST, instance=target, allow_admin=allow_admin,
            acting_user=request.user)
        if form.is_valid():
            form.save()
            return _success_response(
                request,
                f'Login "{target.username}" updated.',
                f'User updated target={target.pk}',
            )
        if is_htmx(request):
            return htmx_response(
                request,
                'users/user_form.html',
                context={'form': form, 'target': target, 'is_create': False,
                         'filters_qs': _filters_qs(request)},
                extra_headers={'HX-Retarget': '#mainModalContent'},
            )
    else:
        form = StaffUserEditForm(
            instance=target, allow_admin=allow_admin,
            acting_user=request.user)
    return render(request, 'users/user_form.html', {
        'form': form,
        'target': target,
        'is_create': False,
        'filters_qs': _filters_qs(request),
    })


# ============================================================
# 4. SET PASSWORD
# ============================================================
@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:user_list')
def user_set_password(request, pk):
    target = _manageable_user(request, pk)
    if request.method == 'POST':
        form = StaffUserPasswordForm(request.POST, user=target)
        if form.is_valid():
            form.save()
            return _success_response(
                request,
                f'Password changed for "{target.username}".',
                f'User password reset target={target.pk}',
            )
        if is_htmx(request):
            return htmx_response(
                request,
                'users/user_password_form.html',
                context={'form': form, 'target': target,
                         'filters_qs': _filters_qs(request)},
                extra_headers={'HX-Retarget': '#mainModalContent'},
            )
    else:
        form = StaffUserPasswordForm(user=target)
    return render(request, 'users/user_password_form.html', {
        'form': form,
        'target': target,
        'filters_qs': _filters_qs(request),
    })


# ============================================================
# 5. ACTIVATE / DEACTIVATE
# ============================================================
@login_required
@require_POST
@csrf_protect
@handle_errors(default_redirect='accounting:user_list')
def user_toggle_active(request, pk):
    target = _manageable_user(request, pk)
    level, message = 'success', ''

    if target.pk == request.user.pk:
        level = 'danger'
        message = 'You cannot deactivate your own login.'
    elif (target.is_superuser and target.is_active
          and User.objects.filter(
              is_superuser=True, is_active=True).count() <= 1):
        level = 'danger'
        message = 'At least one active administrator must stay.'
    else:
        target.is_active = not target.is_active
        target.save(update_fields=['is_active'])
        logger.info(
            'User %s %s by staff=%s',
            target.pk, 'activated' if target.is_active else 'deactivated',
            request.user.username)
        message = (
            f'"{target.username}" is now '
            f'{"active" if target.is_active else "deactivated"}.'
        )

    if level == 'success':
        messages.success(request, message)
    else:
        messages.error(request, message)

    if is_htmx(request):
        return htmx_response(
            request,
            'users/partials/user_table.html',
            context=_list_context(request),
            toast={'level': level, 'message': message},
        )
    return redirect_to_staff('user_list')


# ============================================================
# 6. LINK CUSTOMER LOGIN TO AN OLD SHOP CONTACT
# ============================================================
def _contact_suggestions(target):
    """Unlinked contacts that probably belong to this login."""
    base = Contact.objects.filter(user__isnull=True)
    if target.email or target.first_name:
        cond = Q()
        if target.email:
            cond |= Q(email__iexact=target.email)
        if target.first_name:
            cond |= Q(name__icontains=target.first_name)
        matches = list(base.filter(cond).order_by('name')[:50])
        if matches:
            return matches
    return list(base.order_by('-id')[:50])


@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:user_list')
def user_link_contact(request, pk):
    target = _manageable_user(request, pk)
    if target.is_staff:
        messages.error(
            request, 'Staff logins do not use a customer contact.')
        return redirect_to_staff('user_list')

    linked = Contact.all_objects.filter(user=target).first()
    if linked is not None and linked.is_deleted:
        messages.error(
            request,
            f'"{target.username}" is still attached to deleted contact '
            f'"{linked.name}". Restore that contact first.')
        return redirect_to_staff('user_list')

    error = None
    if request.method == 'POST':
        raw_id = (request.POST.get('contact_id') or '').strip()
        contact = (Contact.objects.filter(pk=raw_id).first()
                   if raw_id.isdigit() else None)
        if contact is None:
            error = 'Choose a shop contact to link.'
        elif contact.user_id == target.pk:
            error = 'This login is already linked to that contact.'
        elif contact.user_id:
            error = ('That contact is already linked to another login. '
                     'Unlink it first.')
        else:
            try:
                with transaction.atomic():
                    link_contact_to_user(contact, target, request.user)
            except ValueError as exc:
                error = str(exc)
            else:
                return _success_response(
                    request,
                    f'"{target.username}" now uses contact '
                    f'"{contact.name}". Old work is visible in the portal.',
                    f'User linked to contact target={target.pk} '
                    f'contact={contact.pk}',
                )

    pending_claim = PortalClaimRequest.objects.filter(
        user=target, status='pending').first()
    context = {
        'target': target,
        'suggestions': _contact_suggestions(target),
        'error': error,
        'existing_profile': linked,
        'pending_claim': pending_claim,
        'filters_qs': _filters_qs(request),
    }
    if error and is_htmx(request):
        return htmx_response(
            request,
            'users/user_link_contact_form.html',
            context=context,
            extra_headers={'HX-Retarget': '#mainModalContent'},
        )
    return render(request, 'users/user_link_contact_form.html', context)
