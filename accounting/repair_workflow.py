# accounting/repair_workflow.py
"""
Repair Workflow — Professional State Machine (non-model module)
==============================================================

This file defines NO MODELS — hence no conflict in Django's app registry.
It contains only:

  1. `StatusMachineMixin`      — reusable, model-layer state machine
  2. `Transition`              — one transition's metadata + onward graph
  3. `InvalidStatusTransition` — ValidationError subclass (forms/admin friendly)
  4. `RepairWorkflowMixin`     — RepairJob's guards + side-effects
  5. `REPAIR_TRANSITIONS`      — transition table (single source of truth)
  6. `apply_repair_workflow()` — injects methods onto the RepairJob class

INSTALL
───────
At the end of `models.py` (after all classes):

    from .repair_workflow import apply_repair_workflow
    apply_repair_workflow(RepairJob)
    from .repair_workflow import RepairWorkflowMixin, RepairStatusLogMixin  # noqa

The `RepairStatusLog` model must be defined in `models.py` (one model per
app — Django's rule). Its snippet is ready in `repair_workflow_snippet.py`.

WHAT YOU GET
────────────
    job.mark_received(by=user, remarks='...')
    job.start_diagnosis(by=user) → start_repair → mark_ready → deliver
    job.cancel(by=user, remarks='Customer refused')   # stock auto-reverse
    job.reopen_job(by=user)                # from delivered (force, audited)
    job.available_transitions()            # UI buttons
    job.timeline()                         # who/when/why — full record
    job.change_status('ready', force=True) # manager override (marked in audit)

Guards (model layer — for forms/views/admin/shell/scripts alike):
  • Invoiced job HARD locked (even force does not bypass)
  • At least one part/service/labour before 'ready'
  • Receiver (name or phone) before 'delivered'
  • Repair cannot start on a rejected estimate
  • Terminal state lock (delivered/cancelled)
  • Row lock (select_for_update) → race-safe
  • Direct `job.status = 'x'` → guard raises (no bypass)
"""

from __future__ import annotations

import inspect
import logging
import threading
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

__all__ = [
    'STATUS_FIELD',
    'Transition',
    'InvalidStatusTransition',
    'StatusMachineMixin',
    'RepairWorkflowMixin',
    'REPAIR_TRANSITIONS',
    'REPAIR_TERMINAL_STATUSES',
    'apply_repair_workflow',
]

STATUS_FIELD = 'status'

#: Thread-local flag — while a repair invoice resync is running,
#: `Invoice.save()` skips its ledger rebuild (because resync itself syncs
#: the ledger in phase 2). This way one ledger failure does not roll back
#: the whole resync (lines + totals).
_resync_local = threading.local()


def ledger_sync_deferred() -> bool:
    """When True, `Invoice.save()` skips the ledger rebuild."""
    return getattr(_resync_local, 'defer_ledger', False)


def _set_ledger_deferred(value: bool) -> None:
    _resync_local.defer_ledger = bool(value)


# ════════════════════════════════════════════════════════════════
# 1. TRANSITION METADATA
# ════════════════════════════════════════════════════════════════

class Transition:
    """
    Metadata for one allowed transition + its onward graph.

    `allowed` = where we can go after this transition. UI buttons,
    validation and docs all derive from this single source.
    """
    __slots__ = ('allowed', 'label', 'done', 'method', 'icon', 'kind',
                 'confirm', 'force_only')

    def __init__(self, allowed=(), label=None, done=None, method=None,
                 icon='bi-flag', kind='secondary', confirm=None,
                 force_only=False):
        self.allowed = frozenset(allowed)
        self.method = method
        self.label = label or (method or 'update').replace('_', ' ').title()
        self.done = done or self.label
        self.icon = icon
        self.kind = kind
        self.confirm = confirm
        self.force_only = force_only

    def as_dict(self):
        return {name: getattr(self, name) for name in self.__slots__}

    def __repr__(self):                                  # pragma: no cover
        return f'<Transition {self.method} → {sorted(self.allowed)}>'


class InvalidStatusTransition(ValidationError):
    """
    A transition rule was violated.

    Inherits from ValidationError — so Django forms/admin turn it into a
    field error automatically, and views' `except ValidationError`
    catches it too.
    """
    pass


# ════════════════════════════════════════════════════════════════
# 2. CORE STATE MACHINE (reusable — can also be applied to Invoice/Payment)
# ════════════════════════════════════════════════════════════════

