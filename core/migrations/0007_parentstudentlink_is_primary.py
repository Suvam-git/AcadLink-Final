from django.db import migrations, models


def set_initial_primary_links(apps, schema_editor):
    ParentStudentLink = apps.get_model('core', 'ParentStudentLink')
    User = apps.get_model('core', 'User')

    parents = User.objects.filter(role='parent')
    for parent in parents:
        approved_links = ParentStudentLink.objects.filter(
            parent=parent,
            status='approved'
        ).order_by('-requested_at')
        if approved_links.exists() and not approved_links.filter(is_primary=True).exists():
            first_link = approved_links.first()
            first_link.is_primary = True
            first_link.save(update_fields=['is_primary'])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0006_alter_workloadsettings_class_name'),
    ]

    operations = [
        migrations.AddField(
            model_name='parentstudentlink',
            name='is_primary',
            field=models.BooleanField(
                default=False,
                help_text='Primary child shown by default in parent dashboard'
            ),
        ),
        migrations.RunPython(set_initial_primary_links, migrations.RunPython.noop),
    ]
