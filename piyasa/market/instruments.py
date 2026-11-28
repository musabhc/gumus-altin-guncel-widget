"""Instrument definitions and price conversion."""

import math


TROY_OUNCE_GRAMS = 31.1035
SILVER_SPOT_SYMBOL = "XAG"
SILVER_SPOT_API_URL = "https://api.gold-api.com/price/XAG"
LEGACY_SILVER_FUTURES_SYMBOL = "SI=F"
SILVER_HISTORY_PROXY_SYMBOL = LEGACY_SILVER_FUTURES_SYMBOL
SILVER_SPOT_KEYS = {"gumus_ons", "gumus_tl"}
SILVER_SPOT_SOURCES = {"silver_spot", "silver_spot_try"}

DEFAULT_WATCHLIST = [
    {
        "key": "gumus_ons",
        "label": "Gümüş ONS",
        "symbol": SILVER_SPOT_SYMBOL,
        "currency": "$",
        "decimals": 2,
        "color": "#ffffff",
        "source": "silver_spot",
    },
    {
        "key": "gumus_tl",
        "label": "Gümüş TL",
        "symbol": SILVER_SPOT_SYMBOL,
        "currency": "₺",
        "decimals": 2,
        "color": "#ffffff",
        "source": "silver_spot_try",
    },
    {
        "key": "altin_tl",
        "label": "Altın TL",
        "symbol": "GC=F",
        "currency": "₺",
        "decimals": 0,
        "color": "#d4af37",
        "source": "metal_try",
    },
    {
        "key": "dolar",
        "label": "Dolar",
        "symbol": "TRY=X",
        "currency": "₺",
        "decimals": 2,
        "color": "#2ecc71",
        "source": "direct",
    },
    {
        "key": "thyao",
        "label": "THYAO",
        "symbol": "THYAO.IS",
        "currency": "₺",
        "decimals": 2,
        "color": "#2196f3",
        "source": "direct",
    },
]


def default_watchlist():
    return [dict(item) for item in DEFAULT_WATCHLIST]


def reorder_instruments(instruments, ordered_keys):
    """Return instruments in an exact, validated key order."""
    current_keys = [item["key"] for item in instruments]
    ordered_keys = list(ordered_keys)
    if (
        len(ordered_keys) != len(current_keys)
        or len(set(ordered_keys)) != len(ordered_keys)
        or set(ordered_keys) != set(current_keys)
    ):
        raise ValueError("Yeni sıra mevcut izleme listesinin tam bir eşleşmesi olmalı.")
    by_key = {item["key"]: item for item in instruments}
    return [by_key[key] for key in ordered_keys]


def calculate_reordered_keys(ordered_keys, dragged_key, pointer_y, midpoints_by_key):
    """Calculate a drag order without mutating the persisted watchlist."""
    ordered_keys = list(ordered_keys)
    if dragged_key not in ordered_keys:
        return ordered_keys
    remaining = [key for key in ordered_keys if key != dragged_key]
    target_index = sum(
        1
        for key in remaining
        if key in midpoints_by_key and pointer_y > midpoints_by_key[key]
    )
    remaining.insert(target_index, dragged_key)
    return remaining


def make_watchlist_key(label, symbol):
    raw = (symbol or label or "asset").lower()
    chars = []
    for ch in raw:
        if ch.isalnum():
            chars.append(ch)
        elif ch in (".", "-", "=", "_", " "):
            chars.append("_")
    key = "".join(chars).strip("_")
    return key or "asset"


def normalize_instrument(item, fallback_index=0):
    item = dict(item or {})
    label = str(item.get("label") or item.get("name") or item.get("symbol") or f"Varlık {fallback_index + 1}").strip()
    symbol = str(item.get("symbol") or "").strip().upper()
    key = str(item.get("key") or make_watchlist_key(label, symbol)).strip()
    if not symbol:
        symbol = key.upper()
    try:
        decimals = int(item.get("decimals", 2))
    except (TypeError, ValueError):
        decimals = 2
    decimals = max(0, min(decimals, 6))
    return {
        "key": key,
        "label": label,
        "symbol": symbol,
        "currency": str(item.get("currency", "₺")),
        "decimals": decimals,
        "color": str(item.get("color") or "#2196f3"),
        "source": str(item.get("source") or "direct"),
    }


def migrate_legacy_silver_instrument(instrument):
    """Move only the two built-in silver rows from COMEX futures to spot XAG."""
    instrument = dict(instrument)
    key = instrument.get("key")
    symbol = str(instrument.get("symbol") or "").strip().upper()
    if key not in SILVER_SPOT_KEYS or symbol != LEGACY_SILVER_FUTURES_SYMBOL:
        return instrument, False
    instrument["symbol"] = SILVER_SPOT_SYMBOL
    instrument["source"] = "silver_spot" if key == "gumus_ons" else "silver_spot_try"
    return instrument, True


def safe_float(value, default=0.0):
    try:
        return float(str(value).replace(',', '.'))
    except (TypeError, ValueError):
        return default


def portfolio_unit(instrument):
    key = instrument.get("key", "")
    source = instrument.get("source", "")
    if key in ("gumus_tl", "altin_tl") or source == "metal_try":
        return "g"
    if key == "dolar" or instrument.get("symbol") == "TRY=X":
        return "USD"
    return "adet"


def portfolio_instruments(watchlist):
    # Gümüş ONS referans fiyat; portföyde gram gümüş için Gümüş TL kullanılır.
    return [item for item in watchlist if item.get("key") != "gumus_ons"]


def price_to_tl(instrument, prices):
    price = prices.get(instrument.get("key"))
    if price is None:
        return None
    price = safe_float(price, None)
    if price is None or not math.isfinite(price) or price <= 0:
        return None
    currency = instrument.get("currency", "₺")
    if currency == "$":
        dolar = safe_float(prices.get("dolar"), 0)
        if not math.isfinite(dolar) or dolar <= 0:
            return None
        return price * dolar
    return price