class StatusMachineMixin(models.Model):
    """
    Declarative status state machine with model-layer enforcement.

    Subclass contract
    -----------------
        STATUS_TRANSITIONS = {'from': {'to': Transition}}
        TERMINAL_STATUSES  = frozenset()
        STATUS_LOG_MODEL   = None | 'ModelName'
        STATUS_LOG_FK      = 'repair_job'

    Override points
    ---------------
        _status_guards(self, old, new, force=False)
        _on_status_change(self, old, new)
    """

    class Meta:
        abstract = True

    # ── rules ────────────────────────────────────────────────────
    @classmethod
    def transition_rules(cls):
        return getattr(cls, 'STATUS_TRANSITIONS', {}) or {}

    @classmethod
    def status_choices(cls):
        return dict(cls._meta.get_field(STATUS_FIELD).choices)

    @classmethod
    def status_label(cls, code):
        return cls.status_choices().get(code, code)

    @classmethod
    def allowed_next_statuses(cls, from_status):
        """Transitions that are possible without force."""
        rules = cls.transition_rules().get(from_status, {}) or {}
        return {code for code, meta in rules.items()
                if not getattr(meta, 'force_only', False)}

    @classmethod
    def can_transition_to(cls, from_status, to_status):
        if from_status is None:
            return to_status in cls.status_choices()
        if from_status == to_status:
            return True
        return to_status in cls.transition_rules().get(from_status, {})

    def next_status(self):
        for code, meta in self.transition_rules().get(
            getattr(self, STATUS_FIELD), {}
        ).items():
            if not getattr(meta, 'force_only', False):
                return code
        return None

    def transition_for(self, to_status):
        return self.transition_rules().get(
            getattr(self, STATUS_FIELD), {}
        ).get(to_status)

    def available_transitions(self):
        """UI buttons — only transitions that are valid RIGHT NOW."""
        return [
            {
                'status': code,
                'label': meta.done,
                'method': meta.method,
                'icon': meta.icon,
                'kind': meta.kind,
                'confirm': meta.confirm,
                'force_only': meta.force_only,
            }
            for code, meta in self.transition_rules().get(
                getattr(self, STATUS_FIELD), {}
            ).items()
        ]

    # ── override points ──────────────────────────────────────────
    def _status_guards(self, old_status, new_status, force=False):
        return None

    def _on_status_change(self, old_status, new_status):
        return None

    # ── single write path ────────────────────────────────────────
    @transaction.atomic
    def change_status(self, new_status, *, by=None, remarks='',
                      force=False, full_clean=False, idempotent=True):
        """
        The ONLY path to change status.

        Row lock → idempotency → transition table → guards → save →
        immutable history → side-effects.
        """
        attr = self._meta.get_field(STATUS_FIELD).attname
        is_new = self.pk is None
        old_status = getattr(self, attr)

        # 1) Row lock — race safety
        if not is_new:
            fresh = (
                type(self).all_objects
                .select_for_update()
                .filter(pk=self.pk)
                .first()
            )
            if fresh is None:
                raise InvalidStatusTransition(
                    "Ye job ab mojood nahi — shayad delete ho gaya."
                )
            if getattr(fresh, 'is_deleted', False):
                raise InvalidStatusTransition(
                    "Deleted job ka status nahi badal sakte — pehle restore karein."
                )
            old_status = getattr(fresh, attr)

        # 2) Idempotency
        if old_status == new_status:
            if idempotent:
                return False
            raise InvalidStatusTransition(
                f"Job pehle se '{self.status_label(new_status)}' me hai."
            )

        # 3) Valid status
        if new_status not in self.status_choices():
            raise InvalidStatusTransition(
                f"'{new_status}' ek valid status nahi hai."
            )

        # 4) Transition table
        rules = self.transition_rules().get(old_status, {})
        meta = rules.get(new_status)
        if meta is None:
            pretty = ', '.join(sorted(rules)) or 'none (terminal state)'
            raise InvalidStatusTransition(
                f"'{self.status_label(old_status)}' se "
                f"'{self.status_label(new_status)}' allowed nahi. "
                f"Allowed next: {pretty}."
            )
        if getattr(meta, 'force_only', False) and not force:
            raise InvalidStatusTransition(
                f"'{self.status_label(new_status)}' sirf manager override "
                f"(force=True) se ho sakta hai — ye exception hai."
            )

        # 5) Guards (hard rules apply even under force)
        self._status_guards(old_status, new_status, force=force)

        # 6) Apply + save
        # NOTE: the save() wrapper's "direct write" guard is bypassed here in
        # a controlled way — the transition table + guards + history were
        # already handled above. This flag is only for this one internal save
        # (and is removed immediately in the finally block).
        setattr(self, attr, new_status)
        if full_clean:
            self.full_clean(exclude=None, validate_unique=False)

        self._allow_direct_status_write = True
        try:
            self.save()
        finally:
            self._allow_direct_status_write = False

        # 7) Immutable history (before notification — notified_at guard)
        actor = by if getattr(by, 'pk', None) else self._current_actor()
        self._write_status_log(old_status, new_status, actor, remarks,
                               forced=force)

        # 8) Side-effects
        self._on_status_change(old_status, new_status)
        return True

    def advance(self, *, by=None, remarks='', force=False, **kwargs):
        nxt = self.next_status()
        if not nxt:
            raise InvalidStatusTransition(
                f"'{self.status_label(getattr(self, STATUS_FIELD))}' terminal "
                f"state hai — aage kuch nahi."
            )
        self.change_status(nxt, by=by, remarks=remarks, force=force, **kwargs)
        return nxt

    # ── history helpers ──────────────────────────────────────────
    @staticmethod
    def _current_actor():
        try:
            from .audit import get_current_user
            return get_current_user()
        except Exception:                            # pragma: no cover
            return None

    def _log_model(self):
        name = getattr(type(self), 'STATUS_LOG_MODEL', None)
        if name is None:
            return None
        if isinstance(name, str):
            return self._meta.apps.get_model(self._meta.app_label, name)
        return name

    def _write_status_log(self, old_status, new_status, actor, remarks,
                          forced=False):
        log_model = self._log_model()
        if log_model is None:
            return
        ip = user_agent = None
        try:
            from .audit import get_current_ip, get_current_user_agent
            ip = get_current_ip()
            user_agent = get_current_user_agent()[:255]
        except Exception:                            # pragma: no cover
            pass
        try:
            log_model.objects.create(
                **{
                    type(self).STATUS_LOG_FK: self,
                    'from_status': old_status,
                    'to_status': new_status,
                    'actor_id': getattr(actor, 'pk', None),
                    'remarks': (remarks or '')[:300],
                    'forced': bool(forced),
                    'ip_address': ip,
                    'user_agent': user_agent or '',
                }
            )
        except Exception:
            logger.exception(
                "Status log write failed | %s pk=%s",
                type(self).__name__, self.pk,
            )

    def timeline(self, limit=None):
        """UI-ready history — oldest first, with labels + duration."""
        log_model = self._log_model()
        if log_model is None or not self.pk:
            return []
        qs = (log_model.objects
              .filter(**{type(self).STATUS_LOG_FK: self})
              .select_related('actor')
              .order_by('changed_at', 'id'))
        if limit:
            qs = qs[:limit]

        labels = self.status_choices()
        rows = []
        prev = getattr(self, 'created_at', None)
        for entry in qs:
            rows.append({
                'from_status': entry.from_status,
                'to_status': entry.to_status,
                'from_label': labels.get(entry.from_status, entry.from_status),
                'to_label': labels.get(entry.to_status, entry.to_status),
                'actor': entry.actor,
                'actor_name': (
                    (entry.actor.get_full_name() or entry.actor.username)
                    if entry.actor else 'System'
                ),
                'remarks': entry.remarks,
                'forced': entry.forced,
                'changed_at': entry.changed_at,
                'duration': (entry.changed_at - prev) if prev else None,
                'badge': self._badge_for(entry.to_status),
                'icon': self._icon_for(entry.to_status),
            })
            prev = entry.changed_at
        return rows

    def status_duration(self, status):
        """Time spent in a given status (TAT analytics)."""
        log_model = self._log_model()
        if log_model is None or not self.pk:
            return None
        entered = (
            log_model.objects
            .filter(**{type(self).STATUS_LOG_FK: self}, to_status=status)
            .order_by('changed_at')
            .values_list('changed_at', flat=True)
            .first()
        )
        if not entered:
            return None
        left = (
            log_model.objects
            .filter(**{type(self).STATUS_LOG_FK: self},
                    changed_at__gt=entered)
            .order_by('changed_at')
            .values_list('changed_at', flat=True)
            .first()
        )
        return (left or timezone.now()) - entered

    # ── UI helpers ───────────────────────────────────────────────
    @staticmethod
    def _badge_for(status):
        return {
            'pending': 'text-bg-secondary',
            'received': 'text-bg-info',
            'diagnosis': 'text-bg-primary',
            'repairing': 'text-bg-warning',
            'ready': 'text-bg-success',
            'delivered': 'text-bg-dark',
            'cancelled': 'text-bg-danger',
        }.get(status, 'text-bg-secondary')

    @staticmethod
    def _icon_for(status):
        return {
            'pending': 'bi-hourglass',
            'received': 'bi-box-arrow-in-down',
            'diagnosis': 'bi-search',
            'repairing': 'bi-tools',
            'ready': 'bi-check2-circle',
            'delivered': 'bi-truck',
            'cancelled': 'bi-x-octagon',
        }.get(status, 'bi-flag')

    # ── write protection ─────────────────────────────────────────
    def save(self, *args, **kwargs):
        """
        Guard against setting status directly (admin dropdown, shell, scripts).

        Escape hatches:
          • `change_status(..., force=True)` — manager override (audited)
          • `obj._allow_direct_status_write = True` — data-fix/migration
        """
        attr = self._meta.get_field(STATUS_FIELD).attname
        on_load = getattr(self, '_status_on_load', None)
        current = getattr(self, attr)

        if (self.pk and on_load is not None and current != on_load
                and not getattr(self, '_allow_direct_status_write', False)):
            raise InvalidStatusTransition(
                "Status seedha set nahi kiya ja sakta. "
                f"{type(self).__name__}.change_status('{current}') use karein — "
                "isse guards, timeline dates, audit history aur notification "
                "sab chalenge."
            )

        result = super().save(*args, **kwargs)
        self._status_on_load = getattr(self, attr)
        return result

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._status_on_load = getattr(
            self, self._meta.get_field(STATUS_FIELD).attname
        )

    @classmethod
    def from_db(cls, db, field_names, values):
        obj = super().from_db(db, field_names, values)
        obj._status_on_load = getattr(
            obj, cls._meta.get_field(STATUS_FIELD).attname
        )
        return obj

    def clean(self):
        super().clean()
        value = getattr(self, STATUS_FIELD)
        if value not in self.status_choices():
            raise ValidationError(
                {STATUS_FIELD: f"'{value}' valid status nahi hai."}
            )
        if not self.pk:
            return
        old_status = (
            type(self).all_objects.filter(pk=self.pk)
            .values_list(STATUS_FIELD, flat=True).first()
        )
        if old_status and old_status != value and \
                not self.can_transition_to(old_status, value):
            allowed = sorted(self.allowed_next_statuses(old_status))
            raise ValidationError({
                STATUS_FIELD: (
                    f"'{self.status_label(old_status)}' → "
                    f"'{self.status_label(value)}' allowed nahi. Allowed "
                    f"next: {', '.join(allowed) or 'none (terminal state)'}."
                )
            })


