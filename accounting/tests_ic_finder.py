"""
IC Search & Pinout Finder — the one Tech Tool that reads a catalog.

Everything the other tools need is a formula, so their maths runs in the
browser. This tool answers "what is this chip?" from a marking code and
"which chip has these pins?" from a pin table, both of which are questions
about data, so its maths lives in ``accounting.ic_catalog`` and its markup is
rendered by ``accounting.views.ic_finder``. These tests pin the three things
that can silently go wrong here:

* the catalog itself — a pin table with a gap, a wrong function or a stale pin
  is worse than no answer, because a technician wires a board from it;
* the ranking — the finder must never collapse a genuinely ambiguous marking
  code into one confident-looking guess;
* the HTTP surface — public access, HTMX fragments rather than pages, escaped
  echo of what he typed, and no 500 on a stray query parameter.

    venv/Scripts/python.exe manage.py test accounting.tests_ic_finder -v 2
"""
from __future__ import annotations

import re

from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import resolve
from django.utils.html import escape
from django.utils.module_loading import import_string

from accounting.ic_catalog import (DEFAULT_LIMIT, MIN_QUERY_LENGTH, identify,
                                   match_pins, marking_ambiguity,
                                   parse_constraints)
from accounting.management.commands.seed_ic_catalog import UC384X_PINS
from accounting.middleware import AccessControlMiddleware
from accounting.models import IcChip, IcMarking, IcPin, normalize_ic_code
from accounting.tools_registry import get_tool
from accounting.views import ic_finder

BASE_DIR = settings.BASE_DIR
PARTIAL_DIR = BASE_DIR / 'templates' / 'tools' / 'partials'
PUBLIC = dict(ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
              SECURE_SSL_REDIRECT=False)


def seed():
    call_command('seed_ic_catalog', quiet=True)


def chip(part):
    return IcChip.objects.get(part_number=part)


def pins_of(part):
    return {pin.number: pin.function for pin in chip(part).pins.all()}


class IcCatalogTestCase(TestCase):
    """Shared fixture: the catalog exactly as the seed command leaves it."""

    @classmethod
    def setUpTestData(cls):
        seed()


