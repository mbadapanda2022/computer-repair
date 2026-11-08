"""
Management command: import_ics

Idempotently import IC catalog data from JSON files into the database.

Re-importing the same data is SAFE — no duplicates are created because
the command matches existing chips by their NORMALIZED part number
(case-insensitive, ignores spaces/hyphens/underscores). So "UC3842",
"uc3842" and "UC-3842" all resolve to a SINGLE chip record.

Usage examples
--------------
    # Production/Render-safe default: imports ic_data/master_ics.json (and all
    # *.json under ic_data/) when no path is given.
    python manage.py import_ics

    # Import a single file
    python manage.py import_ics ic_catalog.json

    # Import multiple files / folders (recursive *.json)
    python manage.py import_ics data/chargers.json data/stepdown.json
    python manage.py import_ics data/

    # Preview what would change, without writing to the database
    python manage.py import_ics --dry-run

    # Only add new ICs, never update existing ones
    python manage.py import_ics --skip-existing

    # Import IC data but ignore markings and pins
    python manage.py import_ics --no-markings --no-pins

Expected JSON structure (matches accounting/models.py)
-------------------------------------------------------
    [
        {
            "part_number": "UC3842",
            "manufacturer": "Texas Instruments",
            "category": "pwm",
            "package": "DIP-8",
            "pin_count": 8,
            "summary": "...",
            "bench_note": "...",
            "datasheet_url": "...",
            "popular": true,
            "markings": ["3842", "UC3842"],
            "pins": [
                {"number": 1, "name": "COMP", "function": "comp", "note": "..."},
                ...
            ]
        },
        ...
    ]
"""

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounting.models import IcChip, IcMarking, IcPin, normalize_ic_code


# ─────────────────────────────────────────────────────────────
#  Small helpers
# ─────────────────────────────────────────────────────────────

_VALID_CATEGORIES = {c[0] for c in IcChip.CATEGORY_CHOICES}
_VALID_FUNCTIONS = {c[0] for c in IcChip.PIN_FUNCTION_CHOICES}

# Model field max_lengths (kept in sync with accounting/models.py)
MAX_PART = 60
MAX_MFR = 60
MAX_PKG = 30
MAX_SUMMARY = 160
MAX_URL = 300
MAX_MARKING = 24
MAX_PIN_NAME = 30
MAX_PIN_NOTE = 160


def _trunc(value, max_len):
    """Safe truncation — always returns a str (never None)."""
    if value is None:
        return ''
    s = str(value)
    return s[:max_len]