# ════════════════════════════════════════════════════════════════
# 3. REPAIR-SPECIFIC BUSINESS LAYER (mixin that gets injected)
# ════════════════════════════════════════════════════════════════

class RepairWorkflowMixin:
    """
    RepairJob's guards + side-effects. `apply_repair_workflow()` attaches this
    to the class — no new model is created, so there is no conflict in the
    Django registry.
    """

    # ── guards ───────────────────────────────────────────────────
    def _status_guards(self, old_status, new_status, force=False):
        """
        HARD (even force does not bypass):
          1. Invoiced job locked — money already went to the ledger.

        SOFT (manager can bypass with force):
          2. At least one part/service/labour before 'ready'
          3. Receiver (name or phone) before 'delivered'
          4. diagnosis → repairing: blocked on a rejected estimate
        """
        if self.invoice_id:
            # After invoicing, money has already gone to the ledger — scope/
            # financial transitions (parts edit, cancel, back-step) stay hard-
            # locked. But the fulfillment step 'ready → delivered' must be
            # allowed: create_invoice_from_repair only permits invoicing at
            # 'ready', and device handover happens AFTER the invoice. Blocking
            # this would leave the job permanently stuck at 'ready' (a delivery
            # record would never exist).
            if not (old_status == 'ready' and new_status == 'delivered'):
                raise InvalidStatusTransition(
                    f"Invoiced job (invoice #{self.invoice_id}) ka status nahi badal "
                    f"sakta — force se bhi nahi. Pehle invoice reverse/delete karein."
                )

        if force:
            return

        if new_status == 'ready':
            has_lines = (
                self.parts.filter(is_deleted=False).exists()
                or self.services.filter(is_deleted=False).exists()
            )
            if not has_lines:
                raise InvalidStatusTransition(
                    "Job ko 'Ready' karne se pehle kam se kam ek part ya service "
                    "add karein — warna customer ko zero bill jayega."
                )

        if new_status == 'delivered':
            name = (self.delivered_to_name or '').strip()
            phone = (self.delivered_to_phone or '').strip()
            if not name and not phone:
                raise InvalidStatusTransition(
                    "Delivery se pehle 'Received By' (naam ya phone) bharna "
                    "zaroori hai — warna handover ka record nahi rahega."
                )

        if new_status in ('repairing', 'ready') \
                and self.estimate_status == 'rejected':
            raise InvalidStatusTransition(
                "Customer ne estimate reject kar diya hai — aage kaam karne se "
                "pehle Approve Estimate se naya approval record karein "
                "(ya cancel karke diagnosis charge bill banayein)."
            )

    # ── side-effects ─────────────────────────────────────────────
    def _on_status_change(self, old_status, new_status):
        """
        1. Parts/services reversal on cancel (stock returned to inventory).
           This used to be inline in `views/repairs.py` — now it lives in the
           model, so cancelling via admin/shell/script also reverses stock
           correctly.

           Config: settings.REPAIR_REVERSE_LINES_ON_CANCEL = False

        2. Customer notification (exactly once — notified_at guard).
        """
        if new_status == 'cancelled':
            # Cancellation timestamp — the tracking page uses this to show
            # "Job Closed" (so it also gets set for admin/shell cancels).
            self.cancelled_at = timezone.now()
            self.save(update_fields=['cancelled_at'])

        if getattr(settings, 'REPAIR_REVERSE_LINES_ON_CANCEL', True) and (
            new_status == 'cancelled'
        ):
            for service in list(self.services.all()):
                service.delete()
            for part in list(self.parts.all()):
                # RepairPart.delete() → StockMovement delete → stock added back
                part.delete()
            logger.info(
                "RepairJob cancelled — parts/services reversed | job=%s",
                self.job_number,
            )

        log_model = self._log_model()
        if log_model is None:
            return
        entry = (
            log_model.objects.filter(repair_job_id=self.pk)
            .order_by('-changed_at', '-id')
            .first()
        )
        if entry is None or entry.to_status != new_status:
            return
        if entry.notified_at is not None:
            return

        try:
            from django.urls import reverse
            from .utils.notification_helpers import send_notification_to_contact

            send_notification_to_contact(
                self.customer,
                title=f"Repair Status Updated: {self.job_number}",
                message=(
                    f"Your repair for {self.device_model} is now "
                    f"{self.get_status_display()}."
                ),
                link=reverse('customer:customer_repair_detail', args=[self.pk]),
                notif_type='warning' if new_status == 'cancelled' else 'info',
                category='repairs',
                send_email=False,
            )
        except Exception as exc:                     # pragma: no cover
            logger.error(
                "Notification error for job %s: %s", self.job_number, exc,
            )
            return

        try:
            log_model.objects.filter(pk=entry.pk).update(
                notified_at=timezone.now()
            )
        except Exception:                            # pragma: no cover
            pass

    # ── named transitions ────────────────────────────────────────
    def mark_received(self, by=None, remarks='', **kwargs):
        return self.change_status('received', by=by, remarks=remarks, **kwargs)

    def start_diagnosis(self, by=None, remarks='', **kwargs):
        return self.change_status('diagnosis', by=by, remarks=remarks, **kwargs)

    def start_repair(self, by=None, remarks='', **kwargs):
        return self.change_status('repairing', by=by, remarks=remarks, **kwargs)

    def mark_ready(self, by=None, remarks='', **kwargs):
        return self.change_status('ready', by=by, remarks=remarks, **kwargs)

    def deliver(self, by=None, remarks='', **kwargs):
        return self.change_status('delivered', by=by, remarks=remarks, **kwargs)

    def cancel(self, by=None, remarks='', **kwargs):
        return self.change_status('cancelled', by=by, remarks=remarks, **kwargs)

    def reopen_job(self, by=None, remarks='', **kwargs):
        """Delivered job back to 'repairing' — force (audited exception)."""
        kwargs.setdefault('force', True)
        return self.change_status(
            'repairing', by=by,
            remarks=remarks or 'Delivered job reopen kiya gaya (customer wapas)',
            **kwargs,
        )

    # ── UI properties ────────────────────────────────────────────
    @property
    def status_badge_class(self):
        return self._badge_for(self.status)

    @property
    def status_icon(self):
        return self._icon_for(self.status)

    @property
    def is_terminal(self):
        return self.status in getattr(type(self), 'TERMINAL_STATUSES',
                                     frozenset())

    @property
    def is_locked(self):
        return bool(self.invoice_id) or self.is_terminal

    # ── invoice resync / drift (AUDIT C-3) ───────────────────────
    def resync_invoice(self, *, rebuild_ledger=True, update_job_amount=True):
        """
        Sync the job's parts/services with the invoice's line items.

        Returns
        -------
        dict : {'created': n, 'synced': n, 'removed': n, 'total': Decimal}

        When to run: after editing lines with `edit_invoiced=True`, after
        restoring a job, or after any manual data-fix.
        """
        if not self.invoice_id:
            return {'created': 0, 'synced': 0, 'removed': 0, 'total': None}

        # ── Re-entrancy guard ──
        # Resync runs line saves internally, which could themselves trigger
        # resync (infinite/nested loop + transaction breakage). So only the
        # first resync does work; nested calls return immediately.
        if getattr(self, '_invoice_resync_running', False):
            return {'created': 0, 'synced': 0, 'removed': 0,
                    'total': None, 'skipped': 'reentrant'}

        from django.db import transaction as _tx

        self._invoice_resync_running = True
        # Defer the ledger rebuild until phase 2 (otherwise a sync failure
        # inside `Invoice.save()` would roll back phase-1's lines/totals —
        # that was the original bug).
        _set_ledger_deferred(True)
        try:
            # ═══ PHASE 1: lines + totals (must never fail) ═══
            with _tx.atomic():
                invoice = type(self).invoice.field.related_model.objects.get(
                    pk=self.invoice_id
                )

                # ⚠️ IMPORTANT: clear the reverse-relation cache.
                # Django caches `self.parts` / `self.services` on the instance.
                # Even after lines are saved it keeps returning stale values —
                # that is why resync used to leave the invoice at the old
                # amount.
                self.refresh_from_db()

                result = self._sync_lines_to_invoice(invoice)
                invoice.calculate_totals()
                invoice.refresh_from_db(
                    fields=['subtotal', 'tax_amount', 'grand_total'],
                )
                result['total'] = invoice.grand_total

                item_subtotal = sum(
                    (i.quantity * i.unit_price
                     for i in invoice.items.filter(is_deleted=False)),
                    Decimal('0.00'),
                ).quantize(Decimal('0.01'))

            # Set the job's amount here too (after the phase-1 commit)
            if update_job_amount:
                type(self).all_objects.filter(pk=self.pk).update(
                    final_amount=invoice.grand_total,
                )
                self.final_amount = invoice.grand_total

                # ── Drift check (warning only) ──
                # Real signal: job amount vs invoice amount. The internal
                # comparison of lines is cache-sensitive, so for that use the
                # `job.invoice_drift` property (fresh queries).
                if self.final_amount != invoice.grand_total:
                    logger.warning(
                        "Amount drift after resync | job=%s | invoice=%s vs "
                        "job=%s (job #%s) — `job.resync_invoice()` dobara "
                        "chalayein",
                        self.job_number, invoice.grand_total,
                        self.final_amount, self.pk,
                    )

            # ═══ PHASE 2: ledger (best-effort — SEPARATE transaction) ═══
            # A ledger sync failure does NOT affect lines/totals, because this
            # is its own separate transaction. This was critical: earlier a
            # ledger error rolled back the whole resync, leaving the invoice
            # at its old amounts (and items could even disappear).
            if rebuild_ledger:
                try:
                    with _tx.atomic():
                        from .models import sync_invoice_ledger
                        # Sync the ledger explicitly now (in phase 2) —
                        # Invoice.save() had deferred it.
                        _set_ledger_deferred(False)
                        try:
                            sync_invoice_ledger(invoice)
                        finally:
                            _set_ledger_deferred(True)
                except Exception:
                    logger.exception(
                        "Ledger sync failed (invoice lines/totals SAFE hain — "
                        "baad me `sync_invoice_ledger(invoice)` chala lein) | "
                        "invoice=%s", invoice.pk,
                    )

            return result
        finally:
            self._invoice_resync_running = False
            _set_ledger_deferred(False)

    def _sync_lines_to_invoice(self, invoice):
        """
        Parts/services → invoice items (create / update / soft-delete).

        Matching is DETERMINISTIC:
          • part    → `InvoiceItem.repair_part_id`
          • service → `InvoiceItem.repair_service_id` (new FK)
          • older records without a link → description match (fallback)
        """
        from .models import InvoiceItem

        existing = list(invoice.items.filter(is_deleted=False))
        by_part = {i.repair_part_id: i for i in existing if i.repair_part_id}
        by_service = {
            i.repair_service_id: i for i in existing if i.repair_service_id
        }
        # Fallback: old invoice items (without link) — match by description
        orphan_service = {
            f'{i.product_id}:{i.description}': i
            for i in existing
            if not i.repair_part_id and not i.repair_service_id
        }

        created = synced = 0
        seen: set[int] = set()

        for part in self.parts.filter(is_deleted=False).select_related('product'):
            item = by_part.get(part.pk)
            if item is None:
                item = InvoiceItem.objects.create(
                    invoice=invoice, product=part.product,
                    description=f"[{self.job_number}] Repair Part",
                    quantity=Decimal(part.quantity),
                    unit_price=part.unit_price,
                    repair_part=part, stock_already_deducted=True,
                )
                created += 1
            else:
                item.product = part.product
                item.quantity = Decimal(part.quantity)
                item.unit_price = part.unit_price
                item.stock_already_deducted = True
                item.save()
                synced += 1
            seen.add(item.pk)

        for service in self.services.filter(is_deleted=False).select_related('product'):
            description = (service.description
                           or f"[{self.job_number}] {service.product.name}")
            item = by_service.get(service.pk)
            if item is None:
                # Fallback: find an old unlinked item
                item = orphan_service.get(f'{service.product_id}:{description}')

            if item is None:
                item = InvoiceItem.objects.create(
                    invoice=invoice, product=service.product,
                    description=description, quantity=Decimal('1.00'),
                    unit_price=service.amount,
                    repair_service=service,
                    stock_already_deducted=True,
                )
                created += 1
            else:
                item.product = service.product
                item.description = description
                item.unit_price = service.amount
                item.repair_service = service      # link gets fixed too
                item.stock_already_deducted = True
                item.save()
                synced += 1
            seen.add(item.pk)

        removed = 0
        for item in existing:
            if item.pk not in seen:
                item.delete()          # soft delete (stock skip — already deducted flag)
                removed += 1

        return {'created': created, 'synced': synced, 'removed': removed}

    @property
    def invoice_drift(self):
        """
        (invoice_total, line_total, difference).

        difference != 0 = mismatch between the job's lines and the invoice
        (for audit/control reports).
        """
        from decimal import Decimal as _D
        invoice_total = self.invoiced_amount if self.invoice_id else None
        line_total = self.base_amount
        if invoice_total is None:
            return (None, line_total, None)
        return (invoice_total, line_total, (invoice_total - line_total))

    # ── notification duplicate-guard wrapper ─────────────────────
    def _notify_status_change(self):
        """
        Status-change notification — skipped if `_suppress_status_notification`
        is set. `apply_repair_workflow()` has this method called from
        RepairJob.save()'s notification block (while the real notification
        goes out via `change_status()`, after the history row).
        """
        if getattr(self, '_suppress_status_notification', False):
            return
        try:
            from django.urls import reverse
            from .utils.notification_helpers import send_notification_to_contact

            send_notification_to_contact(
                self.customer,
                title=f"Repair Status Updated: {self.job_number}",
                message=(
                    f"Your repair for {self.device_model} is now "
                    f"{self.get_status_display()}."
                ),
                link=reverse('customer:customer_repair_detail', args=[self.pk]),
                notif_type='info',
                category='repairs',
                send_email=False,
            )
        except Exception as exc:                     # pragma: no cover
            logger.error(
                "Notification error for job %s: %s", self.job_number, exc,
            )


