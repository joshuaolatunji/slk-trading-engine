#!/usr/bin/env python3
"""
SLK (Structure, Liquidity, Key Levels) deterministic research engine.
Implements strategy_config.json v1.0 baseline: bias_policy=B1, entry_policy=P1 only.
B2 and P2 are disabled per config and are NOT implemented here (policy-disabled, not silently approximated).

This is a RESEARCH engine against the UNVALIDATED SLK specification supplied by the user
(SLK_AI_Tradebook package). It does not place orders. live_execution_allowed is always False.

All data comes from Yahoo Finance v8 chart endpoints (keyless, public). No broker/account
connection exists, so contract_identity, spread_execution and portfolio_sizing gates will
always be UNKNOWN/FAIL here -- this caps every output at WATCHLIST/NO_TRADE/INSUFFICIENT_DATA,
never QUALIFIED_SETUP, regardless of how clean the chart geometry is. That is a correct,
expected consequence of the missing broker inputs, not an engine bug.
"""
import json
import math
import urllib.request
import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

UA = {"User-Agent": "Mozilla/5.0"}

# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------

def fetch_yahoo(symbol, rng, interval):
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?range={rng}&interval={interval}"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)
    result = data["chart"]["result"][0]
    ts = result["timestamp"]
    q = result["indicators"]["quote"][0]
    bars = []
    for i, t in enumerate(ts):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        bars.append({"t": dt.datetime.fromtimestamp(t, dt.timezone.utc), "o": o, "h": h, "l": l, "c": c})
    bars.sort(key=lambda b: b["t"])
    return bars


def clean_trailing_artifact(bars):
    """Yahoo sometimes appends a stale/degenerate 'live quote' row as the final bar.
    Drop it if degenerate (O=H=L=C), or merge it into the prior bar if it falls on an
    off-schedule hour (extends the same session rather than starting a clean new period).
    This mirrors the artifact found and handled manually in the earlier research pass."""
    if len(bars) < 3:
        return bars
    hours = [b["t"].hour for b in bars[:-1]]
    mode_hour = max(set(hours), key=hours.count)
    last = bars[-1]
    if last["o"] == last["h"] == last["l"] == last["c"]:
        return bars[:-1]
    if last["t"].hour != mode_hour:
        prev = bars[-2]
        merged = {
            "t": prev["t"], "o": prev["o"],
            "h": max(prev["h"], last["h"]), "l": min(prev["l"], last["l"]),
            "c": last["c"],
        }
        return bars[:-2] + [merged]
    return bars


