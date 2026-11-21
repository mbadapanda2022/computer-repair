"""Staff-approved linking between portal accounts and shop contacts.

Registration never merges automatically: a signup whose email or phone
matches an existing contact without a portal account becomes a
PortalClaimRequest. Staff approve (link + fill blanks) or reject
(create a fresh profile). Until then the customer portal is gated.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from ..models import Contact, PortalClaimRequest
from .notification_helpers import send_notification, send_notification_to_staff

logger = logging.getLogger(__name__)


def normalize_phone(value: str) -> str:
    digits = ''.join(filter(str.isdigit, value or ''))
    return digits[-10:] if len(digits) >= 10 else ''


def _activity_counts(contact: Contact) -> dict:
    from ..models import Invoice, Payment, RepairJob
    return {
        'invoices': Invoice.objects.filter(customer=contact).count(),
        'repairs': RepairJob.objects.filter(customer=contact).count(),
        'payments': Payment.objects.filter(contact=contact).count(),
    }


def find_match_candidates(email: str, phone: str):
    """Return (primary_contact, alternate_contact, reason) for a signup.

    Only contacts without a portal account are considered. Email is the
    stronger signal, so it becomes the primary suggestion.
    """
    email_contact = None
    phone_contact = None
    if email:
        email_contact = (
            Contact.objects
            .filter(user__isnull=True, email__iexact=email)
            .exclude(email='')
            .order_by('pk').first()
        )
    if phone:
        phone_contact = (
            Contact.objects
            .filter(user__isnull=True, phone=phone)
            .order_by('pk').first()
        )

    if email_contact and phone_contact and email_contact.pk == phone_contact.pk:
        return email_contact, None, 'both'
    if email_contact and phone_contact:
        return email_contact, phone_contact, 'both'
    if email_contact:
        return email_contact, None, 'email'
    if phone_contact:
        return phone_contact, None, 'phone'
    return None, None, ''


def attach_portal_identity(user, name: str, email: str, phone: str):
    """Link a fresh signup to an existing contact or queue a staff claim.

    Returns (contact, claim). Exactly one is non-None.
    """
    phone_10 = normalize_phone(phone)
    primary, alternate, reason = find_match_candidates(email, phone_10)

    if primary is None:
        contact = Contact.objects.create(
            user=user,
            name=name,
            email=email,
            phone=phone_10,
            contact_type='customer',
        )
        return contact, None

    claim = PortalClaimRequest.objects.create(
        user=user,
        contact=primary,
        alternate_contact=alternate,
        matched_by=reason,
        submitted_name=name,
        submitted_email=email or '',
        submitted_phone=phone_10,
    )
    logger.info(
        "Portal claim #%s queued | user=%s | primary=%s | alternate=%s | match=%s",
        claim.pk, user.username, primary.pk,
        alternate.pk if alternate else None, reason,
    )

    counts = _activity_counts(primary)
    send_notification_to_staff(
        title='New portal signup needs linking',
        message=(
            f'{name} ({email or "no email"}{", " + phone_10 if phone_10 else ""}) '
            f'registered and looks like existing contact "{primary.name}" '
            f'({counts["invoices"]} invoices, {counts["repairs"]} repairs, '
            f'{counts["payments"]} payments). Review and link it.'
        ),
        link=reverse('accounting:portal_claims'),
        notif_type='warning',
        category='system',
    )
    send_notification(
        user=user,
        title='Shop approval pending',
        message=(
            'Your account was created. The shop will verify it against '
            'your existing records — you will get access as soon as it is '
            'approved.'
        ),
        link=reverse('customer:account_pending'),
        notif_type='info',
        category='system',
    )
    return None, claim


def _fill_blank_fields(contact: Contact, name='', email='', phone=''):
    """Fill only empty fields; never overwrite staff-maintained data."""
    updates = []
    if not (contact.email or '').strip() and email:
        contact.email = email
        updates.append('email')
    if not (contact.phone or '').strip():
        phone_10 = normalize_phone(phone)
        if phone_10 and not Contact.objects.filter(phone=phone_10).exclude(pk=contact.pk).exists():
            contact.phone = phone_10
            updates.append('phone')
    if updates:
        contact.save(update_fields=updates)


def _user_has_other_contact(user, contact_pk=None):
    qs = Contact.objects.filter(user=user)
    if contact_pk:
        qs = qs.exclude(pk=contact_pk)
    return qs.first()


def _contact_has_data(contact: Contact) -> bool:
    """True when the contact carries anything a report could read."""
    if contact.opening_balance:
        return True
    if contact.sales_invoices.exists() or contact.purchases.exists():
        return True
    if contact.repair_jobs.exists():
        return True
    if contact.payments.exists():
        return True
    return contact.ledger_lines.exclude(
        ledger_entry__entry_type='opening').exists()


def _drop_empty_contact(user, keep_pk=None):
    """Remove the auto-created empty profile when merging is safe.

    Registration immediately creates a contact when no match is found.
    If staff later links the real record, that placeholder (no
    transactions) is deleted so the person does not end up with two
    profiles. A placeholder with any data is never touched.
    """
    other = _user_has_other_contact(user, keep_pk)
    if other is None:
        return None
    if _contact_has_data(other):
        raise ValueError(
            f'This account already has its own record "{other.name}" with '
            'transactions. Merge the two contacts first.'
        )
    other_pk = other.pk
    other.hard_delete()
    logger.info(
        "Dropped empty auto-created contact #%s while linking user=%s",
        other_pk, user.pk,
    )
    return other_pk


def _append_note(existing, line):
    if not line:
        return existing
    return (existing + '\n' if existing else '') + line


def approve_claim(claim: PortalClaimRequest, staff_user, contact=None, note=''):
    """Link the claim's user to an existing contact (default: suggestion)."""
    if claim.status != 'pending':
        raise ValueError('This request was already resolved.')

    chosen = contact or claim.contact
    if chosen is None:
        raise ValueError('The suggested contact no longer exists.')
    if chosen.user_id not in (None, claim.user_id):
        raise ValueError('That contact is already linked to another account.')

    with transaction.atomic():
        _drop_empty_contact(claim.user, chosen.pk)
        _fill_blank_fields(
            chosen,
            name=claim.submitted_name,
            email=claim.submitted_email,
            phone=claim.submitted_phone,
        )
        contact_type = chosen.contact_type
        if contact_type == 'vendor':
            contact_type = 'both'
        chosen.user = claim.user
        chosen.contact_type = contact_type
        chosen.save(update_fields=['user', 'contact_type'])

        claim.status = 'approved'
        claim.contact = chosen
        claim.resolved_by = staff_user
        claim.resolved_at = timezone.now()
        claim.note = _append_note(
            _append_note(claim.note, note),
            f'Linked to contact #{chosen.pk} (existing records kept).',
        )
        claim.save(update_fields=[
            'status', 'contact', 'resolved_by', 'resolved_at', 'note',
        ])

    logger.info(
        "Portal claim #%s approved by %s | user=%s linked to contact=%s",
        claim.pk, staff_user.username, claim.user_id, chosen.pk,
    )
    send_notification(
        user=claim.user,
        title='Account approved — records linked',
        message=(
            f'Your portal account is now linked to your shop record '
            f'"{chosen.name}". Your previous invoices, repairs and payments '
            'are now visible in your dashboard.'
        ),
        link=reverse('customer:customer_dashboard'),
        notif_type='success',
        category='system',
    )
    return chosen


