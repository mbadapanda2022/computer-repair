/*
 * Tech Tools — Computer & Network group.
 *
 * Same contract as electronics.js: pure maths under TT.calc (unit conversion
 * happens inside, so the conversion tables are covered by the tests too), and
 * one TT.tools[<slug>] = init(root) per page. Slugs must match
 * accounting/tools_registry.py.
 */
(function (TT) {
    'use strict';

    var C = TT.calc;

    /* ── battery runtime and charge time ─────────────────────────────── */
    /* A mAh rating alone says nothing about energy — a 5000 mAh phone cell is
     * 19 Wh while a 5000 mAh laptop pack is 44 Wh — so the pack voltage turns
     * charge into watt-hours before anything else is worked out. */
    C.batteryRuntime = function (raw) {
        var cap = TT.num(raw.capacity);
        if (!isFinite(cap) || cap <= 0) return {error: 'Enter the battery capacity.'};
        var capUnit = raw.capacityUnit || 'mAh';
        var volts = TT.num(raw.volts);
        var energyWh;
        var milliampHours = null;

        if (capUnit === 'Wh' || capUnit === 'kWh') {
            energyWh = TT.toSi(cap, 'energy', capUnit);
        } else {
            milliampHours = TT.toSi(cap, 'charge', capUnit);
            if (!isFinite(volts) || volts <= 0) {
                return {error: 'A mAh rating needs the pack voltage to become watt-hours.'};
            }
            energyWh = milliampHours / 1000 * volts;
        }

        var load = TT.num(raw.load);
        if (!isFinite(load) || load <= 0) return {error: 'Enter what the device draws.'};
        var loadUnit = raw.loadUnit || 'W';
        var watts;
        if (loadUnit === 'W' || loadUnit === 'kW') {
            watts = TT.toSi(load, 'power', loadUnit);
        } else {
            if (!isFinite(volts) || volts <= 0) {
                return {error: 'A current draw needs the pack voltage.'};
            }
            /* the load current is taken at the pack voltage, which is what a
             * bare board or an inverter-less fan does */
            watts = TT.toSi(load, 'current', loadUnit) * volts;
        }

        /* Nobody empties a cell to zero, and inverters lose a little, so the
         * usable share is asked for rather than hidden inside the formula. */
        var usable = TT.num(raw.usable);
        if (!isFinite(usable) || usable <= 0 || usable > 100) usable = 90;
        var hours = energyWh * (usable / 100) / watts;

        var result = {
            energyWh: energyWh,
            milliampHours: milliampHours,
            watts: watts,
            usable: usable,
            hours: hours,
            chargeHours: null,
            note: null
        };

        var chargerA = TT.num(raw.chargerA);
        if (isFinite(chargerA) && chargerA > 0) {
            if (milliampHours === null) {
                result.note = 'Charge time needs the capacity in mAh or Ah.';
            } else {
                var amps = TT.toSi(chargerA, 'current', raw.chargerAUnit || 'A');
                /* about 30 % lost to heat and the end-of-charge taper */
                result.chargeHours = milliampHours / 1000 / amps * 1.3;
            }
        }
        return result;
    };

    TT.tools['battery-life'] = function (root) {
        var head = TT.q(root, 'batHead');
        var out = TT.q(root, 'batOut');

        TT.onInput(root, function () {
            var r = C.batteryRuntime({
                capacity: TT.q(root, 'batCapacity').value,
                capacityUnit: TT.q(root, 'batCapacityUnit').value,
                volts: TT.q(root, 'batVolts').value,
                load: TT.q(root, 'batLoad').value,
                loadUnit: TT.q(root, 'batLoadUnit').value,
                usable: TT.q(root, 'batUsable').value,
                chargerA: TT.q(root, 'batChargerA').value,
                chargerAUnit: TT.q(root, 'batChargerUnit').value
            });
            if (r.error) {
                head.textContent = '—';
                TT.empty(out, r.error);
                return;
            }
            head.textContent = TT.hours(r.hours);
            var rows = [
                ['Energy stored', TT.fmt(r.energyWh) + ' Wh'],
                ['Drain', TT.engP(r.watts)],
                ['Usable share of the cell', TT.fmt(r.usable) + ' %'],
                ['Runtime', TT.hours(r.hours)]
            ];
            if (r.milliampHours !== null) {
                rows.push(['Charge rated', TT.fmt(r.milliampHours) + ' mAh']);
            }
            if (r.hours >= 24) {
                rows.push(['That is', TT.fmt(r.hours / 24, 3) + ' days']);
            }
            if (r.chargeHours !== null) {
                rows.push(['Refill at that charger', TT.hours(r.chargeHours)]);
            }
            if (r.note) rows.push(['Note', r.note]);
            TT.lines(out, rows);
        });
    };

    /* ── screen size ─────────────────────────────────────────────────── */
    /* A diagonal alone does not tell you whether a panel fits a stand, a bag
     * or a wall bracket, so both directions are solved. */
    C.RATIOS = {
        '16:9': [16, 9],
        '16:10': [16, 10],
        '4:3': [4, 3],
        '5:4': [5, 4],
        '21:9': [21, 9],
        '32:9': [32, 9],
        '1:1': [1, 1]
    };

    C.screenSize = function (raw) {
        var unit = TT.UNITS.length[raw.unit] ? raw.unit : 'inch';
        /* TT.toSi returns centimetres for every length unit, and the panel
         * maths below runs in inches. */
        var divideBy = 2.54;
        var ratio = 16 / 9;

        if (raw.mode === 'sides') {
            var widthIn = TT.toSi(TT.num(raw.width), 'length', unit) / divideBy;
            var heightIn = TT.toSi(TT.num(raw.height), 'length', unit) / divideBy;
            if (!isFinite(widthIn) || !isFinite(heightIn) ||
                widthIn <= 0 || heightIn <= 0) {
                return {error: 'Enter the width and the height of the panel.'};
            }
            return finish(widthIn, heightIn,
                          Math.sqrt(widthIn * widthIn + heightIn * heightIn));
        }

        var parts = String(raw.ratio || '16:9').split(':');
        var rw = TT.num(parts[0]);
        var rh = TT.num(parts[1]);
        if (!isFinite(rw) || !isFinite(rh) || rw <= 0 || rh <= 0) {
            return {error: 'Write the aspect ratio as two numbers, like 16:9.'};
        }
        ratio = rw / rh;
        var diagonalIn = TT.toSi(TT.num(raw.diagonal), 'length', unit) / divideBy;
        if (!isFinite(diagonalIn) || diagonalIn <= 0) {
            return {error: 'Enter the screen diagonal.'};
        }
        var heightIn2 = diagonalIn / Math.sqrt(1 + ratio * ratio);
        return finish(heightIn2 * ratio, heightIn2, diagonalIn);
    };

    function finish(widthIn, heightIn, diagonalIn) {
        return {
            widthIn: widthIn,
            heightIn: heightIn,
            diagonalIn: diagonalIn,
            widthCm: widthIn * 2.54,
            heightCm: heightIn * 2.54,
            diagonalCm: diagonalIn * 2.54,
            areaCm2: widthIn * heightIn * 2.54 * 2.54,
            areaIn2: widthIn * heightIn,
            measuredRatio: widthIn / heightIn
        };
    }

    TT.tools['screen-size'] = function (root) {
        var head = TT.q(root, 'ssHead');
        var out = TT.q(root, 'ssOut');
        var diagRow = TT.q(root, 'ssDiagRow');
        var whRow = TT.q(root, 'ssWhRow');
        var ratioRow = TT.q(root, 'ssRatioRow');
        var mode = 'diagonal';

        root.querySelectorAll('[data-mode]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                root.querySelectorAll('[data-mode]').forEach(function (b) {
                    b.classList.remove('active');
                });
                btn.classList.add('active');
                mode = btn.dataset.mode;
                var byDiagonal = mode === 'diagonal';
                diagRow.hidden = !byDiagonal;
                whRow.hidden = byDiagonal;
                ratioRow.hidden = !byDiagonal;
                var box = TT.q(root, byDiagonal ? 'ssDiagonal' : 'ssWidth');
                if (box) box.focus();
                root.dispatchEvent(new Event('input', {bubbles: true}));
            });
        });

        TT.onInput(root, function () {
            var choice = TT.q(root, 'ssRatio').value;
            var customRow = TT.q(root, 'ssCustomRow');
            if (customRow) customRow.hidden = choice !== 'custom';
            var r = C.screenSize({
                mode: mode,
                diagonal: TT.q(root, 'ssDiagonal').value,
                width: TT.q(root, 'ssWidth').value,
                height: TT.q(root, 'ssHeight').value,
                unit: TT.q(root, 'ssUnit').value,
                ratio: choice === 'custom' ? TT.q(root, 'ssCustom').value : choice
            });
            if (r.error) {
                head.textContent = '—';
                TT.empty(out, r.error);
                return;
            }
            head.textContent = TT.fmt(r.diagonalIn, 4) + '" diagonal';
            TT.lines(out, [
                ['Width', TT.fmt(r.widthIn, 4) + ' in · ' + TT.fmt(r.widthCm, 4) + ' cm'],
                ['Height', TT.fmt(r.heightIn, 4) + ' in · ' + TT.fmt(r.heightCm, 4) + ' cm'],
                ['Diagonal', TT.fmt(r.diagonalCm, 4) + ' cm · ' +
                             Math.round(r.diagonalCm * 10) + ' mm'],
                ['Active area', TT.fmt(r.areaCm2, 6) + ' cm²'],
                ['Ratio works out as', TT.fmt(r.measuredRatio, 4) + ' : 1']
            ]);
        });
    };

    /* ── IPv4 subnet / CIDR ──────────────────────────────────────────── */
    /* Addresses stay under 2^32, so plain doubles divide exactly — no 32-bit
     * bit operators, which turn 128.0.0.0 and above into negatives. */
    var FULL = 4294967296;

    C.ipToInt = function (text) {
        var parts = String(text || '').trim().split('.');
        if (parts.length !== 4) return null;
        var value = 0;
        for (var i = 0; i < 4; i++) {
            if (!/^\d{1,3}$/.test(parts[i])) return null;
            var octet = parseInt(parts[i], 10);
            if (octet > 255) return null;
            value = value * 256 + octet;
        }
        return value;
    };

    C.intToIp = function (value) {
        var whole = Math.floor(value);
        var a = Math.floor(whole / 16777216) % 256;
        var b = Math.floor(whole / 65536) % 256;
        var c = Math.floor(whole / 256) % 256;
        var d = whole % 256;
        return [a, b, c, d].join('.');
    };

    C.prefixToMask = function (prefix) {
        return FULL - Math.pow(2, 32 - prefix);
    };

    /* Unbroken 1s from the left, then 0s — anything else is not a netmask. */
    C.maskToPrefix = function (maskInt) {
        var prefix = 0;
        var seenZero = false;
        for (var bit = 31; bit >= 0; bit--) {
            if (Math.floor(maskInt / Math.pow(2, bit)) % 2) {
                if (seenZero) return null;
                prefix++;
            } else {
                seenZero = true;
            }
        }
        return prefix;
    };

    C.subnet = function (rawAddress, rawMask) {
        var address = String(rawAddress || '').trim();
        var maskText = String(rawMask || '').trim();

        /* "192.168.1.10/24" typed into one box is how people write it. */
        if (address.indexOf('/') !== -1) {
            var halves = address.split('/');
            address = halves[0];
            maskText = halves[1];
        } else if (!maskText) {
            return {error: 'Add the prefix (like /24) or the netmask.'};
        }

        var ip = C.ipToInt(address);
        if (ip === null) return {error: 'That is not a 4-part IPv4 address.'};

        var prefix;
        if (maskText.indexOf('.') !== -1) {
            var maskInt = C.ipToInt(maskText);
            if (maskInt === null) return {error: 'That netmask is not valid.'};
            prefix = C.maskToPrefix(maskInt);
            if (prefix === null) {
                return {error: 'A netmask must be unbroken 1s followed by 0s.'};
            }
        } else {
            if (!/^\d{1,2}$/.test(maskText)) {
                return {error: 'The prefix is a number from 0 to 32.'};
            }
            prefix = parseInt(maskText, 10);
            if (prefix > 32) return {error: 'A prefix cannot be above 32.'};
        }

        var blockSize = Math.pow(2, 32 - prefix);
        var network = Math.floor(ip / blockSize) * blockSize;
        var broadcast = network + blockSize - 1;
        var lead = Math.floor(ip / 16777216);
        var second = Math.floor(ip / 65536) % 256;

        /* /32 is one host and /31 is a point-to-point link (RFC 3021), so the
         * usual "minus network and broadcast" rule does not apply there. */
        var usable = prefix >= 32 ? 1 : (prefix === 31 ? 2 : Math.max(blockSize - 2, 0));
        var first = blockSize > 2 ? network + 1 : network;
        var last = blockSize > 2 ? broadcast - 1 : broadcast;

        var letter = lead < 128 ? 'A' : lead < 192 ? 'B' :
                     lead < 224 ? 'C' : lead < 240 ? 'D (multicast)' : 'E';

        var scope;
        if (lead === 0) scope = 'this network';
        else if (lead === 127) scope = 'loopback';
        else if (lead === 169 && second === 254) scope = 'link-local (auto-IP)';
        else if (lead >= 224) scope = 'multicast / reserved';
        else if (lead === 10) scope = 'private (RFC 1918)';
        else if (lead === 172 && second >= 16 && second <= 31) {
            scope = 'private (RFC 1918)';
        } else if (lead === 192 && second === 168) scope = 'private (RFC 1918)';
        else scope = 'public';

        return {
            address: C.intToIp(ip),
            prefix: prefix,
            cidr: C.intToIp(ip) + '/' + prefix,
            mask: C.intToIp(C.prefixToMask(prefix)),
            wildcard: C.intToIp(blockSize - 1),
            network: C.intToIp(network),
            broadcast: C.intToIp(broadcast),
            first: C.intToIp(first),
            last: C.intToIp(last),
            total: blockSize,
            usable: usable,
            letter: letter,
            scope: scope
        };
    };

    TT.tools['subnet'] = function (root) {
        var input = TT.q(root, 'subAddr');
        var mask = TT.q(root, 'subMask');
        var head = TT.q(root, 'subHead');
        var out = TT.q(root, 'subOut');

        TT.onInput(root, function () {
            var r = C.subnet(input.value, mask.value);
            if (r.error) {
                head.textContent = '—';
                TT.empty(out, r.error);
                return;
            }
            head.textContent = TT.fmt(r.usable) + ' usable hosts';
            TT.lines(out, [
                ['Address', r.address],
                ['Network', r.network + ' /' + r.prefix],
                ['Netmask', r.mask],
                ['Wildcard', r.wildcard],
                ['Broadcast', r.broadcast],
                ['Usable range', r.first + ' — ' + r.last],
                ['Total / usable', TT.fmt(r.total) + ' / ' + TT.fmt(r.usable)],
                ['Address class', r.letter],
                ['Scope', r.scope]
            ]);
        });
    };

    /* ── number base converter ───────────────────────────────────────── */
    /* BigInt keeps long hex and 64-bit register values exact, where a Number
     * would quietly drop the last digits. */
    var DIGITS = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ';

    C.BASES = {BIN: 2, OCT: 8, DEC: 10, HEX: 16};
    C.BASE_PREFIX = {BIN: /^0B/, OCT: /^0O/, DEC: /^0D/, HEX: /^0X/};

    /* Groups of four, aligned to the right, the way a datasheet prints them. */
    C.groupBits = function (bin) {
        var chunks = [];
        for (var end = bin.length; end > 0; end -= 4) {
            chunks.unshift(bin.slice(Math.max(0, end - 4), end));
        }
        return chunks.join(' ');
    };

    C.convertBase = function (raw, baseKey) {
        var text = String(raw || '').trim().toUpperCase().replace(/[\s_,']/g, '');
        if (!text) return {error: 'Enter a number.'};
        var negative = false;
        if (text.charAt(0) === '-') {
            negative = true;
            text = text.slice(1);
        } else if (text.charAt(0) === '+') {
            text = text.slice(1);
        }
        var base = C.BASES[baseKey];
        if (!base) return {error: 'Pick a base.'};
        text = text.replace(C.BASE_PREFIX[baseKey], '');
        if (!text) return {error: 'That is only a prefix, with no digits.'};
        if (text.length > 64) return {error: 'Keep it under 64 digits.'};
        if (!/^[0-9A-Z]+$/.test(text)) return {error: 'Those are not digits.'};

        var value = BigInt(0);
        for (var i = 0; i < text.length; i++) {
            var digit = DIGITS.indexOf(text.charAt(i));
            if (digit < 0 || digit >= base) {
                return {error: '"' + text.charAt(i) + '" is not a base-' + base +
                              ' digit.'};
            }
            value = value * BigInt(base) + BigInt(digit);
        }

        var bin = value.toString(2);
        var hex = value.toString(16).toUpperCase();
        var bytes = Math.ceil(bin.length / 8) || 1;
        var sign = negative ? '-' : '';
        return {
            negative: negative,
            sign: sign,
            dec: sign + value.toString(10),
            hex: sign + hex,
            oct: sign + value.toString(8),
            bin: sign + bin,
            binGrouped: sign + C.groupBits(bin),
            bits: bin.length,
            bytes: bytes,
            fits: bin.length <= 8 ? '8-bit' : bin.length <= 16 ? '16-bit'
                  : bin.length <= 32 ? '32-bit' : bin.length <= 64 ? '64-bit'
                  : 'more than 64-bit',
            hexByByte: (hex.length % 2 ? '0' + hex : hex).replace(/../g, '$& ').trim()
        };
    };

    TT.tools['base-convert'] = function (root) {
        var input = TT.q(root, 'bcValue');
        var picker = TT.q(root, 'bcBase');
        var head = TT.q(root, 'bcHead');
        var out = TT.q(root, 'bcOut');
        var targets = {
            BIN: TT.q(root, 'bcBin'),
            OCT: TT.q(root, 'bcOct'),
            DEC: TT.q(root, 'bcDec'),
            HEX: TT.q(root, 'bcHex')
        };

        TT.onInput(root, function () {
            var r = C.convertBase(input.value, picker.value);
            if (r.error) {
                head.textContent = '—';
                TT.empty(out, r.error);
                Object.keys(targets).forEach(function (key) {
                    if (targets[key]) targets[key].value = '';
                });
                return;
            }
            Object.keys(targets).forEach(function (key) {
                if (targets[key]) targets[key].value = r[key];
            });
            head.textContent = r.dec + ' (decimal)';
            var rows = [
                ['Binary', r.binGrouped],
                ['Hex', r.hex],
                ['Octal', r.oct],
                ['Bit length', r.bits + ' bits · ' + r.bytes + ' bytes'],
                ['Fits inside', 'a ' + r.fits + ' value'],
                ['Hex by byte', r.hexByByte]
            ];
            if (r.negative) {
                rows.push(['Note', 'Magnitude shown. A two’s complement register '
                                   + 'would store this as the complement.']);
            }
            TT.lines(out, rows);
        });
    };

    /* ── file transfer time ──────────────────────────────────────────── */
    /* Raw link speed is never the rate you get (protocol talk, WiFi retries,
     * disk seeks), so an efficiency factor is asked for and printed. */
    C.LINK_PRESETS = {
        wifi24: {speed: '20', unit: 'Mbps'},
        wifi5: {speed: '200', unit: 'Mbps'},
        wifi6: {speed: '600', unit: 'Mbps'},
        lan100: {speed: '90', unit: 'Mbps'},
        lan1g: {speed: '940', unit: 'Mbps'},
        usb2: {speed: '35', unit: 'MB/s'},
        usb3: {speed: '180', unit: 'MB/s'},
        nvme: {speed: '500', unit: 'MB/s'},
        ftth: {speed: '80', unit: 'Mbps'},
        lte: {speed: '25', unit: 'Mbps'}
    };

    C.transferTime = function (raw) {
        var bytes = TT.toSi(TT.num(raw.size), 'storage', raw.sizeUnit || 'MB');
        if (!isFinite(bytes) || bytes <= 0) return {error: 'Enter the file size.'};
        var bits = TT.toSi(TT.num(raw.speed), 'speed', raw.speedUnit || 'Mbps');
        if (!isFinite(bits) || bits <= 0) return {error: 'Enter the link speed.'};

        var eff = TT.num(raw.efficiency);
        if (!isFinite(eff) || eff <= 0 || eff > 100) eff = 75;
        var bytesPerSecond = bits / 8 * (eff / 100);
        return {
            bytes: bytes,
            bitsPerSecond: bits,
            bytesPerSecond: bytesPerSecond,
            efficiency: eff,
            seconds: bytes / bytesPerSecond,
            idealSeconds: bytes / (bits / 8),
            copiesPerHour: Math.floor(3600 * bytesPerSecond / bytes)
        };
    };

    TT.tools['data-transfer'] = function (root) {
        var head = TT.q(root, 'dtHead');
        var out = TT.q(root, 'dtOut');

        root.querySelectorAll('[data-preset]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var preset = C.LINK_PRESETS[btn.dataset.preset];
                if (!preset) return;
                TT.q(root, 'dtSpeed').value = preset.speed;
                TT.q(root, 'dtSpeedUnit').value = preset.unit;
                root.dispatchEvent(new Event('input', {bubbles: true}));
            });
        });

        TT.onInput(root, function () {
            var r = C.transferTime({
                size: TT.q(root, 'dtSize').value,
                sizeUnit: TT.q(root, 'dtSizeUnit').value,
                speed: TT.q(root, 'dtSpeed').value,
                speedUnit: TT.q(root, 'dtSpeedUnit').value,
                efficiency: TT.q(root, 'dtEff').value
            });
            if (r.error) {
                head.textContent = '—';
                TT.empty(out, r.error);
                return;
            }
            head.textContent = TT.duration(r.seconds);
            TT.lines(out, [
                ['File is', TT.fmt(r.bytes) + ' bytes'],
                ['Real copy rate', TT.fmt(r.bytesPerSecond / 1e6, 4) + ' MB/s'],
                ['Link claims', TT.fmt(r.bitsPerSecond / 1e6, 4) + ' Mbit/s'],
                ['At ' + TT.fmt(r.efficiency) + ' % efficiency',
                 TT.duration(r.seconds)],
                ['On an idle link', TT.duration(r.idealSeconds)],
                ['Copies per hour', TT.fmt(r.copiesPerHour)]
            ]);
        });
    };
})(window.TT);
