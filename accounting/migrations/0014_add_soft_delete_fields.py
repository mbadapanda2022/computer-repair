# accounting/migrations/0014_add_soft_delete_fields.py
from django.db import migrations, models
import django.db.models.deletion
from django.conf import settings

class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0013_migrate_to_chart_of_accounts'),
    ]

    operations = [
        # LedgerLine में SoftDelete फ़ील्ड्स जोड़ें
        migrations.AddField(
            model_name='ledgerline',
            name='is_deleted',
            field=models.BooleanField(default=False, help_text='Soft delete flag'),
        ),
        migrations.AddField(
            model_name='ledgerline',
            name='deleted_at',
            field=models.DateTimeField(null=True, blank=True, help_text='When was this record deleted'),
        ),
        migrations.AddField(
            model_name='ledgerline',
            name='deleted_by',
            field=models.ForeignKey(
                null=True,
                blank=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='ledgerline_deleted',
                to=settings.AUTH_USER_MODEL,
                help_text='Who deleted this record'
            ),
        ),
    ]