def reject_claim(claim: PortalClaimRequest, staff_user, note=''):
    """Reject the match and give the user a fresh standalone profile."""
    if claim.status != 'pending':
        raise ValueError('This request was already resolved.')

    user = claim.user
    with transaction.atomic():
        contact = Contact.objects.filter(user=user).first()
        if contact is None:
            phone_10 = normalize_phone(claim.submitted_phone)
            if phone_10 and Contact.objects.filter(phone=phone_10).exists():
                # The number belongs to the suggested record; leave it blank
                # rather than break the unique-phone rule. Staff can fix later.
                phone_10 = ''
            contact = Contact.objects.create(
                user=user,
                name=claim.submitted_name,
                email=claim.submitted_email,
                phone=phone_10,
                contact_type='customer',
            )

        claim.status = 'rejected'
        claim.resolved_by = staff_user
        claim.resolved_at = timezone.now()
        claim.note = _append_note(
            _append_note(claim.note, note),
            f'Rejected; separate profile #{contact.pk} created.',
        )
        claim.save(update_fields=['status', 'resolved_by', 'resolved_at', 'note'])

    logger.info(
        "Portal claim #%s rejected by %s | user=%s new contact=%s",
        claim.pk, staff_user.username, user.pk, contact.pk,
    )
    send_notification(
        user=user,
        title='Account ready — new profile',
        message=(
            'The shop reviewed your signup and created a separate profile '
            'for you. You can use the portal now; contact the shop if you '
            'believe an older record should be linked.'
        ),
        link=reverse('customer:customer_dashboard'),
        notif_type='info',
        category='system',
    )
    return contact


def link_contact_to_user(contact: Contact, user, staff_user):
    """Manual staff link (contact detail page). No claim required."""
    if contact.user_id == user.pk:
        return contact
    if contact.user_id:
        raise ValueError('This contact is already linked to another account.')

    claim = PortalClaimRequest.objects.filter(
        user=user, status='pending').first()

    with transaction.atomic():
        _drop_empty_contact(user, contact.pk)
        _fill_blank_fields(
            contact,
            name=user.first_name or user.username,
            email=user.email,
            phone=claim.submitted_phone if claim else '',
        )
        contact_type = contact.contact_type
        if contact_type == 'vendor':
            contact_type = 'both'
        contact.user = user
        contact.contact_type = contact_type
        contact.save(update_fields=['user', 'contact_type'])

        if claim:
            claim.status = 'approved'
            claim.contact = contact
            claim.resolved_by = staff_user
            claim.resolved_at = timezone.now()
            claim.note = _append_note(
                claim.note,
                f'Linked manually by staff (contact #{contact.pk}).',
            )
            claim.save(update_fields=[
                'status', 'contact', 'resolved_by', 'resolved_at', 'note',
            ])

    logger.info(
        "Manual portal link | staff=%s | user=%s | contact=%s",
        staff_user.username, user.pk, contact.pk,
    )
    send_notification(
        user=user,
        title='Account approved — records linked',
        message=(
            f'Your portal account is now linked to your shop record '
            f'"{contact.name}".'
        ),
        link=reverse('customer:customer_dashboard'),
        notif_type='success',
        category='system',
    )
    return contact


def pending_claim_for(user):
    if not user or not getattr(user, 'is_authenticated', False):
        return None
    return PortalClaimRequest.objects.filter(
        user=user, status='pending').select_related(
        'contact', 'alternate_contact').first()
