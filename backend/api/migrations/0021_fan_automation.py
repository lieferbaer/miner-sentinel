import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0020_calibration'),
    ]

    operations = [
        migrations.CreateModel(
            name='FanAutomation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('enabled', models.BooleanField(default=False)),
                ('ac_dev_id', models.CharField(blank=True, default='', max_length=40)),
                ('ac_port', models.IntegerField(default=1)),
                ('level', models.IntegerField(default=5)),
                ('state', models.JSONField(blank=True, default=dict)),
                ('updated_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('device', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,
                                                related_name='fan_automation', to='api.device')),
            ],
            options={
                'db_table': 'fan_automation',
            },
        ),
    ]
