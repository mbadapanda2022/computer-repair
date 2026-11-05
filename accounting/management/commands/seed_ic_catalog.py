"""
Seed the IC catalog behind the public IC Finder tool.

    venv/Scripts/python.exe manage.py seed_ic_catalog

Every part here is one a technician actually meets on an SMPS, charger,
laptop, printer or monitor board, and every pin table is the standard one from
that family's datasheet. Parts whose pinout I could not state with certainty
(LNK304, TNY264, LD7535 and similar) are deliberately absent — a wrong pin map
is exactly the thing that costs him a board, and the disclaimer on the page
only helps if the data underneath it is trustworthy.

The command is idempotent: it updates what exists, removes pins and markings
that are no longer part of a family's table, and never touches anything else
in the database.
"""
from __future__ import annotations

from urllib.parse import quote_plus

from django.core.management.base import BaseCommand
from django.db import transaction

from ...models import IcChip, IcMarking, IcPin


def datasheet_search(part_number):
    """A link that always reaches the official PDF, without us inventing a
    URL that rots (or worse, points at the wrong document)."""
    return 'https://www.google.com/search?q=' + quote_plus(
        part_number + ' datasheet pdf')


# (pin number, datasheet name, function key, what to expect on the bench)
UC384X_PINS = (
    (1, 'COMP', 'comp', 'Error-amplifier output. Loop compensation lives here; '
                         'a leaky 10 nF to pin 2 makes the supply hum.'),
    (2, 'FB', 'fb', 'Feedback. The top of the current-sense resistor and the '
                    'optocoupler/TL431 divider both come to this pin.'),
    (3, 'CS / ISENSE', 'cs', 'Current sense. A spike over the internal limit '
                             '(about 1 V on 3842/3843, 0.9 V on 3844/3845) '
                             'skips the next cycle.'),
    (4, 'RT / CT', 'freq', 'Timing. Resistor to VREF and capacitor to ground '
                           'set the switching frequency.'),
    (5, 'GND', 'gnd', 'Signal ground — the small side of the sense resistor. '
                      'Never lift it to chase a noise problem.'),
    (6, 'OUT', 'gate', 'Gate drive to the MOSFET. 3842/3843 are emitter '
                       'followers, 3844/3845 are totem-pole and pull down hard.'),
    (7, 'VCC', 'vcc', 'Supply. If it sags below the hold threshold the chip '
                       'chirps: start-stop, start-stop.'),
    (8, 'VREF', 'ref', '2.5 V reference output. Measuring 0 V here with 12 V '
                       'on VCC means the chip is dead.'),
)

DUAL_OPAMP_PINS = (
    (1, 'OUT A', 'out', 'Output of half A.'),
    (2, 'IN A-', 'in', 'Inverting input of half A.'),
    (3, 'IN A+', 'in', 'Non-inverting input of half A.'),
    (4, 'V-', 'gnd', 'Negative supply — ground on single-rail boards.'),
    (5, 'IN B+', 'in', 'Non-inverting input of half B.'),
    (6, 'IN B-', 'in', 'Inverting input of half B.'),
    (7, 'OUT B', 'out', 'Output of half B.'),
    (8, 'V+', 'vcc', 'Positive supply.'),
)

