/*
 * Tech Tools — Chip-Level Board Repair group.
 *
 * The maths a technician does in his head while probing a laptop, printer or
 * SMPS board: what a divider actually delivers once the circuit loads it, how
 * long an RC network takes, how much current a repaired trace may carry, how
 * hot a package will run, what base resistor saturates a driver transistor,
 * what happens when a shorted board is plugged in behind a series bulb, what a
 * rectifier and its bulk cap leave on the rail, how much heat a linear
 * regulator is shedding, which cartridge fuse fits, and what a sense resistor
 * reads in millivolts.
 *
 * Same contract as the other bundles: pure functions under TT.calc (so they
 * can be checked outside a browser) and one TT.tools[<slug>] = init(root) per
 * tool. The slugs must match accounting/tools_registry.py.
 */
(function (TT) {
    'use strict';

    var C = TT.calc;

    /* Capacitance is asked in the units printed on the part, and the maths
     * below always runs in farads. */
    C.CAP_UNITS = {pF: 1e-12, nF: 1e-9, 'µF': 1e-6, mF: 1e-3, F: 1};

    C.capacitorFarads = function (value, unit) {
        var n = TT.num(value);
        if (!isFinite(n)) return NaN;
        var scale = C.CAP_UNITS[unit];
        return isFinite(scale) ? n * scale : n * 1e-12;
    };

    /* ── voltage divider ─────────────────────────────────────────────── */
    /* The bench formula everyone writes is the no-load one. Once the divider
     * feeds an ADC input, a sense resistor or a transistor base, the bottom
     * leg is in parallel with that load and the output sags — so the loaded
     * figure and the size of the mistake are both printed. */
    C.voltageDivider = function (raw) {
        var vin = TT.toSi(TT.num(raw.vin), 'voltage', raw.vinUnit || 'V');
        var r1 = TT.toSi(TT.num(raw.r1), 'resistance', raw.r1Unit || 'Ω');
        var r2 = TT.toSi(TT.num(raw.r2), 'resistance', raw.r2Unit || 'Ω');
        if (!isFinite(vin) || vin <= 0) return {error: 'Enter the supply voltage.'};
        if (!isFinite(r1) || r1 <= 0) return {error: 'Enter the top resistor R1 (from + to the tap).'};
        if (!isFinite(r2) || r2 <= 0) return {error: 'Enter the bottom resistor R2 (from the tap to ground).'};

        var total = r1 + r2;
        var ideal = vin * r2 / total;
        var ratio = r2 / total;
        /* the current the divider itself burns, whether or not a load is
         * connected — this is what makes a low-value divider wasteful */
        var quiescent = vin / total;

        var out = {
            vin: vin, r1: r1, r2: r2, total: total, ideal: ideal, ratio: ratio,
            quiescent: quiescent,
            p1: quiescent * quiescent * r1,
            p2: quiescent * quiescent * r2,
            load: null
        };

        var rload = TT.toSi(TT.num(raw.rload), 'resistance', raw.rloadUnit || 'Ω');
        if (isFinite(rload) && rload > 0) {
            var bottom = r2 * rload / (r2 + rload);
            var loaded = vin * bottom / (r1 + bottom);
            out.load = {
                rload: rload,
                bottom: bottom,
                volts: loaded,
                error: (loaded - ideal) / ideal * 100,
                /* a divider is called stiff when the bottom leg is at least a
                 * hundredth of the load, which keeps the sag under about 1 % */
                stiff: rload >= 100 * r2
            };
        }
        return out;
    };

    TT.tools['voltage-divider'] = function (root) {
        var head = TT.q(root, 'vdHead');
        var out = TT.q(root, 'vdOut');

        TT.onInput(root, function () {
            var r = C.voltageDivider({
                vin: TT.q(root, 'vdVin').value,
                r1: TT.q(root, 'vdR1').value, r1Unit: TT.q(root, 'vdR1Unit').value,
                r2: TT.q(root, 'vdR2').value, r2Unit: TT.q(root, 'vdR2Unit').value,
                rload: TT.q(root, 'vdLoad').value, rloadUnit: TT.q(root, 'vdLoadUnit').value
            });
            if (r.error) {
                head.textContent = '—';
                TT.empty(out, r.error);
                return;
            }
            head.textContent = r.load ? TT.engV(r.load.volts) : TT.engV(r.ideal);
            var rows = [
                ['Output with nothing connected', TT.engV(r.ideal)],
                ['Divider ratio', TT.fmt(r.ratio, 4) + ' — Vout = Vin × ' + TT.fmt(r.ratio, 4)],
                ['Output per input volt', TT.fmt(r.ideal / r.vin, 4) + ' V'],
                ['Total resistance', TT.engR(r.total)],
                ['Current it burns', TT.engI(r.quiescent)],
                ['Heat in R1 / R2', TT.engP(r.p1) + ' · ' + TT.engP(r.p2)]
            ];
            if (r.load) {
                rows.push(['Bottom leg with the load on it',
                           TT.engR(r.load.bottom) + ' (R2 ∥ ' + TT.engR(r.load.rload) + ')']);
                rows.push(['Output you will actually measure', TT.engV(r.load.volts)]);
                rows.push(['The simple formula is out by',
                           (r.load.error > 0 ? '+' : '−') +
                           Math.abs(r.load.error).toFixed(2) + ' %']);
                rows.push(['Is the divider stiff enough?',
                           r.load.stiff ? 'yes — the load is 100× R2 or more'
                                        : 'no — lower both resistors or buffer the tap']);
            }
            TT.lines(out, rows);
        });
    };

    /* ── RC charge and discharge ─────────────────────────────────────── */
    /* τ does double duty here: it tells you how long a soft-start or a reset
     * line takes, and it tells you how long a bulk capacitor stays dangerous
     * after the board is unplugged. Energy is printed with it because that,
     * not the voltage, is what makes a big cap hurt. */
    C.rcTime = function (raw) {
        var v = TT.toSi(TT.num(raw.volts), 'voltage', raw.voltsUnit || 'V');
        var r = TT.toSi(TT.num(raw.r), 'resistance', raw.rUnit || 'Ω');
        var c = C.capacitorFarads(raw.c, raw.cUnit);
        if (!isFinite(v) || v <= 0) return {error: 'Enter the voltage across the network.'};
        if (!isFinite(r) || r <= 0) return {error: 'Enter the resistance in ohms.'};
        if (!isFinite(c) || c <= 0) return {error: 'Enter the capacitance.'};

        var tau = r * c;
        var out = {
            tau: tau,
            seconds: tau,
            full: 5 * tau,
            inrush: v / r,
            energy: 0.5 * c * v * v,
            watts: v * v / r,
            at: null,
            to: null,
            chargeSeconds: null,
            dischargeSeconds: null
        };

        var t = TT.toSi(TT.num(raw.seconds), 'time', raw.secondsUnit || 'seconds');
        if (isFinite(t) && t >= 0) {
            var rise = 1 - Math.exp(-t / tau);
            out.at = {
                seconds: t,
                chargePct: rise * 100,
                chargeVolts: v * rise,
                remainVolts: v * (1 - rise),
                current: v / r * Math.exp(-t / tau)
            };
        }

        var target = TT.toSi(TT.num(raw.target), 'voltage', raw.voltsUnit || 'V');
        if (isFinite(target) && target > 0 && target < v) {
            out.to = {
                volts: target,
                /* charging: Vt = Vs(1 − e^(−t/τ)) */
                chargeSeconds: -tau * Math.log(1 - target / v),
                /* discharging: Vt = Vs·e^(−t/τ) */
                dischargeSeconds: -tau * Math.log(target / v)
            };
        } else if (isFinite(target) && target >= v) {
            out.to = {unreachable: true, volts: target};
        }
        return out;
    };

    TT.tools['rc-charge'] = function (root) {
        var head = TT.q(root, 'rcHead');
        var out = TT.q(root, 'rcOut');

        TT.onInput(root, function () {
            var r = C.rcTime({
                volts: TT.q(root, 'rcVolts').value,
                voltsUnit: TT.q(root, 'rcVoltsUnit').value,
                r: TT.q(root, 'rcR').value, rUnit: TT.q(root, 'rcRUnit').value,
                c: TT.q(root, 'rcC').value, cUnit: TT.q(root, 'rcCUnit').value,
                seconds: TT.q(root, 'rcSeconds').value,
                secondsUnit: TT.q(root, 'rcSecondsUnit').value,
                target: TT.q(root, 'rcTarget').value
            });
            if (r.error) {
                head.textContent = '—';
                TT.empty(out, r.error);
                return;
            }
            head.textContent = 'τ = ' + TT.duration(r.tau);
            var rows = [
                ['One time constant', TT.duration(r.tau)],
                ['Effectively full / empty', TT.duration(r.full) + ' (5τ, 99.3 %)'],
                ['Current at the first instant', TT.engI(r.inrush) + ' · ' + TT.engP(r.watts)],
                ['Energy stored at that voltage', TT.fmt(r.energy * 1000, 4) + ' mJ'],
                ['At 1τ · 2τ · 3τ · 5τ', '63.2 % · 86.5 % · 95.0 % · 99.3 %']
            ];
            if (r.at) {
                rows.push(['After ' + TT.duration(r.at.seconds) + ' it is at',
                           TT.fmt(r.at.chargePct, 4) + ' % — charging ' +
                           TT.engV(r.at.chargeVolts)]);
                rows.push(['…and the capacitor still has',
                           TT.engV(r.at.remainVolts) + ' left to give out']);
                rows.push(['Current left at that moment', TT.engI(r.at.current)]);
            }
            if (r.to) {
                if (r.to.unreachable) {
                    rows.push(['Target voltage', TT.engV(r.to.volts) +
                               ' — cannot be reached, it is the supply or above']);
                } else {
                    rows.push(['Time to charge up to ' + TT.engV(r.to.volts),
                               TT.duration(r.to.chargeSeconds)]);
                    rows.push(['Time to bleed down to ' + TT.engV(r.to.volts),
                               TT.duration(r.to.dischargeSeconds)]);
                }
            }
            if (r.energy > 1) {
                rows.push(['Safety', 'more than a joule stored — discharge it through a '
                                     + 'resistor before probing']);
            }
            TT.lines(out, rows);
        });
    };

    /* ── PCB trace current (IPC-2221) ────────────────────────────────── */
    /* The published empirical relation for a copper trace is
     *   I = k · ΔT^0.44 · A^0.725
     * with A the cross-section in square mils, k = 0.048 for a trace on the
     * outside of the board and 0.024 for one buried inside (internal layers
     * lose their heat to the prepreg, so they carry roughly half). */
    C.TRACE_K = {external: 0.048, internal: 0.024};
    /* One square centimetre of length units, because TT.UNITS.length is in cm. */
    C.WIDTH_TO_CM = {mil: 0.00254, mm: 0.1, inch: 2.54};

    C.pcbTrace = function (raw) {
        var widthCm = TT.num(raw.width) * (C.WIDTH_TO_CM[raw.widthUnit] || 0.00254);
        if (!isFinite(widthCm) || widthCm <= 0) return {error: 'Enter the trace width.'};
        var widthMil = widthCm / 2.54 * 1000;
        var oz = TT.num(raw.oz);
        if (!isFinite(oz) || oz <= 0) return {error: 'Pick the copper weight.'};
        var dt = TT.num(raw.deltaT);
        if (!isFinite(dt) || dt <= 0 || dt > 260) {
            return {error: 'Enter the allowed temperature rise, 1 to 260 °C.'};
        }
        var k = C.TRACE_K[raw.layer] === undefined ? C.TRACE_K.external : C.TRACE_K[raw.layer];
        /* 1 oz of copper electroplated to a nominal thickness is 1.37 mil. */
        var thickMil = oz * 1.37;
        var area = widthMil * thickMil;
        var amps = k * Math.pow(dt, 0.44) * Math.pow(area, 0.725);

        var out = {
            widthMil: widthMil, widthMm: widthCm * 10,
            thickMil: thickMil, thickUm: thickMil * 25.4,
            area: area, amps: amps, deltaT: dt, oz: oz,
            layer: raw.layer === 'internal' ? 'internal' : 'external',
            need: null
        };

        var want = TT.num(raw.current);
        if (isFinite(want) && want > 0) {
            var needArea = Math.pow(want / (k * Math.pow(dt, 0.44)), 1 / 0.725);
            out.need = {
                amps: want,
                area: needArea,
                widthMil: needArea / thickMil,
                widthMm: needArea / thickMil / 1000 * 25.4,
                ok: want <= amps
            };
        }
        return out;
    };

    TT.tools['pcb-trace'] = function (root) {
        var head = TT.q(root, 'ptHead');
        var out = TT.q(root, 'ptOut');

        function layerChoice() {
            var btn = root.querySelector('[data-ptlayer].active');
            return btn ? btn.dataset.ptlayer : 'external';
        }

        root.querySelectorAll('[data-ptlayer]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                root.querySelectorAll('[data-ptlayer]').forEach(function (b) {
                    b.classList.remove('active');
                });
                btn.classList.add('active');
                root.dispatchEvent(new Event('input', {bubbles: true}));
            });
        });

        TT.onInput(root, function () {
            var r = C.pcbTrace({
                width: TT.q(root, 'ptWidth').value,
                widthUnit: TT.q(root, 'ptWidthUnit').value,
                oz: TT.q(root, 'ptOz').value,
                deltaT: TT.q(root, 'ptDeltaT').value,
                current: TT.q(root, 'ptCurrent').value,
                layer: layerChoice()
            });
            if (r.error) {
                head.textContent = '—';
                TT.empty(out, r.error);
                return;
            }
            head.textContent = TT.fmt(r.amps, 3) + ' A';
            var rows = [
                ['Trace width', TT.fmt(r.widthMil, 4) + ' mil · ' + TT.fmt(r.widthMm, 4) + ' mm'],
                ['Copper ' + TT.fmt(r.oz) + ' oz', TT.fmt(r.thickMil, 3) + ' mil · ' +
                                          TT.fmt(r.thickUm, 3) + ' µm thick'],
                ['Cross-section', TT.fmt(r.area, 4) + ' mil²'],
                ['May carry, ' + r.layer + ' layer, +' + TT.fmt(r.deltaT) + ' °C',
                 TT.fmt(r.amps, 3) + ' A']
            ];
            if (r.need) {
                rows.push([r.need.ok ? 'Your ' + TT.fmt(r.need.amps, 3) + ' A is inside that'
                                     : 'Your ' + TT.fmt(r.need.amps, 3) + ' A is too much',
                           r.need.ok ? 'the drawn trace is wide enough'
                                     : 'widen it or double it up with a wire']);
                rows.push(['Width needed for ' + TT.fmt(r.need.amps, 3) + ' A',
                           TT.fmt(r.need.widthMil, 4) + ' mil · ' +
                           TT.fmt(r.need.widthMm, 4) + ' mm']);
            }
            TT.lines(out, rows);
        });
    };

    /* ── heatsink and junction temperature ───────────────────────────── */
    /* Heat flows like current: Tj is the source, ambient is ground, and each
     * interface is a resistance in series. That makes the whole calculation one
     * subtraction and a division, and it is the arithmetic behind "this MOSFET
     * is burning up in the adapter". */
    C.thermal = function (raw) {
        var pd = TT.toSi(TT.num(raw.pd), 'power', raw.pdUnit || 'W');
        var amb = TT.num(raw.ambient);
        var tjMax = TT.num(raw.tjMax);
        var rjc = TT.num(raw.rjc);
        var rcs = TT.num(raw.rcs);
        var rsa = TT.num(raw.rsa);
        if (!isFinite(pd) || pd <= 0) return {error: 'Enter the power the part burns.'};
        if (!isFinite(amb)) return {error: 'Enter the surrounding temperature.'};
        if (!isFinite(tjMax) || tjMax <= amb) {
            return {error: 'The junction limit must be above the surrounding temperature.'};
        }
        if (!isFinite(rjc) || rjc <= 0) return {error: 'Enter RθJC from the datasheet.'};
        if (!isFinite(rcs) || rcs < 0) rcs = 0;
        if (!isFinite(rsa) || rsa < 0) rsa = 0;

        var budget = (tjMax - amb) / pd;
        return {
            pd: pd, ambient: amb, tjMax: tjMax, rjc: rjc, rcs: rcs, rsa: rsa,
            path: rjc + rcs + rsa,
            tj: amb + pd * (rjc + rcs + rsa),
            caseTemp: amb + pd * (rcs + rsa),
            budget: budget,
            maxRsa: budget - rjc - rcs,
            headroom: tjMax - (amb + pd * (rjc + rcs + rsa)),
            inside: amb + pd * (rjc + rcs + rsa) <= tjMax
        };
    };

    TT.tools['heatsink'] = function (root) {
        var head = TT.q(root, 'thHead');
        var out = TT.q(root, 'thOut');

        TT.onInput(root, function () {
            var r = C.thermal({
                pd: TT.q(root, 'thPd').value,
                pdUnit: TT.q(root, 'thPdUnit').value,
                ambient: TT.q(root, 'thAmbient').value,
                tjMax: TT.q(root, 'thTjMax').value,
                rjc: TT.q(root, 'thRjc').value,
                rcs: TT.q(root, 'thRcs').value,
                rsa: TT.q(root, 'thRsa').value
            });
            if (r.error) {
                head.textContent = '—';
                TT.empty(out, r.error);
                return;
            }
            head.textContent = TT.fmt(r.tj, 4) + ' °C junction';
            var rows = [
                ['Total path used', TT.fmt(r.path, 4) + ' °C/W (junction → case → sink → air)'],
                ['Junction temperature', TT.fmt(r.tj, 4) + ' °C of a ' + TT.fmt(r.tjMax) + ' °C limit'],
                ['Case temperature', TT.fmt(r.caseTemp, 4) + ' °C at the tab — ' +
                 (r.caseTemp > 60 ? 'too hot to touch, check it with a meter'
                                  : 'warm enough to feel through the sink')],
                ['Headroom left', (r.headroom >= 0 ? '' : 'OVER BY ') +
                                  TT.fmt(Math.abs(r.headroom), 4) + ' °C'],
                ['Worst total path that still works', TT.fmt(r.budget, 4) + ' °C/W'],
                ['Biggest heatsink allowed (RθSA)',
                 r.maxRsa > 0 ? TT.fmt(r.maxRsa, 4) + ' °C/W or better'
                              : 'none — even a perfect sink is too much at this wattage']
            ];
            rows.push(['Verdict',
                r.inside ? 'it stays inside its limit as configured'
                         : 'it will overheat — better sink, more airflow, or share the heat']);
            TT.lines(out, rows);
        });
    };

    /* ── transistor switch base resistor ─────────────────────────────── */
    /* A driver transistor is used as a switch, so it is pushed hard rather than
     * run at its datasheet hFE: the base current is taken as a multiple of
     * Ic ÷ hFE, and the multiple is shown rather than hidden. */
    C.transistorSwitch = function (raw) {
        var drive = TT.toSi(TT.num(raw.drive), 'voltage', raw.driveUnit || 'V');
        var vbe = TT.toSi(TT.num(raw.vbe), 'voltage', raw.vbeUnit || 'V');
        var ic = TT.toSi(TT.num(raw.ic), 'current', raw.icUnit || 'mA');
        var hfe = TT.num(raw.hfe);
        var over = TT.num(raw.overdrive);
        if (!isFinite(drive) || drive <= 0) return {error: 'Enter the voltage driving the base.'};
        if (!isFinite(vbe) || vbe < 0) vbe = 0.7;
        if (drive <= vbe) {
            return {error: 'The drive voltage must clear the ' + TT.fmt(vbe) + ' V base drop.'};
        }
        if (!isFinite(ic) || ic <= 0) return {error: 'Enter the collector (load) current.'};
        if (!isFinite(hfe) || hfe <= 1) return {error: 'Enter the transistor hFE (β), e.g. 100.'};
        if (!isFinite(over) || over < 1 || over > 20) over = 2;

        var needed = ic / hfe;
        var used = needed * over;
        var ohms = (drive - vbe) / used;
        return {
            drive: drive, vbe: vbe, ic: ic, hfe: hfe, overdrive: over,
            needed: needed, used: used, ohms: ohms,
            forcedBeta: ic / used,
            voltsAcross: drive - vbe,
            watts: used * used * ohms,
            nearest: TT.nearestE24(ohms)
        };
    };

    TT.tools['transistor-switch'] = function (root) {
        var head = TT.q(root, 'tsHead');
        var out = TT.q(root, 'tsOut');

        TT.onInput(root, function () {
            var r = C.transistorSwitch({
                drive: TT.q(root, 'tsDrive').value,
                driveUnit: TT.q(root, 'tsDriveUnit').value,
                vbe: TT.q(root, 'tsVbe').value,
                vbeUnit: TT.q(root, 'tsVbeUnit').value,
                ic: TT.q(root, 'tsIc').value,
                icUnit: TT.q(root, 'tsIcUnit').value,
                hfe: TT.q(root, 'tsHfe').value,
                overdrive: TT.q(root, 'tsOver').value
            });
            if (r.error) {
                head.textContent = '—';
                TT.empty(out, r.error);
                return;
            }
            head.textContent = TT.engR(r.ohms);
            var rows = [
                ['Base current the load needs', TT.engI(r.needed) + ' (Ic ÷ hFE)'],
                ['With the ×' + TT.fmt(r.overdrive) + ' overdrive', TT.engI(r.used)],
                ['Resistor = (Vdrive − Vbe) ÷ Ib', TT.fmt(r.ohms) + ' Ω'],
                ['Buy this stock value instead',
                 r.nearest ? TT.engR(r.nearest) : '—'],
                ['Heat in that resistor', TT.engP(r.watts)],
                ['Transistor then runs at forced β', TT.fmt(r.forcedBeta, 3) +
                 ' (hFE ' + TT.fmt(r.hfe) + ' ÷ ' + TT.fmt(r.overdrive) + ')'],
                ['Your driver must supply', TT.engI(r.used) + ' at ' + TT.engV(r.drive)]
            ];
            if (r.nearest && Math.abs(r.nearest - r.ohms) / r.ohms > 0.02) {
                rows.push(['With that stock value', 'it is driven harder, which is the safe direction']);
            }
            TT.lines(out, rows);
        });
    };

    /* ── series bulb limiter ─────────────────────────────────────────── */
    /* The old variac-shop trick for a dead-short SMPS: an incandescent bulb in
     * series with the live line. The bulb's own hot resistance is the limit, so
     * a shorted board simply lights the bulb at full brightness instead of
     * tripping the breaker or exploding the bridge rectifier. A real supply is
     * not a resistor, so the healthy-load figure is an estimate and is labelled
     * as one. */
    C.SERIES_LAMP_BULBS = [15, 25, 40, 60, 100, 150, 200, 300];

    C.seriesLamp = function (raw) {
        var mains = TT.num(raw.mains);
        var bulbW = TT.num(raw.bulbW);
        var loadVA = TT.num(raw.loadVA);
        if (!isFinite(mains) || mains < 100 || mains > 300) {
            return {error: 'Enter the supply voltage (100–300 V).'};
        }
        if (!isFinite(bulbW) || bulbW <= 0) {
            return {error: 'Enter the bulb wattage, or pick one below.'};
        }
        var bulbR = mains * mains / bulbW;
        var bulbI = bulbW / mains;

        var out = {
            mains: mains, bulbW: bulbW, bulbR: bulbR, bulbI: bulbI,
            shorted: {current: bulbI, voltsOnBoard: 0, bulbWatts: bulbW},
            load: null
        };

        if (isFinite(loadVA) && loadVA > 0) {
            /* treat the supply as if it drew that power at full mains, which is
             * the usual rough sizing rule for choosing the bulb */
            var loadZ = mains * mains / loadVA;
            var current = mains / (bulbR + loadZ);
            out.load = {
                va: loadVA,
                impedance: loadZ,
                current: current,
                boardVolts: current * loadZ,
                bulbVolts: current * bulbR,
                bulbWatts: current * current * bulbR,
                glow: current / bulbI * 100
            };
        }
        return out;
    };

    TT.tools['series-lamp'] = function (root) {
        var head = TT.q(root, 'slHead');
        var out = TT.q(root, 'slOut');

        root.querySelectorAll('[data-slbulpick]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var box = TT.q(root, 'slBulb');
                if (!box) return;
                box.value = btn.dataset.slbulpick;
                TT.poke(box);
            });
        });

        TT.onInput(root, function () {
            var r = C.seriesLamp({
                mains: TT.q(root, 'slMains').value,
                bulbW: TT.q(root, 'slBulb').value,
                loadVA: TT.q(root, 'slLoad').value
            });
            if (r.error) {
                head.textContent = '—';
                TT.empty(out, r.error);
                return;
            }
            head.textContent = TT.fmt(r.bulbI, 3) + ' A limit';
            var rows = [
                ['Bulb hot resistance', TT.engR(r.bulbR)],
                ['Current it can pass at full glow', TT.fmt(r.bulbI, 3) + ' A (' +
                                        TT.fmt(r.bulbW) + ' W bulb on ' + TT.fmt(r.mains) + ' V)'],
                ['If the board is a dead short', 'the bulb takes the whole mains, glows full, ' +
                                'and limits the current to ' + TT.fmt(r.bulbI, 3) + ' A'],
                ['Board sees in that fault', TT.engV(r.shorted.voltsOnBoard)]
            ];
            if (r.load) {
                rows.push(['Healthy draw of ' + TT.fmt(r.load.va) + ' VA',
                           TT.fmt(r.load.current, 3) + ' A, board gets ' +
                           TT.fmt(r.load.boardVolts, 4) + ' V']);
                rows.push(['Bulb across it', TT.fmt(r.load.bulbVolts, 4) + ' V · ' +
                           TT.fmt(r.load.bulbWatts) + ' W · ' +
                           TT.fmt(r.load.glow, 3) + ' % of full glow']);
                rows.push(['Reading the bulb', 'a bright bulb is a short or a heavy load; ' +
                                'a dim one means the supply is drawing what it should']);
            }
            TT.lines(out, rows);
        });
    };
    /* Shared helper for the mode toggles below: remember which button of a
     * group is active and re-run the calculator when the choice changes. */
    function modeGroup(root, attr, fallback) {
        var buttons = root.querySelectorAll('[' + attr + ']');
        buttons.forEach(function (btn) {
            btn.addEventListener('click', function () {
                buttons.forEach(function (b) { b.classList.remove('active'); });
                btn.classList.add('active');
                root.dispatchEvent(new Event('input', {bubbles: true}));
            });
        });
        return function () {
            var active = root.querySelector('[' + attr + '].active');
            return active ? active.getAttribute(attr) : fallback;
        };
    }

    /* ── rectifier and bulk capacitor ─────────────────────────────────── */
    /* A bridge plus a capacitor never produces a flat rail: the cap refills on
     * the way past each peak and the rail sags in between. That sag is the hum
     * you see on a scope and the "low but not dead" voltage a meter reports, so
     * both the peak and the ripple are printed, plus the capacitance that would
     * fix it. */
    /* Parts are printed in µF and pF, so the output follows the marking rather
     * than the nearest engineering prefix (no "4.7 mF" for a 4700 µF cap). */
    C.farad = function (f) {
        if (!isFinite(f)) return '—';
        var a = Math.abs(f);
        if (a >= 1) return TT.fmt(f) + ' F';
        if (a >= 1e-6) return TT.fmt(f * 1e6) + ' µF';
        if (a >= 1e-9) return TT.fmt(f * 1e9) + ' nF';
        return TT.fmt(f * 1e12) + ' pF';
    };

    C.rectifierCap = function (raw) {
        var vrms = TT.toSi(TT.num(raw.vrms), 'voltage', raw.vrmsUnit || 'V');
        if (!isFinite(vrms) || vrms <= 0) {
            return {error: 'Enter the AC voltage on the secondary.'};
        }
        var drop = TT.toSi(TT.num(raw.drop), 'voltage', raw.dropUnit || 'V');
        if (!isFinite(drop) || drop < 0) drop = 0.7;
        var freq = TT.num(raw.freq);
        if (!isFinite(freq) || freq <= 0) freq = 50;
        var path = raw.path || 'bridge';
        /* a bridge drops two diodes at a time and refills twice per cycle; a
         * single diode drops one and refills once, so it needs twice the cap */
        var drops = path === 'bridge' ? 2 : 1;
        var pulses = path === 'halfwave' ? 1 : 2;

        var out = {
            vrms: vrms, drop: drop, drops: drops, pulses: pulses, freq: freq,
            path: path, peak: vrms * Math.SQRT2 - drops * drop,
            gap: 1 / (pulses * freq), load: null, needed: null
        };

        var amps = TT.toSi(TT.num(raw.amps), 'current', raw.ampsUnit || 'A');
        var cap = C.capacitorFarads(raw.cap, raw.capUnit);
        if (isFinite(amps) && amps > 0 && isFinite(cap) && cap > 0) {
            var ripple = amps / (pulses * freq * cap);
            var rail = out.peak - ripple / 2;
            out.load = {
                amps: amps, cap: cap, ripple: ripple, rail: rail,
                percent: ripple / rail * 100,
                /* the winding only ever feeds the cap in short bursts, so it
                 * carries well above the average DC the load takes */
                secondary: amps * 1.8
            };
        }
        var target = TT.toSi(TT.num(raw.target), 'voltage', raw.targetUnit || 'V');
        if (isFinite(amps) && amps > 0 && isFinite(target) && target > 0) {
            out.needed = {target: target, farads: amps / (pulses * freq * target)};
        }
        return out;
    };

    TT.tools['rectifier-cap'] = function (root) {
        var head = TT.q(root, 'psHead');
        var out = TT.q(root, 'psOut');
        var choice = modeGroup(root, 'data-pspath', 'bridge');

        TT.onInput(root, function () {
            var r = C.rectifierCap({
                vrms: TT.q(root, 'psVrms').value, vrmsUnit: TT.q(root, 'psVrmsUnit').value,
                drop: TT.q(root, 'psDrop').value, dropUnit: TT.q(root, 'psDropUnit').value,
                freq: TT.q(root, 'psFreq').value,
                amps: TT.q(root, 'psAmps').value, ampsUnit: TT.q(root, 'psAmpsUnit').value,
                cap: TT.q(root, 'psCap').value, capUnit: TT.q(root, 'psCapUnit').value,
                target: TT.q(root, 'psTarget').value,
                targetUnit: TT.q(root, 'psTargetUnit').value,
                path: choice()
            });
            if (r.error) {
                head.textContent = '—';
                TT.empty(out, r.error);
                return;
            }
            head.textContent = TT.engV(r.peak) + ' peak';
            var rows = [
                ['AC in', TT.engV(r.vrms) + ' RMS'],
                ['Diodes in the path', r.drops + ' × ' + TT.engV(r.drop) +
                 ' = ' + TT.engV(r.drops * r.drop) + ' lost off every peak'],
                ['Peak the capacitor charges to', TT.engV(r.peak)],
                ['Refills', (r.pulses === 1 ? 'once' : 'twice') + ' per cycle — every ' +
                 TT.duration(r.gap) + ' at ' + TT.fmt(r.freq, 4) + ' Hz']
            ];
            if (r.load) {
                rows.push(['Load drawing', TT.engI(r.load.amps) +
                           ' off ' + C.farad(r.load.cap)]);
                rows.push(['Ripple on the rail', TT.engV(r.load.ripple) + ' peak-to-peak']);
                rows.push(['Voltage you will read', TT.engV(r.load.rail)]);
                rows.push(['Ripple as a share of the rail', TT.fmt(r.load.percent, 4) + ' %']);
                rows.push(['What the winding must supply',
                           TT.fmt(r.load.secondary, 4) + ' A of pulses (about 1.8× the DC load)']);
                rows.push(['Reading it', r.load.percent > 10
                                    ? 'far too much ripple for electronics — more capacitance, ' +
                                      'and a rail like this usually means the cap has dried up'
                                    : (r.load.percent > 2
                                        ? 'normal for a power stage; a regulator or an RC filter ' +
                                          'cleans the rest'
                                        : 'flat enough to feed logic straight away')]);
            }
            if (r.needed) {
                rows.push(['Capacity for ' + TT.engV(r.needed.target) + ' ripple',
                           C.farad(r.needed.farads)]);
            }
            TT.lines(out, rows);
        });
    };

    /* ── linear regulator drop and heat ───────────────────────────────── */
    /* An LDO or a 78xx burns the difference between input and output times the
     * load, all of it as heat — which is why a hot regulator on a laptop board
     * is a clue about a shorted rail downstream, not just a bad part. */
    C.regulatorHeat = function (raw) {
        var vin = TT.toSi(TT.num(raw.vin), 'voltage', raw.vinUnit || 'V');
        var vout = TT.toSi(TT.num(raw.vout), 'voltage', raw.voutUnit || 'V');
        if (!isFinite(vin) || vin <= 0) return {error: 'Enter the voltage coming in.'};
        if (!isFinite(vout) || vout <= 0) return {error: 'Enter the voltage it must hold.'};
        var amps = TT.toSi(TT.num(raw.amps), 'current', raw.ampsUnit || 'A');
        if (!isFinite(amps) || amps <= 0) return {error: 'Enter the current the load takes.'};
        var dropout = vin - vout;
        if (dropout <= 0) {
            return {error: 'The output is not below the input — a linear regulator needs headroom.'};
        }
        var theta = TT.num(raw.theta);
        if (!isFinite(theta) || theta <= 0) {
            return {error: 'Enter the package °C/W (θJA from the datasheet).'};
        }
        var amb = TT.num(raw.ambient);
        if (!isFinite(amb)) amb = 40;
        var tjMax = TT.num(raw.tjMax);
        if (!isFinite(tjMax) || tjMax <= amb) tjMax = 125;

        var pd = dropout * amps;
        return {
            vin: vin, vout: vout, amps: amps, dropout: dropout, pd: pd,
            theta: theta, ambient: amb, tjMax: tjMax,
            tj: amb + pd * theta,
            headroom: tjMax - (amb + pd * theta),
            /* the current at which the package just reaches its limit */
            maxAmps: (tjMax - amb) / (theta * dropout),
            efficiency: vout / vin * 100,
            inside: amb + pd * theta <= tjMax
        };
    };

    TT.tools['regulator-heat'] = function (root) {
        var head = TT.q(root, 'lrHead');
        var out = TT.q(root, 'lrOut');

        TT.onInput(root, function () {
            var r = C.regulatorHeat({
                vin: TT.q(root, 'lrVin').value, vinUnit: TT.q(root, 'lrVinUnit').value,
                vout: TT.q(root, 'lrVout').value, voutUnit: TT.q(root, 'lrVoutUnit').value,
                amps: TT.q(root, 'lrAmps').value, ampsUnit: TT.q(root, 'lrAmpsUnit').value,
                theta: TT.q(root, 'lrTheta').value,
                ambient: TT.q(root, 'lrAmbient').value,
                tjMax: TT.q(root, 'lrTjMax').value
            });
            if (r.error) {
                head.textContent = '—';
                TT.empty(out, r.error);
                return;
            }
            head.textContent = TT.fmt(r.tj, 4) + ' °C junction';
            var rows = [
                ['Drop across the part', TT.engV(r.dropout) + ' × ' + TT.engI(r.amps) +
                 ' = ' + TT.engP(r.pd)],
                ['Junction with θJA ' + TT.fmt(r.theta, 4) + ' °C/W',
                 TT.fmt(r.tj, 4) + ' °C of a ' + TT.fmt(r.tjMax) + ' °C limit'],
                ['Headroom left', (r.headroom >= 0 ? '' : 'OVER BY ') +
                 TT.fmt(Math.abs(r.headroom), 4) + ' °C'],
                ['Most current that survives', TT.fmt(r.maxAmps, 4) + ' A at this drop'],
                ['Efficiency', TT.fmt(r.efficiency, 4) + ' % — everything else becomes heat']
            ];
            if (r.dropout < 1) {
                rows.push(['Dropout margin', 'only ' + TT.engV(r.dropout) +
                           ' of drop — a 78xx wants about 2 V; use a low-dropout part']);
            }
            rows.push(['Verdict', r.inside
                        ? (r.pd > 1
                            ? 'inside its limit, but ' + TT.engP(r.pd) +
                              ' needs copper or airflow under it'
                            : 'inside its limit')
                        : 'it will cook — less drop, a bigger pad, or a switching stage']);
            TT.lines(out, rows);
        });
    };

    /* ── fuse sizing ──────────────────────────────────────────────────── */
    /* A fuse has to survive the load and still open before the wiring does, so
     * it is picked between two limits rather than "the smallest one that fits". */
    C.FUSE_RATINGS = [0.05, 0.063, 0.08, 0.1, 0.125, 0.16, 0.2, 0.25, 0.315,
                      0.4, 0.5, 0.63, 0.8, 1, 1.25, 1.6, 2, 2.5, 3.15, 4, 5,
                      6.3, 8, 10, 12.5, 16, 20, 25, 31.5];

    C.fuseSize = function (raw) {
        var amps = TT.toSi(TT.num(raw.amps), 'current', raw.ampsUnit || 'A');
        if (!isFinite(amps) || amps <= 0) return {error: 'Enter the current the board draws.'};
        var slow = (raw.type || 'slow') === 'slow';
        /* quick-blow fuses age and open on heat they merely feel, so they are
         * sized with more slack; time-delay ones ride out an inrush instead */
        var factor = slow ? 1.2 : 1.5;
        var want = amps * factor;
        var pick = null;
        for (var i = 0; i < C.FUSE_RATINGS.length; i++) {
            if (C.FUSE_RATINGS[i] >= want) {
                pick = C.FUSE_RATINGS[i];
                break;
            }
        }
        var volts = TT.toSi(TT.num(raw.volts), 'voltage', raw.voltsUnit || 'V');
        var wire = TT.toSi(TT.num(raw.wire), 'current', raw.wireUnit || 'A');
        return {
            amps: amps, slow: slow, factor: factor, want: want, pick: pick,
            volts: isFinite(volts) && volts > 0 ? volts : null,
            wire: isFinite(wire) && wire > 0 ? wire : null,
            margin: pick ? pick / amps * 100 : null
        };
    };

    TT.tools['fuse-size'] = function (root) {
        var head = TT.q(root, 'fsHead');
        var out = TT.q(root, 'fsOut');
        var choice = modeGroup(root, 'data-fstype', 'slow');

        TT.onInput(root, function () {
            var r = C.fuseSize({
                amps: TT.q(root, 'fsAmps').value, ampsUnit: TT.q(root, 'fsAmpsUnit').value,
                volts: TT.q(root, 'fsVolts').value, voltsUnit: TT.q(root, 'fsVoltsUnit').value,
                wire: TT.q(root, 'fsWire').value, wireUnit: TT.q(root, 'fsWireUnit').value,
                type: choice()
            });
            if (r.error) {
                head.textContent = '—';
                TT.empty(out, r.error);
                return;
            }
            head.textContent = r.pick ? TT.fmt(r.pick, 4) + ' A' : '—';
            var rows = [
                ['Load it must ride through', TT.fmt(r.amps, 4) + ' A steady'],
                ['Blow character', r.slow
                    ? 'time-delay (slow blow) — rides out an inrush, so ' +
                      TT.fmt(r.factor, 3) + '× the load is enough'
                    : 'quick blow — opens on the first surge, so it is sized at ' +
                      TT.fmt(r.factor, 3) + '× the load'],
                ['Rating needed', TT.fmt(r.want, 4) + ' A at least']
            ];
            if (r.pick) {
                rows.push(['Buy this cartridge', TT.fmt(r.pick, 4) + ' A (' +
                           TT.fmt(r.margin, 4) + ' % of the load)']);
            } else {
                rows.push(['Buy this cartridge',
                           'above 31.5 A — that is a breaker or a bolt-down fuse job']);
            }
            if (r.volts) {
                rows.push(['Voltage rating it must carry',
                           'at least ' + TT.fmt(r.volts, 4) + ' V — a 250 V cartridge on ' +
                           TT.fmt(r.volts, 4) + ' V mains is right, a 125 V one is not']);
            }
            if (r.wire) {
                rows.push(['Does it still protect the wiring?', !r.pick ? 'check the rating first'
                           : (r.pick <= r.wire
                                ? 'yes — ' + TT.fmt(r.pick, 4) + ' A opens before ' +
                                  TT.fmt(r.wire, 4) + ' A of track or cable overheats'
                                : 'no — this fuse outrates the ' + TT.fmt(r.wire, 4) +
                                  ' A it is supposed to guard; fix the wire, not the fuse')]);
            }
            rows.push(['If it keeps blowing', 'something on the board is faulty — ' +
                       'a bigger fuse only moves the burn to the trace']);
            TT.lines(out, rows);
        });
    };

    /* ── current sense resistor ───────────────────────────────────────── */
    /* The low-value leg of a charger or a laptop rail is a measuring resistor:
     * the drop across it is the current. On a bench it is read both ways — what
     * voltage a known current should give, and what current a measured voltage
     * means — so both are printed. */
    C.RES_WATTS = [0.0625, 0.1, 0.125, 0.25, 0.5, 1, 2, 3, 5, 7];

    C.senseResistor = function (raw) {
        var r = TT.toSi(TT.num(raw.r), 'resistance', raw.rUnit || 'Ω');
        if (!isFinite(r) || r <= 0) return {error: 'Enter the resistance of the sense leg.'};
        var out = {r: r, perAmp: r * 1000, at: null, reaches: null, from: null};

        var amps = TT.toSi(TT.num(raw.amps), 'current', raw.ampsUnit || 'A');
        if (isFinite(amps) && amps > 0) {
            var watts = amps * amps * r;
            var rating = null;
            for (var i = 0; i < C.RES_WATTS.length; i++) {
                /* a resistor that runs at half its rating survives — the same
                 * reason a 2 W part goes where 1 W of heat appears */
                if (C.RES_WATTS[i] >= watts * 2) { rating = C.RES_WATTS[i]; break; }
            }
            out.at = {amps: amps, volts: amps * r, watts: watts, rating: rating};
        }
        var full = TT.toSi(TT.num(raw.full), 'voltage', raw.fullUnit || 'V');
        if (isFinite(full) && full > 0) {
            out.reaches = {full: full, amps: full / r};
        }
        var read = TT.toSi(TT.num(raw.read), 'voltage', raw.readUnit || 'mV');
        if (isFinite(read) && read >= 0) {
            /* toSi already put the meter reading into volts, so Ohm's law on
             * its own gives the current — no second millivolt step here */
            out.from = {read: read, amps: read / r};
        }
        return out;
    };

    TT.tools['sense-resistor'] = function (root) {
        var head = TT.q(root, 'srHead');
        var out = TT.q(root, 'srOut');

        TT.onInput(root, function () {
            var r = C.senseResistor({
                r: TT.q(root, 'srR').value, rUnit: TT.q(root, 'srRUnit').value,
                amps: TT.q(root, 'srAmps').value, ampsUnit: TT.q(root, 'srAmpsUnit').value,
                full: TT.q(root, 'srFull').value, fullUnit: TT.q(root, 'srFullUnit').value,
                read: TT.q(root, 'srRead').value, readUnit: TT.q(root, 'srReadUnit').value
            });
            if (r.error) {
                head.textContent = '—';
                TT.empty(out, r.error);
                return;
            }
            head.textContent = TT.fmt(r.perAmp, 4) + ' mV per amp';
            var rows = [
                ['Sense resistor', TT.engR(r.r)],
                ['Signal it gives', TT.fmt(r.perAmp, 4) + ' mV for every amp through it']
            ];
            if (r.at) {
                rows.push(['At ' + TT.engI(r.at.amps), TT.engV(r.at.volts) + ' across it']);
                rows.push(['Heat in it at that current', TT.engP(r.at.watts)]);
                rows.push(['Resistor to fit', r.at.rating
                            ? TT.fmt(r.at.rating * 1000, 4) + ' mW (so it runs at half its rating)'
                            : 'over 7 W — use a proper shunt, not a chip resistor']);
            }
            if (r.reaches) {
                rows.push(['Current that reaches ' + TT.engV(r.reaches.full),
                           TT.engI(r.reaches.amps)]);
            }
            if (r.from) {
                rows.push(['A meter reading ' + TT.engV(r.from.read) + ' means',
                           TT.engI(r.from.amps)]);
            }
            rows.push(['Measuring it', 'the drop is a few millivolts — put the probes ' +
                       'on the resistor body itself, not on the track either side of it']);
            TT.lines(out, rows);
        });
    };
})(window.TT);
