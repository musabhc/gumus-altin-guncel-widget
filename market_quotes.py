"""Quote provenance and display text for live and retained market prices."""

from datetime import datetime, timedelta, timezone
import math


TURKEY_TIME = timezone(timedelta(hours=3))
SESSIONS = {
    "REGULAR": "Normal seans",
    "CLOSED": "Seans kapalı",
    "PRE": "Seans öncesi",
    "PREPRE": "Seans öncesi",
    "POST": "Seans sonrası",
    "POSTPOST": "Seans sonrası",
}


def positive_price(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def quote_time(value):
    """Normalize an aware ISO or provider epoch time, without guessing a zone."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not 0 < value < 32_503_680_000:
                return ""
            stamp = datetime.fromtimestamp(value, timezone.utc)
        else:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc).isoformat(timespec="seconds") if stamp.utcoffset() is not None else ""
    except (ValueError, OverflowError, OSError):
        return ""


def normalize_quotes(values, prices):
    if not isinstance(values, dict):
        return {}
    quotes = {}
    for key, item in values.items():
        if key not in prices or not isinstance(item, dict):
            continue
        state = item.get("state")
        market_state = item.get("market_state")
        quotes[key] = {
            "observed_at": quote_time(item.get("observed_at")),
            "price_at": quote_time(item.get("price_at")),
            "provider": str(item.get("provider") or ""),
            "market_state": market_state if isinstance(market_state, str) and market_state in SESSIONS else "",
            "state": state if state in ("received", "mixed", "cached") else "cached",
        }
    return quotes


def observation(provider, observed_at, *, price_at=None, market_state=None):
    return {
        "provider": provider,
        "observed_at": observed_at,
        "price_at": quote_time(price_at),
        "market_state": market_state or "",
        "state": "received",
    }


def merge_observations(prices, computed_keys, instruments, raw_quotes, old_quotes, observed_at):
    observations = {}
    for key in prices:
        old = old_quotes.get(key, {})
        if key not in computed_keys:
            observations[key] = {**old, "market_state": "", "state": "cached"}
            continue
        instrument = instruments[key]
        symbol = instrument["symbol"]
        meta = raw_quotes.get(symbol) or observation("Yahoo Finance", observed_at)
        if instrument.get("source") in ("metal_try", "silver_spot_try"):
            fx = raw_quotes.get("TRY=X")
            provider = f'{meta["provider"]} + USD/TRY'
            if not fx:
                observations[key] = {**meta, "provider": provider, "state": "mixed", "price_at": "", "market_state": ""}
                continue
            # A derived price is no newer than its oldest known component.
            price_at = min(meta["price_at"], fx["price_at"]) if meta["price_at"] and fx["price_at"] else ""
            meta = observation(provider, observed_at, price_at=price_at)
        observations[key] = dict(meta)
    return observations


def session_from_metadata(metadata, now):
    explicit = metadata.get("marketState")
    if isinstance(explicit, str) and explicit in SESSIONS:
        return explicit
    # A matching published interval is positive evidence of a session. A
    # missing interval does not prove closure (e.g. holiday or delayed feed).
    periods = metadata.get("currentTradingPeriod")
    if not isinstance(periods, dict):
        return ""
    for segment, state in (("regular", "REGULAR"), ("pre", "PRE"), ("post", "POST")):
        period = periods.get(segment)
        if not isinstance(period, dict):
            continue
        start = quote_time(period.get("start"))
        end = quote_time(period.get("end"))
        if start and end and start <= now < end:
            return state
    return ""


def quote_status(price, meta, *, compact=False):
    if not positive_price(price):
        return "Fiyat yok"
    meta = meta or {}
    state = meta.get("state", "cached")
    provider_time = meta.get("price_at")
    stamp = provider_time or meta.get("observed_at")
    if state == "mixed":
        label = "Eski kur"
    elif state == "cached":
        label = "Son kayıt"
    else:
        label = "Fiyat" if provider_time else "Alındı"
    if stamp:
        when = datetime.fromisoformat(stamp).astimezone(TURKEY_TIME)
        stamp = when.strftime("%H:%M" if compact and when.date() == datetime.now(TURKEY_TIME).date() else "%d.%m %H:%M")
        label = f"{label} {stamp}"
    session = SESSIONS.get(meta.get("market_state"), "")
    if compact:
        return label
    return " · ".join(part for part in (label, meta.get("provider"), session or "Seans bilgisi yok") if part)