@override_settings(**PUBLIC)
class IcCatalogDataTests(IcCatalogTestCase):
    """The seeded tables have to be trustworthy enough to wire a board from."""

    def test_every_chip_is_numbered_from_one_without_gaps(self):
        for chip_obj in IcChip.objects.prefetch_related('pins'):
            numbers = [pin.number for pin in chip_obj.pins.all()]
            with self.subTest(part=chip_obj.part_number):
                self.assertEqual(numbers, list(range(1, len(numbers) + 1)),
                                 'pin numbers must run 1..n with no gap')
                self.assertEqual(chip_obj.pin_count, len(numbers),
                                 'pin_count disagrees with the pin table')

    def test_every_pin_function_is_a_known_choice(self):
        known = {value for value, _ in IcChip.PIN_FUNCTION_CHOICES}
        for pin in IcPin.objects.select_related('chip'):
            with self.subTest(pin=str(pin)):
                self.assertIn(pin.function, known)
                self.assertTrue(pin.name)

    def test_every_chip_is_useful_on_its_own(self):
        for chip_obj in IcChip.objects.prefetch_related('markings'):
            with self.subTest(part=chip_obj.part_number):
                self.assertTrue(chip_obj.summary.strip())
                self.assertTrue(chip_obj.bench_note.strip())
                self.assertTrue(chip_obj.package.strip())
                self.assertTrue(chip_obj.manufacturer.strip())
                self.assertTrue(chip_obj.datasheet_url)
                self.assertTrue(chip_obj.markings.all(),
                                'a chip with no marking code cannot be found '
                                'from what is printed on it')

    def test_uc3842_pin_table_is_the_datasheet_one(self):
        """Asserted pin by pin: this is the table a technician will follow."""
        self.assertEqual(pins_of('UC3842'),
                         {1: 'comp', 2: 'fb', 3: 'cs', 4: 'freq',
                          5: 'gnd', 6: 'gate', 7: 'vcc', 8: 'ref'})
        self.assertEqual(chip('UC3842').pinout_text.splitlines(),
                         ['UC3842', 'DIP-8, SOP-8', 'Pin | Name | Function',
                          '1 | COMP | Compensation', '2 | FB | Feedback',
                          '3 | CS / ISENSE | Current Sense',
                          '4 | RT / CT | Frequency / Timing', '5 | GND | GND',
                          '6 | OUT | Drive / Gate', '7 | VCC | VCC / Supply',
                          '8 | VREF | Reference'])

    def test_the_384x_family_really_is_pin_compatible(self):
        for part in ('KA3842', 'UC3843', 'UC3844', 'UC3845'):
            with self.subTest(part=part):
                self.assertEqual(pins_of(part), pins_of('UC3842'))
        self.assertEqual([(pin.name, pin.function) for pin in chip('UC3842').pins.all()],
                         [(name, function) for _, name, function, _ in UC384X_PINS])

    def test_a_pin_that_is_not_ground_is_not_called_ground(self):
        # LM317 and LM7805 are the classic miswiring: the middle pin of the 317
        # is the output, of the 78xx it is ground.
        self.assertEqual(pins_of('LM317'), {1: 'fb', 2: 'out', 3: 'in'})
        self.assertEqual(pins_of('LM7805'), {1: 'in', 2: 'gnd', 3: 'out'})
        self.assertEqual(pins_of('TL431'), {1: 'ref', 2: 'other', 3: 'other'})
        self.assertEqual(pins_of('PC817'), {1: 'in', 2: 'in', 3: 'out', 4: 'out'})
        self.assertEqual(pins_of('NE555')[4], 'en')
        self.assertEqual(pins_of('LM2576'), {1: 'vcc', 2: 'out', 3: 'gnd',
                                             4: 'fb', 5: 'en'})

    def test_the_catalog_is_big_enough_to_be_called_a_finder(self):
        self.assertGreaterEqual(IcChip.objects.count(), 10)
        self.assertGreaterEqual(IcMarking.objects.count(), 40)
        self.assertGreaterEqual(IcPin.objects.count(), 80)

    def test_one_marking_code_is_deliberately_shared(self):
        # Ambiguity is a fact about these parts, not a data error: "3842" is
        # printed by more than one maker. If the seed ever stops containing a
        # shared code, the warning banner below goes untested.
        self.assertEqual({marking.chip.part_number
                          for marking in IcMarking.objects.filter(code='3842')},
                         {'UC3842', 'KA3842'})
        self.assertEqual({marking.chip.part_number
                          for marking in IcMarking.objects.filter(code='817')},
                         {'PC817', 'EL817'})

    def test_marking_codes_are_stored_in_the_form_they_are_compared_in(self):
        for marking in IcMarking.objects.all():
            self.assertEqual(marking.norm_code, normalize_ic_code(marking.code))

    def test_seed_is_idempotent(self):
        before = (IcChip.objects.count(), IcMarking.objects.count(),
                  IcPin.objects.count())
        seed()
        self.assertEqual((IcChip.objects.count(), IcMarking.objects.count(),
                          IcPin.objects.count()), before)

    def test_seed_removes_a_pin_the_family_no_longer_ships(self):
        # A hand-added or renamed pin must not survive a re-seed: the finder
        # would keep offering a table that no longer matches the datasheet.
        target = chip('UC3842')
        IcPin.objects.create(chip=target, number=9, name='MYSTERY',
                             function='other')
        IcMarking.objects.create(chip=target, code='TOTALLY-FAKE')
        seed()
        target.refresh_from_db()
        self.assertEqual(target.pin_count, 8)
        self.assertFalse(IcPin.objects.filter(chip=target, number=9).exists())
        self.assertFalse(IcMarking.objects.filter(code='TOTALLY-FAKE').exists())

    def test_seed_touches_nothing_it_does_not_own(self):
        other = IcChip.objects.create(part_number='CUSTOM-ONE',
                                      manufacturer='Bench special',
                                      category='other', package='SOP-6',
                                      pin_count=1, summary='Kept by hand.',
                                      bench_note='Kept by hand.')
        IcPin.objects.create(chip=other, number=1, name='X', function='other')
        seed()
        self.assertTrue(IcChip.objects.filter(pk=other.pk).exists())


