from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0022_fan_automation_level_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='fanautomation',
            name='maintenance_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
