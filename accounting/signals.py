# accounting/signals.py
import logging
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.contrib.contenttypes.models import ContentType
from .models import Notification, Invoice, RepairJob, Contact, Purchase, Payment

logger = logging.getLogger(__name__)


def delete_notifications_for_instance(instance):
    if not instance.pk:
        return

    content_type = ContentType.objects.get_for_model(instance)
    qs = Notification.objects.filter(content_type=content_type, object_id=instance.pk)
    count = qs.count()
    
    if count:
        qs.delete()
        logger.info(f"Deleted {count} notifications for {content_type.model} #{instance.pk}")


@receiver(post_delete, sender=Invoice)
def delete_invoice_notifications(sender, instance, **kwargs):
    delete_notifications_for_instance(instance)

@receiver(post_delete, sender=RepairJob)
def delete_repair_notifications(sender, instance, **kwargs):
    delete_notifications_for_instance(instance)

@receiver(post_delete, sender=Contact)
def delete_contact_notifications(sender, instance, **kwargs):
    delete_notifications_for_instance(instance)

@receiver(post_delete, sender=Purchase)
def delete_purchase_notifications(sender, instance, **kwargs):
    delete_notifications_for_instance(instance)

@receiver(post_delete, sender=Payment)
def delete_payment_notifications(sender, instance, **kwargs):
    delete_notifications_for_instance(instance)
    
    
