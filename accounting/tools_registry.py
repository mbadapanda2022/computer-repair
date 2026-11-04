"""
Tech Tools registry — the single source of truth for the public /tools/ page.

Every calculator on that page is declared here: slug (used in the URL), the
name/one-line blurb shown in the sidebar, the bootstrap-icons class, the
category it is grouped under, and the partial that renders its form.

The page itself is one shell template; the registry decides what appears in
the navigation, so adding a tool means adding one entry here plus its partial
and its JS init function. ``accounting/tests_tools.py`` fails if any of those
three is missing, which is what stops the feature from drifting half-finished.
"""
from __future__ import annotations

# JS bundles loaded by the tools page. Each file registers one init function
# per tool into ``TT.tools`` (see static/js/tools/common.js).
TOOL_SCRIPTS = ('electronics.js', 'board.js', 'computer.js', 'shop.js')

ELECTRONICS = 'Electronics'
BOARD = 'Chip-Level Board Repair'
COMPUTER = 'Computer & Network'
SHOP = 'Shop & Billing'

TOOLS = (
    {
        'slug': 'resistor-color',
        'name': 'Resistor Color Code',
        'icon': 'bi-palette',
        'category': ELECTRONICS,
        'blurb': 'Read the bands — 4, 5 and 6-band resistors with tolerance '
                 'and temperature coefficient.',
        'partial': 'tools/partials/resistor-color.html',
        'seo_title': 'Resistor Color Code Calculator — 4, 5 & 6 Band',
        'seo_description': 'Free online resistor colour code calculator for '
                           '4, 5 and 6 band resistors. Pick the bands and get '
                           'the resistance, tolerance and ppm instantly.',
    },
    {
        'slug': 'smd-resistor',
        'name': 'SMD Code → Resistance',
        'icon': 'bi-cpu',
        'category': ELECTRONICS,
        'blurb': 'Chip resistors marked 103, 1502 or 4R7, with the nearest '
                 'stock value.',
        'partial': 'tools/partials/smd-resistor.html',
        'seo_title': 'SMD Resistor Code Calculator (103, 4R7, 1502)',
        'seo_description': 'Decode SMD chip resistor markings — 3 and 4 digit '
                           'codes and R as the decimal point — into ohms, with '
                           'the nearest standard E24 value and its marking.',
    },
    {
        'slug': 'capacitor-code',
        'name': 'Capacitor Code Decoder',
        'icon': 'bi-battery-charging',
        'category': ELECTRONICS,
        'blurb': 'Ceramic disc and multilayer marks like 104, 225, 1n — in '
                 'pF, nF and µF.',
        'partial': 'tools/partials/capacitor-code.html',
        'seo_title': 'Capacitor Code Calculator — 104, 225, 1n Explained',
        'seo_description': 'Free capacitor marking decoder: enter a 3-digit '
                           'code such as 104 or 225 and get the capacitance in '
                           'pF, nF and µF plus the tolerance letter.',
    },
    {
        'slug': 'ohms-law',
        'name': "Ohm's Law & Power",
        'icon': 'bi-lightning-charge',
        'category': ELECTRONICS,
        'blurb': 'Any two of voltage, current, resistance and wattage — the '
                 'other two follow.',
        'partial': 'tools/partials/ohms-law.html',
        'seo_title': "Ohm's Law Calculator — Voltage, Current, Resistance, Power",
        'seo_description': "Ohm's law and power calculator. Enter any two of "
                           'V, I, R and P to get the rest, with the formula '
                           'used shown next to each result.',
    },
    {
        'slug': 'led-resistor',
        'name': 'LED Series Resistor',
        'icon': 'bi-lightbulb',
        'category': ELECTRONICS,
        'blurb': 'Safest resistor for an LED from a supply rail, with the '
                 'wattage it must survive.',
        'partial': 'tools/partials/led-resistor.html',
        'seo_title': 'LED Resistor Calculator (Series, Wattage, E24 Value)',
        'seo_description': 'Work out the series resistor for an LED from any '
                           'supply voltage: resistance, power dissipation and '
                           'the nearest standard E24 value to buy.',
    },
    {
        'slug': 'voltage-divider',
        'name': 'Voltage Divider',
        'icon': 'bi-sliders',
        'category': BOARD,
        'blurb': 'R1 / R2 tap voltage, and what it really reads once the '
                 'circuit loads it.',
        'partial': 'tools/partials/voltage-divider.html',
        'seo_title': 'Voltage Divider Calculator — Loaded Output Error',
        'seo_description': 'Voltage divider calculator: enter Vin, R1 and R2 for the tap '
                           'voltage, add the load to see the real output, the loading '
                           'error and whether it is stiff enough.',
    },
    {
        'slug': 'rc-charge',
        'name': 'RC Time Constant',
        'icon': 'bi-stopwatch',
        'category': BOARD,
        'blurb': 'Charge and discharge times, inrush current and the energy a '
                 'capacitor still holds.',
        'partial': 'tools/partials/rc-charge.html',
        'seo_title': 'RC Time Constant Calculator — Charge and Discharge Time',
        'seo_description': 'RC time constant calculator: tau = R x C, the voltage after '
                           'any time, the time to reach a target, inrush current and the '
                           'energy stored in the capacitor.',
    },
    {
        'slug': 'pcb-trace',
        'name': 'PCB Trace Current',
        'icon': 'bi-grid-3x3',
        'category': BOARD,
        'blurb': 'How much current a copper trace may carry, and the width '
                 'needed for repair work.',
        'partial': 'tools/partials/pcb-trace.html',
        'seo_title': 'PCB Trace Current Calculator — IPC-2221 Width and Amps',
        'seo_description': 'PCB trace current calculator from width, copper weight and '
                           'allowed temperature rise, plus the trace width needed for a '
                           'repair on outer or inner layers.',
    },
    {
        'slug': 'heatsink',
        'name': 'Heatsink & Junction Temp',
        'icon': 'bi-thermometer-half',
        'category': BOARD,
        'blurb': 'Thermal path for a MOSFET or regulator, and the biggest sink '
                 'that still keeps it legal.',
        'partial': 'tools/partials/heatsink.html',
        'seo_title': 'Heatsink Calculator — Junction Temperature and RθSA',
        'seo_description': 'Heatsink calculator for TO-220 and power transistors: junction '
                           'and case temperature from dissipation and thermal resistances, '
                           'plus the heatsink needed.',
    },
    {
        'slug': 'transistor-switch',
        'name': 'Transistor Base Resistor',
        'icon': 'bi-diagram-2',
        'category': BOARD,
        'blurb': 'Resistor that saturates a driver transistor for a load, with '
                 'forced beta shown.',
        'partial': 'tools/partials/transistor-switch.html',
        'seo_title': 'Transistor Switch Base Resistor Calculator (Saturation)',
        'seo_description': 'Base resistor calculator for saturating an NPN switching '
                           'transistor from a logic output: base current, overdrive, '
                           'forced beta, resistor watts and E24 value.',
    },
    {
        'slug': 'series-lamp',
        'name': 'Series Bulb Limiter',
        'icon': 'bi-lamp',
        'category': BOARD,
        'blurb': 'What bulb to put in line when powering a shorted SMPS or '
                 'laptop board.',
        'partial': 'tools/partials/series-lamp.html',
        'seo_title': 'Series Bulb Tester Calculator — Dim Bulb Current Limiter',
        'seo_description': 'Series bulb limiter (dim bulb tester) calculator: the bulb to '
                           'fit, the current limit on a dead short, the voltage the board '
                           'sees and what the glow means.',
    },
    {
        'slug': 'rectifier-cap',
        'name': 'Rectifier & Bulk Cap',
        'icon': 'bi-plug',
        'category': BOARD,
        'blurb': 'Peak voltage, ripple on the rail and the capacitance that '
                 'flattens it.',
        'partial': 'tools/partials/rectifier-cap.html',
        'seo_title': 'Bridge Rectifier and Filter Capacitor Calculator',
        'seo_description': 'Bridge rectifier and filter capacitor calculator: peak DC '
                           'after the diodes, ripple voltage for a load at 50 or 60 Hz, '
                           'and the capacitance that reduces it.',
    },
    {
        'slug': 'regulator-heat',
        'name': 'Regulator Drop & Heat',
        'icon': 'bi-fire',
        'category': BOARD,
        'blurb': 'What an LDO or 78xx burns in watts, and how hot its junction '
                 'gets with it.',
        'partial': 'tools/partials/regulator-heat.html',
        'seo_title': 'Linear Regulator Heat Calculator — LDO and 78xx',
        'seo_description': 'Linear regulator and LDO heat calculator: power dissipated '
                           'from dropout and load, junction temperature for a given θJA, '
                           'safe current and efficiency.',
    },
    {
        'slug': 'fuse-size',
        'name': 'Fuse Size',
        'icon': 'bi-shield-exclamation',
        'category': BOARD,
        'blurb': 'Cartridge rating for a load, slow or quick blow, that still '
                 'protects the wiring.',
        'partial': 'tools/partials/fuse-size.html',
        'seo_title': 'Fuse Size Calculator — Slow Blow and Quick Blow',
        'seo_description': 'Fuse size calculator for a board or supply: rating for the '
                           'load current, slow blow versus quick blow margin, the next '
                           'standard IEC value and voltage rating.',
    },
    {
        'slug': 'sense-resistor',
        'name': 'Current Sense Resistor',
        'icon': 'bi-speedometer2',
        'category': BOARD,
        'blurb': 'Millivolts per amp across a shunt, the wattage to fit, and '
                 'the current a meter reading means.',
        'partial': 'tools/partials/sense-resistor.html',
        'seo_title': 'Current Sense Resistor Calculator — mV per Amp',
        'seo_description': 'Current sense resistor calculator: millivolts per amp, the '
                           'drop at a given current, heat dissipated, the power rating to '
                           'fit and the current a reading means.',
    },
    {
        'slug': 'battery-life',
        'name': 'Battery Runtime & Charger',
        'icon': 'bi-battery-half',
        'category': COMPUTER,
        'blurb': 'How long a laptop/power bank lasts on a load, and whether a '
                 'charger is a match.',
        'partial': 'tools/partials/battery-life.html',
        'seo_title': 'Battery Life Calculator — mAh, Wh and Charge Time',
        'seo_description': 'Estimate battery runtime from capacity (mAh / Wh) '
                           'and load, plus a charger compatibility check from '
                           'voltage and amps.',
    },
    {
        'slug': 'screen-size',
        'name': 'Screen Size Calculator',
        'icon': 'bi-aspect-ratio',
        'category': COMPUTER,
        'blurb': 'Panel diagonal to width and height in inches and cm — for '
                 'laptops, monitors, LED TVs.',
        'partial': 'tools/partials/screen-size.html',
        'seo_title': 'Screen Size Calculator — Inches to cm Width & Height',
        'seo_description': 'Convert a display diagonal into actual width and '
                           'height in inches, centimetres and millimetres for '
                           '16:9, 16:10, 4:3 and custom aspect ratios.',
    },
    {
        'slug': 'subnet',
        'name': 'IP Subnet / CIDR',
        'icon': 'bi-diagram-3',
        'category': COMPUTER,
        'blurb': 'Network, broadcast, mask, wildcard and usable host count for '
                 'any IPv4 address.',
        'partial': 'tools/partials/subnet.html',
        'seo_title': 'IP Subnet Calculator — CIDR, Netmask, Hosts',
        'seo_description': 'IPv4 subnet calculator: enter an address with CIDR '
                           'or a netmask and get network, broadcast, wildcard, '
                           'usable host range and host count.',
    },
    {
        'slug': 'base-convert',
        'name': 'Number Base Converter',
        'icon': 'bi-123',
        'category': COMPUTER,
        'blurb': 'Binary, octal, decimal and hex side by side, with bit count '
                 'and per-nibble breakdown.',
        'partial': 'tools/partials/base-convert.html',
        'seo_title': 'Binary, Decimal & Hex Converter',
        'seo_description': 'Convert numbers between binary, octal, decimal and '
                           'hexadecimal instantly, with bit length, sign and '
                           'nibble breakdown for technicians.',
    },
    {
        'slug': 'data-transfer',
        'name': 'File Transfer Time',
        'icon': 'bi-arrow-down-up',
        'category': COMPUTER,
        'blurb': 'How long an image, backup or folder takes over WiFi, LAN or '
                 'USB — real-world speeds.',
        'partial': 'tools/partials/data-transfer.html',
        'seo_title': 'File Transfer Time Calculator — MB, GB, Mbps, USB',
        'seo_description': 'Estimate upload, download and copy time for any '
                           'file size over LAN, WiFi, internet (Mbps) or USB, '
                           'with a practical efficiency factor.',
    },
    {
        'slug': 'power-cost',
        'name': 'Electricity Cost',
        'icon': 'bi-cash-coin',
        'category': SHOP,
        'blurb': 'Units consumed and monthly bill for any device at your '
                 'per-unit rate.',
        'partial': 'tools/partials/power-cost.html',
        'seo_title': 'Electricity Cost Calculator — Units and Bill in ₹',
        'seo_description': 'Calculate power consumption in units (kWh) and the '
                           'monthly or yearly electricity cost in rupees for a '
                           'device at your own per-unit tariff.',
    },
    {
        'slug': 'gst-calc',
        'name': 'GST Add / Remove',
        'icon': 'bi-percent',
        'category': SHOP,
        'blurb': 'Split a bill into CGST + SGST, work out IGST, or strip GST '
                 'back out of a total.',
        'partial': 'tools/partials/gst-calc.html',
        'seo_title': 'GST Calculator — Add or Remove GST, CGST SGST IGST Split',
        'seo_description': 'Indian GST calculator: add GST to a basic amount '
                           'with the CGST+SGST or IGST breakup, or remove GST '
                           'from a gross figure. Rates 0, 3, 5, 12, 18, 28%.',
    },
)

TOOLS_BY_SLUG = {tool['slug']: tool for tool in TOOLS}

#: Category order for the sidebar groups.
CATEGORY_ORDER = (ELECTRONICS, BOARD, COMPUTER, SHOP)


def get_tool(slug):
    """Return the registry entry for ``slug`` or ``None``."""
    return TOOLS_BY_SLUG.get(slug)


def grouped_tools():
    """Registry as ``[(category, [tool, …]), …]`` in CATEGORY_ORDER."""
    out = []
    for category in CATEGORY_ORDER:
        tools = [t for t in TOOLS if t['category'] == category]
        if tools:
            out.append((category, tools))
    return out
