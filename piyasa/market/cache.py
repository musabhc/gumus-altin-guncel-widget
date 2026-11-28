"""Serialization for current and legacy price caches."""

import time

from piyasa.market.quotes import normalize_quotes, positive_price


LEGACY_MARKET_FIELDS = {
    "ons_gumus": "gumus_ons",
    "gram_gumus_tl": "gumus_tl",
    "gram_altin_tl": "altin_tl",
    "dolar": "dolar",
}


def normalize_market_data(data):
    if not isinstance(data, dict):
        return None

    prices = {}
    raw_prices = data.get("prices")
    if isinstance(raw_prices, dict):
        for key, value in raw_prices.items():
            price = positive_price(value)
            if price:
                prices[str(key)] = price

    for legacy_key, new_key in LEGACY_MARKET_FIELDS.items():
        if new_key in prices:
            continue
        value = positive_price(data.get(legacy_key))
        if value:
            prices[new_key] = value

    sources = {}
    raw_sources = data.get("sources")
    if isinstance(raw_sources, dict):
        for key, symbol in raw_sources.items():
            key = str(key)
            symbol = str(symbol or "").strip().upper()
            if key in prices and symbol:
                sources[key] = symbol

    normalized = {
        "prices": prices,
        "sources": sources,
        "timestamp": data.get("timestamp", ""),
        "quotes": normalize_quotes(data.get("quotes"), prices),
    }
    for legacy_key, new_key in LEGACY_MARKET_FIELDS.items():
        if new_key in prices:
            normalized[legacy_key] = prices[new_key]
    return normalized


def market_data_for_save(prices, timestamp=None, sources=None, *, quotes=None):
    data = {
        "prices": {str(key): price for key, value in prices.items() if (price := positive_price(value))},
        "timestamp": timestamp or time.strftime("%d.%m %H:%M"),
    }
    if quotes is not None:
        data["quotes"] = normalize_quotes(quotes, data["prices"])
    sources = sources or {}
    data["sources"] = {
        str(key): str(symbol).strip().upper()
        for key, symbol in sources.items()
        if str(key) in data["prices"] and str(symbol or "").strip()
    }
    for legacy_key, new_key in LEGACY_MARKET_FIELDS.items():
        if new_key in data["prices"]:
            data[legacy_key] = data["prices"][new_key]
    return data
