# accounting/templatetags/math_filters.py
from django import template
from decimal import Decimal, InvalidOperation

register = template.Library()


@register.filter
def divide(value, arg):
    """
    Divides value by arg, returns 0 on division by zero or invalid types.
    Usage: {{ value|divide:arg }}
    """
    try:
        if not value or not arg:
            return 0
        # Convert to Decimal for precision
        v = Decimal(str(value))
        a = Decimal(str(arg))
        if a == 0:
            return 0
        return float(v / a)
    except (ZeroDivisionError, TypeError, ValueError, InvalidOperation):
        return 0


@register.filter
def multiply(value, arg):
    """
    Multiplies value by arg.
    Usage: {{ value|multiply:arg }}
    """
    try:
        if not value or not arg:
            return 0
        v = Decimal(str(value))
        a = Decimal(str(arg))
        return float(v * a)
    except (TypeError, ValueError, InvalidOperation):
        return 0


@register.filter
def subtract(value, arg):
    """
    Subtracts arg from value.
    Usage: {{ value|subtract:arg }}
    """
    try:
        if value is None or arg is None:
            return 0
        v = Decimal(str(value))
        a = Decimal(str(arg))
        return float(v - a)
    except (TypeError, ValueError, InvalidOperation):
        return 0


@register.filter
def add_filter(value, arg):
    """
    Adds arg to value.
    Usage: {{ value|add_filter:arg }}
    (Django has built-in 'add', but this is safer for Decimal)
    """
    try:
        if value is None:
            return float(arg) if arg else 0
        if arg is None:
            return float(value) if value else 0
        v = Decimal(str(value))
        a = Decimal(str(arg))
        return float(v + a)
    except (TypeError, ValueError, InvalidOperation):
        return 0


@register.filter
def abs_filter(value):
    """
    Returns absolute value.
    Usage: {{ value|abs_filter }}
    """
    try:
        if value is None:
            return 0
        return abs(float(value))
    except (TypeError, ValueError):
        return 0


@register.filter
def percentage(value, total):
    """
    Returns percentage (value/total * 100) with one decimal place.
    Usage: {{ value|percentage:total }}
    """
    try:
        if not value or not total:
            return 0
        v = Decimal(str(value))
        t = Decimal(str(total))
        if t == 0:
            return 0
        result = (v / t) * 100
        return round(float(result), 1)
    except (TypeError, ValueError, InvalidOperation):
        return 0


@register.filter
def currency(value):
    """
    Formats a number as Indian Rupees with comma separators.
    Usage: {{ value|currency }}
    """
    try:
        if value is None:
            return "₹0.00"
        # Convert to Decimal for precision
        val = Decimal(str(value))
        # Format with 2 decimal places and thousands separators
        formatted = f"{val:,.2f}"
        return f"₹{formatted}"
    except (TypeError, ValueError, InvalidOperation):
        return "₹0.00"


@register.filter
def sum_line_totals(items):
    """
    Sums the 'line_total' field from a list of objects or dicts.
    Usage: {{ items|sum_line_totals }}
    """
    if not items:
        return 0

    total = Decimal('0')
    for item in items:
        try:
            if isinstance(item, dict):
                line_total = item.get('line_total', 0)
            elif hasattr(item, 'line_total'):
                line_total = item.line_total
            elif isinstance(item, (int, float, Decimal)):
                line_total = item
            else:
                continue
            total += Decimal(str(line_total))
        except (TypeError, ValueError, InvalidOperation):
            continue

    return float(total)


@register.filter
def total(items):
    """
    Alias for sum_line_totals - easier to remember.
    Usage: {{ items|total }}
    """
    return sum_line_totals(items)


@register.filter
def floatformat(value, arg=2):
    """
    Formats a number with specified decimal places.
    Usage: {{ value|floatformat:2 }}
    """
    try:
        if value is None:
            return "0.00"
        val = Decimal(str(value))
        places = int(arg)
        formatted = f"{val:.{places}f}"
        return formatted
    except (TypeError, ValueError, InvalidOperation):
        return "0.00"


@register.filter
def positive(value):
    """
    Returns the number if positive, else 0.
    Usage: {{ value|positive }}
    """
    try:
        if value is None:
            return 0
        val = Decimal(str(value))
        return float(val) if val > 0 else 0
    except (TypeError, ValueError, InvalidOperation):
        return 0


@register.filter
def negative(value):
    """
    Returns the number if negative, else 0.
    Usage: {{ value|negative }}
    """
    try:
        if value is None:
            return 0
        val = Decimal(str(value))
        return float(val) if val < 0 else 0
    except (TypeError, ValueError, InvalidOperation):
        return 0


@register.filter
def round_number(value, places=2):
    """
    Rounds a number to specified decimal places.
    Usage: {{ value|round_number:2 }}
    """
    try:
        if value is None:
            return 0
        val = Decimal(str(value))
        return float(round(val, int(places)))
    except (TypeError, ValueError, InvalidOperation):
        return 0
    

@register.filter
def sum_value(items, field):
    """
    Sums the specified field from a list of objects or dicts.
    Usage: {{ items|sum:'line_total' }}
    """
    if not items:
        return 0
    total = Decimal('0')
    for item in items:
        try:
            if isinstance(item, dict):
                val = item.get(field, 0)
            else:
                val = getattr(item, field, 0)
            total += Decimal(str(val))
        except (TypeError, ValueError, InvalidOperation):
            continue
    return float(total)

