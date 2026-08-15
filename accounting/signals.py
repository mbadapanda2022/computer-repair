# accounting/signals.py
import logging
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth import get_user_model
from .models import Notification, Invoice, RepairJob, Contact, Purchase, Payment

logger = logging.getLogger(__name__)
User = get_user_model()

def delete_notifications_for_instance(instance):
    if not instance or not instance.pk:
        return
    try:
        ct = ContentType.objects.get_for_model(instance)
        qs = Notification.objects.filter(content_type=ct, object_id=instance.pk)
        count = qs.count()
        if count:
            qs.delete()
            logger.info(f"Deleted {count} notifications for {ct.model} #{instance.pk}")
    except Exception as e:
        logger.error(f"Error deleting notifications for {instance._meta.model_name} #{instance.pk}: {e}")

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

@receiver(post_delete, sender=User)
def delete_user_notifications(sender, instance, **kwargs):
    if instance and instance.pk:
        count = Notification.objects.filter(recipient=instance).count()
        if count:
            Notification.objects.filter(recipient=instance).delete()
            logger.info(f"Deleted {count} notifications for user {instance.username}")