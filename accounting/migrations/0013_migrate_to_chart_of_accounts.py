# accounting/migrations/0013_migrate_to_chart_of_accounts.py
from django.db import migrations, models
import django.db.models.deletion
from decimal import Decimal

def create_accounts_and_migrate(apps, schema_editor):
    AccountGroup = apps.get_model('accounting', 'AccountGroup')
    Account = apps.get_model('accounting', 'Account')
    LedgerLine = apps.get_model('accounting', 'LedgerLine')

    # 1. Account Groups बनाएँ
    group_1, _ = AccountGroup.objects.get_or_create(code='1', defaults={'name': 'Assets'})
    group_2, _ = AccountGroup.objects.get_or_create(code='2', defaults={'name': 'Liabilities'})
    group_3, _ = AccountGroup.objects.get_or_create(code='3', defaults={'name': 'Equity'})
    group_4, _ = AccountGroup.objects.get_or_create(code='4', defaults={'name': 'Income'})
    group_5, _ = AccountGroup.objects.get_or_create(code='5', defaults={'name': 'Expense'})
    group_9, _ = AccountGroup.objects.get_or_create(code='9', defaults={'name': 'Uncategorized'})

    groups = {'1': group_1, '2': group_2, '3': group_3, '4': group_4, '5': group_5, '9': group_9}

    # 2. Standard Accounts बनाएँ (Mapping Dictionary)
    account_defs = {
        'Cash': {'code': '1001', 'name': 'Cash', 'type': 'asset', 'group': group_1},
        'Bank Account': {'code': '1002', 'name': 'Bank Account', 'type': 'asset', 'group': group_1},
        'Bank': {'code': '1002', 'name': 'Bank Account', 'type': 'asset', 'group': group_1},
        'Customer Receivable': {'code': '1011', 'name': 'Customer Receivable', 'type': 'asset', 'group': group_1},
        'Vendor Payable': {'code': '2011', 'name': 'Vendor Payable', 'type': 'liability', 'group': group_2},
        'Purchases': {'code': '5011', 'name': 'Purchases', 'type': 'expense', 'group': group_5},
        'Sales Revenue': {'code': '4010', 'name': 'Sales Revenue', 'type': 'income', 'group': group_4},
        'Sales': {'code': '4010', 'name': 'Sales', 'type': 'income', 'group': group_4},
        'GST Payable': {'code': '2010', 'name': 'GST Payable', 'type': 'liability', 'group': group_2},
        'GST Input': {'code': '1013', 'name': 'GST Input', 'type': 'asset', 'group': group_1},
        'Discount Allowed': {'code': '5010', 'name': 'Discount Allowed', 'type': 'expense', 'group': group_5},
        'Discount Received': {'code': '4011', 'name': 'Discount Received', 'type': 'income', 'group': group_4},
        'Advance from Customer': {'code': '1012', 'name': 'Advance from Customer', 'type': 'liability', 'group': group_2},
        'Advance to Vendor': {'code': '1014', 'name': 'Advance to Vendor', 'type': 'asset', 'group': group_1},
        'Opening Balance': {'code': '3010', 'name': 'Opening Balance', 'type': 'equity', 'group': group_3},
        'Opening Balance Equity': {'code': '3010', 'name': 'Opening Balance Equity', 'type': 'equity', 'group': group_3},
    }

    created_accounts = {}
    for text, attrs in account_defs.items():
        acc, _ = Account.objects.get_or_create(
            code=attrs['code'],
            defaults={
                'name': attrs['name'],
                'account_type': attrs['type'],
                'group': attrs['group'],
                'is_system': True
            }
        )
        created_accounts[text] = acc

    # Fallback Account
    fallback_account, _ = Account.objects.get_or_create(
        code='9999',
        defaults={'name': 'Unknown Account', 'account_type': 'expense', 'group': group_9, 'is_system': False}
    )

    # 3. सभी LedgerLine को Migrate करें
    for line in LedgerLine.objects.all():
        old_text = line.account
        new_account = None

        if old_text in created_accounts:
            new_account = created_accounts[old_text]
        else:
            if 'Customer' in old_text or 'customer' in old_text:
                new_account = created_accounts['Customer Receivable']
            elif 'Vendor' in old_text or 'vendor' in old_text:
                new_account = created_accounts['Vendor Payable']
            elif 'Cash' in old_text:
                new_account = created_accounts['Cash']
            elif 'Bank' in old_text:
                new_account = created_accounts['Bank Account']
            elif 'GST' in old_text or 'Tax' in old_text or 'tax' in old_text:
                new_account = created_accounts['GST Payable']
            elif 'Opening' in old_text:
                new_account = created_accounts['Opening Balance Equity']
            elif 'Sales' in old_text or 'Revenue' in old_text:
                new_account = created_accounts['Sales Revenue']
            elif 'Purchase' in old_text:
                new_account = created_accounts['Purchases']
            elif 'Discount' in old_text:
                if 'Received' in old_text:
                    new_account = created_accounts['Discount Received']
                else:
                    new_account = created_accounts['Discount Allowed']

        if new_account is None:
            safe_name = old_text[:50] if old_text else "Unknown"
            unique_code = f"UNK_{line.id}"
            new_account, _ = Account.objects.get_or_create(
                code=unique_code,
                defaults={
                    'name': safe_name,
                    'account_type': 'expense',
                    'group': group_9,
                    'is_system': False
                }
            )

        line.account_new = new_account
        line.save(update_fields=['account_new'])


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0012_alter_companyprofile_hero_image_and_more'),  # आपकी आखिरी माइग्रेशन
    ]

    operations = [
        migrations.CreateModel(
            name='AccountGroup',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, unique=True)),
                ('code', models.CharField(max_length=10, unique=True, help_text='e.g., 1 for Assets, 2 for Liabilities')),
                ('is_active', models.BooleanField(default=True)),
                ('parent', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='children', to='accounting.accountgroup')),
            ],
            options={
                'ordering': ['code'],
                'verbose_name': 'Account Group',
                'verbose_name_plural': 'Account Groups',
            },
        ),
        migrations.CreateModel(
            name='Account',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=20, unique=True, help_text='e.g., 1010 for Cash, 4010 for Sales Revenue')),
                ('name', models.CharField(max_length=100)),
                ('account_type', models.CharField(choices=[('asset', 'Asset'), ('liability', 'Liability'), ('equity', 'Equity'), ('income', 'Income/Revenue'), ('expense', 'Expense'), ('contra_asset', 'Contra Asset')], max_length=15)),
                ('default_tax_rate', models.DecimalField(decimal_places=2, default=0, max_digits=5, validators=[django.core.validators.MinValueValidator(0)])),
                ('is_active', models.BooleanField(default=True)),
                ('is_system', models.BooleanField(default=False, help_text='System accounts cannot be deleted')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('group', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='accounts', to='accounting.accountgroup')),
                ('parent', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='children', to='accounting.account')),
            ],
            options={
                'ordering': ['code'],
                'verbose_name': 'Account',
                'verbose_name_plural': 'Accounts',
            },
        ),
        migrations.AddField(
            model_name='ledgerline',
            name='account_new',
            field=models.ForeignKey(
                to='accounting.Account',
                on_delete=django.db.models.deletion.PROTECT,
                null=True,
                blank=True,
                related_name='ledger_lines_new'
            ),
        ),
        migrations.RunPython(create_accounts_and_migrate, reverse_code=migrations.RunPython.noop),

        # 🆕 Drop the old index before dropping the column
        migrations.RunSQL("DROP INDEX accounting__account_bac47e_idx;"),

        migrations.RemoveField(
            model_name='ledgerline',
            name='account',
        ),
        migrations.RenameField(
            model_name='ledgerline',
            old_name='account_new',
            new_name='account',
        ),
        migrations.AlterField(
            model_name='ledgerline',
            name='account',
            field=models.ForeignKey(
                to='accounting.Account',
                on_delete=django.db.models.deletion.PROTECT,
                related_name='ledger_lines'
            ),
        ),
    ]