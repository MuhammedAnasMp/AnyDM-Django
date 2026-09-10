from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('settings', '0006_systemsettings_creator_commission_default_term_months_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='systemsettings',
            name='instant_payout_commission_percentage',
            field=models.DecimalField(decimal_places=2, default=3.00, max_digits=5),
        ),
    ]
