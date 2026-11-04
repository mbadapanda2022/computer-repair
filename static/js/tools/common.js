window.TT = window.TT || {};

TT.fmt = function (n, sig = 5) {
    if (!isFinite(n)) return '—';
    if (n === 0) return '0';
    return parseFloat(n.toPrecision(sig)).toString();
};

TT.engV = function (v) {
    if (!isFinite(v)) return '—';
    const a = Math.abs(v);
    if (a === 0) return '0 V';
    if (a < 1e-3) return TT.fmt(v * 1e6) + ' µV';
    if (a < 1)    return TT.fmt(v * 1e3) + ' mV';
    if (a >= 1e3) return TT.fmt(v / 1e3) + ' kV';
    return TT.fmt(v) + ' V';
};

TT.engI = function (i) {
    if (!isFinite(i)) return '—';
    const a = Math.abs(i);
    if (a === 0) return '0 A';
    if (a < 1e-3) return TT.fmt(i * 1e6) + ' µA';
    if (a < 1)    return TT.fmt(i * 1e3) + ' mA';
    return TT.fmt(i) + ' A';
};

TT.engP = function (p) {
    if (!isFinite(p)) return '—';
    const a = Math.abs(p);
    if (a === 0) return '0 W';
    if (a < 1e-3) return TT.fmt(p * 1e6) + ' µW';
    if (a < 1)    return TT.fmt(p * 1e3) + ' mW';
    return TT.fmt(p) + ' W';
};

TT.engR = function (r) {
    if (!isFinite(r)) return '—';
    const a = Math.abs(r);
    if (a === 0) return '0 Ω';
    if (a < 1)    return TT.fmt(r * 1e3) + ' mΩ';
    if (a >= 1e6) return TT.fmt(r / 1e6) + ' MΩ';
    if (a >= 1e3) return TT.fmt(r / 1e3) + ' kΩ';
    return TT.fmt(r) + ' Ω';
};

TT.COLOR_DATA = {
    black:  { name:'Black',  hex:'#1a1a1a', digit:0, mult:1,     tol:null, tc:null },
    brown:  { name:'Brown',  hex:'#8B4513', digit:1, mult:10,    tol:1,    tc:100 },
    red:    { name:'Red',    hex:'#DC2626', digit:2, mult:100,   tol:2,    tc:50 },
    orange: { name:'Orange', hex:'#F97316', digit:3, mult:1e3,   tol:null, tc:15 },
    yellow: { name:'Yellow', hex:'#FBBF24', digit:4, mult:1e4,   tol:null, tc:25 },
    green:  { name:'Green',  hex:'#16A34A', digit:5, mult:1e5,   tol:0.5,  tc:null },
    blue:   { name:'Blue',   hex:'#2563EB', digit:6, mult:1e6,   tol:0.25, tc:10 },
    violet: { name:'Violet', hex:'#7C3AED', digit:7, mult:1e7,   tol:0.1,  tc:5 },
    grey:   { name:'Grey',   hex:'#9CA3AF', digit:8, mult:1e8,   tol:0.05, tc:null },
    white:  { name:'White',  hex:'#F9FAFB', digit:9, mult:1e9,   tol:null, tc:null },
    gold:   { name:'Gold',   hex:'#D4AF37', digit:null, mult:0.1,  tol:5,  tc:null },
    silver: { name:'Silver', hex:'#C0C0C0', digit:null, mult:0.01, tol:10, tc:null },
};

TT.nearestE24 = function (ohms) {
    const E24 = [1.0,1.1,1.2,1.3,1.5,1.6,1.8,2.0,2.2,2.4,2.7,3.0,3.3,3.6,3.9,4.3,4.7,5.1,5.6,6.2,6.8,7.5,8.2,9.1];
    if (!isFinite(ohms) || ohms <= 0) return null;
    const exp = Math.floor(Math.log10(ohms));
    let best = null, diff = Infinity;
    for (let e = exp - 1; e <= exp + 1; e++) {
        for (const v of E24) {
            const cand = v * Math.pow(10, e);
            const d = Math.abs(Math.log(cand / ohms));
            if (d < diff) { diff = d; best = cand; }
        }
    }
    return best;
};
/* ── tool bootstrapping ─────────────────────────────────────────────────
 * Each tool registers TT.tools[slug] = function (rootElement) { … } and the
 * shell calls TT.boot(pane) once for the initial server render and again
 * after every HTMX swap. A tool is initialised at most once per element, so
 * re-booting a pane that is already live cannot stack duplicate listeners.
 */
