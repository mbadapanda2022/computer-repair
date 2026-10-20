"""Guards for the staff quick-key feature (static assets).

The quick keys live in static/js/app.js (QUICK_KEYS map + ? help modal) and
static/css/custom.css (kbd badges). A stray '}' once swallowed the first CSS
rule after it, so brace balance is asserted here.
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

STATIC = Path(__file__).resolve().parent.parent / 'static'


class QuickKeyAssetTests(SimpleTestCase):
    def test_app_js_defines_quick_keys(self):
        js = (STATIC / 'js' / 'app.js').read_text(encoding='utf-8')
        self.assertIn('QUICK_KEYS', js)
        self.assertIn('quickKeyHelp', js)
        for marker in ["e.key === '?'", "e.key === '/'"]:
            self.assertIn(marker, js)

    def test_custom_css_balanced_and_has_badge_rules(self):
        css = (STATIC / 'css' / 'custom.css').read_text(encoding='utf-8')
        self.assertEqual(css.count('{'), css.count('}'))
        for marker in ['sidebar-key', 'quick-key-table', 'margin-left: auto']:
            self.assertIn(marker, css)

    def test_no_top_level_stray_brace(self):
        # A '}' at nesting level 0 is what broke badge styling before.
        css = (STATIC / 'css' / 'custom.css').read_text(encoding='utf-8')
        css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
        depth = 0
        for ch in css:
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                self.assertGreaterEqual(depth, 0, 'stray } at top level')
        self.assertEqual(depth, 0)
