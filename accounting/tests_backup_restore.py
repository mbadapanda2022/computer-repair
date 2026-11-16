"""Regression tests for the JSON backup/restore engine."""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase

from accounting.models import Contact, NotificationPreference
from backup_restore_common import (
    _serialize_value,
    backup_all_data,
    restore_from_json,
)


class SerializeValueTests(TestCase):
    def test_dict_and_list_stay_structured(self):
        payload = {'a': 1, 'b': [1, 2, 3]}
        self.assertEqual(_serialize_value(payload), payload)
        self.assertEqual(_serialize_value([1, 2]), [1, 2])

    def test_scalars_preserved(self):
        self.assertEqual(_serialize_value('x'), 'x')
        self.assertEqual(_serialize_value(7), 7)
        self.assertIsNone(_serialize_value(None))


class RestoreEngineTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username='restore-test', password='x'
        )
        self.contact = Contact.objects.create(
            name='Restore Contact', phone='9000000001'
        )
        self.prefs = NotificationPreference.objects.get(user=self.user)
        self.prefs.categories = {'sales': True, 'stock': False}
        self.prefs.save()

    def test_round_trip_restores_rows_and_json(self):
        backup = backup_all_data()
        contact_pk = self.contact.pk

        self.contact.delete()
        Contact.objects.create(name='Should Vanish', phone='9000000002')
        NotificationPreference.objects.all().delete()

        restored = restore_from_json(backup)

        self.assertGreater(restored, 0)
        self.assertTrue(Contact.objects.filter(pk=contact_pk).exists())
        self.assertFalse(Contact.objects.filter(name='Should Vanish').exists())
        prefs = NotificationPreference.objects.get(user=self.user)
        self.assertEqual(prefs.categories, {'sales': True, 'stock': False})

    def test_invalid_backup_is_rejected_before_wipe(self):
        with self.assertRaises(ValueError):
            restore_from_json({'metadata': {}})
        self.assertTrue(Contact.objects.filter(pk=self.contact.pk).exists())

    def test_unknown_only_backup_is_rejected_before_wipe(self):
        with self.assertRaises(ValueError):
            restore_from_json({'data': {'nope.Missing': []}})
        self.assertTrue(Contact.objects.filter(pk=self.contact.pk).exists())
