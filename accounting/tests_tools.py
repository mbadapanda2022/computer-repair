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
from django.utils.module_loading import import_string

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

    def test_every_subfolder_fragment_is_referenced(self):
        # A pane may split into fragments to keep each file readable
        # (tools/partials/ic_finder/_chip_card.html). The orphan check above
        # only globs the top level, so fragments are pinned here: a fragment no
        # template includes and no view renders is dead markup that drifts out
        # of sync with nothing complaining.
        panes = {p: p.read_text(encoding='utf-8') for p in PARTIAL_DIR.rglob('*.html')}
        sources = ' '.join(list(panes.values()) + [
            (BASE_DIR / 'templates' / 'tools' / 'index.html').read_text(encoding='utf-8'),
            *(p.read_text(encoding='utf-8')
              for p in (BASE_DIR / 'accounting' / 'views').glob('*.py'))])
        referenced = set(re.findall(r"['\"]([\w/-]+\.html)['\"]", sources))
        for path in panes:
            if path.parent == PARTIAL_DIR:
                continue
            rel = str(path.relative_to(BASE_DIR / 'templates')).replace('\\', '/')
            with self.subTest(fragment=rel):
                self.assertIn(rel, referenced)

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
        # ``pane_view`` is the only extra key a tool may carry: it marks a pane
        # that needs server data and is rendered by that dotted view path
        # instead of being included straight from the template.
        for tool in TOOLS:
            with self.subTest(slug=tool.get('slug')):
                self.assertEqual(required - set(tool), set())
                self.assertLessEqual(set(tool) - required, {'pane_view'})
                for key in required:
                    self.assertTrue(str(tool[key]).strip(), f'{key} is blank')
                self.assertIn(tool['category'], CATEGORY_ORDER)
                self.assertTrue(tool['icon'].startswith('bi-'), tool['icon'])
                # An icon missing from the bundled font renders as an empty box.
                self.assertIn(f'.{tool["icon"]}::before', ICON_CSS, tool['icon'])
                view = tool.get('pane_view')
                if view:
                    self.assertTrue(callable(import_string(view)),
                                    f'{view} is not a callable view')

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

    def test_panes_carry_every_field_their_js_reads(self):
        # A tool looks its controls up by id. A typo in one of those ids costs
        # nothing at boot time and leaves the result box blank forever, so the
        # ids named inside each init function have to exist in that pane.
        for name in TOOL_SCRIPTS:
            src = bundle_source(name)
            chunks = re.split(r"TT\.tools\['([^']+)'\] = function \(root\)", src)
            for slug, body in zip(chunks[1::2], chunks[2::2]):
                tool = get_tool(slug)
                self.assertIsNotNone(tool, f'{slug} is in {name} but not the registry')
                partial = partial_source(tool)
                for field_id in sorted(set(
                        re.findall(r"TT\.q\(root,\s*'([A-Za-z0-9_]+)'\)", body))):
                    with self.subTest(bundle=name, slug=slug, field=field_id):
                        self.assertIn(f'id="{field_id}"', partial,
                                      f'{slug} reads #{field_id}, its pane has no such id')

    def test_every_result_row_has_a_label_and_a_value(self):
        # TT.lines() draws r[0] as the label and r[1] as the value, and a row
        # pushed with the array closed one bracket early loses its value as a
        # dropped extra argument — the pane then shows a bare label forever and
        # nothing in the browser or the suite complains. Built one row per line
        # instead, so a closed array on the opening line is the mistake.
        for name in TOOL_SCRIPTS:
            for line in bundle_source(name).splitlines():
                if 'push([' in line:
                    with self.subTest(bundle=name, line=line.strip()[:80]):
                        self.assertNotRegex(line.rstrip(), r'push\(\[.*\],\s*$',
                                            'row array closed before its value')

    def test_panes_do_not_depend_on_view_context(self):
        # A plain pane is swapped in with no context at all, so a template
        # variable inside it renders as an empty string forever and nothing but
        # a careful read of the pane shows that. Only a tool that declares a
        # pane_view — a view that actually supplies context — may use variables,
        # and the contract for its own keys is pinned in tests_ic_finder.py.
        for tool in TOOLS:
            with self.subTest(slug=tool['slug']):
                if tool.get('pane_view'):
                    continue
                self.assertNotIn('{{', partial_source(tool))
        static = {tool['slug'] for tool in TOOLS if not tool.get('pane_view')}
        self.assertIn('subnet', static)
        self.assertIn('ic-finder', {tool['slug'] for tool in TOOLS
                                    if tool.get('pane_view')})

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
            groups = set(re.findall(
                r'data-(?:mode|bands|gstmix|ptlayer|pspath|fstype)="([^"]+)"', src))
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

    def test_shell_hands_the_js_its_fallback_title(self):
        # Pressing Back to /tools/ leaves no tool in the pane, so the shell has
        # to carry the welcome title or the tab keeps naming the previous tool.
        body = self.client.get('/tools/gst-calc/').content.decode()
        self.assertIn('data-index-title="Tech Tools | ', body)

    def test_shell_reboots_a_pane_coming_back_from_history(self):
        # htmx restores a pushed URL out of its own cache, and for that swap it
        # fires no htmx:afterSwap — only htmx:historyRestore. The cached markup
        # still carries data-tt-inited plus the results it rendered last time, so
        # a pane that is not re-booted comes back dead: it ignores every keystroke
        # and shows figures for field values the snapshot no longer holds.
        src = (BASE_DIR / 'static' / 'js' / 'tech-tools.js').read_text(
            encoding='utf-8')

        restore = re.search(
            r"addEventListener\('htmx:historyRestore'[^{]*\{(.*?)\}\);", src, re.S)
        self.assertIsNotNone(restore, 'nothing handles htmx:historyRestore')
        self.assertIn('bootPane(', restore.group(1))

        boot = re.search(r'function bootPane\(target\) \{(.*?)\n    \}', src, re.S)
        self.assertIsNotNone(boot, 'bootPane is missing')
        body = boot.group(1)
        self.assertIn('delete el.dataset[TT.INITED]', body)
        self.assertIn('TT.boot(target)', body)
        self.assertIn('data-default-mode', body)
        self.assertIn('syncChrome(target)', body)

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