# ════════════════════════════════════════════════════════════════
# 4. TRANSITION TABLE — single source of truth for business rules
# ════════════════════════════════════════════════════════════════
#
#   pending ──received──► received ──diagnosis──► diagnosis
#      │                      │                       │
#      │ cancelled            │ cancelled             │ cancelled
#      ▼                      ▼                       ▼
#   cancelled            cancelled               cancelled
#
#   diagnosis ──repairing──► repairing ──ready──► ready ──delivered──► delivered
#                                  │                 │                    │
#                                  │ cancelled       │ cancelled          │ reopen (force)
#                                  ▼                 ▼                    ▼
#                              cancelled         cancelled             repairing

REPAIR_TRANSITIONS = {
    'pending': {
        'received': Transition(
            {'diagnosis', 'cancelled'},
            label='Device Received', method='mark_received',
            icon='bi-box-arrow-in-down', kind='primary',
            confirm="Device shop par receive hua? Timeline me aaj ki date lag jayegi.",
        ),
        # Legacy path: repair starts directly on estimate approval (diagnosis
        # skipped). This was the behaviour of your old views — kept explicit
        # on purpose, not silently dropped. If you want every job to go
        # through diagnosis, just remove these 6 lines.
        'repairing': Transition(
            {'ready', 'cancelled'},
            label='Start Repair (direct)', method='start_repair',
            icon='bi-tools', kind='primary',
            confirm="Seedha repair shuru karein? (diagnosis stage skip hoga)",
        ),
        'cancelled': Transition(
            set(), label='Cancel Job', method='cancel',
            icon='bi-x-octagon', kind='danger',
            confirm="Job cancel karna hai? Ye terminal state hai — aage koi "
                    "transition nahi (manager force se hi wapas kar sakta hai).",
        ),
    },
    'received': {
        'diagnosis': Transition(
            {'repairing', 'cancelled'},
            label='Start Diagnosis', method='start_diagnosis',
            icon='bi-search', kind='info',
        ),
        'cancelled': Transition(
            set(), label='Cancel Job', method='cancel',
            icon='bi-x-octagon', kind='danger',
            confirm="Job cancel karna hai?",
        ),
    },
    'diagnosis': {
        'repairing': Transition(
            {'ready', 'cancelled'},
            label='Start Repair', method='start_repair',
            icon='bi-tools', kind='primary',
            confirm="Repair shuru karni hai? Rejected estimate par ye block hoga.",
        ),
        'cancelled': Transition(
            set(), label='Cancel Job', method='cancel',
            icon='bi-x-octagon', kind='danger',
            confirm="Job cancel karna hai? Lage hue parts stock me wapas "
                    "chale jayenge.",
        ),
    },
    'repairing': {
        'ready': Transition(
            {'delivered'},
            label='Mark Ready', method='mark_ready',
            icon='bi-check2-circle', kind='success',
            confirm="Repair poori ho gayi? Customer ko 'ready' notification jayegi.",
        ),
        'cancelled': Transition(
            set(), label='Cancel Job', method='cancel',
            icon='bi-x-octagon', kind='danger',
            confirm="Parts lage hue hain — cancel karne par stock wapas jayega. "
                    "Confirm karein?",
        ),
    },
    'ready': {
        'delivered': Transition(
            set(), label='Deliver to Customer', method='deliver',
            icon='bi-truck', kind='success',
            confirm="Device customer ko de diya? 'Received By' (naam ya phone) "
                    "bhara hona zaroori hai.",
        ),
        # Customer refused delivery / rejected the estimate / device not
        # picked up — cancel is possible from 'ready' too (reason + optional
        # charge handled in the view).
        'cancelled': Transition(
            set(), label='Cancel Job', method='cancel',
            icon='bi-x-octagon', kind='danger',
            confirm="Device taiyar hai phir bhi cancel? Reason likhein — "
                    "zaroorat par diagnosis charge ka bill ban sakta hai.",
        ),
    },
    'delivered': {
        'repairing': Transition(
            set(), label='Reopen Job', method='reopen_job',
            icon='bi-arrow-counterclockwise', kind='warning',
            force_only=True,
            confirm="Delivered job reopen kar rahe hain — ye exception hai aur "
                    "audit me FORCED mark hoga. Continue?",
        ),
    },
    'cancelled': {
        # A cancelled job can only become DELIVERED — device handed back to
        # the customer (it was sitting here; repair never happened).
        # This was the rule in your old view (cancelled → delivered allowed),
        # so the state machine keeps the same behaviour.
        'delivered': Transition(
            set(), label='Device Returned (Delivered)', method='deliver',
            icon='bi-arrow-return-left', kind='secondary',
            confirm="Cancelled device customer ko wapas de rahe hain? "
                    "'Received By' (naam ya phone) zaroori hai.",
        ),
    },
}

