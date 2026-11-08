from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0027_fan_automation_target'),
    ]

    # Balance und Pool-Hashrate je Tag (v2.2.9, Nutzer 08.11.): last value of the hour, filled by the data-service
    # rollup (and its one-time back-fill from the remaining raw pool_stats)
    operations = [
        migrations.AddField(
            model_name='poolstatshourly',
            name='balance_last',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='poolstatshourly',
            name='hashrate_1d_last',
            field=models.FloatField(blank=True, null=True),
        ),
    ]
