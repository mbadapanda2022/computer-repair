# accounting/demo_trial.py
"""6-month trial state for the Windows demo package (DEMO_MODE only).

Guards (deliberately simple — a demo, not DRM):
1. First-ever launch stamps install time into trial_state.json AND the
   DB cache table; the EARLIEST persisted stamp wins, so deleting the
   JSON or setting the clock back does not reset the 180-day window.
2. A build-time hard deadline (env DEMO_TRIAL_HARD_DEADLINE, ISO date
   embedded when the installer is built) expires old builds regardless
   of install date.
"""

import json
import logging
import math
import time
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

# Developer contact shown on warning banner + expiry page. This is
# intentional hardcoding (like the developer footer): it must survive
# even when the demo company profile is rebranded by an evaluator.
TRIAL_CONTACT = {
    'name': 'Manoj Badapanda',
    'company': 'A1 Computer Solutions',
    'phone': '7008601963',
    'whatsapp': '918895745441',
    'website': 'https://a1computersolutions.in',
}

_DB_STAMP_KEY = 'demo_trial_install_ts'


def _state_path():
    return Path(settings.DEMO_DATA_DIR) / 'trial_state.json'


def _read_file_ts():
    try:
        data = json.loads(_state_path().read_text(encoding='utf-8'))
        return float(data['install_ts'])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_file_ts(ts):
    try:
        _state_path().write_text(
            json.dumps({'install_ts': ts,
                        'installed_at': datetime.fromtimestamp(
                            ts, tz=timezone.get_current_timezone())
                        .strftime('%Y-%m-%d %H:%M')}),
            encoding='utf-8')
    except OSError:
        logger.exception('demo_trial: could not write state file')


def _db_ts():
    try:
        val = cache.get(_DB_STAMP_KEY)
        return float(val) if val is not None else None
    except Exception:
        return None


def _stamp_db(ts):
    try:
        existing = cache.get(_DB_STAMP_KEY)
        if existing is None:
            cache.add(_DB_STAMP_KEY, ts, None)
    except Exception:
        logger.exception('demo_trial: could not stamp DB')


def _hard_deadline_ts():
    raw = getattr(settings, 'DEMO_TRIAL_HARD_DEADLINE', '') or ''
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        return dt.timestamp()
    except ValueError:
        return None


def get_trial_state(now=None):
    """Returns {'install_ts', 'expiry_ts', 'days_left', 'expired',
    'warn', 'hard_deadline_ts'}."""
    now = now or timezone.now()
    now_ts = now.timestamp()

    file_ts = _read_file_ts()
    db_ts = _db_ts()
    candidates = [t for t in (file_ts, db_ts) if t is not None]
    install_ts = min(candidates) if candidates else now_ts

    if file_ts != install_ts:
        _write_file_ts(install_ts)
    _stamp_db(install_ts)

    trial_days = getattr(settings, 'DEMO_TRIAL_DAYS', 180)
    expiry_ts = install_ts + trial_days * 86400
    hd_ts = _hard_deadline_ts()

    effective_expiry = expiry_ts
    if hd_ts is not None:
        effective_expiry = min(expiry_ts, hd_ts)

    expired = now_ts >= effective_expiry
    days_left = max(0, math.ceil((effective_expiry - now_ts) / 86400))
    warn_days = getattr(settings, 'DEMO_TRIAL_WARN_DAYS', 30)

    return {
        'install_ts': install_ts,
        'expiry_ts': effective_expiry,
        'expiry_display': datetime.fromtimestamp(
            effective_expiry, tz=timezone.get_current_timezone()
        ).strftime('%d %b %Y'),
        'days_left': days_left,
        'expired': expired,
        'warn': (not expired) and days_left <= warn_days,
        'hard_deadline_ts': hd_ts,
    }
