"""Time series and chart viewport calculations."""

import math
from datetime import datetime, timedelta


def parse_history_timestamp(value):
    """Return a local, timezone-naive datetime for stored/provider timestamps."""
    try:
        if hasattr(value, "to_pydatetime"):
            parsed = value.to_pydatetime()
        elif isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_history_timestamp(value):
    """Normalize provider timestamps so SQLite text ordering stays reliable."""
    parsed = parse_history_timestamp(value)
    if parsed is None:
        return str(value)
    return parsed.replace(microsecond=0).isoformat()


def downsample_history_rows(rows, max_points):
    """Reduce dense history while retaining each bucket's local high and low."""
    rows = list(rows or [])
    try:
        max_points = int(max_points)
    except (TypeError, ValueError):
        max_points = 0
    if max_points < 3 or len(rows) <= max_points:
        return rows

    interior = rows[1:-1]
    bucket_count = max(1, (max_points - 2) // 2)
    bucket_size = max(1, (len(interior) + bucket_count - 1) // bucket_count)
    sampled = [rows[0]]
    for offset in range(0, len(interior), bucket_size):
        bucket = interior[offset:offset + bucket_size]
        if not bucket:
            continue
        indexed = list(enumerate(bucket))
        low = min(indexed, key=lambda item: float(item[1][1]))
        high = max(indexed, key=lambda item: float(item[1][1]))
        for _index, row in sorted({low[0]: low, high[0]: high}.values()):
            sampled.append(row)
    sampled.append(rows[-1])
    return sampled[:max_points - 1] + [rows[-1]] if len(sampled) > max_points else sampled


def history_rows_in_window(rows, seconds):
    """Return the requested time span, anchored to the newest available bar."""
    prepared = []
    for timestamp, value in rows or []:
        parsed = parse_history_timestamp(timestamp)
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if parsed is None or not math.isfinite(numeric) or numeric <= 0:
            continue
        prepared.append((parsed, normalize_history_timestamp(parsed), numeric))
    prepared.sort(key=lambda row: row[0])
    if not prepared or not seconds:
        return [(stored, value) for _parsed, stored, value in prepared]
    cutoff = prepared[-1][0] - timedelta(seconds=float(seconds))
    return [
        (stored, value)
        for parsed, stored, value in prepared
        if parsed >= cutoff
    ]


def price_changes(rows):
    """Keep the first sample of each unchanged run in old poll-based history."""
    changes = []
    for row in rows:
        if not changes or row[1] != changes[-1][1]:
            changes.append(row)
    return changes


def sample_chart_rows(rows, max_points, interval, *, downsample=downsample_history_rows):
    """Preserve both ends of gaps in five-minute provider bars while sampling."""
    if interval != "5m" or len(rows) < 2:
        return downsample(rows, max_points), ()

    starts = [0]
    previous = parse_history_timestamp(rows[0][0])
    for index, row in enumerate(rows[1:], start=1):
        timestamp = parse_history_timestamp(row[0])
        if previous and timestamp and timestamp - previous > timedelta(minutes=15):
            starts.append(index)
        previous = timestamp
    if len(starts) == 1:
        return downsample(rows, max_points), ()

    sampled, breaks = [], []
    for start, end in zip(starts, starts[1:] + [len(rows)]):
        breaks.append(len(sampled))
        budget = max(3, round(max_points * (end - start) / len(rows)))
        sampled.extend(downsample(rows[start:end], budget))
    return sampled, tuple(breaks[1:])


def calculate_zoomed_range(start, end, zoom_in, anchor=0.5, min_span=0.02):
    """Calculate a clamped normalized chart viewport around an anchor point."""
    start = max(0.0, min(float(start), 1.0))
    end = max(start, min(float(end), 1.0))
    anchor = max(0.0, min(float(anchor), 1.0))
    min_span = max(0.001, min(float(min_span), 1.0))
    span = max(min_span, end - start)
    new_span = span * (0.65 if zoom_in else (1 / 0.65))
    new_span = max(min_span, min(new_span, 1.0))
    anchor_value = start + (span * anchor)
    new_start = anchor_value - (new_span * anchor)
    new_end = new_start + new_span
    if new_start < 0:
        new_end -= new_start
        new_start = 0.0
    if new_end > 1:
        new_start -= new_end - 1.0
        new_end = 1.0
    return max(0.0, new_start), min(1.0, new_end)


def calculate_panned_range(start, end, shift):
    """Shift a normalized chart viewport without changing its zoom level."""
    start = float(start)
    end = float(end)
    span = round(max(0.0, min(end - start, 1.0)), 12)
    new_start = start + float(shift)
    new_end = new_start + span
    if new_start < 0:
        return 0.0, span
    if new_end > 1:
        return 1.0 - span, 1.0
    return new_start, new_end
