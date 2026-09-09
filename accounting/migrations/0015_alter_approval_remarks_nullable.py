# accounting/migrations/0015_alter_approval_remarks_nullable.py
from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0014_add_soft_delete_fields'),  # यह आपकी आखिरी माइग्रेशन है
    ]

    operations = [
        migrations.AlterField(
            model_name='repairjob',
            name='approval_remarks',
            field=models.TextField(blank=True, null=True),
        ),
    ]