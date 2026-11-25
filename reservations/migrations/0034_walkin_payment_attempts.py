# Walk-in online payments: a payment attempt now says where it came from and which staff member started it.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('reservations', '0033_online_payment_labels'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='paymentattempt',
            name='source',
            field=models.CharField(choices=[('online', 'Online checkout'), ('walkin', 'Walk-in at the shop')], default='online', max_length=6),
        ),
        migrations.AddField(
            model_name='paymentattempt',
            name='staff',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='walkin_payment_attempts', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddConstraint(
            model_name='paymentattempt',
            constraint=models.UniqueConstraint(condition=models.Q(('request_id', ''), _negated=True), fields=('staff', 'request_id'), name='paymentattempt_walkin_one_per_press'),
        ),
    ]
