from django.db import migrations


LEGACY_TO_NEW = {
    'Awaiting Pickup': 'Pick-up',
    'Picked Up': 'Reserved',
    # 'Returned' is unchanged (same string in both the old and new choice sets).
}


def remap_forward(apps, schema_editor):
    ReservationItem = apps.get_model('reservations', 'ReservationItem')
    for old_value, new_value in LEGACY_TO_NEW.items():
        ReservationItem.objects.filter(stage=old_value).update(stage=new_value)


def remap_backward(apps, schema_editor):
    ReservationItem = apps.get_model('reservations', 'ReservationItem')
    for old_value, new_value in LEGACY_TO_NEW.items():
        ReservationItem.objects.filter(stage=new_value).update(stage=old_value)


class Migration(migrations.Migration):

    dependencies = [
        ('reservations', '0003_alter_reservationitem_stage'),
    ]

    operations = [
        migrations.RunPython(remap_forward, remap_backward),
    ]
