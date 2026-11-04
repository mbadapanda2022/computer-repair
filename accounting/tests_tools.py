"""
Tech Tools (/tools/) — the free public calculators.

The feature is registry driven, so the guarantee that matters is that its three
halves agree: ``accounting.tools_registry`` (Python), ``templates/tools/``
(markup) and ``static/js/tools/`` (behaviour). These tests also pin the public
access, the HTMX "pane only" contract, per-tool SEO metadata and the
robots/sitemap integration.

    venv/Scripts/python.exe manage.py test accounting.tests_tools -v 2
"""
from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.test import Client, TestCase, override_settings
from django.utils.html import escape

from accounting.middleware import AccessControlMiddleware
from accounting.tools_registry import (CATEGORY_ORDER, TOOLS, TOOL_SCRIPTS,
                                       get_tool, grouped_tools)

BASE_DIR = Path(settings.BASE_DIR)
PARTIAL_DIR = BASE_DIR / 'templates' / 'tools' / 'partials'
ICON_CSS = (BASE_DIR / 'static' / 'bootstrap-icons'
            / 'bootstrap-icons.css').read_text(encoding='utf-8')

PUBLIC = dict(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
              SECURE_SSL_REDIRECT=False)


def partial_source(tool):
    return (BASE_DIR / 'templates' / tool['partial']).read_text(encoding='utf-8')


def bundle_source(name):
    return (BASE_DIR / 'static' / 'js' / 'tools' / name).read_text(encoding='utf-8')


@override_settings(**PUBLIC)
class TechToolsRegistryTests(TestCase):
    """The registry is the single source of truth — nothing dangling."""

    def test_every_tool_has_a_partial_on_disk(self):
        for tool in TOOLS:
            with self.subTest(slug=tool['slug']):
                self.assertTrue((BASE_DIR / 'templates' / tool['partial']).is_file(),
                                f'{tool["partial"]} is missing')

    def test_no_orphan_partials(self):
        registered = {Path(tool['partial']).name for tool in TOOLS}
        on_disk = {p.name for p in PARTIAL_DIR.glob('*.html')}
        self.assertEqual(on_disk - registered, {'_welcome.html'})

    def test_every_tool_is_registered_in_a_js_bundle(self):
        sources = {name: bundle_source(name) for name in TOOL_SCRIPTS}
        declared = set()
        for src in sources.values():
            declared |= set(re.findall(r"TT\.tools\[\s*'([^']+)'\s*\]", src))

        for tool in TOOLS:
            with self.subTest(slug=tool['slug']):
                self.assertIn(tool['slug'], declared,
                              f"no TT.tools['{tool['slug']}'] init function")
                # The pane must carry its own slug: that is what TT.boot and the
                # shell's title lookup match on after an HTMX swap.
                self.assertIn(f'data-tool="{tool["slug"]}"', partial_source(tool))

    def test_no_js_tool_is_unregistered(self):
        known = {tool['slug'] for tool in TOOLS}
        for name in TOOL_SCRIPTS:
            for slug in re.findall(r"TT\.tools\[\s*'([^']+)'\s*\]", bundle_source(name)):
                with self.subTest(bundle=name, slug=slug):
                    self.assertIn(slug, known)

    def test_registry_fields_are_complete(self):
        required = {'slug', 'name', 'icon', 'category', 'blurb', 'partial',
                    'seo_title', 'seo_description'}
        for tool in TOOLS:
            with self.subTest(slug=tool.get('slug')):
                self.assertEqual(set(tool), required)
                for key in required:
                    self.assertTrue(str(tool[key]).strip(), f'{key} is blank')
                self.assertIn(tool['category'], CATEGORY_ORDER)
                self.assertTrue(tool['icon'].startswith('bi-'), tool['icon'])
                # An icon missing from the bundled font renders as an empty box.
                self.assertIn(f'.{tool["icon"]}::before', ICON_CSS, tool['icon'])

    def test_grouping_covers_every_category_in_order(self):
        groups = grouped_tools()
        self.assertEqual([category for category, _ in groups], list(CATEGORY_ORDER))
        self.assertEqual(sum(len(items) for _, items in groups), len(TOOLS))

    def test_get_tool_rejects_unknown_and_blanks(self):
        self.assertIsNone(get_tool('not-a-tool'))
        self.assertIsNone(get_tool(''))
        self.assertIsNone(get_tool('../etc/passwd'))
        self.assertEqual(get_tool('gst-calc')['slug'], 'gst-calc')

    def test_tool_js_never_reaches_off_site(self):
        for name in TOOL_SCRIPTS + ('common.js',):
            with self.subTest(bundle=name):
                self.assertFalse(re.search(r'https?://', bundle_source(name)),
                                 'tools must work with no network at all')