class IcSearchRankingTests(IcCatalogTestCase):
    """identify() ranks; it does not guess."""

    def test_full_part_number_wins_and_is_exact(self):
        hits = identify('UC3842')
        self.assertEqual(hits[0].chip.part_number, 'UC3842')
        self.assertEqual(hits[0].confidence, 'exact')
        self.assertEqual(hits[0].reason, 'Part number matches exactly')
        # KA3842 is listed below it because its own description says it is
        # pin-for-pin identical — a drop-in the technician can use, labelled as
        # a text match rather than as if it were the part he typed.
        self.assertEqual([(hit.chip.part_number, hit.confidence)
                          for hit in hits],
                         [('UC3842', 'exact'), ('KA3842', 'possible')])

    def test_typing_is_tolerated(self):
        for messy in ('uc3842', '  UC3842  ', 'UC-3842', 'uc_3842'):
            with self.subTest(query=messy):
                hits = identify(messy)
                self.assertEqual(hits[0].chip.part_number, 'UC3842')
                self.assertEqual(hits[0].confidence, 'exact')

    def test_a_bare_marking_code_shows_every_chip_that_carries_it(self):
        hits = identify('3842')
        self.assertEqual(sorted(hit.chip.part_number for hit in hits),
                         ['KA3842', 'UC3842'])
        for hit in hits:
            with self.subTest(part=hit.chip.part_number):
                self.assertEqual(hit.confidence, 'strong')
                self.assertEqual(hit.reason, 'Marking code matches exactly')

    def test_a_prefix_finds_the_family_not_a_guess(self):
        hits = identify('LM3')
        self.assertEqual(sorted(hit.chip.part_number for hit in hits),
                         ['LM317', 'LM324', 'LM358', 'LM393'])

    def test_a_shared_prefix_is_not_collapsed(self):
        # '817' is printed on both the Sharp and the Everlight coupler.
        hits = identify('817')
        self.assertEqual(sorted(hit.chip.part_number for hit in hits),
                         ['EL817', 'PC817'])

    def test_too_short_a_query_is_not_a_search_yet(self):
        for query in ('', ' ', '5', '-', '3'):
            with self.subTest(query=query):
                self.assertEqual(identify(query), [])
        self.assertEqual(MIN_QUERY_LENGTH, 2)

    def test_an_unknown_code_returns_nothing_instead_of_the_closest_lie(self):
        self.assertEqual(identify('LNK304'), [])
        self.assertEqual(identify('ZZZZ9'), [])

    def test_confidence_and_reason_are_always_present(self):
        allowed = {'exact', 'strong', 'probable', 'possible'}
        for query in ('UC3842', 'lm', '431', '555', '78'):
            for hit in identify(query):
                with self.subTest(query=query, part=hit.chip.part_number):
                    self.assertIn(hit.confidence, allowed)
                    self.assertTrue(hit.reason.strip())
                    self.assertGreater(hit.score, 0)

    def test_the_list_is_capped_and_ordered(self):
        hits = identify('lm')
        self.assertEqual([hit.chip.part_number for hit in hits],
                         ['LM317', 'LM7805', 'LM78L05', 'LM2576', 'LM358',
                          'LM393', 'LM324', 'NE555'],
                         'part-number hits first, smallest package first '
                         'inside a band')
        self.assertEqual(hits[-1].chip.part_number, 'NE555',
                         'it is only here because LM555 is one of its markings, '
                         'so it must rank below the real LM parts')
        self.assertEqual(hits[-1].confidence, 'probable')
        self.assertEqual(len(identify('lm', limit=3)), 3)
        self.assertEqual(DEFAULT_LIMIT, 40)

    def test_cards_are_prefetched_not_queried_one_by_one(self):
        # 40 cards each reading pins and markings is 80 queries per keystroke;
        # the live search box would stall the bench on a slow connection.
        hits = identify('lm')
        with CaptureQueriesContext(connection) as ctx:
            for hit in hits:
                list(hit.chip.pins.all())
                list(hit.chip.markings.all())
        self.assertEqual(len(ctx.captured_queries), 0)

    def test_ambiguity_counts_the_chips_not_the_codes(self):
        self.assertEqual(marking_ambiguity('3842'), 2)
        self.assertEqual(marking_ambiguity('817'), 2)
        self.assertEqual(marking_ambiguity('UC3842'), 1)
        self.assertEqual(marking_ambiguity('nothing'), 0)
        self.assertEqual(marking_ambiguity('4'), 0)


