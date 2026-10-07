"""
Seed the standard Chart of Accounts.

Invoice / Purchase / Payment / ledger sync code calls get_account(...)
lazily — pehli invoice par CoA rows transaction ke andar create hoti
thin. Fresh DB (Render) par ye hot-path writes ko read-only bana deta
hai aur reports me accounts pehle din se dikhte hain.

Codes/names MUST match models.get_account call-sites exactly.
"""
from django.db import migrations

GROUPS = [
    ("1", "Assets"),
    ("2", "Liabilities"),
    ("3", "Equity"),
    ("4", "Income"),
    ("5", "Expenses"),
]

# (code, name, account_type, group_code)
ACCOUNTS = [
    ("1001", "Cash", "asset", "1"),
    ("1010", "Bank Account", "asset", "1"),
    ("1011", "Customer Receivable", "asset", "1"),
    ("1012", "Advance from Customer", "liability", "2"),
    ("1013", "GST Input", "asset", "1"),
    ("1014", "Advance to Vendor", "asset", "1"),
    ("1020", "Bank Suspense", "asset", "1"),
    ("2010", "GST Payable", "liability", "2"),
    ("2011", "Vendor Payable", "liability", "2"),
    ("3010", "Opening Balance Equity", "equity", "3"),
    ("4010", "Sales Revenue", "income", "4"),
    ("4011", "Discount Received", "income", "4"),
    ("4012", "Interest Income", "income", "4"),
    ("4020", "Sales Returns", "income", "4"),
    ("5010", "Discount Allowed", "expense", "5"),
    ("5011", "Purchases", "expense", "5"),
    ("5012", "Carriage Inward", "expense", "5"),
    ("5013", "Office Expenses", "expense", "5"),
    ("5014", "Bank Charges", "expense", "5"),
]


def seed_chart_of_accounts(apps, schema_editor):
    AccountGroup = apps.get_model("accounting", "AccountGroup")
    Account = apps.get_model("accounting", "Account")

    groups = {}
    for code, name in GROUPS:
        group, _ = AccountGroup.objects.get_or_create(
            code=code,
            defaults={"name": name, "is_active": True},
        )
        groups[code] = group

    for code, name, account_type, group_code in ACCOUNTS:
        Account.objects.get_or_create(
            code=code,
            defaults={
                "name": name,
                "account_type": account_type,
                "group": groups[group_code],
                "is_system": True,
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ("accounting", "0002_repairjob_date_in_optional"),
    ]

    operations = [
        migrations.RunPython(seed_chart_of_accounts, migrations.RunPython.noop),
    ]