@override_settings(**PUBLIC)
class TechToolsPaneMarkupTests(TestCase):
    """Every pane is a self-contained fragment the shell can swap in."""

    def test_panes_are_fragments_not_pages(self):
        for tool in TOOLS:
            src = partial_source(tool)
            with self.subTest(slug=tool['slug']):
                self.assertNotIn('{% extends', src)
                self.assertNotIn('{% block', src)
                self.assertNotIn('<!DOCTYPE', src)
                self.assertNotIn('<html', src)

    def test_panes_have_a_live_result_and_a_reset(self):
        for tool in TOOLS:
            src = partial_source(tool)
            with self.subTest(slug=tool['slug']):
                self.assertIn('aria-live="polite"', src)
                self.assertIn('data-clear', src)
                self.assertIn('tt-result-box', src)

    def test_panes_do_not_depend_on_view_context(self):
        # A pane swapped in by HTMX gets no context, so nothing inside a tool
        # pane may read a template variable except the url tag.
        for tool in TOOLS:
            with self.subTest(slug=tool['slug']):
                self.assertNotIn('{{ tool.', partial_source(tool))

    def test_every_icon_the_pages_draw_is_bundled(self):
        # bootstrap-icons ships locally; a class the font does not define shows
        # up as an empty square in the sidebar or in a pane heading.
        templates = list(PARTIAL_DIR.glob('*.html')) + [
            BASE_DIR / 'templates' / 'tools' / 'index.html']
        for path in templates:
            src = path.read_text(encoding='utf-8')
            for icon in sorted(set(re.findall(r'\bbi-([a-z0-9-]+)', src))):
                with self.subTest(file=path.name, icon=icon):
                    self.assertIn(f'.bi-{icon}::before', ICON_CSS)

    def test_every_pane_links_back_to_the_index(self):
        for tool in TOOLS:
            with self.subTest(slug=tool['slug']):
                self.assertIn('tt-alltools', partial_source(tool))
                self.assertIn("{% url 'tools:tools_index' %}", partial_source(tool))

    def test_mode_groups_declare_their_default(self):
        # Clear re-clicks [data-default-mode] so private tool state (which mode
        # is active) is restored too; one default per group is required.
        for tool in TOOLS:
            src = partial_source(tool)
            groups = set(re.findall(r'data-(?:mode|bands|gstmix)="([^"]+)"', src))
            if not groups:
                continue
            with self.subTest(slug=tool['slug']):
                self.assertGreaterEqual(src.count('data-default-mode'), 1)
                for button in re.findall(r'<button[^>]*data-default-mode[^>]*>', src):
                    self.assertIn('active', button)


