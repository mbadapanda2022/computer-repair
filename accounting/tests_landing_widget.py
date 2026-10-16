"""
Landing page floating contact widget (WhatsApp + Call) tests.

    python manage.py test accounting.tests_landing_widget -v 2
"""
from __future__ import annotations

from django.test import Client, TestCase, override_settings

from accounting.models import CompanyProfile


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class FloatWidgetTests(TestCase):

    def setUp(self):
        self.client = Client()
        CompanyProfile.objects.all().delete()

    def _get_body(self):
        r = self.client.get('/')
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_widget_shown_with_whatsapp_and_phone(self):
        CompanyProfile.objects.create(
            name='A1 Computer Solutions',
            phone='7008601963', whatsapp_number='918895745441')
        body = self._get_body()
        self.assertIn('float-widget', body)
        self.assertIn('https://wa.me/918895745441?text=', body)
        self.assertIn('tel:7008601963', body)

    def test_widget_falls_back_to_phone_for_whatsapp(self):
        CompanyProfile.objects.create(
            name='A1 Computer Solutions',
            phone='+917008601963', whatsapp_number='')
        body = self._get_body()
        self.assertIn('float-widget', body)
        # '+' and separators stripped for wa.me
        self.assertIn('https://wa.me/917008601963?text=', body)
        self.assertIn('tel:+917008601963', body)

    def test_widget_hidden_without_any_contact(self):
        CompanyProfile.objects.create(name='A1 Computer Solutions', phone='', whatsapp_number='')
        body = self._get_body()
        self.assertNotIn('float-widget', body)

    def test_whatsapp_link_is_secure_new_tab(self):
        CompanyProfile.objects.create(
            name='A1 Computer Solutions',
            phone='7008601963', whatsapp_number='918895745441')
        body = self._get_body()
        segment = body[body.index('float-btn-wa'):]
        wa_tag = segment[:segment.index('>') + 1]
        self.assertIn('target="_blank"', wa_tag)
        self.assertIn('rel="noopener', wa_tag)
