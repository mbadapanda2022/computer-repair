from decimal import Decimal

from django.db import migrations
from django.db.models import Sum


def recalculate_balances(apps, schema_editor):
    BankAccount = apps.get_model('accounting', 'BankAccount')
    BankTransaction = apps.get_model('accounting', 'BankTransaction')

    for account in BankAccount.objects.all():
        deposits = BankTransaction.objects.filter(
            bank_account=account,
            transaction_type='deposit',
            is_deleted=False,
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

        withdrawals = BankTransaction.objects.filter(
            bank_account=account,
            transaction_type='withdrawal',
            is_deleted=False,
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

        account.current_balance = (
            account.opening_balance + deposits - withdrawals
        ).quantize(Decimal('0.01'))
        account.save(update_fields=['current_balance'])


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0006_remove_blogcategory_deleted_at_and_more'),
    ]

    operations = [
        migrations.RunPython(recalculate_balances, migrations.RunPython.noop),
    ]