@override_settings(**PUBLIC)
class TechToolsPublicAccessTests(TestCase):
    """Anonymous visitors reach every tool; nothing redirects to login."""

    def setUp(self):
        self.client = Client()

    def test_tools_are_whitelisted_as_public(self):
        self.assertIn('tools_index', AccessControlMiddleware.PUBLIC_URL_NAMES)
        self.assertIn('tools_detail', AccessControlMiddleware.PUBLIC_URL_NAMES)

    def test_index_anonymous_is_not_login_gated(self):
        r = self.client.get('/tools/')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('/login/', r.get('Location', ''))

    def test_index_lists_every_tool(self):
        body = self.client.get('/tools/').content.decode()
        for tool in TOOLS:
            with self.subTest(slug=tool['slug']):
                self.assertIn(f'data-slug="{tool["slug"]}"', body)
                self.assertIn(f'>{escape(tool["name"])}<', body)

    def test_tool_page_renders_the_full_shell(self):
        for tool in TOOLS:
            with self.subTest(slug=tool['slug']):
                body = self.client.get(f'/tools/{tool["slug"]}/').content.decode()
                self.assertIn('<!DOCTYPE html>', body)
                self.assertIn('id="ttSidebar"', body)
                self.assertIn(f'data-tool="{tool["slug"]}"', body)
                self.assertNotIn('tt-welcome-title', body)

    def test_welcome_pane_offers_a_quick_pick(self):
        body = self.client.get('/tools/').content.decode()
        self.assertIn('id="ttQuickPick"', body)
        for tool in TOOLS:
            with self.subTest(slug=tool['slug']):
                self.assertIn(f'/tools/{tool["slug"]}/', body)

    def test_hx_request_returns_only_the_pane(self):
        for tool in TOOLS:
            with self.subTest(slug=tool['slug']):
                r = self.client.get(f'/tools/{tool["slug"]}/',
                                    headers={'HX-Request': 'true'})
                self.assertEqual(r.status_code, 200)
                self.assertTrue(r['Content-Type'].startswith('text/html'))
                body = r.content.decode()
                self.assertIn(f'data-tool="{tool["slug"]}"', body)
                self.assertNotIn('<!DOCTYPE html>', body)
                self.assertNotIn('ttSidebar', body)

    def test_unknown_slug_is_404_for_both_navigation_modes(self):
        for headers in ({}, {'HX-Request': 'true'}):
            with self.subTest(headers=headers):
                r = self.client.get('/tools/time-travel/', headers=headers)
                self.assertEqual(r.status_code, 404)

    def test_encoded_traversal_cannot_reach_a_pane(self):
        # Unresolvable paths are handled site-wide (redirect or 404); the
        # guarantee is that no tool markup is ever served for them.
        for path in ('/tools/..%2f..%2fetc%2fpasswd/', '/tools/%2e%2e/tools/',
                     '/tools/resistor-color%00/'):
            with self.subTest(path=path):
                r = self.client.get(path)
                self.assertNotEqual(r.status_code, 200)
                self.assertNotIn(b'data-tool=', r.content)

    def test_views_are_get_only(self):
        for url in ('/tools/', '/tools/subnet/'):
            with self.subTest(url=url):
                r = self.client.post(url, {})
                self.assertEqual(r.status_code, 405)
                self.assertIn('GET', r['Allow'])
                self.assertNotIn('POST', r['Allow'])

    def test_seo_metadata_is_per_tool(self):
        for tool in TOOLS:
            with self.subTest(slug=tool['slug']):
                body = self.client.get(f'/tools/{tool["slug"]}/').content.decode()
                self.assertIn(f'<title>{escape(tool["seo_title"])}', body)
                self.assertIn(escape(tool['seo_description']), body)
                self.assertIn(f'<link rel="canonical" href="http://testserver'
                              f'/tools/{tool["slug"]}/"', body)

    def test_index_has_its_own_title_and_promise(self):
        body = self.client.get('/tools/').content.decode()
        self.assertIn('<title>Tech Tools', body)
        self.assertIn('Nothing is uploaded', body)

    def test_shell_loads_local_scripts_in_registry_order(self):
        body = self.client.get('/tools/').content.decode()
        positions = [body.index(f'/static/js/tools/{name}') for name in TOOL_SCRIPTS]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('/static/js/tools/common.js', body)
        self.assertIn('/static/js/tech-tools.js', body)
        self.assertIn('/static/css/tech-tools.css', body)


@override_settings(**PUBLIC)
class TechToolsSeoIntegrationTests(TestCase):

    def test_robots_allows_tools(self):
        body = self.client.get('/robots.txt').content.decode()
        self.assertIn('Allow: /tools/', body)

    def test_sitemap_lists_the_index_and_every_tool(self):
        body = self.client.get('/sitemap.xml').content.decode()
        self.assertIn('<loc>http://testserver/tools/</loc>', body)
        for tool in TOOLS:
            with self.subTest(slug=tool['slug']):
                self.assertIn(f'<loc>http://testserver/tools/{tool["slug"]}/</loc>',
                              body)

    def test_public_pages_link_to_the_tools(self):
        for url in ('/', '/blog/'):
            with self.subTest(url=url):
                body = self.client.get(url).content.decode()
                self.assertIn('href="/tools/"', body)
