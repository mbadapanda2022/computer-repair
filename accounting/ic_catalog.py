"""
IC Finder search logic — the two questions a chip-level technician asks.

``identify()`` answers "what is this chip?" from a part number or the short
marking code printed on the package. ``match_pins()`` answers the reverse
question: "which IC has VCC on pin 1, GND on pin 2 and a gate drive on pin 5?"
— the only way to identify an unmarked chip when the PCB traces are readable.

Both are pure functions over the catalog so they can be unit-tested without
any HTTP layer, and both rank rather than guess: a marking code such as "3842"
is printed by several makers, and silently picking one of them is exactly how a
wrong part ends up back on the board.
"""
from __future__ import annotations

from django.db.models import Q

from .models import IcChip, IcMarking, normalize_ic_code

# Shortest query worth searching. A single character matches almost every
# marking code in existence, so it is treated as "he has not typed yet".
MIN_QUERY_LENGTH = 2

# How many candidates to show before the list stops being useful at a bench.
DEFAULT_LIMIT = 40

# Ranking: an exact part number beats an exact marking code, which beats a
# prefix, which beats a substring. The number is also shown to the user, so
# the bands have to stay meaningful on their own.
SCORE_EXACT_PART = 100
SCORE_PART_PREFIX = 80
SCORE_EXACT_MARKING = 75
SCORE_MARKING_PREFIX = 60
SCORE_PART_CONTAINS = 50
SCORE_MARKING_CONTAINS = 45
SCORE_TEXT_MATCH = 25


class Hit:
    """One candidate chip, with why it was picked."""

    def __init__(self, chip, score, reason):
        self.chip = chip
        self.score = score
        self.reason = reason

    @property
    def confidence(self):
        if self.score >= SCORE_EXACT_PART:
            return 'exact'
        if self.score >= SCORE_EXACT_MARKING:
            return 'strong'
        if self.score >= SCORE_MARKING_PREFIX:
            return 'probable'
        return 'possible'


class PinMatch:
    """One chip scored against the pin constraints the technician typed."""

    def __init__(self, chip, matched, wrong, missing, total):
        self.chip = chip
        self.matched = matched          # pin numbers that agree
        self.wrong = wrong              # pin exists but does a different job
        self.missing = missing          # pin number not on this package
        self.total = total

    @property
    def percent(self):
        if not self.total:
            return 0
        return round(100 * len(self.matched) / self.total)

    @property
    def is_full_match(self):
        return self.total > 0 and len(self.matched) == self.total

    @property
    def matched_set(self):
        return set(self.matched)

    @property
    def wrong_set(self):
        return set(self.wrong)

    @property
    def checked_pins(self):
        """The pins he constrained, as pin rows.

        A card preview has to show these rather than the first six by number:
        on a 384x the evidence sits on 7 and 5, which a leading slice hides.
        Reads the prefetched pin list, so it costs no query.
        """
        wanted = self.matched_set | self.wrong_set
        return [pin for pin in self.chip.pins.all() if pin.number in wanted]


def _prefetch(chips):
    """Re-read the winners with their pins and markings in two queries, so
    rendering 40 cards never fires 40 pairs of them."""
    by_id = {chip.pk: chip for chip in chips}
    return list(IcChip.objects.filter(pk__in=by_id)
                .prefetch_related('pins', 'markings')
                .order_by('part_number'))


def identify(query, limit=DEFAULT_LIMIT):
    """Ranked candidates for a part number or a marking code."""
    code = normalize_ic_code(query)
    if len(code) < MIN_QUERY_LENGTH:
        return []

    found = {}

    def offer(chips, score, reason):
        for chip in chips:
            current = found.get(chip.pk)
            if current is None or score > current[1]:
                found[chip.pk] = (chip, score, reason)

    full = IcChip.objects.all()

    offer(full.filter(norm_part=code), SCORE_EXACT_PART,
          'Part number matches exactly')
    offer(full.filter(norm_part__startswith=code), SCORE_PART_PREFIX,
          'Part number starts with this')
    offer(IcChip.objects.filter(markings__norm_code=code)
          .distinct(), SCORE_EXACT_MARKING,
          'Marking code matches exactly')
    offer(IcChip.objects.filter(markings__norm_code__startswith=code)
          .distinct(), SCORE_MARKING_PREFIX,
          'Marking code starts with this')
    if len(code) >= 3:
        offer(full.filter(norm_part__contains=code), SCORE_PART_CONTAINS,
              'Part number contains this')
        offer(IcChip.objects.filter(markings__norm_code__contains=code)
              .distinct(), SCORE_MARKING_CONTAINS,
              'Marking code contains this')
        offer(full.filter(Q(manufacturer__icontains=code)
                          | Q(summary__icontains=code)
                          | Q(package__icontains=code)).distinct(),
              SCORE_TEXT_MATCH, 'Matches the maker, package or description')

    ranked = sorted(found.values(),
                    key=lambda row: (-row[1], row[0].pin_count,
                                     row[0].part_number))
    if not ranked:
        return []

    top = ranked[:limit]
    chips = _prefetch([chip for chip, _, _ in top])
    by_id = {chip.pk: chip for chip in chips}
    return [Hit(by_id[chip.pk], score, reason)
            for chip, score, reason in top if chip.pk in by_id]


def marking_ambiguity(query):
    """How many different chips carry exactly this marking code.

    More than one is the case that must be shouted about, not buried: the
    technician has to confirm with the package and where the chip sits on the
    board before ordering a replacement.
    """
    code = normalize_ic_code(query)
    if len(code) < MIN_QUERY_LENGTH:
        return 0
    return (IcMarking.objects.filter(norm_code=code)
            .values_list('chip_id', flat=True).distinct().count())


def parse_constraints(pairs):
    """[(pin number, function value)] from the form, ignoring blank rows."""
    constraints = []
    for number, function in pairs:
        try:
            pin = int(str(number).strip())
        except (TypeError, ValueError):
            continue
        function = (function or '').strip()
        if pin < 1 or pin > 256 or not function:
            continue
        constraints.append((pin, function))
    return constraints


def match_pins(constraints, limit=DEFAULT_LIMIT):
    """Chips ranked by how many of the given pin functions they agree with."""
    if not constraints:
        return []

    highest = max(pin for pin, _ in constraints)
    wanted = dict(constraints)

    candidates = (IcChip.objects.filter(pin_count__gte=highest)
                  .prefetch_related('pins', 'markings')
                  .order_by('pin_count', 'part_number'))

    results = []
    for chip in candidates:
        by_number = {pin.number: pin for pin in chip.pins.all()}
        matched, wrong, missing = [], [], []
        for pin, function in wanted.items():
            found = by_number.get(pin)
            if found is None:
                missing.append(pin)
            elif found.function == function:
                matched.append(pin)
            else:
                wrong.append(pin)
        if not matched:
            continue
        results.append(PinMatch(chip, sorted(matched), sorted(wrong),
                                sorted(missing), len(wanted)))

    results.sort(key=lambda row: (-row.percent, len(row.wrong),
                                  row.chip.pin_count,
                                  row.chip.part_number))
    return results[:limit]
