from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('gowns', '0028_two_gcash_accounts'),
    ]

    operations = [
        migrations.AddField(
            model_name='sitesettings',
            name='late_fee_per_day',
            field=models.PositiveIntegerField(db_default=200, default=200),
        ),
    ]
