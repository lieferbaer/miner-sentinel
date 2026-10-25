import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


def drop_a15_throttle(apps, schema_editor):
    """The Drosselung of the A15 is abolished (24.10.): one left from before (a manual clock in MHz has no TH/s
    target, the throttle controller cannot regulate it) is removed. No clock is sent, it stays as it is."""
    Device = apps.get_model('api', 'Device')
    now = django.utils.timezone.now().isoformat()
    for dev in Device.objects.all():
        config = dict(dev.connection_config or {})
        st = dict(config.get('throttle') or {})
        active = st.get('active')
        text = ' '.join(str(getattr(dev, k, '') or '') for k in ('name', 'model', 'device_id')).lower()
        a15 = 'nano' not in text and ('a15' in text or '15-' in text)     # as DeviceViewSet._throttle_family
        if not isinstance(active, dict) or not (a15 or active.get('target_ths') is None):
            continue
        st['active'] = None
        st['last_event'] = {'type': 'automation', 'detail': 'Drosselung des A15 abgeschafft', 'at': now,
                            'target_ths': active.get('target_ths'), 'clock_mhz': active.get('clock_mhz')}
        st['stable_since'] = None
        st['rev'] = int(st.get('rev', 0) or 0) + 1      # a throttle controller round read before drops its write
        config['throttle'] = st
        dev.connection_config = config
        dev.save(update_fields=['connection_config'])


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0023_fan_automation_maintenance_at'),
    ]

    operations = [
        migrations.CreateModel(
            name='AutomationCycle',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('recorded_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('phase', models.CharField(max_length=16)),
                ('chip_temp', models.FloatField(blank=True, null=True)),
                ('boards', models.JSONField(blank=True, default=list)),
                ('tendency', models.IntegerField(blank=True, null=True)),
                ('clock_set', models.FloatField(blank=True, null=True)),
                ('clock_reported', models.FloatField(blank=True, null=True)),
                ('points', models.JSONField(blank=True, default=list)),
                ('mode_key', models.CharField(blank=True, default='', max_length=16)),
                ('base_level', models.IntegerField(blank=True, null=True)),
                ('fan_level', models.IntegerField(blank=True, null=True)),
                ('fan_read', models.IntegerField(blank=True, null=True)),
                ('power_w', models.FloatField(blank=True, null=True)),
                ('ghsspd_ths', models.FloatField(blank=True, null=True)),
                ('dh_pct', models.FloatField(blank=True, null=True)),
                ('status', models.CharField(blank=True, default='', max_length=40)),
                ('window_active', models.BooleanField(default=False)),
                ('learn_since', models.DateTimeField(blank=True, null=True)),
                ('pending', models.CharField(blank=True, default='', max_length=40)),
                ('decision', models.CharField(blank=True, default='', max_length=200)),
                ('log', models.BigIntegerField(blank=True, null=True)),
                ('device', models.ForeignKey(db_index=False, on_delete=django.db.models.deletion.CASCADE,
                                             related_name='automation_cycles', to='api.device')),
            ],
            options={
                'db_table': 'automation_cycle',
                'ordering': ['-recorded_at'],
                'indexes': [models.Index(fields=['device', '-recorded_at'], name='automation_cycle_dev_rec_idx'),
                            models.Index(fields=['recorded_at'], name='automation_cycle_rec_idx')],
            },
        ),
        migrations.CreateModel(
            name='AutomationLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('recorded_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('kind', models.CharField(max_length=32)),
                ('source', models.CharField(default='regelung', max_length=16)),
                ('trigger', models.TextField(blank=True, default='')),
                ('phase', models.CharField(blank=True, default='', max_length=16)),
                ('chip_temp', models.FloatField(blank=True, null=True)),
                ('tendency', models.IntegerField(blank=True, null=True)),
                ('base_old', models.IntegerField(blank=True, null=True)),
                ('base_new', models.IntegerField(blank=True, null=True)),
                ('fan_old', models.IntegerField(blank=True, null=True)),
                ('fan_new', models.IntegerField(blank=True, null=True)),
                ('clock_old', models.FloatField(blank=True, null=True)),
                ('clock_new', models.FloatField(blank=True, null=True)),
                ('clock_reported', models.FloatField(blank=True, null=True)),
                ('mode_old', models.CharField(blank=True, default='', max_length=16)),
                ('mode_new', models.CharField(blank=True, default='', max_length=16)),
                ('target', models.CharField(blank=True, default='', max_length=24)),
                ('endpoint', models.CharField(blank=True, default='', max_length=200)),
                ('request', models.TextField(blank=True, default='')),
                ('response', models.TextField(blank=True, default='')),
                ('status', models.CharField(blank=True, default='', max_length=40)),
                ('ok', models.BooleanField(blank=True, null=True)),
                ('error', models.TextField(blank=True, default='')),
                ('attempt', models.IntegerField(default=1)),
                ('api_sent_at', models.DateTimeField(blank=True, null=True)),
                ('api_reply_at', models.DateTimeField(blank=True, null=True)),
                ('api_ms', models.IntegerField(blank=True, null=True)),
                ('takeover_at', models.DateTimeField(blank=True, null=True)),
                ('takeover_s', models.FloatField(blank=True, null=True)),
                ('takeover_state', models.CharField(blank=True, default='', max_length=16)),
                ('temp_before', models.FloatField(blank=True, null=True)),
                ('thermal', models.JSONField(blank=True, default=list)),
                ('details', models.JSONField(blank=True, default=dict)),
                ('device', models.ForeignKey(blank=True, db_index=False, null=True,
                                             on_delete=django.db.models.deletion.CASCADE,
                                             related_name='automation_logs', to='api.device')),
            ],
            options={
                'db_table': 'automation_log',
                'ordering': ['-recorded_at'],
                'indexes': [models.Index(fields=['device', '-recorded_at'], name='automation_log_dev_rec_idx'),
                            models.Index(fields=['kind', '-recorded_at'], name='automation_log_kind_rec_idx'),
                            models.Index(fields=['recorded_at'], name='automation_log_rec_idx')],
            },
        ),
        migrations.RunPython(drop_a15_throttle, migrations.RunPython.noop),
    ]
