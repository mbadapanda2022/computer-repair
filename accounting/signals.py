# accounting/signals.py
"""
Signal handlers — Notification cleanup only.

IMPORTANT — What's in this file vs. models.py
═════════════════════════════════════════════

This file ONLY handles one thing:
    Delete linked Notification rows when parent objects are removed.

The following signals are registered in `models.py` (NOT here):
    - AuditLog create/delete     (Invoice, Purchase, Payment, Contact, Product)
    - Payment allocation sync    (PaymentAllocation)
    - Advance adjustment sync    (AdvanceAdjustment)
    - Opening balance ledger     (Contact)
    - NotificationPreference     (User)
    - Contact balance recalc     (LedgerLine)

The two files DO NOT conflict and DO NOT duplicate work.

═══════════════════════════════════════════════════════════

Note on Soft Delete:
────────────────────
`SoftDeleteModel.delete()` sets `is_deleted=True` and calls `save()`.
It does NOT call `super().delete()`, so Django's `post_delete` signal
does NOT fire on soft delete.

Therefore, these handlers only fire on HARD delete
(e.g. User deletion cascade, or explicit `.hard_delete()`).
This is intentional — soft-deleted parents can still be restored,
and their notifications should remain intact until that time.
"""

import logging

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import (
    Contact,
    Invoice,
    Notification,
    Payment,
    Purchase,
    RepairJob,
)

logger = logging.getLogger(__name__)
User = get_user_model()


# ════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════

def _delete_notifications_for_instance(instance):
    """
    Delete all Notification rows that reference `instance` via
    the GenericForeignKey (content_type + object_id).

    Uses the SoftDelete-aware `Notification.objects` manager, so
    calling `.delete()` performs a SOFT delete of each row.

    Fail-safe: any exception is logged but never propagated.
    """
    if not instance or not instance.pk:
        return

    try:
        content_type = ContentType.objects.get_for_model(instance)
        qs = Notification.objects.filter(
            content_type=content_type,
            object_id=instance.pk,
        )
        count = qs.count()
        if count:
            qs.delete()   # SoftDeleteQuerySet -> soft delete
            logger.info(
                "Deleted %s notification(s) for %s #%s",
                count, content_type.model, instance.pk,
            )
    except Exception:
        logger.exception(
            "Notification cleanup failed for %s #%s",
            getattr(instance._meta, 'model_name', '?'),
            getattr(instance, 'pk', '?'),
        )


# ════════════════════════════════════════════════════════════
# OBJECT DELETE → NOTIFICATION CLEANUP
# ════════════════════════════════════════════════════════════

@receiver(post_delete, sender=Invoice)
@receiver(post_delete, sender=RepairJob)
@receiver(post_delete, sender=Contact)
@receiver(post_delete, sender=Purchase)
@receiver(post_delete, sender=Payment)
def _cleanup_object_notifications(sender, instance, **kwargs):
    """
    When a parent object is HARD-deleted, remove its linked notifications.

    Skipped for soft deletes (handled automatically since post_delete
    doesn't fire on SoftDeleteModel.delete()).
    """
    _delete_notifications_for_instance(instance)


# ════════════════════════════════════════════════════════════
# USER DELETE → THEIR NOTIFICATIONS CLEANUP
# ════════════════════════════════════════════════════════════

@receiver(post_delete, sender=User)
def _cleanup_user_notifications(sender, instance, **kwargs):
    """
    When a User is HARD-deleted, remove all their notifications.

    (Usually User deletion cascades from admin panel hard delete;
    soft delete of User is not supported by our model.)
    """
    if not instance or not instance.pk:
        return

    try:
        qs = Notification.objects.filter(recipient=instance)
        count = qs.count()
        if count:
            qs.delete()
            logger.info(
                "Deleted %s notification(s) for user %s",
                count, getattr(instance, 'username', instance.pk),
            )
    except Exception:
        logger.exception(
            "User notification cleanup failed | user_id=%s", instance.pk,
        )