# part_number, maker, category, package, summary, bench note, markings, pins
PARTS = (
    {
        'part': 'UC3842', 'maker': 'Unitrode / TI / ON', 'category': 'pwm',
        'package': 'DIP-8, SOP-8', 'popular': True,
        'summary': 'Current-mode PWM controller — the most common heart of an '
                   'ATX, laptop or charger SMPS.',
        'bench': 'No gate pulses on pin 6 with 12 V on pin 7 and 2.5 V on pin 8 '
                 'means the chip is dead. If it only starts when the load is '
                 'disconnected, look at the start resistor and the VCC cap '
                 'before blaming the controller.',
        'markings': ('3842', 'UC3842', 'UC3842B', 'UC3842N', 'CA3842', 'HA173842'),
        'pins': UC384X_PINS,
    },
    {
        'part': 'KA3842', 'maker': 'Fairchild / ON Semi', 'category': 'pwm',
        'package': 'DIP-8, SOP-8', 'popular': False,
        'summary': 'Same UC384x family from Fairchild; pin-for-pin identical '
                   'to UC3842.',
        'bench': 'Marked "3842" it is indistinguishable from a UC3842 by code '
                 'alone — the package and the board position decide, not the '
                 'marking.',
        'markings': ('3842', 'KA3842', 'KA3842A'),
        'pins': UC384X_PINS,
    },
    {
        'part': 'UC3843', 'maker': 'Unitrode / TI', 'category': 'pwm',
        'package': 'DIP-8, SOP-8', 'popular': True,
        'summary': 'Current-mode PWM controller with a higher start threshold, '
                   'common in offline flyback adapters.',
        'bench': 'Starts near 15 V and holds near 10 V, so a VCC rail stuck at '
                 '8 V that runs a 3842 will never start a 3843.',
        'markings': ('3843', 'UC3843', 'KA3843'),
        'pins': UC384X_PINS,
    },
    {
        'part': 'UC3844', 'maker': 'Unitrode / TI', 'category': 'pwm',
        'package': 'DIP-8, SOP-8', 'popular': False,
        'summary': 'Current-mode PWM controller with a totem-pole output for '
                   'faster MOSFET switching.',
        'bench': 'The totem-pole output sinks as well as sources, so a gate '
                 'that stays high at shutdown points at this chip, not the '
                 'driver.',
        'markings': ('3844', 'UC3844'),
        'pins': UC384X_PINS,
    },
    {
        'part': 'UC3845', 'maker': 'Unitrode / TI', 'category': 'pwm',
        'package': 'DIP-8, SOP-8', 'popular': False,
        'summary': 'Totem-pole PWM controller with a 0.9 V current-limit '
                   'threshold, used where the sense resistor is small.',
        'bench': 'Because the limit is 0.9 V not 1.0 V, a sense resistor '
                 'swapped "to the same value" from a 3842 board trips earlier '
                 'and the supply looks loaded down.',
        'markings': ('3845', 'UC3845', 'KA3845'),
        'pins': UC384X_PINS,
    },
    {
        'part': 'TL431', 'maker': 'TI / Motorola / JCET', 'category': 'reference',
        'package': 'TO-92, SOT-23', 'popular': True,
        'summary': 'Programmable shunt reference — the error amplifier in '
                   'almost every flyback feedback loop.',
        'bench': 'With the reference pin open it behaves like an open circuit; '
                 'a shorted cathode-anode gives a dead 5 V rail with no '
                 'hiccup. Check the divider into pin 1 first.',
        'markings': ('431', 'TL431', 'TL431A', 'KIA431', 'KA431'),
        'pins': (
            (1, 'REF', 'ref', 'Reference input. The resistive divider from the '
                              'output taps here; 2.495 V is the regulation point.'),
            (2, 'ANODE', 'other', 'Usually the low side, toward ground or the '
                                  'optocoupler emitter.'),
            (3, 'CATHODE', 'other', 'The regulated node. Measure '
                                    'cathode-to-anode: about 2.5 V means it is working.'),
        ),
    },
    {
        'part': 'PC817', 'maker': 'Sharp', 'category': 'optocoupler',
        'package': 'DIP-4', 'popular': True,
        'summary': 'Optocoupler that carries the feedback signal across the '
                   'isolation barrier of a flyback supply.',
        'bench': 'A dry joint on pin 1-2 is more common than a failed '
                 'coupler. Measure LED forward drop with the diode range of '
                 'the meter before ordering one.',
        'markings': ('817', 'PC817', '817C', 'PC817C'),
        'pins': (
            (1, 'ANODE', 'in', 'LED anode, driven from the TL431 cathode side.'),
            (2, 'CATHODE', 'in', 'LED cathode.'),
            (3, 'EMITTER', 'out', 'Transistor emitter, usually to the primary '
                                  'side ground.'),
            (4, 'COLLECTOR', 'out', 'Transistor collector; pulls the FB/COMP '
                                    'node of the PWM chip down.'),
        ),
    },
    {
        'part': 'EL817', 'maker': 'Everlight', 'category': 'optocoupler',
        'package': 'DIP-4', 'popular': False,
        'summary': 'Pin-compatible PC817 alternative from Everlight — same '
                   'four-pin function order.',
        'bench': 'Marked "817" on the package it cannot be told apart from a '
                 'PC817 by code alone. Both are interchangeable in this role, '
                 'but check the CTR grade (B/C/D) when the loop is marginal.',
        'markings': ('817', 'EL817', 'EL817C'),
        'pins': (
            (1, 'ANODE', 'in', 'LED anode.'),
            (2, 'CATHODE', 'in', 'LED cathode.'),
            (3, 'EMITTER', 'out', 'Transistor emitter.'),
            (4, 'COLLECTOR', 'out', 'Transistor collector.'),
        ),
    },
    {
        'part': 'LM358', 'maker': 'TI / ST / JCET', 'category': 'opamp',
        'package': 'DIP-8, SOP-8', 'popular': True,
        'summary': 'Dual operational amplifier — error amps, current limit '
                   'comparators and signal conditioning on PSU boards.',
        'bench': 'Its outputs cannot reach the positive rail; a signal that '
                 'stops about 1.5 V below V+ is normal behaviour, not a fault.',
        'markings': ('358', 'LM358', 'LM358N', 'LM358M'),
        'pins': DUAL_OPAMP_PINS,
    },
    {
        'part': 'LM393', 'maker': 'TI / ST / JCET', 'category': 'comparator',
        'package': 'DIP-8, SOP-8', 'popular': False,
        'summary': 'Dual comparator with open-collector outputs, used for '
                   'over-current and over-voltage trips.',
        'bench': 'The outputs only sink, so a pull-up resistor is mandatory — '
                 'a "dead" output with no pull-up is a design fact, not a '
                 'broken chip.',
        'markings': ('393', 'LM393', 'LM393N'),
        'pins': (
            (1, 'OUT A', 'out', 'Open-collector output of half A.'),
            (2, 'IN A-', 'in', 'Inverting input of half A.'),
            (3, 'IN A+', 'in', 'Non-inverting input of half A.'),
            (4, 'GND', 'gnd', 'Negative supply.'),
            (5, 'IN B+', 'in', 'Non-inverting input of half B.'),
            (6, 'IN B-', 'in', 'Inverting input of half B.'),
            (7, 'OUT B', 'out', 'Open-collector output of half B.'),
            (8, 'V+', 'vcc', 'Positive supply.'),
        ),
    },
    {
        'part': 'LM324', 'maker': 'TI / ST / JCET', 'category': 'opamp',
        'package': 'DIP-14, SOP-14', 'popular': False,
        'summary': 'Quad operational amplifier — four amplifiers in one '
                   'package for multi-rail protection circuits.',
        'bench': 'Power is on 4 and 11, which is the opposite of what people '
                 'expect from an 8-pin part. Confirm with the board traces '
                 'before judging a socketed chip bad.',
        'markings': ('324', 'LM324', 'LM324N', 'LM324M'),
        'pins': (
            (1, 'OUT A', 'out', ''), (2, 'IN A-', 'in', ''), (3, 'IN A+', 'in', ''),
            (4, 'V+', 'vcc', 'Positive supply — not ground.'),
            (5, 'IN B+', 'in', ''), (6, 'IN B-', 'in', ''), (7, 'OUT B', 'out', ''),
            (8, 'OUT C', 'out', ''), (9, 'IN C-', 'in', ''), (10, 'IN C+', 'in', ''),
            (11, 'V-', 'gnd', 'Negative supply / ground.'),
            (12, 'IN D+', 'in', ''), (13, 'IN D-', 'in', ''), (14, 'OUT D', 'out', ''),
        ),
    },
    {
        'part': 'NE555', 'maker': 'TI / Signetics', 'category': 'timer',
        'package': 'DIP-8, SOP-8', 'popular': True,
        'summary': 'Timer IC — clock, PWM and delay stages in monitors, LED '
                   'drivers and older supplies.',
        'bench': 'Pin 5 (control) is the one people forget: a noisy 10 nF '
                 'there changes the threshold and makes the whole stage '
                 'unstable.',
        'markings': ('555', 'NE555', 'SA555', 'LM555', 'SE555'),
        'pins': (
            (1, 'GND', 'gnd', 'Ground.'),
            (2, 'TRIG', 'in', 'Trigger; goes low to start a pulse.'),
            (3, 'OUT', 'out', 'Output; swings between the rails.'),
            (4, 'RESET', 'en', 'Active-low reset — must be high for the chip to '
                               'run, a classic no-output cause.'),
            (5, 'CTRL', 'other', 'Control voltage, 2/3 of supply; the '
                                 'compensation point for noise.'),
            (6, 'THRESH', 'in', 'Threshold; the timing capacitor charges to it.'),
            (7, 'DISCHARGE', 'out', 'Open-collector discharge between the '
                                   'timing resistor and capacitor.'),
            (8, 'VCC', 'vcc', 'Supply, 4.5-15 V.'),
        ),
    },
    {
        'part': 'LM7805', 'maker': 'TI / ON / ST', 'category': 'regulator',
        'package': 'TO-220, TO-92', 'popular': True,
        'summary': 'Fixed 5 V linear regulator, 1 A — the standby rail on '
                   'motherboards and control boards.',
        'bench': 'Drop-out is about 2 V, so an input under 7 V cannot hold '
                 '5 V. A hot tab with no load is a shorted output capacitor, '
                 'not a bad regulator.',
        'markings': ('7805', 'LM7805', 'KA7805', '7805CV'),
        'pins': (
            (1, 'IN', 'in', 'Unregulated input.'),
            (2, 'GND', 'gnd', 'Ground — the middle pin, unlike the 317.'),
            (3, 'OUT', 'out', 'Regulated 5 V.'),
        ),
    },
    {
        'part': 'LM317', 'maker': 'TI / ON / ST', 'category': 'regulator',
        'package': 'TO-220, TO-92', 'popular': False,
        'summary': 'Adjustable linear regulator, 1.25 V to 37 V — bench '
                   'supplies and adjustable bias rails.',
        'bench': 'The tab is OUTPUT, not ground, so a heatsink bolted to a '
                 'grounded chassis shorts the regulator. Open the adjust pin '
                 'and the output goes to the input rail.',
        'markings': ('317', 'LM317', 'KA317', 'LM317T'),
        'pins': (
            (1, 'ADJ', 'fb', 'Adjust; 1.25 V sits between this and the output.'),
            (2, 'OUT', 'out', 'Regulated output — also on the tab.'),
            (3, 'IN', 'in', 'Unregulated input.'),
        ),
    },
    {
        'part': 'LM78L05', 'maker': 'TI / ON / ST', 'category': 'regulator',
        'package': 'TO-92', 'popular': False,
        'summary': '100 mA fixed 5 V regulator — bias rail for optocouplers '
                   'and reference dividers.',
        'bench': 'Same three pins as a 78L12 or 78L15 with only the code '
                 'different, so the printed number is the whole story here.',
        'markings': ('78L05', 'LM78L05', 'KA78L05'),
        'pins': (
            (1, 'IN', 'in', 'Input.'),
            (2, 'GND', 'gnd', 'Ground, middle pin.'),
            (3, 'OUT', 'out', '5 V out.'),
        ),
    },
    {
        'part': 'LM2576', 'maker': 'TI / National', 'category': 'dc-dc',
        'package': 'TO-220 (5 pin), TO-263', 'popular': False,
        'summary': '3 A step-down switching regulator — the 12 V to 5 V rail '
                   'on desktop and monitor boards.',
        'bench': 'Switching, so the output is a square wave into an inductor; '
                 'a "noisy" 5 V with a dry 100 uF output cap is normal to '
                 'misdiagnose. Pin 5 shuts the whole stage down when low.',
        'markings': ('2576', 'LM2576', 'LM2576T'),
        'pins': (
            (1, 'VIN', 'vcc', 'Input supply.'),
            (2, 'VOUT', 'out', 'Switch output into the inductor.'),
            (3, 'GND', 'gnd', 'Ground.'),
            (4, 'FEEDBACK', 'fb', 'Feedback from the smoothed output; sets the '
                                  'rail with the divider.'),
            (5, 'ON/OFF', 'en', 'Active-low shutdown; open or high means run.'),
        ),
    },
)