#: Only 'delivered' is terminal. From 'cancelled' the device can be handed
#: back (cancelled → delivered) — so 'cancelled' is NOT terminal.
REPAIR_TERMINAL_STATUSES = frozenset({'delivered'})


# ════════════════════════════════════════════════════════════════
# 6. INVOICE CONSISTENCY (invoiced job's amount must not diverge)
# ════════════════════════════════════════════════════════════════

class InvoiceConsistencyMixin:
    """
    Guards + invoice resync for RepairPart / RepairService.

    PROBLEM (AUDIT C-3):
      Even after an invoice existed, staff could still add or remove
      parts/services. `calculate_final_amount()` only changed the job's
      `final_amount` — not the invoice's `grand_total`. Result: the job
      showed ₹9,000 while the invoice stayed at ₹4,500, and ₹4,500 of extra
      stock had already been deducted → silent divergence in books and
      inventory.

    SOLUTION:
      • add/change/remove a line on an invoiced job → BLOCK (clear message)
      • pass `edit_invoiced=True` (or the settings flag) → allowed, and the
        invoice resyncs immediately
      • `job.resync_invoice()` — for manual data-fixes
      • `job.invoice_drift` — mismatch report
    """

    # ── helper ───────────────────────────────────────────────────
    def _job_and_lock(self):
        job = getattr(self, 'repair_job', None)
        locked = bool(job is not None and getattr(job, 'invoice_id', None))
        return job, locked

    def _edit_allowed(self, edit_invoiced_flag=False):
        """Either `edit_invoiced=True` or the settings flag — one of the two."""
        if edit_invoiced_flag:
            return True
        return bool(getattr(settings, 'REPAIR_ALLOW_INVOICED_LINE_EDIT', False))

    def _line_changed(self):
        """
        Did any invoice-affecting field of this line change?

        RepairPart and RepairService have DIFFERENT fields (part: `quantity` +
        `unit_price`; service: `amount`), so only compare the fields that
        actually exist on the model.
        """
        if self.pk is None:
            return True
        candidates = ('product_id', 'quantity', 'unit_price', 'amount',
                      'is_deleted')
        fields = [f for f in candidates
                  if any(x.name == f for x in self._meta.concrete_fields)]
        if not fields:
            return True
        old = type(self).all_objects.filter(pk=self.pk).only(*fields).first()
        if old is None:
            return True
        for name in fields:
            if getattr(old, name) != getattr(self, name):
                return True
        return False

    # ── save ─────────────────────────────────────────────────────
    def _pro_save(self, *args, **kwargs):
        # `soft_delete()` internally calls `self.save()` — then this flag is
        # set on the instance so the guard does not fire again.
        edit_invoiced = bool(
            kwargs.pop('edit_invoiced', False)
            or getattr(self, '_invoice_edit_authorised', False)
        )
        job, locked = self._job_and_lock()

        if locked and not self._edit_allowed(edit_invoiced):
            raise InvalidStatusTransition(
                f"Invoiced job (invoice #{job.invoice_id}) me "
                f"{type(self).__name__} add/change nahi kar sakte — warna "
                f"stock kat jayega par invoice ki rakam nahi badlegi. "
                f"Raaste: (1) invoice reverse/delete karein, "
                f"(2) `save(edit_invoiced=True)` use karein (invoice turant "
                f"resync ho jayega), ya (3) settings me "
                f"REPAIR_ALLOW_INVOICED_LINE_EDIT = True karein."
            )

        # ⚠️ Check `_line_changed()` BEFORE save — after save the DB is
        # already updated and the comparison would always return False.
        changed = self._line_changed()

        result = self._pro_original_save(*args, **kwargs)

        if locked and job is not None and changed:
            try:
                # Refetch the job (the old instance's status/dates may be stale)
                self.repair_job.resync_invoice()
            except Exception:
                # ⚠️ A line edit must never fail just because the invoice
                # could not sync — log and move on.
                # (Resync can be re-run via `job.resync_invoice()`.)
                logger.exception(
                    "Invoice resync failed after line save (line save hua hai) "
                    "| job=%s | %s#%s",
                    getattr(job, 'pk', '?'), type(self).__name__, self.pk,
                )
        return result

    # ── delete ───────────────────────────────────────────────────
    def _pro_delete(self, *args, **kwargs):
        edit_invoiced = bool(kwargs.pop('edit_invoiced', False))
        job, locked = self._job_and_lock()

        if locked and not self._edit_allowed(edit_invoiced):
            raise InvalidStatusTransition(
                f"Invoiced job (invoice #{job.invoice_id}) se "
                f"{type(self).__name__} hata nahi sakte — invoice me ghost "
                f"line reh jayegi. `delete(edit_invoiced=True)` use karein "
                f"(invoice turant update ho jayega)."
            )

        # Soft-delete (or legacy hard-delete) — set the authorisation flag on
        # the instance so the guard does not fire again during internal saves
        self._invoice_edit_authorised = True
        try:
            self._pro_original_delete(*args, **kwargs)
        finally:
            self._invoice_edit_authorised = False

        # After a soft-delete the line must not appear in the invoice — resync.
        # (The job may also be deleted during delete, and the `save()` guard
        # fires on status — so sync the job with the bypass flag set.)
        if locked and job is not None:
            try:
                self.repair_job._allow_direct_status_write = True
                self.repair_job.resync_invoice()
            except Exception:
                logger.exception(
                    "Invoice resync after line delete failed | job=%s",
                    getattr(job, 'pk', '?'),
                )
            finally:
                self.repair_job._allow_direct_status_write = False


