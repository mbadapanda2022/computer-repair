/*
 * Tech Tools — Electronics group.
 *
 * Each tool registers TT.tools[<slug>] = init(root); the shell in
 * static/js/tech-tools.js calls it once for a server-rendered pane and again
 * after every HTMX swap. The maths itself lives under TT.calc as pure
 * functions so it can be checked without a browser.
 *
 * The slug keys here must match accounting/tools_registry.py exactly —
 * accounting/tests_tools.py fails the suite if one goes missing.
 */
(function (TT) {
    'use strict';

    var C = TT.calc;
    var COLORS = TT.COLOR_DATA;

    var DIGIT_COLORS = ['black', 'brown', 'red', 'orange', 'yellow', 'green',
                        'blue', 'violet', 'grey', 'white'];
    var MULT_COLORS = DIGIT_COLORS.concat(['gold', 'silver']);
    var TOL_COLORS = ['brown', 'red', 'green', 'blue', 'violet', 'grey',
                      'gold', 'silver'];
    var TC_COLORS = ['brown', 'red', 'orange', 'yellow', 'blue', 'violet'];

    /* Which role each band plays, the colour it starts on, and where the
     * painted band sits across the body so the drawing looks real. */
    var BAND_MODES = {
        '4': {
            roles: ['digit', 'digit', 'mult', 'tol'],
            defaults: ['brown', 'black', 'orange', 'gold'],
            positions: [16, 32, 48, 80]
        },
        '5': {
            roles: ['digit', 'digit', 'digit', 'mult', 'tol'],
            defaults: ['brown', 'black', 'black', 'red', 'brown'],
            positions: [13, 27, 41, 55, 80]
        },
        '6': {
            roles: ['digit', 'digit', 'digit', 'mult', 'tol', 'tc'],
            defaults: ['brown', 'black', 'black', 'red', 'brown', 'brown'],
            positions: [10, 22, 34, 46, 58, 78]
        }
    };

    /* ── resistor colour code ────────────────────────────────────────── */
    C.colorResistor = function (colors, bandCount) {
        var mode = BAND_MODES[String(bandCount)];
        if (!mode) return {error: 'Unsupported band count.'};
        if (!colors || colors.length !== mode.roles.length) {
            return {error: 'Not enough bands selected.'};
        }
        var digits = '';
        var ohms = NaN;
        var tolerance = null;
        var ppm = null;
        for (var i = 0; i < mode.roles.length; i++) {
            var color = COLORS[colors[i]];
            if (!color) return {error: 'Unknown colour: ' + colors[i]};
            var role = mode.roles[i];
            if (role === 'digit') {
                if (color.digit === null) {
                    return {error: 'Gold and silver cannot be digit bands.'};
                }
                digits += color.digit;
            } else if (role === 'mult') {
                ohms = parseInt(digits, 10) * color.mult;
            } else if (role === 'tol') {
                tolerance = color.tol;
            } else if (role === 'tc') {
                ppm = color.tc;
            }
        }
        if (!isFinite(ohms)) return {error: 'Select the multiplier band too.'};
        return {ohms: ohms, tolerance: tolerance, ppm: ppm};
    };

    TT.tools['resistor-color'] = function (root) {
        var body = TT.q(root, 'rcBody');
        var selectors = TT.q(root, 'rcSelectors');
        var head = TT.q(root, 'rcHead');
        var out = TT.q(root, 'rcOut');
        var mode = '4';

        function optionsFor(role) {
            if (role === 'digit') return DIGIT_COLORS;
            if (role === 'mult') return MULT_COLORS;
            if (role === 'tol') return TOL_COLORS;
            return TC_COLORS;
        }

        function build() {
            var cfg = BAND_MODES[mode];
            body.innerHTML = '';
            selectors.innerHTML = '';

            cfg.positions.forEach(function (pos, i) {
                var band = document.createElement('div');
                band.className = 'r-band';
                band.id = 'rcvb' + i;
                band.style.left = pos + '%';
                body.appendChild(band);
            });

            cfg.roles.forEach(function (role, i) {
                var col = document.createElement('div');
                col.className = 'col-6 col-md-4';
                var label = document.createElement('label');
                label.className = 'tt-label';
                label.setAttribute('for', 'rcSel' + i);
                label.textContent = 'Band ' + (i + 1) + ' · ' + role;
                var select = document.createElement('select');
                select.className = 'form-select form-select-sm';
                select.id = 'rcSel' + i;
                select.setAttribute('aria-label', label.textContent);
                optionsFor(role).forEach(function (name) {
                    var opt = document.createElement('option');
                    opt.value = name;
                    opt.textContent = COLORS[name].name;
                    // Marks the tool's own starting band so "Reset" (which
                    // restores each control's default) lands back here.
                    opt.defaultSelected = (name === cfg.defaults[i]);
                    select.appendChild(opt);
                });
                select.value = cfg.defaults[i];
                col.appendChild(label);
                col.appendChild(select);
                selectors.appendChild(col);
            });
            calc();
        }

        function calc() {
            var colors = [];
            selectors.querySelectorAll('select').forEach(function (sel, i) {
                colors.push(sel.value);
                var band = TT.q(root, 'rcvb' + i);
                if (band) band.style.backgroundColor = COLORS[sel.value].hex;
            });
            var r = C.colorResistor(colors, mode);
            if (r.error) {
                head.textContent = '—';
                TT.empty(out, r.error);
                return;
            }
            head.textContent = TT.engR(r.ohms);
            var rows = [['Marked value', TT.fmt(r.ohms) + ' Ω']];
            if (r.tolerance !== null) {
                rows.push(['Tolerance', '±' + TT.fmt(r.tolerance) + ' %']);
                rows.push(['A good part reads between',
                    TT.engR(r.ohms * (1 - r.tolerance / 100)) + ' and ' +
                    TT.engR(r.ohms * (1 + r.tolerance / 100))]);
            } else {
                rows.push(['Tolerance', 'not marked']);
            }
            if (r.ppm !== null) {
                rows.push(['Temp. coefficient', TT.fmt(r.ppm) + ' ppm/K']);
            }
            var nearest = TT.nearestE24(r.ohms);
            if (nearest) rows.push(['Nearest stock value', TT.engR(nearest)]);
            TT.lines(out, rows);
        }

        root.querySelectorAll('.band-mode').forEach(function (btn) {
            btn.addEventListener('click', function () {
                root.querySelectorAll('.band-mode').forEach(function (b) {
                    b.classList.remove('active');
                });
                btn.classList.add('active');
                mode = btn.dataset.bands;
                build();
            });
        });
        selectors.addEventListener('change', calc);
        build();
    };

    /* ── SMD chip resistor markings ──────────────────────────────────── */
    /* "103" / "1502": every digit except the last is significant, the last
     * says how many zeros follow. "R" is the decimal point, so 4R7 = 4.7 Ω
     * and R22 = 0.22 Ω.
     *
     * EIA-96 letter codes ("68C" style) are deliberately NOT decoded: the
     * published multiplier-letter tables disagree with each other, and a
     * wrong value on a technician's screen is worse than no value. */
    C.smdResistor = function (raw) {
        var code = String(raw || '').trim().toUpperCase().replace(/\s+/g, '');
        if (!code) return {error: 'Enter a marking such as 103, 4R7 or 1502.'};
        if (!/^[0-9R]+$/.test(code)) {
            return {error: 'Only digits and R (the decimal point) can be read.'};
        }

        var ohms;
        var reading;
        if (code.indexOf('R') !== -1) {
            if ((code.match(/R/g) || []).length > 1) {
                return {error: 'A marking has only one R.'};
            }
            var halves = code.split('R');
            if (!/^[0-9]+$/.test(halves[1])) {
                return {error: 'There must be digits after the R.'};
            }
            var whole = halves[0] === '' ? 0 : parseInt(halves[0], 10);
            ohms = parseFloat(whole + '.' + halves[1]);
            reading = 'R marks the decimal point — ' + whole + '.' + halves[1] + ' Ω';
        } else {
            if (code.length < 3 || code.length > 4) {
                return {error: 'Numeric SMD markings are 3 or 4 digits, e.g. 103 or 1502.'};
            }
            var zeros = parseInt(code.charAt(code.length - 1), 10);
            var sig = code.slice(0, code.length - 1);
            ohms = parseInt(sig, 10) * Math.pow(10, zeros);
            reading = sig + ' with ' + zeros + ' zero' + (zeros === 1 ? '' : 's') +
                      ' = ' + TT.fmt(ohms) + ' Ω';
        }

        return {
            code: code,
            ohms: ohms,
            reading: reading,
            jumper: ohms === 0,
            nearest: TT.nearestE24(ohms)
        };
    };

    /* The reverse direction: what a shop stock value would be marked as. */
    C.toSmdCode = function (ohms) {
        if (!isFinite(ohms) || ohms <= 0) return '—';
        if (ohms >= 10) {
            var str = String(Math.round(ohms));
            if (str.length < 3) return str + '0';
            return str.slice(0, 2) + String(str.length - 2);
        }
        if (ohms >= 1) return ohms.toFixed(1).replace('.', 'R');
        if (ohms >= 0.1) return 'R' + ohms.toFixed(2).split('.')[1];
        return TT.fmt(ohms * 1000) + 'm';
    };

    TT.tools['smd-resistor'] = function (root) {
        var input = TT.q(root, 'smdCode');
        var head = TT.q(root, 'smdHead');
        var out = TT.q(root, 'smdOut');

        TT.onInput(root, function () {
            var r = C.smdResistor(input.value);
            if (r.error) {
                head.textContent = '—';
                TT.empty(out, r.error);
                return;
            }
            head.textContent = r.jumper ? '0 Ω — jumper link' : TT.engR(r.ohms);
            var rows = [
                ['Marking', r.code],
                ['How it reads', r.reading]
            ];
            if (!r.jumper && r.nearest) {
                var off = (r.ohms - r.nearest) / r.nearest * 100;
                if (Math.abs(off) > 0.01) {
                    rows.push(['Nearest stock value',
                        TT.engR(r.nearest) + ' (' + C.toSmdCode(r.nearest) + ')']);
                    rows.push(['That part differs by',
                        (off > 0 ? '+' : '−') + Math.abs(off).toFixed(1) + ' %']);
                } else {
                    rows.push(['Standard E24 value', 'yes — ' + C.toSmdCode(r.ohms)]);
                }
            }
            TT.lines(out, rows);
        });
    };

    /* ── capacitor markings ──────────────────────────────────────────── */
    /* Two notations are seen on the bench: the 3-digit pico-farad code
     * ("104" = 10 × 10^4 pF = 100 nF) and the decimal-letter form used on
     * film and electrolytic parts ("1n" = 1 nF, "4n7" = 4.7 nF,
     * "2u2" = 2.2 µF). */
    C.CAP_TOLERANCE = {
        B: {pf: 0.1, name: '±0.1 pF'},
        C: {pf: 0.25, name: '±0.25 pF'},
        D: {pf: 0.5, name: '±0.5 pF'},
        F: {pct: 1},
        G: {pct: 2},
        J: {pct: 5},
        K: {pct: 10},
        M: {pct: 20},
        N: {pct: 30}
    };

    C.capacitorCode = function (raw) {
        var code = String(raw || '').trim().toUpperCase().replace(/\s+/g, '');
        if (!code) return {error: 'Enter a marking such as 104, 225 or 4n7.'};

        /* "104K" — the trailing letter is the tolerance, not part of the
         * value, so read it off the marking instead of asking again.
         * N and U are excluded: after a numeric code they are far more often
         * the nano / micro unit marker ("100n" = 100 nF), and reading one as
         * a tolerance letter would print a value 10x or 1000x out. */
        var marked = '';
        var loose = code.match(/^([0-9]{3})([BCFGJKM])$/);
        if (loose && C.CAP_TOLERANCE[loose[2]]) {
            code = loose[1];
            marked = loose[2];
        }
        if (!/^[0-9NUµ]+$/.test(code)) {
            return {error: 'Use digits, or n / u as the decimal letter.'};
        }

        var picofarad;
        var reading;
        if (/[NUµ]/.test(code)) {
            var sides = code.split(/[NUµ]/);
            if (sides.length !== 2) return {error: 'Only one n or u per marking.'};
            if (!/^[0-9]*$/.test(sides[0]) || !/^[0-9]*$/.test(sides[1])) {
                return {error: 'There must be digits around the n or u.'};
            }
            if (!sides[0] && !sides[1]) return {error: 'No digits in that marking.'};
            var base = parseFloat((sides[0] || '0') + '.' + (sides[1] || '0'));
            if (!isFinite(base)) return {error: 'No usable digits around that letter.'};
            var letter = code.match(/[NUµ]/)[0];
            picofarad = letter === 'N' ? base * 1e3 : base * 1e6;
            reading = (sides[0] || '0') + '.' + (sides[1] || '0') + ' ' +
                      (letter === 'N' ? 'nF' : 'µF') +
                      ' — the letter is the decimal point';
        } else {
            if (!/^[0-9]{3}$/.test(code)) {
                return {error: 'The numeric ceramic code is 3 digits, e.g. 104.'};
            }
            var zeros = parseInt(code.charAt(2), 10);
            picofarad = parseInt(code.slice(0, 2), 10) * Math.pow(10, zeros);
            reading = code.slice(0, 2) + ' with ' + zeros +
                      ' zero' + (zeros === 1 ? '' : 's') + ' pF';
        }
        return {
            code: code,
            pf: picofarad,
            nf: picofarad / 1e3,
            uf: picofarad / 1e6,
            toleranceLetter: marked,
            reading: reading
        };
    };

    TT.tools['capacitor-code'] = function (root) {
        var input = TT.q(root, 'capCode');
        var tol = TT.q(root, 'capTol');
        var head = TT.q(root, 'capHead');
        var out = TT.q(root, 'capOut');

        TT.onInput(root, function () {
            var r = C.capacitorCode(input.value);
            if (r.error) {
                head.textContent = '—';
                TT.empty(out, r.error);
                return;
            }
            head.textContent = r.uf >= 1 ? TT.fmt(r.uf) + ' µF'
                           : r.nf >= 1 ? TT.fmt(r.nf) + ' nF'
                           : TT.fmt(r.pf) + ' pF';
            var rows = [
                ['Marking', r.code],
                ['How it reads', r.reading],
                ['In pF', TT.fmt(r.pf)],
                ['In nF', TT.fmt(r.nf)],
                ['In µF', TT.fmt(r.uf)]
            ];
            var letter = r.toleranceLetter || (tol.value || '').toUpperCase();
            var t = C.CAP_TOLERANCE[letter];
            if (t) {
                if (t.pct) {
                    rows.push(['Tolerance ' + tol.value, '±' + t.pct + ' %']);
                    rows.push(['A good part reads between',
                        TT.fmt(r.pf * (1 - t.pct / 100)) + ' and ' +
                        TT.fmt(r.pf * (1 + t.pct / 100)) + ' pF']);
                } else {
                    rows.push(['Tolerance ' + tol.value, t.name]);
                    rows.push(['A good part reads between',
                        TT.fmt(r.pf - t.pf) + ' and ' +
                        TT.fmt(r.pf + t.pf) + ' pF']);
                }
            }
            TT.lines(out, rows);
        });
    };

    /* ── Ohm's law + power ───────────────────────────────────────────── */
    /* Any two of the four are entered; the pair decides the formulas, and
     * each formula is shown beside the value it produced. */
    C.ohmsLaw = function (known) {
        var v = known.v, i = known.i, r = known.r, p = known.p;
        var keys = ['v', 'i', 'r', 'p'].filter(function (k) {
            return isFinite(known[k]) && known[k] > 0;
        });
        if (keys.length !== 2) {
            return {error: 'Fill in exactly any two of the four fields.'};
        }
        var pair = keys.slice().sort().join('');
        var out = {};
        var how = {};

        if (pair === 'iv') {
            out.r = v / i; how.r = 'R = V ÷ I';
            out.p = v * i; how.p = 'P = V × I';
        } else if (pair === 'rv') {
            out.i = v / r; how.i = 'I = V ÷ R';
            out.p = v * v / r; how.p = 'P = V² ÷ R';
        } else if (pair === 'pv') {
            out.i = p / v; how.i = 'I = P ÷ V';
            out.r = v * v / p; how.r = 'R = V² ÷ P';
        } else if (pair === 'ir') {
            out.v = i * r; how.v = 'V = I × R';
            out.p = i * i * r; how.p = 'P = I² × R';
        } else if (pair === 'ip') {
            out.v = p / i; how.v = 'V = P ÷ I';
            out.r = p / (i * i); how.r = 'R = P ÷ I²';
        } else if (pair === 'pr') {
            out.v = Math.sqrt(p * r); how.v = 'V = √(P × R)';
            out.i = Math.sqrt(p / r); how.i = 'I = √(P ÷ R)';
        }

        var values = {v: v, i: i, r: r, p: p};
        Object.keys(out).forEach(function (k) { values[k] = out[k]; });
        ['v', 'i', 'r', 'p'].forEach(function (k) {
            if (!isFinite(values[k])) {
                values[k] = NaN;
            }
        });
        return {values: values, solved: Object.keys(how), how: how};
    };

    TT.tools['ohms-law'] = function (root) {
        var fields = {
            v: TT.q(root, 'olV'),
            i: TT.q(root, 'olI'),
            r: TT.q(root, 'olR'),
            p: TT.q(root, 'olP')
        };
        var units = {
            v: TT.q(root, 'olVUnit'),
            i: TT.q(root, 'olIUnit'),
            r: TT.q(root, 'olRUnit'),
            p: TT.q(root, 'olPUnit')
        };
        var groups = {v: 'voltage', i: 'current', r: 'resistance', p: 'power'};
        var head = TT.q(root, 'olHead');
        var out = TT.q(root, 'olOut');

        TT.onInput(root, function () {
            var known = {};
            Object.keys(fields).forEach(function (k) {
                known[k] = TT.toSi(fields[k].value, groups[k], units[k].value);
            });
            var r = C.ohmsLaw(known);
            if (r.error) {
                head.textContent = '—';
                TT.empty(out, r.error);
                return;
            }
            head.textContent = TT.engV(r.values.v) + ' · ' + TT.engI(r.values.i);
            var shown = {
                v: ['Voltage', TT.engV(r.values.v)],
                i: ['Current', TT.engI(r.values.i)],
                r: ['Resistance', TT.engR(r.values.r)],
                p: ['Power', TT.engP(r.values.p)]
            };
            TT.lines(out, ['v', 'i', 'r', 'p'].map(function (k) {
                var solved = r.how[k];
                return [shown[k][0] + (solved ? '' : ' (entered)'),
                        shown[k][1] + (solved ? ' · ' + solved : ''),
                        solved ? 'tt-out-solved' : ''];
            }));
        });
    };

    /* ── LED series resistor ─────────────────────────────────────────── */
    /* Typical forward drops for choosing quickly; the number box is always
     * editable because the real part decides. */
    C.LED_PRESETS = {
        red: '2.0',
        orange: '2.1',
        yellow: '2.1',
        green: '2.2',
        green_bright: '3.2',
        blue: '3.2',
        white: '3.2',
        uv: '3.3',
        infrared: '1.4',
        diode: '0.7'
    };

    C.RESISTOR_RATINGS = [
        {w: 0.0625, name: '1/10 W'},
        {w: 0.125, name: '1/8 W'},
        {w: 0.25, name: '1/4 W'},
        {w: 0.5, name: '1/2 W'},
        {w: 1, name: '1 W'},
        {w: 2, name: '2 W'},
        {w: 3, name: '3 W'},
        {w: 5, name: '5 W'}
    ];

    /* Doubled, because a resistor run near its rating gets hot and drifts. */
    C.nextResistorRating = function (watts) {
        for (var i = 0; i < C.RESISTOR_RATINGS.length; i++) {
            if (C.RESISTOR_RATINGS[i].w >= watts * 2) return C.RESISTOR_RATINGS[i];
        }
        return null;
    };

    C.ledResistor = function (supply, drop, milliamps, count) {
        if (!isFinite(supply) || supply <= 0) return {error: 'Enter the supply voltage.'};
        if (!isFinite(drop) || drop <= 0) return {error: 'Enter the LED forward voltage.'};
        if (!isFinite(milliamps) || milliamps <= 0) return {error: 'Enter the LED current.'};
        var leds = (isFinite(count) && count >= 1) ? Math.floor(count) : 1;
        var total = drop * leds;
        if (supply <= total) {
            return {error: 'Supply must be above ' + total.toFixed(2) +
                           ' V for ' + leds + ' LED' + (leds > 1 ? 's' : '') +
                           ' in series.'};
        }
        var amps = milliamps / 1000;
        var across = supply - total;
        var ohms = across / amps;
        var watts = across * amps;
        var nearest = TT.nearestE24(ohms);
        return {
            ohms: ohms,
            watts: watts,
            across: across,
            leds: leds,
            dropTotal: total,
            nearest: nearest,
            actualMilliamps: nearest ? across / nearest * 1000 : null,
            rating: C.nextResistorRating(watts)
        };
    };

    TT.tools['led-resistor'] = function (root) {
        var supply = TT.q(root, 'ledSupply');
        var preset = TT.q(root, 'ledPreset');
        var drop = TT.q(root, 'ledDrop');
        var current = TT.q(root, 'ledCurrent');
        var count = TT.q(root, 'ledCount');
        var head = TT.q(root, 'ledHead');
        var out = TT.q(root, 'ledOut');

        if (preset) {
            preset.addEventListener('change', function () {
                var value = C.LED_PRESETS[preset.value];
                if (value) {
                    drop.value = value;
                    TT.poke(drop);
                }
            });
        }

        TT.onInput(root, function () {
            var r = C.ledResistor(TT.num(supply.value), TT.num(drop.value),
                                  TT.num(current.value), TT.num(count.value));
            if (r.error) {
                head.textContent = '—';
                TT.empty(out, r.error);
                return;
            }
            head.textContent = TT.engR(r.ohms) + ' · ' + TT.engP(r.watts);
            var rows = [
                ['Resistor needed', TT.fmt(r.ohms) + ' Ω'],
                ['Voltage across it', TT.fmt(r.across) + ' V'],
                ['Heat it must carry', TT.fmt(r.watts * 1000) + ' mW'],
                ['Buy this size', r.rating ? r.rating.name + ' or bigger'
                                           : 'over 5 W — use a wirewound']
            ];
            if (r.nearest) {
                rows.push(['Nearest stock value',
                    TT.engR(r.nearest) + ' (' + C.toSmdCode(r.nearest) + ')']);
                if (r.actualMilliamps !== null) {
                    rows.push(['LED current then',
                        TT.fmt(r.actualMilliamps) + ' mA']);
                }
            }
            if (r.leds > 1) {
                rows.push(['LEDs in series', r.leds + ' · ' +
                           TT.fmt(r.dropTotal) + ' V total drop']);
            }
            TT.lines(out, rows);
        });
    };
})(window.TT);
