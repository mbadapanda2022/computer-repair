"""
Add Direct Expenses and Indirect Expenses account groups with granular ledgers.

This migration:
1. Creates parent group "Direct Expenses" and "Indirect Expenses" under Expenses (code 5)
2. Moves existing expense accounts to proper groups
3. Adds granular ledgers for proper Tally-style reporting
4. Preserves all existing data - no data loss
"""

from django.db import migrations


def create_expense_groups_and_accounts(apps, schema_editor):
    AccountGroup = apps.get_model("accounting", "AccountGroup")
    Account = apps.get_model("accounting", "Account")

    # Get or create parent Expenses group (code 5)
    expenses_group, _ = AccountGroup.objects.get_or_create(
        code='5',
        defaults={'name': 'Expenses', 'is_active': True}
    )

    # ============================================================
    # CREATE SUB-GROUPS
    # ============================================================
    
    # Direct Expenses group (code 51)
    direct_expenses_group, _ = AccountGroup.objects.get_or_create(
        code='51',
        defaults={
            'name': 'Direct Expenses',
            'parent': expenses_group,
            'is_active': True
        }
    )

    # Indirect Expenses group (code 52)
    indirect_expenses_group, _ = AccountGroup.objects.get_or_create(
        code='52',
        defaults={
            'name': 'Indirect Expenses',
            'parent': expenses_group,
            'is_active': True
        }
    )

    # ============================================================
    # DEFINE ACCOUNTS: (code, name, account_type, group_code, is_system)
    # ============================================================
    
    # --- DIRECT EXPENSES (Group 51) ---
    direct_accounts = [
        # Existing accounts - will be moved to Direct Expenses group
        ("5011", "Purchases", "expense", "51", True),
        ("5012", "Carriage Inward", "expense", "51", True),
        # New granular direct expense accounts
        ("5101", "Wages - Direct Labour", "expense", "51", True),
        ("5102", "Factory Rent", "expense", "51", True),
        ("5103", "Power & Fuel - Factory", "expense", "51", True),
        ("5104", "Manufacturing Expenses", "expense", "51", True),
        ("5105", "Job Work Charges", "expense", "51", True),
        ("5106", "Consumable Stores", "expense", "51", True),
        ("5107", "Freight & Octroi Inward", "expense", "51", True),
        ("5108", "Import Duty & Clearing", "expense", "51", True),
    ]

    # --- INDIRECT EXPENSES (Group 52) ---
    indirect_accounts = [
        # Existing - move Office Expenses here
        ("5013", "Office Expenses", "expense", "52", True),
        ("5014", "Bank Charges", "expense", "52", True),
        ("5010", "Discount Allowed", "expense", "52", True),
        # New granular indirect expense accounts
        ("5201", "Rent - Shop/Office", "expense", "52", True),
        ("5202", "Salaries - Staff", "expense", "52", True),
        ("5203", "Electricity - Office/Shop", "expense", "52", True),
        ("5204", "Telephone & Internet", "expense", "52", True),
        ("5205", "Printing & Stationery", "expense", "52", True),
        ("5206", "Conveyance & Travel", "expense", "52", True),
        ("5207", "Repairs & Maintenance", "expense", "52", True),
        ("5208", "Insurance", "expense", "52", True),
        ("5209", "Professional & Legal Fees", "expense", "52", True),
        ("5210", "Audit Fees", "expense", "52", True),
        ("5211", "Advertisement & Marketing", "expense", "52", True),
        ("5212", "Commission Paid", "expense", "52", True),
        ("5213", "Bad Debts Written Off", "expense", "52", True),
        ("5214", "Miscellaneous Expenses", "expense", "52", True),
        ("5215", "Staff Welfare", "expense", "52", True),
        ("5216", "Security Charges", "expense", "52", True),
        ("5217", "Software & Subscription", "expense", "52", True),
        ("5218", "GST Late Fees / Interest", "expense", "52", True),
    ]

    # Helper to get group by code
    def get_group(code):
        if code == '51':
            return direct_expenses_group
        elif code == '52':
            return indirect_expenses_group
        return expenses_group

    # Create/update Direct Expenses accounts
    for code, name, acc_type, group_code, is_sys in direct_accounts:
        group = get_group(group_code)
        account, created = Account.objects.get_or_create(
            code=code,
            defaults={
                'name': name,
                'account_type': acc_type,
                'group': group,
                'is_system': is_sys,
            }
        )
        if not created:
            # Update group if changed (for existing accounts like 5011, 5012)
            if account.group_id != group.id:
                account.group = group
                account.save(update_fields=['group'])

    # Create/update Indirect Expenses accounts
    for code, name, acc_type, group_code, is_sys in indirect_accounts:
        group = get_group(group_code)
        account, created = Account.objects.get_or_create(
            code=code,
            defaults={
                'name': name,
                'account_type': acc_type,
                'group': group,
                'is_system': is_sys,
            }
        )
        if not created:
            if account.group_id != group.id:
                account.group = group
                account.save(update_fields=['group'])


def reverse_expense_groups_and_accounts(apps, schema_editor):
    """
    Reverse: Move accounts back to parent Expenses group (code 5),
    delete the sub-groups.
    """
    AccountGroup = apps.get_model("accounting", "AccountGroup")
    Account = apps.get_model("accounting", "Account")

    expenses_group = AccountGroup.objects.filter(code='5').first()
    if not expenses_group:
        return

    # Move all accounts from sub-groups back to parent Expenses
    sub_groups = AccountGroup.objects.filter(code__in=['51', '52'])
    for sg in sub_groups:
        Account.objects.filter(group=sg).update(group=expenses_group)

    # Delete sub-groups
    sub_groups.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounting", "0007_recalculate_bank_balances"),
    ]

    operations = [
        migrations.RunPython(
            create_expense_groups_and_accounts,
            reverse_expense_groups_and_accounts
        ),
    ]