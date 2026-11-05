"""
IC Search & Pinout Finder — the public /tools/ic-finder/ page.

Two jobs, one page. "Identify this chip" takes a part number or the two-to-five
character marking code actually printed on the package and returns ranked
candidates. "Find by pins" takes the reverse question — pin 1 is VCC, pin 5
drives a MOSFET gate — and returns the chips whose pinout agrees, scored.

The page itself is served through the normal Tech Tools shell (see
``accounting/views/tools.py``); these views only add the parts a calculator
never needs: a database-backed pane, HTMX result fragments, and an inline
pinout card that can be opened on several chips at once so they can be compared.
"""
from __future__ import annotations

from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from ..ic_catalog import (MIN_QUERY_LENGTH, identify, match_pins,
                          marking_ambiguity, parse_constraints)
from ..models import IcChip, IcPin, normalize_ic_code
from .tools import SHELL_TEMPLATE, shell_context

PANE_TEMPLATE = 'tools/partials/ic-finder.html'
RESULTS_TEMPLATE = 'tools/partials/ic_finder/_results.html'
PIN_RESULTS_TEMPLATE = 'tools/partials/ic_finder/_pin_results.html'
PINOUT_TEMPLATE = 'tools/partials/ic_finder/_pinout.html'

# Rows the pin-match form opens with: enough to start, short enough to read
# on a phone at a bench without scrolling past the Search button.
DEFAULT_PIN_ROWS = 3


def _is_fragment(request):
    return request.headers.get('HX-Request', '').lower() == 'true'


def _pane_context(request):
    """Server-side state the pane cannot compute in the browser.

    Both result areas are rendered by the very fragments the search views
    return, so the empty-state keys are supplied rather than left undefined:
    a box that renders nothing because a key is missing and a box correctly
    showing "nothing typed yet" look identical on screen, and only one of them
    is the design.
    """
    try:
        rows = int(request.GET.get('rows', DEFAULT_PIN_ROWS))
    except (TypeError, ValueError):
        rows = DEFAULT_PIN_ROWS
    rows = max(1, min(rows, 12))
    return {
        'ic_functions': IcChip.PIN_FUNCTION_CHOICES,
        'ic_popular': list(IcChip.objects.filter(popular=True)[:8]),
        'ic_chip_count': IcChip.objects.count(),
        'ic_pin_count': IcPin.objects.count(),
        'ic_default_rows': range(rows),
        'ic_query': '',
        'ic_hits': [],
        'ic_ambiguous': 0,
        'ic_matches': [],
        'ic_constraints': 0,
    }


@require_GET
def ic_finder(request, tool=None):
    """The IC Finder pane, inside the shell for a page load and alone for HTMX."""
    context = _pane_context(request)
    if _is_fragment(request):
        return render(request, PANE_TEMPLATE, context)
    context.update(shell_context(request, tool))
    return render(request, SHELL_TEMPLATE, context)


@require_GET
def ic_search(request):
    """Ranked candidates for the typed part number or marking code."""
    query = (request.GET.get('q') or '').strip()
    hits = identify(query)
    # A query shorter than the search minimum is someone still typing, not a
    # code that exists and matches nothing. Only the second one deserves the
    # "no IC found" message, so the short case keeps showing what to type.
    searched = len(normalize_ic_code(query)) >= MIN_QUERY_LENGTH
    return render(request, RESULTS_TEMPLATE, {
        'ic_query': query if searched else '',
        'ic_hits': hits,
        'ic_ambiguous': marking_ambiguity(query) if hits else 0,
    })


@require_GET
def ic_pin_match(request):
    """Chips ranked by how many of the submitted pin constraints they satisfy.

    The form repeats ``pin``/``function`` pairs, so the rows arrive as two
    parallel lists; a row with either half blank is simply not a constraint.
    """
    numbers = request.GET.getlist('pin')
    functions = request.GET.getlist('function')
    constraints = parse_constraints(zip(numbers, functions))
    matches = match_pins(constraints)
    return render(request, PIN_RESULTS_TEMPLATE, {
        'ic_matches': matches,
        'ic_constraints': len(constraints),
    })


@require_GET
def ic_pinout(request, pk):
    """The pinout card HTMX drops inside an open result, so two chips can be
    compared without losing the list behind them.

    ``matched`` carries the pin numbers that came from a Pin Match search, so
    the table and the diagram tick exactly the pins he already verified.
    """
    chip = get_object_or_404(IcChip, pk=pk)
    pins = list(chip.pins.all())
    half = (len(pins) + 1) // 2
    return render(request, PINOUT_TEMPLATE, {
        'ic_chip': chip,
        'ic_pins': pins,
        'ic_left': pins[:half],
        'ic_right': list(reversed(pins[half:])),
        'ic_matched': _matched_numbers(request),
        'ic_copy_text': chip.pinout_text,
    })


def _matched_numbers(request):
    """Pin numbers echoed back from the card that opened this pinout."""
    numbers = []
    for part in (request.GET.get('matched') or '').split(','):
        try:
            numbers.append(int(part))
        except ValueError:
            continue
    return numbers
