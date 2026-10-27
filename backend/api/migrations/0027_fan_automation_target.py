from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0026_stats_hourly'),
    ]

    # Zielbereich (Nutzer 27.10.): the existing row gets 82–85 °C (start value chosen by the user)
    operations = [
        migrations.AddField(
            model_name='fanautomation',
            name='target_low',
            field=models.IntegerField(default=82),
        ),
        migrations.AddField(
            model_name='fanautomation',
            name='target_high',
            field=models.IntegerField(default=85),
        ),
    ]
