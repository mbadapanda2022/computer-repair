# accounting/stock_manager.py

from decimal import Decimal
from django.db import transaction, models
from django.utils import timezone
from .models import StockMovement, Product


class StockManager:
    """
    Centralized stock operations for the entire application.
    All stock adjustments must go through this class.
    """

    @staticmethod
    @transaction.atomic
    def adjust_stock(product, quantity, movement_type, reference, date=None, notes=""):
        """
        Adjust stock for a product.
        - quantity: positive for IN, negative for OUT
        - movement_type: one of StockMovement.MOVEMENT_TYPE choices
        - reference: invoice number, purchase number, job number, etc.
        """
        if date is None:
            date = timezone.now()

        # Create movement record
        movement = StockMovement.objects.create(
            product=product,
            movement_type=movement_type,
            quantity=quantity,
            reference=reference,
            date=date,
            notes=notes
        )

        # Update product's current_stock
        total = StockMovement.objects.filter(product=product).aggregate(
            total=models.Sum('quantity')
        )['total'] or Decimal('0')
        product.current_stock = total
        product.save(update_fields=['current_stock'])

        return movement

    @staticmethod
    def get_current_stock(product):
        """Get current stock for a product."""
        total = StockMovement.objects.filter(product=product).aggregate(
            total=models.Sum('quantity')
        )['total'] or Decimal('0')
        return total

    @staticmethod
    @transaction.atomic
    def reverse_movement(movement):
        """Reverse a stock movement by creating an opposite movement."""
        if movement.quantity > 0:
            reverse_qty = -movement.quantity
        else:
            reverse_qty = abs(movement.quantity)
        return StockManager.adjust_stock(
            product=movement.product,
            quantity=reverse_qty,
            movement_type='adjustment',
            reference=f"REV-{movement.reference}",
            notes=f"Reversed movement {movement.id}",
            date=timezone.now()
        )