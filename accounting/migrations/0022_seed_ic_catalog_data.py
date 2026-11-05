"""Load the starter IC catalog.

The public IC Finder is a search over a table, so an empty table is not a
half-finished feature — it is a page that answers "no ICs found" to every
question. Nobody stands at a shell during a deploy, so the rows that have been
checked against datasheets travel with the schema instead of waiting for a
manual ``seed_ic_catalog`` run.

The data itself lives in the management command (one list, one place to
correct a pin table), and that function is idempotent, so a fresh database and
a re-run land on the same rows.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    from accounting.management.commands.seed_ic_catalog import seed
    seed(quiet=True)


def backwards(apps, schema_editor):
    # The rows are reference data, and staff may have edited a pin table
    # because the board disagreed with it. Rolling back the migration is not
    # permission to delete their work.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0021_icchip_icmarking_icpin'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