TT.tools = TT.tools || {};
TT.calc = TT.calc || {};
TT.INITED = 'ttInited';

/* Tell a tool its field just changed, the way a real keystroke would: the
 * event has to start on the field itself, because some tools listen on an
 * inner container (resistor bands) and others on the tool root. */
TT.poke = function (field) {
    ['input', 'change'].forEach(function (type) {
        field.dispatchEvent(new Event(type, {bubbles: true}));
    });
};

/* Put every control inside a tool back the way the page first served it:
 * typed values use their initial value attribute, selects their marked
 * default option. Read-only outputs are left to the tool's own recompute. */
TT.resetControls = function (scope) {
    var touched = [];
    scope.querySelectorAll('input, select, textarea').forEach(function (field) {
        if (field.readOnly) return;
        var before = field.type === 'checkbox' || field.type === 'radio'
                     ? field.checked : field.value;
        if (field.tagName === 'SELECT') {
            var chosen = null;
            for (var i = 0; i < field.options.length; i++) {
                if (field.options[i].defaultSelected) { chosen = field.options[i].value; break; }
            }
            field.value = chosen !== null ? chosen
                          : (field.options.length ? field.options[0].value : '');
        } else if (field.type === 'checkbox' || field.type === 'radio') {
            field.checked = field.defaultChecked;
        } else {
            field.value = field.defaultValue;
        }
        if (before !== (field.type === 'checkbox' || field.type === 'radio'
                        ? field.checked : field.value)) touched.push(field);
    });
    touched.forEach(TT.poke);
    return touched;
};

TT.boot = function (scope) {
    if (!scope) return;
    var nodes = [];
    if (scope.matches && scope.matches('.tt-tool[data-tool]')) nodes.push(scope);
    scope.querySelectorAll('.tt-tool[data-tool]').forEach(function (el) {
        nodes.push(el);
    });
    nodes.forEach(function (el) {
        if (el.dataset[TT.INITED]) return;
        var init = TT.tools[el.dataset.tool];
        if (typeof init !== 'function') return;
        el.dataset[TT.INITED] = '1';
        el.querySelectorAll('[data-clear]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                /* Mode buttons keep their state in the tool's own closure, so
                 * re-click the one the page shipped as active rather than
                 * guessing at its private variables. */
                el.querySelectorAll('[data-default-mode]').forEach(function (mode) {
                    mode.click();
                });
                TT.resetControls(el);
                var first = el.querySelector('input:not([readonly])');
                if (first) first.focus();
            });
        });
        /* "Try:" chips — [data-target=<input id>][data-value=<text>],
         * optionally also setting a unit select with
         * [data-unit-target=<select id>][data-unit=<option value>]. */
        el.querySelectorAll('[data-target][data-value]').forEach(function (chip) {
            chip.addEventListener('click', function () {
                var box = el.querySelector('#' + chip.dataset.target);
                if (!box) return;
                if (chip.dataset.unitTarget && chip.dataset.unit) {
                    var picker = el.querySelector('#' + chip.dataset.unitTarget);
                    if (picker) {
                        picker.value = chip.dataset.unit;
                        TT.poke(picker);
                    }
                }
                box.value = chip.dataset.value;
                TT.poke(box);
                box.focus();
            });
        });
        init(el);
    });
};

/* ── small shared DOM helpers ────────────────────────────────────────── */
TT.q = function (root, id) { return root.querySelector('#' + id); };

TT.num = function (value) {
    var n = parseFloat(String(value).replace(/,/g, ''));
    return isFinite(n) ? n : NaN;
};

/* Recompute on every keystroke/select inside the tool. */
TT.onInput = function (root, fn) {
    root.addEventListener('input', fn);
    root.addEventListener('change', fn);
    fn();
};

/* One "label : value" line inside a result box. */
TT.line = function (label, value, cls) {
    var row = document.createElement('div');
    row.className = 'tt-out-row' + (cls ? ' ' + cls : '');
    var l = document.createElement('span');
    l.className = 'tt-out-label';
    l.textContent = label;
    var v = document.createElement('span');
    v.className = 'tt-out-value';
    v.textContent = value;
    row.appendChild(l);
    row.appendChild(v);
    return row;
};

TT.lines = function (container, rows) {
    container.innerHTML = '';
    rows.forEach(function (r) {
        if (r === null || r === undefined) return;
        container.appendChild(TT.line(r[0], r[1], r[2]));
    });
};

