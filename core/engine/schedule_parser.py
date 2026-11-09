"""
Natural-language schedule parser for ToolSmithAI.
Pure Python — stdlib only (re, datetime).

parse_schedule_intent(text)  → {frequency, cron, timezone, human_label}
compute_next_run_from_cron(cron_expr) → datetime (UTC)
"""
from __future__ import annotations

import datetime
import re
from typing import Optional

# Unix cron DOW: 0=Sunday, 1=Monday, ..., 6=Saturday
_WEEKDAY_NUMS: dict[str, int] = {
    "monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4,
    "friday": 5, "saturday": 6, "sunday": 0,
}
_WEEKDAY_LABELS: dict[str, str] = {
    "monday": "Monday", "tuesday": "Tuesday", "wednesday": "Wednesday",
    "thursday": "Thursday", "friday": "Friday",
    "saturday": "Saturday", "sunday": "Sunday",
}
_ORDINALS: dict[str, int] = {
    "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5,
    "6th": 6, "7th": 7, "8th": 8, "9th": 9, "10th": 10,
    "11th": 11, "12th": 12, "13th": 13, "14th": 14, "15th": 15,
    "16th": 16, "17th": 17, "18th": 18, "19th": 19, "20th": 20,
    "21st": 21, "22nd": 22, "23rd": 23, "24th": 24, "25th": 25,
    "26th": 26, "27th": 27, "28th": 28,
}
_DEFAULT_HOUR = 9  # 9 AM when no time is specified


def _parse_hour(text: str) -> Optional[int]:
    """Extract hour (0–23) from '9 AM', '14', '2pm', '8:00', or bare '8'."""
    text = text.strip().lower()
    m = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', text)
    if not m:
        return None
    h = int(m.group(1))
    meridiem = m.group(3)
    if meridiem == "pm" and h != 12:
        h += 12
    elif meridiem == "am" and h == 12:
        h = 0
    return h if 0 <= h <= 23 else None


def _fmt_hour(h: int) -> str:
    """Format an hour integer as '12 AM', '9 AM', '12 PM', '1 PM'."""
    if h == 0:
        return "12 AM"
    if h == 12:
        return "12 PM"
    if h < 12:
        return f"{h} AM"
    return f"{h - 12} PM"


