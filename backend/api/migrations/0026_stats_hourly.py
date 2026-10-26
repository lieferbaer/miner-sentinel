import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0025_fan_automation_temp_test'),
    ]

    operations = [
        migrations.CreateModel(
            name='DeviceStatsHourly',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('hour', models.DateTimeField()),
                ('mining_samples', models.IntegerField(default=0)),
                ('first_at', models.DateTimeField(blank=True, null=True)),
                ('last_at', models.DateTimeField(blank=True, null=True)),
                ('hashrate_avg', models.FloatField(blank=True, null=True)),
                ('hashrate_max', models.FloatField(blank=True, null=True)),
                ('hashrate_min', models.FloatField(blank=True, null=True)),
                ('best_difficulty', models.FloatField(blank=True, null=True)),
                ('shares_accepted_min', models.BigIntegerField(blank=True, null=True)),
                ('shares_accepted_max', models.BigIntegerField(blank=True, null=True)),
                ('shares_rejected_min', models.BigIntegerField(blank=True, null=True)),
                ('shares_rejected_max', models.BigIntegerField(blank=True, null=True)),
                ('hardware_samples', models.IntegerField(default=0)),
                ('temperature_avg', models.FloatField(blank=True, null=True)),
                ('temperature_max', models.FloatField(blank=True, null=True)),
                ('temperature_n', models.IntegerField(default=0)),
                ('chip_temperature_avg', models.FloatField(blank=True, null=True)),
                ('chip_temperature_max', models.FloatField(blank=True, null=True)),
                ('chip_temperature_n', models.IntegerField(default=0)),
                ('power_avg', models.FloatField(blank=True, null=True)),
                ('power_max', models.FloatField(blank=True, null=True)),
                ('power_n', models.IntegerField(default=0)),
                ('efficiency_avg', models.FloatField(blank=True, null=True)),
                ('efficiency_n', models.IntegerField(default=0)),
                ('device', models.ForeignKey(db_index=False, on_delete=django.db.models.deletion.CASCADE,
                                             related_name='hourly_stats', to='api.device')),
            ],
            options={
                'db_table': 'device_stats_hourly',
                'ordering': ['hour'],
                'constraints': [models.UniqueConstraint(fields=('device', 'hour'), name='device_stats_hourly_dev_hour')],
            },
        ),
        migrations.CreateModel(
            name='PoolStatsHourly',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('pool_type', models.CharField(max_length=32)),
                ('pool_address', models.CharField(max_length=255)),
                ('hour', models.DateTimeField()),
                ('samples', models.IntegerField(default=0)),
                ('hashrate_1m_avg', models.FloatField(blank=True, null=True)),
                ('hashrate_1h_avg', models.FloatField(blank=True, null=True)),
                ('hashrate_1d_avg', models.FloatField(blank=True, null=True)),
                ('workers_max', models.IntegerField(blank=True, null=True)),
            ],
            options={
                'db_table': 'pool_stats_hourly',
                'ordering': ['hour'],
                'constraints': [models.UniqueConstraint(fields=('pool_type', 'pool_address', 'hour'),
                                                        name='pool_stats_hourly_pool_hour')],
            },
        ),
    ]