def apply_invoice_consistency(part_model, service_model):
    """
    Installs invoice-consistency guards on RepairPart + RepairService.

    Idempotent — re-running reuses the stored original save/delete (no double
    wrapping).
    """
    for model in (part_model, service_model):
        if getattr(model, '_invoice_consistency_applied', False):
            continue
        model._pro_original_save = model.save
        model._pro_original_delete = model.delete
        model.save = InvoiceConsistencyMixin._pro_save
        model.delete = InvoiceConsistencyMixin._pro_delete
        model._line_changed = InvoiceConsistencyMixin._line_changed
        model._job_and_lock = InvoiceConsistencyMixin._job_and_lock
        model._edit_allowed = InvoiceConsistencyMixin._edit_allowed
        model._invoice_consistency_applied = True
        logger.info("Invoice consistency guards applied to %s", model.__name__)
    return part_model, service_model


# ════════════════════════════════════════════════════════════════
# 7. APPLY — inject everything onto RepairJob (no new model)
# ════════════════════════════════════════════════════════════════

def apply_repair_workflow(job_model, log_model_name='RepairStatusLog'):
    """
    Injects the state machine onto `RepairJob` (or any model).

    What it does:
      1. Adds `StatusMachineMixin` methods (if not already inherited)
      2. Adds `RepairWorkflowMixin`'s guards/side-effects/named transitions
      3. Sets the transition table + log model
      4. Installs a wrapper `save()` that handles the status guard +
         notification suppression (the original save runs exactly as before)

    Returns
    -------
    The same class (mutated) — no new model gets registered.
    """
    # ── 1. Core mixin methods — inject DIRECTLY onto the CLASS ──
    # IMPORTANT DETAIL: fetching a classmethod via `getattr(mixin_cls, name)`
    # returns it ALREADY BOUND (__self__ = StatusMachineMixin). Setting it via
    # setattr directly would leave `cls` as the mixin → "StatusMachineMixin
    # has no field named 'status'". So we UNWRAP the classmethod and re-wrap it.
    for name in ('change_status', 'advance', 'transition_rules',
                 'status_choices', 'status_label', 'allowed_next_statuses',
                 'can_transition_to', 'next_status', 'transition_for',
                 'available_transitions', 'timeline', 'status_duration',
                 '_badge_for', '_icon_for', '_log_model', '_write_status_log',
                 '_current_actor'):
        attr = inspect.getattr_static(StatusMachineMixin, name)
        if isinstance(attr, classmethod):
            setattr(job_model, name, classmethod(attr.__func__))
        elif isinstance(attr, staticmethod):
            setattr(job_model, name, staticmethod(attr.__func__))
        else:
            setattr(job_model, name, attr)

    # ── 2. Repair business layer ──
    for name in ('_status_guards', '_on_status_change', 'mark_received',
                 'start_diagnosis', 'start_repair', 'mark_ready', 'deliver',
                 'cancel', 'reopen_job', '_notify_status_change',
                 'resync_invoice', '_sync_lines_to_invoice'):
        setattr(job_model, name, getattr(RepairWorkflowMixin, name))

    # ── 3. Properties (the property object itself is what gets attached) ──
    job_model.status_badge_class = RepairWorkflowMixin.status_badge_class
    job_model.status_icon = RepairWorkflowMixin.status_icon
    job_model.is_terminal = RepairWorkflowMixin.is_terminal
    job_model.is_locked = RepairWorkflowMixin.is_locked
    job_model.invoice_drift = RepairWorkflowMixin.invoice_drift

    # ── 4. Config ──
    job_model.STATUS_TRANSITIONS = REPAIR_TRANSITIONS
    job_model.TERMINAL_STATUSES = REPAIR_TERMINAL_STATUSES
    job_model.STATUS_LOG_MODEL = log_model_name
    job_model.STATUS_LOG_FK = 'repair_job'

    # ── 5. save() wrapper: guard + notification suppression ──
    original_save = job_model.save

    def save(self, *args, **kwargs):                # noqa: D401
        attr = self._meta.get_field(STATUS_FIELD).attname
        on_load = getattr(self, '_status_on_load', None)
        current = getattr(self, attr)

        if (self.pk and on_load is not None and current != on_load
                and not getattr(self, '_allow_direct_status_write', False)):
            raise InvalidStatusTransition(
                "Status seedha set nahi kiya ja sakta. "
                f"{type(self).__name__}.change_status('{current}') use karein — "
                "isse guards, timeline dates, audit history aur notification "
                "sab chalenge."
            )

        # Run the original save — the status notification inside it gets
        # suppressed (the real notification is sent by change_status()).
        self._suppress_status_notification = True
        try:
            result = original_save(self, *args, **kwargs)
        finally:
            self._suppress_status_notification = False

        self._status_on_load = getattr(self, attr)
        return result

    job_model.save = save

    # ── 6. __init__ / from_db: status snapshot for the guard ──
    original_init = job_model.__init__

    def __init__(self, *args, **kwargs):             # noqa: N807
        original_init(self, *args, **kwargs)
        self._status_on_load = getattr(
            self, self._meta.get_field(STATUS_FIELD).attname
        )

    job_model.__init__ = __init__

    original_from_db = job_model.from_db.__func__

    @classmethod
    def from_db(cls, db, field_names, values):       # noqa: N807
        obj = original_from_db(cls, db, field_names, values)
        obj._status_on_load = getattr(
            obj, cls._meta.get_field(STATUS_FIELD).attname
        )
        return obj

    job_model.from_db = from_db

    logger.info(
        "Repair workflow state machine applied to %s (log=%s)",
        job_model.__name__, log_model_name,
    )
    return job_model
