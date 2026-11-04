/*
 * Tech Tools — Shop & Billing group.
 *
 * The money tools round the way an Indian bill does: each line to paise
 * (half up), and the split tax halves are derived so CGST + SGST always adds
 * back to the GST amount exactly. Currency shown with Indian grouping.
 */
(function (TT) {
    'use strict';

    var C = TT.calc;

    /* ── electricity cost ────────────────────────────────────────────── */
    /* One "unit" on the meter is one kWh, so watt-hours divided by a thousand
     * is the number the board charges for. */
    C.powerCost = function (raw) {
        var watts = TT.toSi(TT.num(raw.power), 'power', raw.powerUnit || 'W');
        if (!isFinite(watts) || watts < 0) return {error: 'Enter the device power in watts.'};
        var hours = TT.num(raw.hoursPerDay);
        if (!isFinite(hours) || hours < 0 || hours > 24) {
            return {error: 'Hours per day must be between 0 and 24.'};
        }
        var days = TT.num(raw.daysPerMonth);
        if (!isFinite(days) || days <= 0 || days > 31) days = 30;
        var tariff = TT.num(raw.tariff);
        if (!isFinite(tariff) || tariff < 0) {
            return {error: 'Enter your per-unit rate, e.g. 8.50.'};
        }
        var count = TT.num(raw.count);
        if (!isFinite(count) || count < 1) count = 1;

        var unitsDay = watts * hours * count / 1000;
        var unitsMonth = unitsDay * days;
        var unitsYear = unitsDay * 365;
        return {
            watts: watts,
            count: count,
            hours: hours,
            days: days,
            tariff: tariff,
            unitsDay: unitsDay,
            unitsMonth: unitsMonth,
            unitsYear: unitsYear,
            costDay: TT.round2(unitsDay * tariff),
            costMonth: TT.round2(unitsMonth * tariff),
            costYear: TT.round2(unitsYear * tariff),
            /* the current a single device pulls off a 230 V socket */
            amps: watts / 230
        };
    };

    TT.tools['power-cost'] = function (root) {
        var head = TT.q(root, 'pcHead');
        var out = TT.q(root, 'pcOut');

        TT.onInput(root, function () {
            var r = C.powerCost({
                power: TT.q(root, 'pcPower').value,
                powerUnit: TT.q(root, 'pcPowerUnit').value,
                hoursPerDay: TT.q(root, 'pcHours').value,
                daysPerMonth: TT.q(root, 'pcDays').value,
                tariff: TT.q(root, 'pcTariff').value,
                count: TT.q(root, 'pcCount').value
            });
            if (r.error) {
                head.textContent = '—';
                TT.empty(out, r.error);
                return;
            }
            head.textContent = TT.money(r.costMonth) + ' per month';
            var rows = [
                ['Units (kWh) per day', TT.fmt(r.unitsDay, 4)],
                ['Units per month', TT.fmt(r.unitsMonth, 5)],
                ['Bill per day', TT.money(r.costDay)],
                ['Bill per month', TT.money(r.costMonth)],
                ['Bill in a year', TT.money(r.costYear)],
                ['Units in a year', TT.fmt(r.unitsYear, 5)]
            ];
            if (r.count > 1) {
                rows.push(['Each of the ' + TT.fmt(r.count) + ' devices',
                           TT.money(TT.round2(r.costMonth / r.count))]);
            }
            if (r.watts > 0) {
                rows.push(['Draws at 230 V', TT.fmt(r.amps, 3) + ' A']);
            }
            TT.lines(out, rows);
        });
    };

    /* ── GST add / remove ────────────────────────────────────────────── */
    C.gstCalc = function (raw) {
        var amount = TT.num(raw.amount);
        if (!isFinite(amount) || amount < 0) return {error: 'Enter an amount.'};
        var rate = TT.num(raw.rate);
        if (!isFinite(rate) || rate < 0 || rate > 100) {
            return {error: 'The GST rate is between 0 and 100.'};
        }
        var inter = raw.supply === 'inter';
        var removing = raw.mode === 'remove';

        var basic, tax, total;
        if (removing) {
            /* round the gross first, then take the tax out of that, so the
             * printed lines always add back to the printed total */
            total = TT.round2(amount);
            basic = TT.round2(total / (1 + rate / 100));
            tax = TT.round2(total - basic);
        } else {
            basic = TT.round2(amount);
            tax = TT.round2(basic * rate / 100);
            total = TT.round2(basic + tax);
        }
        /* Split so the two halves always re-add to the tax figure. */
        var half = TT.round2(tax / 2);
        var other = TT.round2(tax - half);
        var rounded = Math.round(total);

        return {
            basic: basic,
            rate: rate,
            tax: tax,
            total: total,
            cgst: half,
            sgst: other,
            inter: inter,
            removing: removing,
            roundOff: TT.round2(rounded - total),
            roundedTotal: rounded
        };
    };

    TT.tools['gst-calc'] = function (root) {
        var rate = TT.q(root, 'gstRate');
        var picker = TT.q(root, 'gstRatePick');
        var head = TT.q(root, 'gstHead');
        var out = TT.q(root, 'gstOut');

        if (picker) {
            picker.addEventListener('change', function () {
                if (picker.value) {
                    rate.value = picker.value;
                    TT.poke(rate);
                }
            });
        }

        /* Two independent button groups (add/remove and intra/inter): clear
         * only the siblings that share the same data-gstmix name. */
        root.querySelectorAll('[data-gstmix]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var name = btn.dataset.gstmix;
                root.querySelectorAll('[data-gstmix="' + name + '"]')
                    .forEach(function (b) { b.classList.remove('active'); });
                btn.classList.add('active');
                root.dispatchEvent(new Event('input', {bubbles: true}));
            });
        });

        function choice(name) {
            var btn = root.querySelector('[data-gstmix="' + name + '"].active');
            return btn ? btn.dataset.value : '';
        }

        TT.onInput(root, function () {
            var r = C.gstCalc({
                amount: TT.q(root, 'gstAmount').value,
                rate: rate.value,
                mode: choice('mode') === 'plus' ? 'add' : 'remove',
                supply: choice('supply') === 'inter' ? 'inter' : 'intra'
            });
            if (r.error) {
                head.textContent = '—';
                TT.empty(out, r.error);
                return;
            }
            head.textContent = TT.money(r.total);
            var label = TT.q(root, 'gstModeLabel');
            if (label) {
                label.textContent = r.removing ? 'Removed GST from'
                                               : 'Added GST to';
            }

            var rows = [
                [r.removing ? 'Basic value' : 'Basic amount', TT.money(r.basic)],
                ['GST rate', TT.fmt(r.rate) + ' %']
            ];
            if (r.inter) {
                rows.push(['IGST', TT.money(r.tax)]);
            } else {
                rows.push(['CGST', TT.money(r.cgst)]);
                rows.push(['SGST / UTGST', TT.money(r.sgst)]);
            }
            rows.push([r.removing ? 'GST inside the total' : 'Total GST',
                       TT.money(r.tax)]);
            rows.push(['Bill total', TT.money(r.total)]);
            rows.push(['After round off', TT.money(r.roundedTotal) + ' (' +
                       (r.roundOff >= 0 ? '+ ' : '− ') +
                       TT.money(Math.abs(r.roundOff)) + ')']);
            TT.lines(out, rows);
        });
    };
})(window.TT);