class IcPinMatchTests(IcCatalogTestCase):
    """match_pins() scores what he knows about the pins against each table."""

    def test_a_whole_known_table_is_a_hundred_percent(self):
        matches = match_pins([(1, 'comp'), (2, 'fb'), (3, 'cs'), (4, 'freq'),
                              (5, 'gnd'), (6, 'gate'), (7, 'vcc'), (8, 'ref')])
        self.assertEqual([row.chip.part_number for row in matches],
                         ['KA3842', 'UC3842', 'UC3843', 'UC3844', 'UC3845'])
        for row in matches:
            with self.subTest(part=row.chip.part_number):
                self.assertTrue(row.is_full_match)
                self.assertEqual(row.percent, 100)
                self.assertEqual(row.wrong, [])
                self.assertEqual(row.missing, [])

    def test_partial_knowledge_is_scored_not_discarded(self):
        # VCC on 8 and GND on 4 is the dual op-amp/comparator power pinout; the
        # 384x family powers on 7 and 5, so it must fall out and the 555 must
        # stay listed at half right rather than disappear.
        matches = match_pins([(8, 'vcc'), (4, 'gnd')])
        by_part = {row.chip.part_number: row for row in matches}
        self.assertEqual(sorted(by_part), ['LM358', 'LM393', 'NE555'])
        self.assertEqual(by_part['LM358'].percent, 100)
        self.assertTrue(by_part['LM358'].is_full_match)
        self.assertEqual(by_part['NE555'].percent, 50)
        self.assertEqual(by_part['NE555'].matched, [8])
        self.assertEqual(by_part['NE555'].wrong, [4])
        self.assertEqual([row.percent for row in matches], [100, 100, 50])

    def test_a_third_of_the_pins_wrong_is_shown_as_two_thirds_right(self):
        row = match_pins([(7, 'vcc'), (8, 'ref'), (6, 'drain')])[0]
        self.assertEqual(row.chip.part_number, 'KA3842')
        self.assertEqual(row.percent, 67)
        self.assertFalse(row.is_full_match)
        self.assertEqual(row.wrong, [6])

    def test_a_package_too_small_to_have_that_pin_cannot_match(self):
        # A DIP-4 optocoupler has no pin 8; offering it would be a wrong answer.
        matches = match_pins([(1, 'vcc'), (8, 'gnd')])
        self.assertEqual(matches, [])
        matches = match_pins([(7, 'vcc'), (8, 'ref')])
        for row in matches:
            with self.subTest(part=row.chip.part_number):
                self.assertGreaterEqual(row.chip.pin_count, 8)

    def test_a_function_no_chip_uses_returns_nothing(self):
        self.assertEqual(match_pins([(1, 'drain')]), [])

    def test_no_constraints_is_no_search(self):
        self.assertEqual(match_pins([]), [])
        self.assertEqual(parse_constraints([]), [])

    def test_the_sort_favours_certainty_then_cleanliness_then_small_packages(self):
        matches = match_pins([(8, 'vcc'), (5, 'gnd'), (4, 'en')])
        percents = [row.percent for row in matches]
        self.assertEqual(percents, sorted(percents, reverse=True))
        for row in matches:
            self.assertTrue(row.matched, 'a chip with nothing in common is not shown')

    def test_blank_and_impossible_rows_are_not_constraints(self):
        self.assertEqual(
            parse_constraints([('1', 'vcc'), ('', 'gnd'), ('2', ''),
                               ('abc', 'vcc'), (' 5 ', ' gnd '), ('0', 'vcc'),
                               ('-1', 'gnd'), ('300', 'vcc')]),
            [(1, 'vcc'), (5, 'gnd')],
            'pin numbers come back as ints — they are compared against the '
            'datasheet numbering, and a string would match nothing forever')
        self.assertEqual(parse_constraints([('3', 'nonsense')]), [(3, 'nonsense')],
                         'an unknown function is the caller\'s problem, not a crash')
        self.assertEqual(match_pins(parse_constraints([('3', 'nonsense')])), [])

    def test_the_result_list_is_capped(self):
        self.assertEqual(len(match_pins([(8, 'vcc')], limit=2)), 2)

    def test_checked_pins_read_the_prefetched_rows(self):
        # The card preview asks each match for its pinned rows; if that went to
        # the database, a 40-result search would be 40 more queries per submit.
        matches = match_pins([(7, 'vcc'), (5, 'gnd'), (3, 'out')])
        with CaptureQueriesContext(connection) as ctx:
            for row in matches:
                self.assertTrue(row.checked_pins)
        self.assertEqual(len(ctx.captured_queries), 0)

    def test_checked_pins_are_the_constraints_in_pin_order(self):
        row = match_pins([(7, 'vcc'), (3, 'out'), (5, 'gnd')])[0]
        self.assertEqual([pin.number for pin in row.checked_pins], [3, 5, 7],
                         'number order, not the order he typed the rows in')
        self.assertEqual({pin.number for pin in row.checked_pins},
                         set(row.matched) | set(row.wrong))


