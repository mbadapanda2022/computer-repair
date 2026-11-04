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
TOOL_SCRIPTS = ('electronics.js', 'computer.js', 'shop.js')

ELECTRONICS = 'Electronics'
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
CATEGORY_ORDER = (ELECTRONICS, COMPUTER, SHOP)


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