/* The single-line placeholder used before enough input is available. */
TT.empty = function (container, msg) {
    container.innerHTML = '';
    var p = document.createElement('div');
    p.className = 'tt-out-empty';
    p.textContent = msg || 'Enter values to calculate';
    container.appendChild(p);
};

/* ── formatting used across the tool groups ──────────────────────────── */

/* Rupee amount with Indian digit grouping (₹ 1,23,456.78). */
TT.money = function (n, symbol) {
    if (!isFinite(n)) return '—';
    var digits;
    try {
        digits = new Intl.NumberFormat('en-IN', {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        }).format(n);
    } catch (err) {
        digits = n.toFixed(2);
    }
    return (symbol || '₹') + ' ' + digits;
};

/* Round to paise the way a bill does (half up), so the tool and the
 * invoice never disagree by one paisa. */
TT.round2 = function (n) {
    if (!isFinite(n)) return NaN;
    return Math.round((n + Number.EPSILON) * 100) / 100;
};

/* Seconds into the two biggest units — "1 h 20 min", "3 d 4 h", "90 s". */
TT.duration = function (seconds) {
    if (!isFinite(seconds) || seconds < 0) return '—';
    if (seconds < 1) return TT.fmt(seconds * 1000) + ' ms';
    if (seconds < 60) return TT.fmt(seconds) + ' s';
    var left = Math.round(seconds);
    var d = Math.floor(left / 86400); left -= d * 86400;
    var h = Math.floor(left / 3600);  left -= h * 3600;
    var m = Math.floor(left / 60);    left -= m * 60;
    var parts = [];
    if (d) parts.push(d + ' d');
    if (h) parts.push(h + ' h');
    if (m) parts.push(m + ' min');
    if (left && parts.length < 2) parts.push(left + ' s');
    return parts.slice(0, 2).join(' ');
};

/* Hours into "18 h 30 min" style, which reads better than seconds here. */
TT.hours = function (h) {
    if (!isFinite(h) || h < 0) return '—';
    return TT.duration(h * 3600);
};

/* ── unit scales ───────────────────────────────────────────────────────
 * Every number box on these pages is multiplied by the unit beside it, so
 * the maths below always runs in one base unit and a technician can type
 * "20" with mA selected instead of 0.02. Storage uses binary prefixes on
 * purpose — that is how Windows reports a file or a drive.
 */
TT.UNITS = {
    voltage: {V: 1, mV: 1e-3, kV: 1e3},
    current: {A: 1, mA: 1e-3, 'µA': 1e-6},
    resistance: {'Ω': 1, 'mΩ': 1e-3, 'kΩ': 1e3, 'MΩ': 1e6},
    power: {W: 1, mW: 1e-3, kW: 1e3},
    capacitance: {pF: 1, nF: 1e3, 'µF': 1e6},
    charge: {mAh: 1, Ah: 1e3},
    energy: {Wh: 1, kWh: 1e3},
    length: {inch: 2.54, cm: 1, mm: 0.1, m: 10},
    /* Drive makers and network tools count KB/MB/GB as 1000; Windows counts
     * KiB/MiB/GiB as 1024, so both are offered and the tool prints the exact
     * byte count to remove the guesswork. */
    storage: {
        KB: 1e3, MB: 1e6, GB: 1e9, TB: 1e12,
        KiB: 1024, MiB: 1048576, GiB: 1073741824, TiB: 1099511627776
    },
    speed: {
        'bps': 1, 'Kbps': 1e3, 'Mbps': 1e6, 'Gbps': 1e9,
        'B/s': 8, 'KB/s': 8e3, 'MB/s': 8e6, 'GB/s': 8e9
    },
    time: {seconds: 1, minutes: 60, hours: 3600, days: 86400}
};

/* value × the scale of `unit` in that group; unknown unit or blank ⇒ 1. */
TT.toSi = function (value, group, unit) {
    var n = typeof value === 'number' ? value : TT.num(value);
    if (!isFinite(n)) return NaN;
    var scales = TT.UNITS[group];
    if (!scales) return n;
    var factor = scales[unit];
    return isFinite(factor) ? n * factor : n;
};

/* [[code, label], …] for a unit <select>, in the order written above. */
TT.unitOptions = function (group) {
    var scales = TT.UNITS[group] || {};
    return Object.keys(scales).map(function (code) { return [code, code]; });
};