@override_settings(**PUBLIC)
class IcFinderEndpointTests(IcCatalogTestCase):
    """The five URLs behind the pane: public, GET-only, fragment-or-page."""

    HX = {'HX-Request': 'true'}

    def setUp(self):
        self.client = Client()

    def test_endpoints_are_public(self):
        for name in ('ic_search', 'ic_pin_match', 'ic_pinout'):
            self.assertIn(name, AccessControlMiddleware.PUBLIC_URL_NAMES)

    def test_the_literal_routes_cannot_be_shadowed_by_the_slug_route(self):
        # /tools/<slug>/ would happily swallow "ic" as a tool name, which is
        # how a search endpoint quietly turns into a 404 tool page.
        self.assertEqual(resolve('/tools/ic/search/').func.__name__, 'ic_search')
        self.assertEqual(resolve('/tools/ic/pin-match/').func.__name__,
                         'ic_pin_match')
        self.assertEqual(resolve('/tools/ic/7/pinout/').func.__name__, 'ic_pinout')
        self.assertEqual(resolve('/tools/ic-finder/').func.__name__, 'tools_detail')

    def test_page_load_renders_the_shell_around_the_pane(self):
        body = self.client.get('/tools/ic-finder/').content.decode()
        self.assertIn('<!DOCTYPE html>', body)
        self.assertIn('id="ttSidebar"', body)
        self.assertIn('data-tool="ic-finder"', body)
        self.assertIn(escape(get_tool('ic-finder')['seo_title']), body)
        self.assertIn(escape(get_tool('ic-finder')['seo_description']), body)

    def test_hx_request_gets_the_pane_only(self):
        body = self.client.get('/tools/ic-finder/', headers=self.HX).content.decode()
        self.assertIn('data-tool="ic-finder"', body)
        self.assertNotIn('<!DOCTYPE html>', body)
        self.assertNotIn('ttSidebar', body)

    def test_neither_result_area_is_ever_left_blank(self):
        # Both panels open on the same friendly states the server returns after
        # a search, so switching tabs or pressing Clear shows a message, never
        # an empty box.
        body = self.client.get('/tools/ic-finder/', headers=self.HX).content.decode()
        self.assertIn('Chip in haath?', body)
        self.assertIn('Kam se kam', body)

    def test_the_popular_chips_are_listed_once(self):
        expected = len(list(IcChip.objects.filter(popular=True)[:8]))
        body = self.client.get('/tools/ic-finder/', headers=self.HX).content.decode()
        self.assertEqual(body.count('data-ic-pick='), expected)
        self.assertGreater(expected, 0)

    def test_the_search_endpoint_returns_a_fragment_with_candidates(self):
        body = self.client.get('/tools/ic/search/', {'q': '3842'},
                               headers=self.HX).content.decode()
        self.assertIn('UC3842', body)
        self.assertIn('KA3842', body)
        self.assertIn('Same marking code 2 ICs par hota hai', body)
        self.assertIn('ic-conf', body)
        self.assertNotIn('<!DOCTYPE html>', body)

    def test_an_exact_hit_does_not_shout_about_ambiguity(self):
        body = self.client.get('/tools/ic/search/', {'q': 'UC3842'},
                               headers=self.HX).content.decode()
        self.assertNotIn('ic-warning', body)

    def test_the_typed_query_is_escaped_on_the_way_back(self):
        # The summary line repeats what he typed; unescaped, the search box
        # becomes an XSS handler on a page anyone can visit.
        body = self.client.get('/tools/ic/search/',
                               {'q': '<script>alert(1)</script>'},
                               headers=self.HX).content.decode()
        self.assertNotIn('<script>alert(1)', body)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', body)

    def test_a_marking_code_with_letters_and_dashes_is_searchable_as_typed(self):
        body = self.client.get('/tools/ic/search/', {'q': '78-L05'},
                               headers=self.HX).content.decode()
        self.assertIn('LM78L05', body)

    def test_the_search_endpoint_answers_a_missing_or_boring_query_politely(self):
        for query in ('', '  ', 'x'):
            with self.subTest(query=query):
                r = self.client.get('/tools/ic/search/', {'q': query},
                                    headers=self.HX)
                self.assertEqual(r.status_code, 200)
                self.assertIn('Chip in haath?', r.content.decode())

    def test_an_unknown_code_offers_the_other_mode(self):
        body = self.client.get('/tools/ic/search/', {'q': 'QQQQ'},
                               headers=self.HX).content.decode()
        self.assertIn('se koi IC match nahi hua', body)
        self.assertIn('data-ic-goto-tab="pins"', body)

    def test_the_pin_endpoint_scores_the_rows_he_filled(self):
        body = self.client.get('/tools/ic/pin-match/',
                               {'pin': ['7', '5'], 'function': ['vcc', 'gnd']},
                               headers=self.HX).content.decode()
        self.assertIn('100% match', body)
        self.assertIn('UC3842', body)

    def test_the_pin_endpoint_ignores_half_filled_rows(self):
        body = self.client.get('/tools/ic/pin-match/',
                               {'pin': ['7', '', 'zz'],
                                'function': ['vcc', 'gnd', '']},
                               headers=self.HX).content.decode()
        self.assertIn('1 pin constraint', body)
        self.assertIn('100% match', body)

    def test_the_pin_endpoint_without_constraints_still_answers(self):
        r = self.client.get('/tools/ic/pin-match/', headers=self.HX)
        self.assertEqual(r.status_code, 200)
        self.assertIn('Kam se kam', r.content.decode())

    def test_the_pinout_card_carries_the_table_diagram_and_copy_text(self):
        target = chip('UC3842')
        body = self.client.get(f'/tools/ic/{target.pk}/pinout/',
                               headers=self.HX).content.decode()
        self.assertIn('COMP', body)
        self.assertIn('VREF', body)
        self.assertIn('Copy pinout', body)
        self.assertIn('ic-copy-source', body)
        self.assertIn(escape(target.bench_note), body)
        # 8 pins in the diagram and the same 8 rows in the table, joined by
        # data-pin so hovering one lights the other.
        self.assertEqual(body.count('data-pin='), 16)

    def test_a_pinout_opens_with_the_verified_pins_marked(self):
        # Pin Match already told him 7 and 5 agree; the card has to keep that
        # work instead of making him compare two lists by eye.
        target = chip('UC3842')
        body = self.client.get(f'/tools/ic/{target.pk}/pinout/',
                               {'matched': '5,7'}, headers=self.HX).content.decode()
        self.assertEqual(body.count('ic-hit'), 4,
                         'one diagram pin and one table row per matched pin')
        self.assertIn('aapke pin match se', body)

    def test_a_pinout_opened_from_a_search_does_not_claim_a_match(self):
        # The legend key says "these are the pins you matched". Opened from IC
        # Search there is no match, so the key would be a lie about a highlight
        # that is not on screen.
        target = chip('UC3842')
        body = self.client.get(f'/tools/ic/{target.pk}/pinout/',
                               headers=self.HX).content.decode()
        self.assertNotIn('aapke pin match se', body)
        self.assertNotIn('ic-key-hit', body)
        self.assertIn('notch/dot = pin 1', body,
                      'the pin-1 key is always true, so it always stays')

    def test_a_match_card_previews_the_pins_it_checked_not_the_first_six(self):
        # On a 384x the evidence is pins 7 and 5; a preview cut at six would
        # show the two pins nobody asked about and hide the answer.
        body = self.client.get('/tools/ic/pin-match/',
                               {'pin': ['7', '5'], 'function': ['vcc', 'gnd']},
                               headers=self.HX).content.decode()
        card = body.split('<article', 2)[1]
        self.assertIn('ic-hit', card)
        preview = card.split('ic-pin-preview', 1)[1].split('ic-card-actions')[0]
        self.assertIn('<b>7</b>', preview)
        self.assertIn('<b>5</b>', preview)
        self.assertNotIn('<b>1</b>', preview,
                         'unchecked pins do not belong in the evidence row')

    def test_a_search_card_shows_the_whole_small_package(self):
        # Hiding pin 8 of a DIP-8 makes the catalog look thinner than it is,
        # and the pins he is most likely hunting (supply, reference) are the
        # last ones.
        body = self.client.get('/tools/ic/search/', {'q': 'UC3842'},
                               headers=self.HX).content.decode()
        card = body.split('<article', 2)[1]
        preview = card.split('ic-pin-preview', 1)[1].split('ic-card-actions')[0]
        for number in ('1', '2', '3', '4', '5', '6', '7', '8'):
            self.assertIn(f'<b>{number}</b>', preview)
        self.assertNotIn('ic-pin-more', preview,
                         'an 8-pin package has nothing hidden')

    def test_a_malformed_matched_parameter_is_ignored(self):
        target = chip('TL431')
        for matched in ('', 'abc', '1;2', '-3'):
            with self.subTest(matched=matched):
                r = self.client.get(f'/tools/ic/{target.pk}/pinout/',
                                    {'matched': matched}, headers=self.HX)
                self.assertEqual(r.status_code, 200)
                self.assertNotIn('ic-hit', r.content.decode())

    def test_an_unknown_chip_id_is_a_404_not_a_500(self):
        for pk in (999999, 0, 'abc'):
            with self.subTest(pk=pk):
                r = self.client.get(f'/tools/ic/{pk}/pinout/', headers=self.HX)
                self.assertEqual(r.status_code, 404)

    def test_a_chip_with_no_pins_yet_still_renders_a_card(self):
        bare = IcChip.objects.create(part_number='HAND-Y', manufacturer='X',
                                     category='other', package='SOP-8',
                                     pin_count=0, summary='No table.',
                                     bench_note='No table.')
        r = self.client.get(f'/tools/ic/{bare.pk}/pinout/', headers=self.HX)
        self.assertEqual(r.status_code, 200)
        self.assertIn('catalog me nahi hain', r.content.decode())

    def test_every_endpoint_refuses_a_post(self):
        target = chip('UC3842')
        for url in ('/tools/ic-finder/', '/tools/ic/search/',
                    '/tools/ic/pin-match/', f'/tools/ic/{target.pk}/pinout/'):
            with self.subTest(url=url):
                r = self.client.post(url, {})
                self.assertEqual(r.status_code, 405)
                self.assertIn('GET', r['Allow'])

    def test_the_row_count_parameter_cannot_break_or_bloat_the_pane(self):
        for rows, expected in (('', 3), ('abc', 3), ('0', 1), ('99', 12),
                               ('-5', 1), ('7', 7)):
            with self.subTest(rows=rows):
                body = self.client.get('/tools/ic-finder/', {'rows': rows},
                                       headers=self.HX).content.decode()
                self.assertEqual(body.count('name="pin"'), expected)


