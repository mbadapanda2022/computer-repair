# accounting/audit.py
"""
Audit context — thread-local request storage + diff helpers.

Why thread-local?
─────────────────
Django signals (post_save, pre_save) do NOT receive the request object.
To capture "who did this", we stash the current request in thread-local
storage from middleware, and read it back inside signal handlers.

Gunicorn sync workers reuse threads across requests, so we MUST clear
the storage in a `finally` block (see middleware update).
"""

import logging
import threading
from datetime import date, datetime, time
from decimal import Decimal

logger = logging.getLogger(__name__)

_local = threading.local()


# ════════════════════════════════════════════════════════════
# REQUEST CONTEXT
# ════════════════════════════════════════════════════════════

def set_current_request(request):
    """Called by middleware at the START of each request."""
    _local.request = request


def clear_current_request():
    """Called by middleware in a `finally` block."""
    if hasattr(_local, 'request'):
        del _local.request


def get_current_request():
    return getattr(_local, 'request', None)


def get_current_user():
    """Return authenticated User or None. Never raises."""
    req = get_current_request()
    if not req:
        return None
    try:
        user = getattr(req, 'user', None)
        if user and user.is_authenticated:
            return user
    except Exception:
        pass
    return None


def get_current_ip():
    """Best-effort client IP (respects X-Forwarded-For)."""
    req = get_current_request()
    if not req:
        return None
    try:
        xff = req.META.get('HTTP_X_FORWARDED_FOR')
        if xff:
            return xff.split(',')[0].strip()[:45]
        return req.META.get('REMOTE_ADDR')
    except Exception:
        return None


def get_current_user_agent():
    req = get_current_request()
    if not req:
        return ''
    try:
        return (req.META.get('HTTP_USER_AGENT') or '')[:255]
    except Exception:
        return ''


# ════════════════════════════════════════════════════════════
# SERIALIZATION + DIFF
# ════════════════════════════════════════════════════════════

def _serialize(value):
    """Convert any field value to a JSON-safe representation."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if hasattr(value, 'pk') and hasattr(value, '_meta'):
        return f"{value._meta.model_name}#{value.pk}"
    return str(value)


# Fields never worth diffing
_DIFF_SKIP = frozenset({
    'id', 'pk',
    'created_at', 'updated_at',
    'is_deleted', 'deleted_at', 'deleted_by',
})


def build_change_diff(old_obj, new_obj, fields=None):
    """
    Return {field: {'from': old, 'to': new}} for fields that changed.

    - `fields` = None → diff all concrete fields (excluding audit noise).
    - Comparison is value-based (Decimal("1.00") == Decimal("1")).
    - Never raises; returns {} on any error.
    """
    if old_obj is None or new_obj is None:
        return {}

    try:
        if fields is None:
            fields = [
                f.name for f in new_obj._meta.concrete_fields
                if f.name not in _DIFF_SKIP
            ]
    except Exception:
        return {}

    changes = {}
    for name in fields:
        try:
            old_val = getattr(old_obj, name, None)
            new_val = getattr(new_obj, name, None)
            if old_val != new_val:
                changes[name] = {
                    'from': _serialize(old_val),
                    'to': _serialize(new_val),
                }
        except Exception:
            continue
    return changes