class Command(BaseCommand):
    help = ('Load the IC Finder catalog: part numbers, marking codes and pin '
            'tables for common SMPS and board-level ICs. Safe to re-run.')

    def add_arguments(self, parser):
        parser.add_argument('--quiet', action='store_true',
                            help='Print only the totals line.')

    @transaction.atomic
    def handle(self, *args, **options):
        totals = seed(quiet=options['quiet'], writer=self.stdout)
        self.stdout.write(self.style.SUCCESS(
            f'IC catalog: {totals["chips"]} ICs, {totals["markings"]} marking '
            f'codes, {totals["pins"]} pins. Total in database: '
            f'{totals["total"]} ICs.'))


@transaction.atomic
def seed(quiet=True, writer=None):
    """Write the catalog rows the IC Finder reads, and report the counts.

    The management command and migration 0022 both call this. The finder is
    empty-handed without data, and a production deploy has nobody standing at
    a shell — so the starter tables have to arrive with ``migrate``.
    """
    chips = markings = pins = 0

    for entry in PARTS:
        chip, created = IcChip.objects.update_or_create(
            part_number=entry['part'],
            defaults={
                'manufacturer': entry['maker'],
                'category': entry['category'],
                'package': entry['package'],
                'pin_count': len(entry['pins']),
                'summary': entry['summary'],
                'bench_note': entry['bench'],
                'datasheet_url': datasheet_search(entry['part']),
                'popular': entry.get('popular', False),
            })
        chips += 1
        if writer and not quiet:
            verb = 'added' if created else 'updated'
            writer.write(f'  {verb} {chip.part_number} '
                         f'({len(entry["pins"])} pins)')

        wanted = {code.upper() for code in entry['markings']}
        for code in wanted:
            _, made = IcMarking.objects.update_or_create(
                chip=chip, code=code, defaults={})
            markings += 1
            if made and writer and not quiet:
                writer.write(f'      marking {code}')
        stale = IcMarking.objects.filter(chip=chip).exclude(code__in=wanted)
        stale.delete()

        numbers = [row[0] for row in entry['pins']]
        for number, name, function, note in entry['pins']:
            IcPin.objects.update_or_create(
                chip=chip, number=number,
                defaults={'name': name, 'function': function, 'note': note})
            pins += 1
        IcPin.objects.filter(chip=chip).exclude(number__in=numbers).delete()

    return {'chips': chips, 'markings': markings, 'pins': pins,
            'total': IcChip.objects.count()}