@override_settings(**PUBLIC)
class IcFinderWiringTests(IcCatalogTestCase):
    """The new tool is inside the Tech Tools contract, not beside it."""

    HX = {'HX-Request': 'true'}

    def setUp(self):
        self.client = Client()

    def test_the_registry_and_the_view_agree_on_the_pane(self):
        tool = get_tool('ic-finder')
        self.assertIsNotNone(tool)
        self.assertEqual(tool['partial'], ic_finder.PANE_TEMPLATE)
        self.assertEqual(import_string(tool['pane_view']), ic_finder.ic_finder)

    def test_the_pane_uses_only_fragments_the_views_render(self):
        templates = {ic_finder.RESULTS_TEMPLATE, ic_finder.PIN_RESULTS_TEMPLATE,
                     ic_finder.PINOUT_TEMPLATE}
        for name in templates:
            with self.subTest(template=name):
                self.assertTrue((BASE_DIR / 'templates' / name).is_file())

    def test_the_finder_is_reachable_from_the_index_and_the_sitemap(self):
        body = self.client.get('/tools/').content.decode()
        self.assertIn('data-slug="ic-finder"', body)
        self.assertIn('href="/tools/ic-finder/"', body)
        sitemap = self.client.get('/sitemap.xml').content.decode()
        self.assertIn('<loc>http://testserver/tools/ic-finder/</loc>', sitemap)

    def test_a_search_card_opens_its_pinout_without_inline_evaluation(self):
        # hx-vals="js:…" is evaluated by htmx in a Function whose only argument
        # is the event, so an element variable is undefined and the click throws
        # before any request goes out. The pin numbers are known when the card
        # is rendered, so they belong in the URL.
        body = self.client.get('/tools/ic/search/', {'q': '3842'},
                               headers=self.HX).content.decode()
        self.assertNotIn('hx-vals', body)
        self.assertIn('/pinout/"', body,
                      'a search card asks for the whole pinout, so the URL must '
                      'not carry an empty ?matched=')

    def test_a_pin_match_card_links_to_a_pinout_that_keeps_the_verified_pins(self):
        body = self.client.get('/tools/ic/pin-match/',
                               {'pin': ['7', '5'], 'function': ['vcc', 'gnd']},
                               headers=self.HX).content.decode()
        self.assertIn('/pinout/?matched=5,7"', body,
                      'the expand URL must carry the pins this card matched')

    def test_every_hook_the_module_uses_exists_in_the_markup(self):
        # A renamed data attribute is a silent dead button: nothing errors, the
        # feature simply stops working, and only a click at the bench finds it.
        js = (BASE_DIR / 'static' / 'js' / 'tools'
              / 'ic-finder.js').read_text(encoding='utf-8')
        markup = ''
        for path in [PARTIAL_DIR / 'ic-finder.html'] + \
                   list((PARTIAL_DIR / 'ic_finder').glob('*.html')):
            markup += path.read_text(encoding='utf-8')
        hooks = set(re.findall(r"data-([a-z][a-z0-9-]*)", js))
        self.assertIn('ic-expand', hooks)
        self.assertIn('ictab', hooks)
        for hook in sorted(hooks):
            with self.subTest(hook=hook):
                self.assertIn(f'data-{hook}', markup)
        for target in ('icSearchPanel', 'icPinPanel', 'icResults', 'icPinResults',
                       'icQuery', 'icPinForm', 'icPinRows', 'icAddRow'):
            with self.subTest(target=target):
                self.assertIn(target, markup)

    def test_the_modules_selectors_are_the_classes_the_templates_write(self):
        # Same failure mode as the data hooks: a class renamed on one side only
        # leaves the other side querying an empty node list.
        js = (BASE_DIR / 'static' / 'js' / 'tools'
              / 'ic-finder.js').read_text(encoding='utf-8')
        markup = ''
        for path in [PARTIAL_DIR / 'ic-finder.html'] + \
                   list((PARTIAL_DIR / 'ic_finder').glob('*.html')):
            markup += path.read_text(encoding='utf-8')
        for cls in sorted(set(re.findall(r"classList\.contains\('([\w-]+)'\)", js))
                          | set(re.findall(r"querySelector\('\.([\w-]+)", js))):
            with self.subTest(class_name=cls):
                self.assertIn(cls, markup)

    def test_every_class_the_pane_writes_is_styled_or_scripted(self):
        # An unstyled class is how a page ends up with a block of text that
        # looks like a bug nobody can point at: nothing errors, it just sits
        # there unformatted. Either the stylesheet paints it or the module
        # reads it; a class that does neither is a leftover.
        css = (BASE_DIR / 'static' / 'css' / 'tech-tools.css').read_text(encoding='utf-8')
        js = (BASE_DIR / 'static' / 'js' / 'tools'
              / 'ic-finder.js').read_text(encoding='utf-8')
        markup = ''
        for path in [PARTIAL_DIR / 'ic-finder.html'] + \
                   list((PARTIAL_DIR / 'ic_finder').glob('*.html')):
            markup += path.read_text(encoding='utf-8')
        for cls in sorted({tok for match in re.findall(r'class="([^"]*)"', markup)
                           for tok in re.split(r'[\s{}]+', match)
                           if re.fullmatch(r'ic-[a-z0-9-]+', tok)}):
            with self.subTest(class_name=cls):
                self.assertTrue(f'.{cls}' in css or cls in js,
                                f'{cls} is written by a template but styled by '
                                f'neither the CSS nor the module')
