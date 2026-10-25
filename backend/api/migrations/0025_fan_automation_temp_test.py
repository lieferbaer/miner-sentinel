from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0024_automation_logging'),
    ]

    operations = [
        migrations.AddField(
            model_name='fanautomation',
            name='temp_test_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
