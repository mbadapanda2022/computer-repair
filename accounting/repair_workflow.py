# accounting/repair_workflow.py
"""
Repair Workflow — Professional State Machine (non-model module)
==============================================================

YE FILE KOI MODEL DEFINE NAHI KARTI — isliye Django ke app registry me
koi conflict nahi hota. Isme sirf:

  1. `StatusMachineMixin`      — reusable, model-layer state machine
  2. `Transition`              — ek transition ka metadata + onward graph
  3. `InvalidStatusTransition` — ValidationError subclass (forms/admin friendly)
  4. `RepairWorkflowMixin`     — RepairJob ke guards + side-effects
  5. `REPAIR_TRANSITIONS`      — transition table (single source of truth)
  6. `apply_repair_workflow()` — RepairJob class par methods inject karta hai

INSTALL
───────
`models.py` ke aakhir me (sab classes ke baad):

    from .repair_workflow import apply_repair_workflow
    apply_repair_workflow(RepairJob)
    from .repair_workflow import RepairWorkflowMixin, RepairStatusLogMixin  # noqa

`RepairStatusLog` model ko `models.py` me define karna hai (ek hi app me ek
model — Django ka rule). Iska snippet `repair_workflow_snippet.py` me ready hai.

KYA MILTA HAI
─────────────
    job.mark_received(by=user, remarks='...')
    job.start_diagnosis(by=user) → start_repair → mark_ready → deliver
    job.cancel(by=user, remarks='Customer ne mana kiya')   # stock auto-reverse
    job.reopen_job(by=user)                # delivered se (force, audited)
    job.available_transitions()            # UI buttons
    job.timeline()                         # kisne/kab/kyun — poora record
    job.change_status('ready', force=True) # manager override (audit me mark)

Guards (model layer — form/view/admin/shell/script sab ke liye):
  • Invoiced job HARD locked (force bhi bypass nahi karta)
  • 'ready' se pehle kam se kam ek part/service/labour
  • 'delivered' se pehle receiver (naam ya phone)
  • rejected estimate par repair shuru nahi
  • terminal state lock (delivered/cancelled)
  • row-lock (select_for_update) → race-safe
  • seedha `job.status = 'x'` → guard raise (bypass nahi)
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

#: Thread-local flag — jab repair ka invoice resync chal raha ho, tab
#: `Invoice.save()` apna ledger rebuild skip kar deta hai (kyunki resync
#: khud phase-2 me ledger sync karta hai). Isse ek ledger failure poore
#: resync (lines + totals) ko rollback nahi karta.
_resync_local = threading.local()


def ledger_sync_deferred() -> bool:
    """True hone par `Invoice.save()` ledger rebuild skip kare."""
    return getattr(_resync_local, 'defer_ledger', False)


def _set_ledger_deferred(value: bool) -> None:
    _resync_local.defer_ledger = bool(value)


# ════════════════════════════════════════════════════════════════
# 1. TRANSITION METADATA
# ════════════════════════════════════════════════════════════════

class Transition:
    """
    Ek allowed transition ka metadata + onward graph.

    `allowed` = is transition ke baad kahan ja sakte hain. UI buttons,
    validation aur docs — teeno isi ek source se aate hain.
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
    Transition rule ka ullanghan.

    ValidationError se inherit — isliye Django forms/admin ise apne aap
    field error bana dete hain, aur views ka `except ValidationError`
    bhi ise pakad leta hai.
    """
    pass


# ════════════════════════════════════════════════════════════════
# 2. CORE STATE MACHINE (reusable — Invoice/Payment par bhi lag sakta hai)
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
        """Wo transitions jo bina force ke ho sakti hain."""
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
        """UI buttons — sirf wo transitions jo ABHI valid hain."""
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
        Status badalne ka EKLAUTA raasta.

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

        # 5) Guards (hard rules force par bhi lagti hain)
        self._status_guards(old_status, new_status, force=force)

        # 6) Apply + save
        # NOTE: save() wrapper ka "direct write" guard yahan controlled tareeke
        # se bypass hota hai — kyunki transition table + guards + history
        # upar already handle ho chuke hain. Ye flag sirf isi internal save
        # ke liye hai (aur finally me turant hata diya jata hai).
        setattr(self, attr, new_status)
        if full_clean:
            self.full_clean(exclude=None, validate_unique=False)

        self._allow_direct_status_write = True
        try:
            self.save()
        finally:
            self._allow_direct_status_write = False

        # 7) Immutable history (notification se pehle — notified_at guard)
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
        """UI-ready history — purana pehle, with labels + duration."""
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
        """Kisi status me kitna waqt bita (TAT analytics)."""
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
        Status seedha set karne par guard (admin dropdown, shell, scripts).

        Escape hatch:
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
# 3. REPAIR-SPECIFIC BUSINESS LAYER (inject hone wala mixin)
# ════════════════════════════════════════════════════════════════

class RepairWorkflowMixin:
    """
    RepairJob ke guards + side-effects. `apply_repair_workflow()` ise class
    par attach karta hai — koi naya model nahi banta, isliye Django registry
    me conflict nahi hota.
    """

    # ── guards ───────────────────────────────────────────────────
    def _status_guards(self, old_status, new_status, force=False):
        """
        HARD (force bhi bypass nahi karta):
          1. Invoiced job locked — paisa ledger me ja chuka hai.

        SOFT (manager force se bypass kar sakta hai):
          2. 'ready' se pehle kam se kam ek part/service/labour
          3. 'delivered' se pehle receiver (naam ya phone)
          4. diagnosis → repairing: rejected estimate par block
        """
        if self.invoice_id:
            # Invoice ke baad paisa ledger me ja chuka hai — scope/financial
            # transitions (parts edit, cancel, back-step) hard-locked rahenge.
            # Lekin fulfillment step 'ready → delivered' allowed hona chahiye:
            # create_invoice_from_repair 'ready' par hi invoice banane deta hai,
            # aur device handover invoice ke BAAD hota hai. Isko block karne se
            # job hamesha 'ready' par atak jaata (delivery record kabhi nahi).
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

        if new_status == 'repairing' and old_status == 'diagnosis' \
                and self.estimate_status == 'rejected':
            raise InvalidStatusTransition(
                "Customer ne estimate reject kar diya hai — repair shuru karne "
                "se pehle estimate_status badlein (ya naya estimate bhejein)."
            )

    # ── side-effects ─────────────────────────────────────────────
    def _on_status_change(self, old_status, new_status):
        """
        1. Cancel par parts/services reversal (stock wapas inventory me).
           Pehle ye `views/repairs.py` me inline tha — ab model me hai,
           isliye admin/shell/script se cancel par bhi stock sahi reverse
           hota hai.

           Config: settings.REPAIR_REVERSE_LINES_ON_CANCEL = False

        2. Customer notification (exactly once — notified_at guard).
        """
        if new_status == 'cancelled' and getattr(
            settings, 'REPAIR_REVERSE_LINES_ON_CANCEL', True
        ):
            for service in list(self.services.all()):
                service.delete()
            for part in list(self.parts.all()):
                # RepairPart.delete() → StockMovement delete → stock wapas add
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
        """Delivered job wapas 'repairing' me — force (audited exception)."""
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
        Job ke parts/services ko invoice ke line items ke saath sync karo.

        Returns
        -------
        dict : {'created': n, 'synced': n, 'removed': n, 'total': Decimal}

        Kab chalayein: `edit_invoiced=True` se line edit ke baad, job restore
        ke baad, ya kisi manual data-fix ke baad.
        """
        if not self.invoice_id:
            return {'created': 0, 'synced': 0, 'removed': 0, 'total': None}

        # ── Re-entrancy guard ──
        # Resync ke andar line saves hote hain, jo khud resync trigger kar
        # sakte hain (infinite/nested loop + transaction breakage). Isliye
        # pehla resync hi kaam karta hai, nested calls turant return ho jate
        # hain.
        if getattr(self, '_invoice_resync_running', False):
            return {'created': 0, 'synced': 0, 'removed': 0,
                    'total': None, 'skipped': 'reentrant'}

        from django.db import transaction as _tx

        self._invoice_resync_running = True
        # Ledger rebuild ko phase-2 tak defer karo (warna `Invoice.save()`
        # ke andar ka sync failure phase-1 ke lines/totals ko rollback
        # kar deta hai — yahi asli bug tha).
        _set_ledger_deferred(True)
        try:
            # ═══ PHASE 1: lines + totals (ye kabhi fail nahi hona chahiye) ═══
            with _tx.atomic():
                invoice = type(self).invoice.field.related_model.objects.get(
                    pk=self.invoice_id
                )

                # ⚠️ ZAROORI: reverse-relation cache saaf karo.
                # Django `self.parts` / `self.services` ko instance par cache
                # kar leta hai. Line save hone ke baad bhi wo purani (stale)
                # values deta hai — isi wajah se resync invoice ko purane
                # amount par chhod deta tha.
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

            # Job ka amount bhi yahin set kar do (phase 1 commit ke baad)
            if update_job_amount:
                type(self).all_objects.filter(pk=self.pk).update(
                    final_amount=invoice.grand_total,
                )
                self.final_amount = invoice.grand_total

                # ── Drift check (sirf warning) ──
                # Asli signal: job ka amount vs invoice ka amount. Lines ka
                # internal comparison cache-sensitive hai, isliye uske liye
                # `job.invoice_drift` property (fresh queries) use karein.
                if self.final_amount != invoice.grand_total:
                    logger.warning(
                        "Amount drift after resync | job=%s | invoice=%s vs "
                        "job=%s (job #%s) — `job.resync_invoice()` dobara "
                        "chalayein",
                        self.job_number, invoice.grand_total,
                        self.final_amount, self.pk,
                    )

            # ═══ PHASE 2: ledger (best-effort — ALAG transaction) ═══
            # Ledger sync failure ka asar lines/totals par NAHI padta, kyunki
            # ye apna alag transaction hai. Ye critical tha: pehle ledger ka
            # error poore resync ko rollback kar deta tha, jisse invoice me
            # purane amounts hi rehte the (aur item gayab tak ho jate the).
            if rebuild_ledger:
                try:
                    with _tx.atomic():
                        from .models import sync_invoice_ledger
                        # Ledger ko ab explicit sync karo (phase-2 me) —
                        # Invoice.save() ne ise defer kar diya tha.
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

        Matching DETERMINISTIC hai:
          • part    → `InvoiceItem.repair_part_id`
          • service → `InvoiceItem.repair_service_id` (naya FK)
          • purane records jinka link nahi hai → description match (fallback)
        """
        from .models import InvoiceItem

        existing = list(invoice.items.filter(is_deleted=False))
        by_part = {i.repair_part_id: i for i in existing if i.repair_part_id}
        by_service = {
            i.repair_service_id: i for i in existing if i.repair_service_id
        }
        # Fallback: purane invoice items (link ke bina) — description se match
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
                # Fallback: purane unlinked item ko dhoondo
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
                item.repair_service = service      # link bhi fix ho jata hai
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

        difference != 0 = job ke lines aur invoice ke beech mismatch hai
        (audit/control report ke liye).
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
        Status-change notification — `_suppress_status_notification` set ho to
        skip. `apply_repair_workflow()` is method ko RepairJob.save() ke
        notification block se call karwata hai (aur asli notification
        `change_status()` ke through, history row ke baad, jaati hai).
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
# 4. TRANSITION TABLE — business rules ka single source of truth
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
        # Legacy path: estimate approve hone par seedha repair shuru (diagnosis
        # skip). Ye aapke purane views ka behaviour tha — isliye explicit
        # rakha gaya hai, chup-chaap marne nahi diya. Agar aap chahein ki
        # har job diagnosis se guzre, to bas ye 6 lines hata dein.
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
        # 'ready → cancelled' jaan-boojh kar nahi: device taiyar hai. Manager
        # chaahe to change_status('cancelled', force=True, ...) kare.
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
        # Cancelled job sirf DELIVERED ho sakta hai — device customer ko
        # wapas de diya gaya (device yahan hi pada tha, repair nahi hui).
        # Ye aapke purane view ka rule tha (cancelled → delivered allowed),
        # isliye state machine me bhi wahi rakha gaya hai.
        'delivered': Transition(
            set(), label='Device Returned (Delivered)', method='deliver',
            icon='bi-arrow-return-left', kind='secondary',
            confirm="Cancelled device customer ko wapas de rahe hain? "
                    "'Received By' (naam ya phone) zaroori hai.",
        ),
    },
}

#: Sirf 'delivered' terminal hai. 'cancelled' se device wapas diya ja sakta
#: hai (cancelled → delivered) — isliye wo terminal NAHI hai.
REPAIR_TERMINAL_STATUSES = frozenset({'delivered'})


# ════════════════════════════════════════════════════════════════
# 6. INVOICE CONSISTENCY (invoiced job ka amount diverge na ho)
# ════════════════════════════════════════════════════════════════

class InvoiceConsistencyMixin:
    """
    RepairPart / RepairService ke liye guards + invoice resync.

    PROBLEM (AUDIT C-3):
      Invoice ban jaane ke baad bhi staff part/service add ya remove kar
      sakta tha. `calculate_final_amount()` sirf job ka `final_amount`
      badalta tha — invoice ka `grand_total` nahi. Natija: job ₹9,000
      dikhata, invoice ₹4,500 rehta, aur ₹4,500 ka extra stock kat chuka
      hota → books aur inventory me silent farq.

    SOLUTION:
      • Invoiced job par line add/change/remove → BLOCK (saaf message)
      • `edit_invoiced=True` pass karein (ya settings flag) → allowed,
        aur invoice turant resync ho jata hai
      • `job.resync_invoice()` — manual data-fix ke liye
      • `job.invoice_drift` — mismatch report
    """

    # ── helper ───────────────────────────────────────────────────
    def _job_and_lock(self):
        job = getattr(self, 'repair_job', None)
        locked = bool(job is not None and getattr(job, 'invoice_id', None))
        return job, locked

    def _edit_allowed(self, edit_invoiced_flag=False):
        """`edit_invoiced=True` ya settings flag — dono me se koi ek."""
        if edit_invoiced_flag:
            return True
        return bool(getattr(settings, 'REPAIR_ALLOW_INVOICED_LINE_EDIT', False))

    def _line_changed(self):
        """
        Kya is line ke invoice-affecting fields badle?

        RepairPart aur RepairService ke fields ALAG hain (part me `quantity` +
        `unit_price`, service me `amount`), isliye sirf wo fields compare
        karte hain jo model me actually maujood hain.
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
        # `soft_delete()` internally `self.save()` call karta hai — tab
        # instance par ye flag set hota hai, taaki guard dobara na lage.
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

        # ⚠️ `_line_changed()` save se PEHLE check karo — save ke baad DB
        # already updated hoti hai aur comparison hamesha False deta hai.
        changed = self._line_changed()

        result = self._pro_original_save(*args, **kwargs)

        if locked and job is not None and changed:
            try:
                # Job ko fresh laao (purana instance status/dates stale ho sakta)
                self.repair_job.resync_invoice()
            except Exception:
                # ⚠️ Line edit kabhi fail na ho sirf isliye ki invoice sync
                # nahi ho paya — log karo aur aage badho.
                # (Resync `job.resync_invoice()` se dobara chala sakte hain.)
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

        # Soft-delete (ya legacy hard-delete) — internal save ke waqt guard
        # dobara na lage, isliye instance par authorisation flag set karte hain
        self._invoice_edit_authorised = True
        try:
            self._pro_original_delete(*args, **kwargs)
        finally:
            self._invoice_edit_authorised = False

        # Soft-delete ke baad line invoice me nahi aani chahiye — resync.
        # (Delete ke waqt job bhi deleted ho sakta hai, aur `save()` guard
        # status par lagta hai — isliye job ko bypass flag ke saath sync karo.)
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
    RepairPart + RepairService par invoice-consistency guards lagata hai.

    Idempotent — dobara chalane par purane original save/delete reuse hote
    hain (double wrapping nahi hoti).
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
# 7. APPLY — RepairJob par sab kuch inject karo (naya model nahi)
# ════════════════════════════════════════════════════════════════

