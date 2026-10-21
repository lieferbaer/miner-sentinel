from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0021_fan_automation'),
    ]

    operations = [
        migrations.AddField(
            model_name='fanautomation',
            name='level_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