def _safe_int(value, default, min_val=1, max_val=1000):
    """Parse an integer safely; return `default` on any failure/out-of-range."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    if v < min_val or v > max_val:
        return default
    return v


# ─────────────────────────────────────────────────────────────
#  Command
# ─────────────────────────────────────────────────────────────

class Command(BaseCommand):
    help = 'Idempotently import IC catalog data from JSON files (no duplicates).'

    def add_arguments(self, parser):
        parser.add_argument(
            'paths',
            nargs='*',
            help=(
                'JSON files or directories (recursive *.json). '
                'If omitted, imports ic_data/master_ics.json (and other '
                'ic_data/*.json) from the project — safe for Render deploys.'
            ),
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview changes without writing to the database.',
        )
        parser.add_argument(
            '--skip-existing',
            action='store_true',
            help='Only add new ICs; never update existing ones.',
        )
        parser.add_argument(
            '--no-markings',
            action='store_true',
            help='Skip importing SMD marking codes.',
        )
        parser.add_argument(
            '--no-pins',
            action='store_true',
            help='Skip importing pin-level data.',
        )

    # ──────────────────────────────────────────────────────

    def handle(self, *args, **options):
        self.dry_run = options['dry_run']
        self.skip_existing = options['skip_existing']
        self.skip_markings = options['no_markings']
        self.skip_pins = options['no_pins']

        paths = options.get('paths') or self._default_paths()
        json_files = self._collect_json_files(paths)
        if not json_files:
            self.stdout.write(self.style.WARNING(
                'No IC JSON files found — nothing to import. '
                'Add ic_data/master_ics.json (or pass a path) and redeploy.'
            ))
            return

        # Header
        mode = []
        if self.dry_run:
            mode.append('DRY RUN')
        if self.skip_existing:
            mode.append('SKIP EXISTING')
        mode_str = f' [{" | ".join(mode)}]' if mode else ''
        self.stdout.write(self.style.NOTICE(
            f'\nImporting from {len(json_files)} JSON file(s){mode_str}\n'
        ))

        # Stats
        self.stats = {
            'chips_created': 0,
            'chips_updated': 0,
            'chips_unchanged': 0,
            'chips_skipped': 0,
            'chips_failed': 0,
            'markings_created': 0,
            'markings_unchanged': 0,
            'pins_created': 0,
            'pins_updated': 0,
            'pins_unchanged': 0,
        }
        self.errors = []

        # Process every file
        for file_path in json_files:
            self.stdout.write(self.style.MIGRATE_HEADING(f'\n>>> {file_path}'))
            try:
                data = self._load_json(file_path)
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f'    !! Load failed: {exc}'))
                continue

            if isinstance(data, dict):
                data = [data]   # accept single-object JSON
            if not isinstance(data, list):
                self.stdout.write(self.style.ERROR(
                    '    !! JSON root must be an array of IC objects'
                ))
                continue

            for idx, row in enumerate(data, start=1):
                if not isinstance(row, dict):
                    self.stdout.write(self.style.WARNING(
                        f'    [{idx}] Skipped — row is not an object'
                    ))
                    continue

                part = _trunc((row.get('part_number') or '').strip(), MAX_PART)
                if not part:
                    self.stdout.write(self.style.WARNING(
                        f'    [{idx}] Skipped — missing part_number'
                    ))
                    continue

                try:
                    if self.dry_run:
                        action = self._dry_run_one(row, part)
                    else:
                        with transaction.atomic():
                            action = self._import_one(row, part)
                    self._report_one(part, action)
                except Exception as exc:
                    self.stats['chips_failed'] += 1
                    self.errors.append((part, str(exc)))
                    self.stdout.write(self.style.ERROR(f'    [x] {part}: {exc}'))

        self._print_summary()

        if self.stats['chips_failed'] > 0:
            raise CommandError(
                f"{self.stats['chips_failed']} IC(s) failed — see errors above."
            )

    # ──────────────────────────────────────────────────────
    #  File helpers
    # ──────────────────────────────────────────────────────

    def _default_paths(self):
        """Default IC data location shipped with the repo (Render-safe)."""
        base = Path(getattr(settings, 'BASE_DIR', Path.cwd()))
        candidates = [
            base / 'ic_data' / 'master_ics.json',
            base / 'ic_data',
        ]
        found = [str(p) for p in candidates if p.exists()]
        if found:
            return found
        # Last-resort: import any JSON under ic_data even if only directory exists later
        return [str(base / 'ic_data')]

    def _collect_json_files(self, paths):
        files = []
        seen = set()
        for p in paths:
            path = Path(p).expanduser()
            if not path.is_absolute():
                base = Path(getattr(settings, 'BASE_DIR', Path.cwd()))
                path = (base / path).resolve() if not (base / path).exists() else (base / path)
            if not path.exists():
                self.stdout.write(self.style.WARNING(f'Path not found: {path}'))
                continue
            if path.is_file():
                if path.suffix.lower() == '.json':
                    rp = str(path.resolve())
                    if rp not in seen:
                        seen.add(rp)
                        files.append(path)
                else:
                    self.stdout.write(self.style.WARNING(
                        f'Skipping non-JSON file: {path}'
                    ))
            elif path.is_dir():
                for child in sorted(path.rglob('*.json')):
                    rp = str(child.resolve())
                    if rp not in seen:
                        seen.add(rp)
                        files.append(child)
        return files

    def _load_json(self, path):
        # utf-8-sig tolerates BOM from Windows editors; avoids JSON parse errors.
        with open(path, 'r', encoding='utf-8-sig') as fh:
            return json.load(fh)

    # ──────────────────────────────────────────────────────
    #  Lookup / duplicate prevention
    # ──────────────────────────────────────────────────────

    def _find_existing_chip(self, part_number):
        """
        Return the existing IcChip matching part_number, or None.

        Match strategy (in order):
          1) By `norm_part` (case- and whitespace-insensitive) — catches
             "UC3842" == "uc3842" == "UC-3842".
          2) Fallback: exact `part_number__iexact` — for very old rows
             that pre-date the norm_part field.
        """
        norm = normalize_ic_code(part_number)
        chip = IcChip.objects.filter(norm_part=norm).first()
        if chip is not None:
            return chip
        return IcChip.objects.filter(part_number__iexact=part_number).first()

    # ──────────────────────────────────────────────────────
    #  Dry run (no DB writes)
    # ──────────────────────────────────────────────────────

    def _dry_run_one(self, row, part):
        existing = self._find_existing_chip(part)

        if existing is None:
            self.stats['chips_created'] += 1
            return 'create'

        if self.skip_existing:
            self.stats['chips_skipped'] += 1
            return 'skip'

        chip_data = self._sanitize_chip_data(row, part)
        would_change = any(
            getattr(existing, field) != value
            for field, value in chip_data.items()
            if field != 'part_number'   # by construction it matches
        )
        if would_change:
            self.stats['chips_updated'] += 1
            return 'update'

        self.stats['chips_unchanged'] += 1
        return 'unchanged'

    # ──────────────────────────────────────────────────────
    #  Real import (inside caller's atomic block)
    # ──────────────────────────────────────────────────────

    def _import_one(self, row, part):
        chip = self._find_existing_chip(part)

        # --skip-existing: don't touch an existing chip at all
        if chip is not None and self.skip_existing:
            self.stats['chips_skipped'] += 1
            return 'skip'

        chip_data = self._sanitize_chip_data(row, part)

        # ── Create or update the IC header ──
        if chip is None:
            chip = IcChip.objects.create(**chip_data)
            self.stats['chips_created'] += 1
            action = 'create'
        else:
            changed = False
            for field, value in chip_data.items():
                if getattr(chip, field) != value:
                    setattr(chip, field, value)
                    changed = True
            if changed:
                chip.save()
                self.stats['chips_updated'] += 1
                action = 'update'
            else:
                self.stats['chips_unchanged'] += 1
                action = 'unchanged'

        # ── Nested rows (skipped in --skip-existing mode above) ──
        if not self.skip_markings:
            self._import_markings(chip, row.get('markings'))
        if not self.skip_pins:
            self._import_pins(chip, row.get('pins'))

        return action

    # ──────────────────────────────────────────────────────

    def _sanitize_chip_data(self, row, part):
        category = (row.get('category') or 'other').strip().lower()
        if category not in _VALID_CATEGORIES:
            category = 'other'

        return {
            'part_number': part,
            'manufacturer': _trunc(row.get('manufacturer'), MAX_MFR),
            'category': category,
            'package': _trunc(row.get('package'), MAX_PKG),
            'pin_count': _safe_int(row.get('pin_count'), default=8),
            'summary': _trunc(row.get('summary'), MAX_SUMMARY),
            'bench_note': row.get('bench_note') or '',
            'datasheet_url': _trunc(row.get('datasheet_url'), MAX_URL),
            'popular': bool(row.get('popular', False)),
        }

    # ──────────────────────────────────────────────────────

    def _import_markings(self, chip, markings):
        # Accept string or list
        if isinstance(markings, str):
            markings = [markings]
        if not isinstance(markings, list):
            return

        for raw in markings:
            if not isinstance(raw, str):
                continue
            code = raw.strip()[:MAX_MARKING]
            if not code:
                continue
            norm = normalize_ic_code(code)

            # Exact code match → already present
            if IcMarking.objects.filter(chip=chip, code=code).exists():
                self.stats['markings_unchanged'] += 1
                continue

            # Same marking with different casing → don't duplicate
            if IcMarking.objects.filter(chip=chip, norm_code=norm).exists():
                self.stats['markings_unchanged'] += 1
                continue

            IcMarking.objects.create(chip=chip, code=code)
            self.stats['markings_created'] += 1

    # ──────────────────────────────────────────────────────

    def _import_pins(self, chip, pins):
        if not isinstance(pins, list):
            return

        for raw in pins:
            if not isinstance(raw, dict):
                continue
            number = _safe_int(raw.get('number'), default=None)
            if number is None:
                continue

            name = _trunc(raw.get('name'), MAX_PIN_NAME)
            function = (raw.get('function') or 'other').strip().lower()
            if function not in _VALID_FUNCTIONS:
                function = 'other'
            note = _trunc(raw.get('note'), MAX_PIN_NOTE)

            existing = IcPin.objects.filter(chip=chip, number=number).first()
            if existing is None:
                IcPin.objects.create(
                    chip=chip,
                    number=number,
                    name=name,
                    function=function,
                    note=note,
                )
                self.stats['pins_created'] += 1
                continue

            if (
                existing.name == name
                and existing.function == function
                and existing.note == note
            ):
                self.stats['pins_unchanged'] += 1
                continue

            existing.name = name
            existing.function = function
            existing.note = note
            existing.save()
            self.stats['pins_updated'] += 1

    # ──────────────────────────────────────────────────────
    #  Reporting
    # ──────────────────────────────────────────────────────

    def _report_one(self, part, action):
        style_map = {
            'create':    (self.style.SUCCESS,  '[+]'),
            'update':    (self.style.WARNING,  '[~]'),
            'unchanged': (self.style.NOTICE,   '[=]'),
            'skip':      (self.style.NOTICE,   '[s]'),
        }
        style, icon = style_map.get(action, (self.style.NOTICE, '[?]'))
        label = {
            'create': 'created',
            'update': 'updated',
            'unchanged': 'unchanged',
            'skip': 'skipped (already exists)',
        }.get(action, action)
        self.stdout.write(style(f'    {icon} {part}  ->  {label}'))

    def _print_summary(self):
        s = self.stats
        sep = '=' * 60

        self.stdout.write('\n' + sep)
        self.stdout.write(self.style.NOTICE('  IMPORT SUMMARY'))
        self.stdout.write(sep)
        self.stdout.write(
            f'  ICs        : created={s["chips_created"]:>3}  '
            f'updated={s["chips_updated"]:>3}  '
            f'unchanged={s["chips_unchanged"]:>3}  '
            f'skipped={s["chips_skipped"]:>3}  '
            f'failed={s["chips_failed"]:>3}'
        )

        if not self.dry_run:
            self.stdout.write(
                f'  Markings   : created={s["markings_created"]:>3}  '
                f'unchanged={s["markings_unchanged"]:>3}'
            )
            self.stdout.write(
                f'  Pins       : created={s["pins_created"]:>3}  '
                f'updated={s["pins_updated"]:>3}  '
                f'unchanged={s["pins_unchanged"]:>3}'
            )

        if self.errors:
            self.stdout.write(self.style.ERROR('\n  FAILED RECORDS:'))
            for part, msg in self.errors:
                self.stdout.write(self.style.ERROR(f'    - {part}: {msg}'))

        if self.dry_run:
            self.stdout.write(self.style.WARNING(
                '\n  DRY RUN — nothing written to the database.\n'
            ))
        else:
            self.stdout.write(self.style.SUCCESS('\n  Import complete.\n'))