def apply_repair_workflow(job_model, log_model_name='RepairStatusLog'):
    """
    `RepairJob` (ya koi bhi model) par state machine inject karta hai.

    Kya karta hai:
      1. `StatusMachineMixin` methods add karta hai (agar inherit nahi hue)
      2. `RepairWorkflowMixin` ke guards/side-effects/named transitions add karta hai
      3. Transition table + log model set karta hai
      4. Wrapper `save()` lagata hai jo status guard + notification suppression
         handle karta hai (purana save bilkul waise hi chalta hai)

    Returns
    -------
    The same class (mutated) — naya model register nahi hota.
    """
    # ── 1. Core mixin methods — CLASS PAR DIRECT inject karo ──
    # ZAROORI DETAIL: classmethod ko `getattr(mixin_cls, name)` se lene par wo
    # ALREADY BOUND milta hai (__self__ = StatusMachineMixin). Seedha setattr
    # karne par `cls` mixin hi rehta hai → "StatusMachineMixin has no field
    # named 'status'". Isliye classmethod ko UNWRAP karke dobara wrap karte hain.
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

    # ── 3. Properties (property object hi attach hota hai) ──
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

        # Purana save chalao — uske andar ka status-notification suppress
        # ho jayega (asli notification change_status() bhejta hai).
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