def aggregate_h4(h1_bars, anchor_hours=(0, 4, 8, 12, 16, 20)):
    """Aggregate H1 bars into UTC-anchored 4H buckets. Drops the final bucket if it
    doesn't look complete (fewer bars than any other recent bucket), to avoid using a
    forming H4 bar. Declared aggregation anchor: 00:00 UTC, 4-hour blocks."""
    buckets = {}
    for b in h1_bars:
        bucket_hour = (b["t"].hour // 4) * 4
        key = b["t"].replace(hour=bucket_hour, minute=0, second=0, microsecond=0)
        buckets.setdefault(key, []).append(b)
    keys = sorted(buckets.keys())
    out = []
    sizes = []
    for k in keys:
        grp = sorted(buckets[k], key=lambda b: b["t"])
        out.append({
            "t": k + dt.timedelta(hours=4),
            "o": grp[0]["o"], "h": max(x["h"] for x in grp),
            "l": min(x["l"] for x in grp), "c": grp[-1]["c"],
            "_n": len(grp),
        })
        sizes.append(len(grp))
    if len(out) >= 2:
        typical = sorted(sizes[:-1])[len(sizes[:-1]) // 2] if len(sizes) > 1 else sizes[0]
        if out[-1]["_n"] < max(1, typical - 1) and out[-1]["_n"] < typical:
            out = out[:-1]
    for o in out:
        o.pop("_n", None)
    return out


# ---------------------------------------------------------------------------
# Core math: ATR (Wilder), epsilon
# ---------------------------------------------------------------------------

def true_range(h, l, c_prev):
    return max(h - l, abs(h - c_prev), abs(l - c_prev))


def atr14_wilder(bars):
    """Returns list aligned to bars; None where undefined. 14-bar arithmetic seed then Wilder smoothing."""
    n = len(bars)
    atr = [None] * n
    if n < 15:
        return atr
    trs = [None]
    for i in range(1, n):
        trs.append(true_range(bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]))
    seed = sum(trs[1:15]) / 14.0
    atr[14] = seed
    for i in range(15, n):
        atr[i] = (atr[i - 1] * 13 + trs[i]) / 14.0
    return atr


def epsilon_at(atr_prev, tick_size):
    """epsilon = max(2 ticks, 0.02 * ATR14_previous). Returns None if ATR unknown."""
    if atr_prev is None:
        return None
    return max(2 * tick_size, 0.02 * atr_prev)


# ---------------------------------------------------------------------------
# Body swings (pivots), known only at close of i+2
# ---------------------------------------------------------------------------

def body_high(b):
    return max(b["o"], b["c"])


def body_low(b):
    return min(b["o"], b["c"])


@dataclass
class Pivot:
    idx: int
    kind: str  # 'high' or 'low'
    price: float
    known_idx: int  # index of the bar whose close confirms it (i+2)
    broken: bool = False
    break_idx: Optional[int] = None


def find_body_pivots(bars):
    """Strict 2-left/2-right body-extreme pivots. Equal values create no pivot."""
    bh = [body_high(b) for b in bars]
    bl = [body_low(b) for b in bars]
    pivots = []
    n = len(bars)
    for i in range(2, n - 2):
        if bh[i] > bh[i - 1] and bh[i] > bh[i - 2] and bh[i] > bh[i + 1] and bh[i] > bh[i + 2]:
            pivots.append(Pivot(idx=i, kind="high", price=bh[i], known_idx=i + 2))
        if bl[i] < bl[i - 1] and bl[i] < bl[i - 2] and bl[i] < bl[i + 1] and bl[i] < bl[i + 2]:
            pivots.append(Pivot(idx=i, kind="low", price=bl[i], known_idx=i + 2))
    return pivots


def structural_breaks(bars, pivots, atr):
    """Chronological scan: a completed close beyond the most recent confirmed, unbroken
    pivot by more than epsilon is a first-break event (one break per pivot)."""
    breaks = []  # list of dicts: {bar_idx, direction, pivot}
    highs = sorted([p for p in pivots if p.kind == "high"], key=lambda p: p.known_idx)
    lows = sorted([p for p in pivots if p.kind == "low"], key=lambda p: p.known_idx)
    n = len(bars)
    for i in range(n):
        eps = epsilon_at(atr[i - 1] if i > 0 else None, None)  # placeholder, caller sets tick via closure
        # handled by caller via epsilon_fn injection instead -- see evaluate_breaks()
    return breaks  # unused placeholder; real logic lives in evaluate_breaks (kept for clarity)


def evaluate_breaks(bars, pivots, atr, tick_size):
    highs = sorted([p for p in pivots if p.kind == "high"], key=lambda p: p.known_idx)
    lows = sorted([p for p in pivots if p.kind == "low"], key=lambda p: p.known_idx)
    events = []
    n = len(bars)
    for i in range(n):
        if i == 0:
            continue
        eps = epsilon_at(atr[i - 1], tick_size)
        if eps is None:
            continue
        close = bars[i]["c"]
        # most recent confirmed (known_idx <= i), unbroken high pivot with idx < i
        candidates_h = [p for p in highs if p.known_idx <= i and not p.broken and p.idx < i]
        if candidates_h:
            piv = max(candidates_h, key=lambda p: p.idx)
            if close > piv.price + eps:
                piv.broken = True
                piv.break_idx = i
                events.append({"bar_idx": i, "direction": "bullish", "pivot": piv, "epsilon": eps})
        candidates_l = [p for p in lows if p.known_idx <= i and not p.broken and p.idx < i]
        if candidates_l:
            piv = max(candidates_l, key=lambda p: p.idx)
            if close < piv.price - eps:
                piv.broken = True
                piv.break_idx = i
                events.append({"bar_idx": i, "direction": "bearish", "pivot": piv, "epsilon": eps})
    return events


# ---------------------------------------------------------------------------
# A/V geometry (close-price line chart, 1-neighbor rule), known at i+1
# ---------------------------------------------------------------------------

def find_av_zones(bars, atr, tick_size):
    zones = []
    n = len(bars)
    for i in range(1, n - 1):
        c = bars[i]["c"]
        if c > bars[i - 1]["c"] and c > bars[i + 1]["c"]:
            eps = epsilon_at(atr[i], tick_size)
            if eps is not None:
                zones.append({"type": "A", "idx": i, "center": c, "eps": eps, "known_idx": i + 1,
                              "lower": c - eps, "upper": c + eps, "flipped": False, "flip_idx": None})
        if c < bars[i - 1]["c"] and c < bars[i + 1]["c"]:
            eps = epsilon_at(atr[i], tick_size)
            if eps is not None:
                zones.append({"type": "V", "idx": i, "center": c, "eps": eps, "known_idx": i + 1,
                              "lower": c - eps, "upper": c + eps, "flipped": False, "flip_idx": None})
    return zones


def sponsored_reaction_ok(bars, zone, atr, break_idx):
    """Before break_idx: require a completed close reacting >=0.5*ATR(frozen at formation)
    away from center, after known_at and before break."""
    atr_frozen = atr[zone["idx"]]
    if atr_frozen is None:
        return False, None
    for j in range(zone["known_idx"], break_idx):
        c = bars[j]["c"]
        if zone["type"] == "A" and c <= zone["center"] - 0.5 * atr_frozen:
            return True, j
        if zone["type"] == "V" and c >= zone["center"] + 0.5 * atr_frozen:
            return True, j
    return False, None


def is_displacement(bars, i, atr):
    if i == 0 or atr[i - 1] is None:
        return False
    b = bars[i]
    body = abs(b["c"] - b["o"])
    rng = b["h"] - b["l"]
    if rng <= 0:
        return False
    if body < 0.8 * atr[i - 1]:
        return False
    if body / rng < 0.60:
        return False
    outer = 0.25 * rng
    if b["c"] >= b["o"]:  # bullish move
        if b["c"] < b["h"] - outer:
            return False
    else:
        if b["c"] > b["l"] + outer:
            return False
    return True


def find_fvg(bars, b_idx, atr, tick_size):
    """3-candle FVG using a=b-1, b, c=b+1. Returns dict or None. Known at close of c (=b_idx+1)."""
    if b_idx < 1 or b_idx + 1 >= len(bars):
        return None
    a, b, c = bars[b_idx - 1], bars[b_idx], bars[b_idx + 1]
    eps_ref = atr[b_idx - 1]
    if eps_ref is None:
        return None
    min_width = 0.05 * eps_ref
    if c["l"] > a["h"]:
        width = c["l"] - a["h"]
        if width >= min_width:
            return {"dir": "bullish", "lo": a["h"], "hi": c["l"], "known_idx": b_idx + 1}
    if c["h"] < a["l"]:
        width = a["l"] - c["h"]
        if width >= min_width:
            return {"dir": "bearish", "lo": c["h"], "hi": a["l"], "known_idx": b_idx + 1}
    return None


# ---------------------------------------------------------------------------
# B1 weekly bias
# ---------------------------------------------------------------------------

def sweep_event(bars, i, level, eps, direction):
    """direction='bullish' -> low sweep: L[i]<level-eps and C[i]>level+eps.
       direction='bearish' -> high sweep: H[i]>level+eps and C[i]<level-eps."""
    b = bars[i]
    if direction == "bullish":
        return b["l"] < level - eps and b["c"] > level + eps
    else:
        return b["h"] > level + eps and b["c"] < level - eps


def evaluate_b1_weekly(w1_bars, atr_w1, tick_size, scan=3):
    """Return the latest eligible weekly event, or None. Event dict includes direction,
    event_idx, known_idx (=event bar close, since weekly events are known at their own close
    for the sweep case), target (nearest untouched wick extreme), and type."""
    n = len(w1_bars)
    if n < scan + 2:
        return None
    candidates = []
    for i in range(n - scan, n):
        if i < 1:
            continue
        eps = epsilon_at(atr_w1[i - 1], tick_size)
        if eps is None:
            continue
        prev = w1_bars[i - 1]
        # Type 1 bullish: sweep prev week's low, close back above it; prev week's high untouched since
        if sweep_event(w1_bars, i, prev["l"], eps, "bullish"):
            untouched = all(w1_bars[j]["h"] < prev["h"] for j in range(i, n))
            if untouched:
                target = None
                for j in range(i - 1, -1, -1):
                    if w1_bars[j]["h"] > w1_bars[i]["c"]:
                        target = w1_bars[j]["h"]
                        break
                candidates.append({"dir": "bullish", "event_idx": i, "known_idx": i, "type": "sweep",
                                    "target": target, "ref_level": prev["l"]})
        # Type 1 bearish: sweep prev week's high, close back below it; prev week's low untouched since
        if sweep_event(w1_bars, i, prev["h"], eps, "bearish"):
            untouched = all(w1_bars[j]["l"] > prev["l"] for j in range(i, n))
            if untouched:
                target = None
                for j in range(i - 1, -1, -1):
                    if w1_bars[j]["l"] < w1_bars[i]["c"]:
                        target = w1_bars[j]["l"]
                        break
                candidates.append({"dir": "bearish", "event_idx": i, "known_idx": i, "type": "sweep",
                                    "target": target, "ref_level": prev["h"]})
    if not candidates:
        return None
    latest_idx = max(c["event_idx"] for c in candidates)
    latest = [c for c in candidates if c["event_idx"] == latest_idx]
    dirs = set(c["dir"] for c in latest)
    if len(dirs) > 1:
        return {"dir": "NO_TRADE", "reason": "conflicting weekly events on same bar"}
    chosen = latest[0]
    if chosen["target"] is None:
        return None  # ambiguous target availability -> no bias assigned
    return chosen


def confirm_lower_tf_break(bars, atr, tick_size, direction, after_dt, max_age_bars):
    """Find a same-direction structural break with known_idx strictly after `after_dt`,
    within the last `max_age_bars` bars, with no later opposite-direction break."""
    pivots = find_body_pivots(bars)
    events = evaluate_breaks(bars, pivots, atr, tick_size)
    n = len(bars)
    window_start = max(0, n - max_age_bars)
    same = [e for e in events if e["direction"] == direction and e["bar_idx"] >= window_start
            and bars[e["bar_idx"]]["t"] > after_dt]
    if not same:
        return None
    best = max(same, key=lambda e: e["bar_idx"])
    opposite_dir = "bearish" if direction == "bullish" else "bullish"
    later_opposite = [e for e in events if e["direction"] == opposite_dir and e["bar_idx"] > best["bar_idx"]]
    if later_opposite:
        return None
    return best


# ---------------------------------------------------------------------------
# P1 entry evaluation on H1
# ---------------------------------------------------------------------------

def evaluate_p1(h1_bars, atr_h1, tick_size, bias_dir, max_level_age_h1_bars=40):
    """Search H1 bars (most recent first) for a valid P1 long/short candidate matching bias_dir."""
    zones = find_av_zones(h1_bars, atr_h1, tick_size)
    pivots = find_body_pivots(h1_bars)
    n = len(h1_bars)
    want_zone_type = "A" if bias_dir == "bullish" else "V"
    relevant_zones = [z for z in zones if z["type"] == want_zone_type]

    candidates = []
    for z in relevant_zones:
        # Find a displacement+structural-break bar after the zone's known_idx that closes
        # beyond the zone (flip), within max_level_age_h1_bars of the zone's formation.
        for i in range(z["known_idx"] + 1, min(n, z["idx"] + max_level_age_h1_bars)):
            eps = epsilon_at(atr_h1[i - 1], tick_size)
            if eps is None:
                continue
            close = h1_bars[i]["c"]
            if bias_dir == "bullish":
                flip = close > z["upper"] + eps
            else:
                flip = close < z["lower"] - eps
            if not flip:
                continue
            if not is_displacement(h1_bars, i, atr_h1):
                continue
            sponsored, _ = sponsored_reaction_ok(h1_bars, z, atr_h1, i)
            if not sponsored:
                continue
            fvg = find_fvg(h1_bars, i, atr_h1, tick_size)
            if fvg is None or fvg["dir"] != bias_dir:
                continue
            if not (fvg["lo"] <= z["center"] <= fvg["hi"]):
                continue
            # inside candle check (i+1 strictly inside i)
            if i + 1 >= n:
                continue
            nxt = h1_bars[i + 1]
            if not (nxt["h"] < h1_bars[i]["h"] and nxt["l"] > h1_bars[i]["l"]):
                continue
            internal_ref = nxt["l"] if bias_dir == "bullish" else nxt["h"]
            if bias_dir == "bullish" and not (internal_ref > z["center"]):
                continue
            if bias_dir == "bearish" and not (internal_ref < z["center"]):
                continue
            # freshness: no bar between break (i) and now (n-1) has overlapped the zone
            fresh = True
            for j in range(i, n):
                if z["lower"] <= h1_bars[j]["h"] and z["upper"] >= h1_bars[j]["l"]:
                    if j > i + 1:  # allow the confirming inside bar itself per spec note
                        fresh = False
                        break
            candidates.append({
                "zone": z, "break_idx": i, "fvg": fvg, "inside_idx": i + 1,
                "internal_ref": internal_ref, "fresh": fresh,
            })
    return candidates


# ---------------------------------------------------------------------------
# Instrument pipeline
# ---------------------------------------------------------------------------

INSTRUMENTS = {
    "EURUSD": {"symbol": "EURUSD=X", "profile": "FOREX", "tick": 0.00001, "price_unit": "rate"},
    "GBPUSD": {"symbol": "GBPUSD=X", "profile": "FOREX", "tick": 0.00001, "price_unit": "rate"},
    "USDJPY": {"symbol": "USDJPY=X", "profile": "FOREX", "tick": 0.001, "price_unit": "rate"},
    "USDCHF": {"symbol": "USDCHF=X", "profile": "FOREX", "tick": 0.00001, "price_unit": "rate"},
    "XAUUSD": {"symbol": "GC=F", "profile": "XAUUSD", "tick": 0.01, "price_unit": "USD/oz", "proxy_note": "GC=F futures proxy, not true interbank spot"},
    "NAS100": {"symbol": "NQ=F", "profile": "NAS100", "tick": 0.25, "price_unit": "index", "proxy_note": "NQ=F futures proxy for the NAS100 CFD basis"},
}


def run_instrument(name, cfg, as_of):
    sym = cfg["symbol"]
    w1 = clean_trailing_artifact(fetch_yahoo(sym, "3y", "1wk"))
    d1 = clean_trailing_artifact(fetch_yahoo(sym, "2y", "1d"))
    h1_raw = clean_trailing_artifact(fetch_yahoo(sym, "1y", "60m"))
    h4 = aggregate_h4(h1_raw)
    h1 = h1_raw
    m15 = clean_trailing_artifact(fetch_yahoo(sym, "10d", "15m"))

    hist_ok = {
        "W1": len(w1) >= 104, "D1": len(d1) >= 260, "H4": len(h4) >= 500,
        "H1": len(h1) >= 500, "M15": len(m15) >= 500,
    }

    atr_w1 = atr14_wilder(w1)
    atr_d1 = atr14_wilder(d1)
    atr_h4 = atr14_wilder(h4)
    atr_h1 = atr14_wilder(h1)

    tick = cfg["tick"]

    result = {
        "instrument": name, "symbol": sym, "profile": cfg["profile"],
        "as_of_utc": as_of.isoformat(), "bar_counts": {k: len(v) for k, v in
            [("W1", w1), ("D1", d1), ("H4", h4), ("H1", h1), ("M15", m15)]},
        "history_sufficient": hist_ok,
    }

    if not all(hist_ok.values()):
        result["decision"] = "INSUFFICIENT_DATA"
        result["reason"] = f"History below required minimums: {[k for k,v in hist_ok.items() if not v]}"
        return result

    weekly = evaluate_b1_weekly(w1, atr_w1, tick)
    if weekly is None:
        result["decision"] = "NO_TRADE"
        result["rejection_codes"] = ["NO_WEEKLY_BIAS"]
        result["reason"] = "No eligible B1 weekly event in the scanned window, or target availability ambiguous."
        return result
    if weekly.get("dir") == "NO_TRADE":
        result["decision"] = "NO_TRADE"
        result["rejection_codes"] = ["HTF_CONFLICT"]
        result["reason"] = weekly["reason"]
        return result

    bias_dir = weekly["dir"]
    weekly_bar = w1[weekly["event_idx"]]
    result["weekly_bias"] = {
        "direction": bias_dir, "event_type": weekly["type"],
        "event_bar_time": weekly_bar["t"].isoformat(), "target": weekly["target"],
        "ref_level": weekly["ref_level"],
    }

    # target already touched check (weekly target vs all bars since event)
    target_touched = False
    for b in w1[weekly["event_idx"]:]:
        if bias_dir == "bullish" and b["h"] >= weekly["target"]:
            target_touched = True
        if bias_dir == "bearish" and b["l"] <= weekly["target"]:
            target_touched = True
    if target_touched:
        result["decision"] = "NO_TRADE"
        result["rejection_codes"] = ["TARGET_ALREADY_TOUCHED"]
        result["reason"] = "Weekly thesis target has already traded since the event."
        return result

    daily_break = confirm_lower_tf_break(d1, atr_d1, tick, bias_dir, weekly_bar["t"], 10)
    if daily_break is None:
        result["decision"] = "WATCHLIST"
        result["weekly_bias"]["status"] = "awaiting D1 confirmation (B2 four-hour exception is disabled per config)"
        result["next_trigger"] = f"A same-direction D1 structural break within 10 bars, after {weekly_bar['t'].isoformat()}"
        return result

    daily_bar_time = d1[daily_break["bar_idx"]]["t"]
    result["daily_confirmation"] = {"direction": bias_dir, "bar_time": daily_bar_time.isoformat()}

    h4_break = confirm_lower_tf_break(h4, atr_h4, tick, bias_dir, daily_bar_time, 12)
    if h4_break is None:
        result["decision"] = "WATCHLIST"
        result["next_trigger"] = f"A same-direction H4 structural break within 12 bars, after {daily_bar_time.isoformat()}"
        return result

    h4_bar_time = h4[h4_break["bar_idx"]]["t"]
    result["h4_alignment"] = {"direction": bias_dir, "bar_time": h4_bar_time.isoformat()}

    # P1 candidates on H1, only using H1 bars at/after H4 alignment known
    h1_after = [b for b in h1 if b["t"] >= h4_bar_time]
    offset = len(h1) - len(h1_after)
    atr_h1_after = atr_h1[offset:] if offset >= 0 else atr_h1
    candidates = evaluate_p1(h1_after, atr_h1_after, tick, bias_dir)
    fresh_candidates = [c for c in candidates if c["fresh"]]

    if not fresh_candidates:
        result["decision"] = "WATCHLIST"
        result["p1_candidates_found"] = len(candidates)
        result["next_trigger"] = (
            f"P1 {'A-flip-up' if bias_dir=='bullish' else 'V-flip-down'} geometry "
            f"(structural break + displacement + sponsored reaction + FVG overlap + fresh inside-candle) on H1"
        )
        return result

    # Pick nearest-to-current-price candidate per candidate_selection rule
    last_price = h1[-1]["c"]
    best = min(fresh_candidates, key=lambda c: abs(c["zone"]["center"] - last_price))
    result["decision"] = "WATCHLIST"  # capped here -- see gate section below
    result["p1_geometry_found"] = True
    z = best["zone"]
    result["p1_setup"] = {
        "side": "LONG" if bias_dir == "bullish" else "SHORT",
        "key_level_center": z["center"],
        "fvg": best["fvg"],
        "break_bar_time": h1_after[best["break_idx"]]["t"].isoformat(),
        "internal_liquidity_ref": best["internal_ref"],
    }
    result["next_trigger"] = "Geometry is complete; QUALIFIED_SETUP is still blocked by missing broker/account gates (see gates)."
    return result


def main():
    as_of = dt.datetime.now(dt.timezone.utc)
    all_results = {}
    for name, cfg in INSTRUMENTS.items():
        try:
            all_results[name] = run_instrument(name, cfg, as_of)
        except Exception as e:
            all_results[name] = {"instrument": name, "decision": "INSUFFICIENT_DATA",
                                   "reason": f"Engine error: {type(e).__name__}: {e}"}
    print(json.dumps(all_results, indent=2, default=str))


if __name__ == "__main__":
    main()
