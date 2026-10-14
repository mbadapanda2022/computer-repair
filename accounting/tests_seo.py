"""
SEO essentials tests: robots.txt + dynamic sitemap.

    python manage.py test accounting.tests_seo -v 2
"""
from __future__ import annotations

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from accounting.models import BlogCategory, BlogPost


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class SeoTests(TestCase):

    def setUp(self):
        self.client = Client()

    def _published_post(self, slug='laptop-repair-guide'):
        cat = BlogCategory.objects.create(name=f'Cat {slug}', slug=f'{slug}-cat')
        post = BlogPost.objects.create(
            title=f'Post {slug}', slug=slug, category=cat,
            body='content', status='published',
            published_at=timezone.now(),
        )
        return post

    def test_robots_txt_served_with_sitemap_pointer(self):
        r = self.client.get('/robots.txt')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'text/plain')
        body = r.content.decode()
        self.assertIn('User-agent: *', body)
        self.assertIn('Disallow: /customer/', body)
        self.assertIn('Disallow: /admin/', body)
        self.assertIn('/sitemap.xml', body)

    def test_sitemap_includes_published_posts_only(self):
        post = self._published_post('good-post')
        self._published_post('draft-post')  # noqa — make second, then draft it
        draft = BlogPost.objects.get(slug='draft-post')
        draft.status = 'draft'
        draft.save()

        r = self.client.get('/sitemap.xml')
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn('/blog/good-post/', body)
        self.assertNotIn('/blog/draft-post/', body)
        self.assertIn('/blog/', body)          # listing page
        self.assertIn('good-post-cat', body)   # category page

    def test_sitemap_is_valid_xml_root(self):
        r = self.client.get('/sitemap.xml')
        body = r.content.decode()
        self.assertTrue(body.lstrip().startswith('<?xml'))
        self.assertIn('urlset', body)