def _ordinal_suffix(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def parse_schedule_intent(text: str) -> dict:
    """
    Parse a natural-language schedule phrase into cron metadata.

    Supported patterns:
      "every N minutes"               → */N * * * *
      "every N hours"                 → 0 */N * * *
      "every weekday [at H]"          → 0 H * * 1-5
      "every weekend [at H]"          → 0 H * * 0,6
      "every <weekday name> [at H]"   → 0 H * * D   (Unix DOW: 0=Sun)
      "daily [at H]" / "every day"    → 0 H * * *
      "weekly [at H]" / "every week"  → 0 H * * 1   (Monday)
      "monthly on the Nth [at H]"     → 0 H N * *
      "monthly [at H]" / "every month"→ 0 H 1 * *   (1st of month)

    Returns:
        {
          "frequency":   "daily" | "weekly" | "monthly" | "custom",
          "cron":        5-field Unix cron string,
          "timezone":    "UTC",
          "human_label": plain-English description
        }

    Raises ValueError for unrecognisable or out-of-range inputs.
    """
    if not text or not text.strip():
        raise ValueError("Schedule text must not be empty.")

    raw = text.strip()
    s = re.sub(r"\s+", " ", raw.lower())

    # ── Every N minutes ───────────────────────────────────────────────────────
    m = re.search(r"\bevery\s+(\d+)\s+min(?:ute)?s?\b", s)
    if m:
        n = int(m.group(1))
        if not (1 <= n <= 59):
            raise ValueError(f"Minute interval {n} is out of range (1–59).")
        label = f"Every {n} minute{'s' if n > 1 else ''}"
        return {"frequency": "custom", "cron": f"*/{n} * * * *", "timezone": "UTC", "human_label": label}

    # ── Every N hours ─────────────────────────────────────────────────────────
    m = re.search(r"\bevery\s+(\d+)\s+hours?\b", s)
    if m:
        n = int(m.group(1))
        if not (1 <= n <= 23):
            raise ValueError(f"Hour interval {n} is out of range (1–23).")
        label = f"Every {n} hour{'s' if n > 1 else ''}"
        return {"frequency": "custom", "cron": f"0 */{n} * * *", "timezone": "UTC", "human_label": label}

    # Extract optional "at H" suffix for all remaining patterns
    at_m = re.search(r"\bat\s+(.+)", s)
    h: int = _DEFAULT_HOUR
    if at_m:
        parsed_h = _parse_hour(at_m.group(1))
        if parsed_h is not None:
            h = parsed_h

    # ── Every weekday (Mon–Fri) ───────────────────────────────────────────────
    if re.search(r"\bweekdays?\b", s):
        label = f"Every weekday at {_fmt_hour(h)}"
        return {"frequency": "custom", "cron": f"0 {h} * * 1-5", "timezone": "UTC", "human_label": label}

    # ── Every weekend (Sat–Sun) ───────────────────────────────────────────────
    if re.search(r"\bweekends?\b", s):
        label = f"Every weekend at {_fmt_hour(h)}"
        return {"frequency": "custom", "cron": f"0 {h} * * 0,6", "timezone": "UTC", "human_label": label}

    # ── Monthly on the Nth (checked before weekday loop to avoid false positives) ──
    mon_m = re.search(
        r"\b(?:monthly|every\s+month)\b.*?\bon\s+(?:the\s+)?(\d+(?:st|nd|rd|th)?)\b",
        s,
    )
    if mon_m:
        raw_day = mon_m.group(1)
        digit_only = re.sub(r"[a-z]+$", "", raw_day)
        day_n: Optional[int] = int(digit_only) if digit_only.isdigit() else _ORDINALS.get(raw_day)
        if day_n is not None and 1 <= day_n <= 28:
            suf = _ordinal_suffix(day_n)
            label = f"Monthly on the {day_n}{suf} at {_fmt_hour(h)}"
            return {"frequency": "monthly", "cron": f"0 {h} {day_n} * *", "timezone": "UTC", "human_label": label}

    # ── Every <named weekday> [at H] ──────────────────────────────────────────
    for day_name, unix_dow in _WEEKDAY_NUMS.items():
        if re.search(rf"\b{day_name}s?\b", s):
            label_day = _WEEKDAY_LABELS[day_name]
            label = f"Every {label_day} at {_fmt_hour(h)}"
            return {"frequency": "weekly", "cron": f"0 {h} * * {unix_dow}", "timezone": "UTC", "human_label": label}

    # ── Daily ─────────────────────────────────────────────────────────────────
    if re.search(r"\b(?:daily|every\s+day)\b", s):
        label = f"Daily at {_fmt_hour(h)}"
        return {"frequency": "daily", "cron": f"0 {h} * * *", "timezone": "UTC", "human_label": label}

    # ── Weekly (no specific day) ───────────────────────────────────────────────
    if re.search(r"\b(?:weekly|every\s+week)\b", s):
        label = f"Weekly on Mondays at {_fmt_hour(h)}"
        return {"frequency": "weekly", "cron": f"0 {h} * * 1", "timezone": "UTC", "human_label": label}

    # ── Monthly (no specific day) ──────────────────────────────────────────────
    if re.search(r"\b(?:monthly|every\s+month)\b", s):
        label = f"Monthly on the 1st at {_fmt_hour(h)}"
        return {"frequency": "monthly", "cron": f"0 {h} 1 * *", "timezone": "UTC", "human_label": label}

    raise ValueError(
        f"Could not parse schedule from: {raw!r}. "
        "Try phrases like 'every Friday at 9 AM', 'daily at 6 AM', or 'every 2 hours'."
    )


def compute_next_run_from_cron(cron_expr: str) -> datetime.datetime:
    """
    Compute the next UTC datetime matching a 5-field Unix cron expression.

    Unix DOW convention: 0=Sunday, 1=Monday, ..., 6=Saturday.
    Handles all patterns produced by parse_schedule_intent.
    """
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Expected 5-field cron expression, got: {cron_expr!r}")

    minute_f, hour_f, dom_f, month_f, dow_f = parts
    now = datetime.datetime.now(datetime.timezone.utc).replace(second=0, microsecond=0)

    # ── Every N minutes ───────────────────────────────────────────────────────
    step_min = re.fullmatch(r"\*/(\d+)", minute_f)
    if step_min and hour_f == "*" and dom_f == "*" and dow_f == "*":
        n = int(step_min.group(1))
        nxt = now + datetime.timedelta(minutes=1)
        rem = nxt.minute % n
        if rem != 0:
            nxt += datetime.timedelta(minutes=(n - rem))
        return nxt

    # ── Every N hours ─────────────────────────────────────────────────────────
    step_hr = re.fullmatch(r"\*/(\d+)", hour_f)
    if minute_f == "0" and step_hr and dom_f == "*" and dow_f == "*":
        n = int(step_hr.group(1))
        nxt = (now + datetime.timedelta(minutes=1)).replace(minute=0)
        rem = nxt.hour % n
        if rem != 0:
            nxt += datetime.timedelta(hours=(n - rem))
        return nxt

    # ── Fixed-hour patterns ───────────────────────────────────────────────────
    if not hour_f.isdigit():
        raise ValueError(f"Unsupported cron hour field: {hour_f!r}")
    h = int(hour_f)

    # ── Monthly on specific DOM ───────────────────────────────────────────────
    if dom_f != "*" and dow_f == "*":
        day_n = int(dom_f)
        try:
            candidate = now.replace(day=day_n, hour=h, minute=0)
            if candidate > now:
                return candidate
        except ValueError:
            pass
        year, month = now.year, now.month
        for _ in range(24):
            month += 1
            if month > 12:
                month = 1
                year += 1
            try:
                return datetime.datetime(year, month, day_n, h, 0, 0, tzinfo=datetime.timezone.utc)
            except ValueError:
                continue
        return now + datetime.timedelta(days=32)

    # ── DOW-based patterns (daily, specific day, weekdays, weekends) ──────────
    # Python isoweekday(): 1=Mon,...,7=Sun  →  Unix DOW: 7%7=0(Sun), 1=Mon,...,6=Sat
    def _unix_dow(dt: datetime.datetime) -> int:
        return dt.isoweekday() % 7

    def _matches_dow(dt: datetime.datetime) -> bool:
        u = _unix_dow(dt)
        if dow_f == "*":
            return True
        if "-" in dow_f and "," not in dow_f:
            lo, hi = (int(x) for x in dow_f.split("-"))
            return lo <= u <= hi
        if "," in dow_f:
            return u in {int(x) for x in dow_f.split(",")}
        return u == int(dow_f)

    candidate = now.replace(hour=h, minute=0)
    if candidate <= now:
        candidate += datetime.timedelta(days=1)

    for _ in range(8):
        if _matches_dow(candidate):
            return candidate
        candidate += datetime.timedelta(days=1)

    return now + datetime.timedelta(days=1)
