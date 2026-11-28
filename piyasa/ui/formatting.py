"""Display formatting for instrument prices."""

from piyasa.ui.theme import turkish_number


def format_instrument_value(instrument, value):
    if value is None:
        return "..."
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "..."
    decimals = int(instrument.get("decimals", 2))
    prefix = instrument.get("currency", "")
    return f"{prefix}{turkish_number(value, decimals)}"
