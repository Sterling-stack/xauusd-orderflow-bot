"""
XAUUSD ORDER FLOW BOT - CENTRAL STRATEGY ENGINE
================================================
Follows the two PDFs:
  * "XAUUSD Order Flow Micro-Scalping Bot Blueprint"
  * "XAUUSD Order Flow Bot - 10 Advanced Filter Conditions"

What this file does
  1. Builds CVD and 15-minute candles in memory from the ticks in DuckDB.
  2. Detects liquidity sweep + reclaim events (the trade TRIGGER).
  3. Runs every PDF filter on each trigger and applies the AND-gate:
     a trade is authorized ONLY if every required filter is PASS.
  4. Gives a clear reason for every filter that fails or is UNAVAILABLE.
  5. Holds the risk rules (2% daily loss, trades-per-day ceiling).

What it does NOT do (on purpose)
  * It places NO trades. There is no MT5 order code in this project yet.
  * It never invents data. Filters that need order-book depth or traded
    volume report UNAVAILABLE, and UNAVAILABLE blocks the trade.

Run:  python main.py            (status report, no trades)
Backtest lives in backtest.py and imports everything from this file.
"""

import csv
import sys
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

import duckdb
import numpy as np

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "xauusd_ticks.duckdb"

PASS, FAIL, UNAVAILABLE = "PASS", "FAIL", "UNAVAILABLE"
CANDLE_MS = 15 * 60 * 1000
DAY_MS = 24 * 60 * 60 * 1000


# =====================================================================
# SETTINGS
# =====================================================================
@dataclass
class Config:
    # ---------- Values taken from the PDFs ----------
    spread_multiplier: float = 1.20          # Filter PDF #1 (Blueprint says 1.3 - conflict, Filter PDF used)
    spread_window_ms: int = 5 * 60 * 1000    # 5-minute rolling average
    news_before_s: int = 300                 # Filter #2: pause 5 min before
    news_after_s: int = 600                  # Filter #2: pause 10 min after
    news_file: str = "news_events.csv"       # YOU must supply this (see README text in chat)
    accel_window_s: int = 15                 # Filter #3: 15-second windows
    accel_threshold: float = None            # PDFs give NO number -> UNAVAILABLE until you set one
    tobi_min: float = 0.70                   # Filter #4: >= 0.70 (Blueprint says 0.65 - Filter PDF used)
    stacked_min_steps: int = 3               # Filter #5
    stacked_min_ratio: float = 3.0           # Filter #5: 3:1
    sweep_window_s: int = 30                 # Filter #6: reclaim within 30 s
    htf_periods: int = 4                     # Filter #8: last 4 completed 15m candles
    early_eject_after_s: float = 4.0         # Filter #10: from t >= 4 s
    session_mode: str = "NEW_YORK"           # "NEW_YORK" (daylight saving) or "EST_FIXED" (UTC-5 all year)
    session_windows: tuple = ((dtime(8, 0), dtime(11, 30)),
                              (dtime(13, 30), dtime(16, 0)))   # Filter #9
    hold_seconds: float = 14.0               # Blueprint: 10-16 s (its code sample uses 14)
    max_daily_loss_pct: float = 2.0          # Blueprint risk rule
    max_trades_per_day: int = 12             # Blueprint: 5-12 per day (only the ceiling can be enforced)
    target_min_trades_per_day: int = 5

    # ---------- NOT in the PDFs: my assumptions, all editable ----------
    sweep_min_break_spreads: float = 1.0     # a break must exceed the level by >= 1 x current spread (0 = off)
    early_eject_cvd_window_s: float = 2.0    # "live CVD direction" = CVD change over the last 2 s
    latency_ms: int = 250                    # delay between signal and fill (and between exit decision and fill)
    slippage_usd_per_side: float = 0.05      # extra price slippage per order, in $ per ounce
    commission_usd_per_lot_per_side: float = 0.0   # set this if your Exness account charges commission
    contract_oz: float = 100.0               # 1 lot = 100 oz of gold
    lot_size: float = 0.01
    start_balance: float = 1000.0
    max_fill_delay_ms: int = 5000            # if no tick arrives within 5 s of the intended fill, skip the trade


@dataclass
class FilterResult:
    name: str
    status: str
    reason: str


# Entry filters in evaluation order. Filter 10 (early eject) is an EXIT rule.
ENTRY_FILTERS = [
    "SESSION",           # 9
    "NEWS",              # 2
    "SPREAD",            # 1
    "MTF_SLOPE",         # 8
    "SWEEP_RECLAIM",     # 6
    "CVD_VELOCITY",      # 3
    "TOBI",              # 4
    "STACKED_IMBALANCE", # 5
    "VOLUME_PROFILE",    # 7
]


# =====================================================================
# MARKET DATA (ticks -> CVD, candles)
# =====================================================================
class Market:
    """
    Holds ticks as numpy arrays plus everything derived from them.

    CVD here is the tick-direction PROXY: +1 when the mid price rises,
    -1 when it falls, 0 when unchanged. It is NOT the PDF's tick-rule CVD
    (that needs trade prices and volumes, which quote data does not have).
    """

    def __init__(self, ts, bid, ask):
        self.ts = np.asarray(ts, dtype=np.int64)
        self.bid = np.asarray(bid, dtype=np.float64)
        self.ask = np.asarray(ask, dtype=np.float64)
        self.n = len(self.ts)
        self.mid = (self.bid + self.ask) / 2.0
        self.spread = self.ask - self.bid

        delta = np.zeros(self.n, dtype=np.int64)
        if self.n > 1:
            delta[1:] = np.sign(np.diff(self.mid)).astype(np.int64)
        self.delta = delta
        self.cvd = np.cumsum(delta)
        self.cs_spread = np.concatenate(([0.0], np.cumsum(self.spread)))
        self._build_candles()

    def _build_candles(self):
        if self.n == 0:
            self.c_start = np.zeros(0, dtype=np.int64)
            self.c_high = np.zeros(0)
            self.c_low = np.zeros(0)
            self.c_cvd = np.zeros(0, dtype=np.int64)
            self.tick_candle = np.zeros(0, dtype=np.int64)
            self.has_prev = np.zeros(0, dtype=bool)
            return
        cid = self.ts // CANDLE_MS
        change = np.flatnonzero(np.diff(cid)) + 1
        first = np.concatenate(([0], change))
        last = np.concatenate((change - 1, [self.n - 1]))
        self.c_start = cid[first] * CANDLE_MS
        self.c_high = np.maximum.reduceat(self.mid, first)
        self.c_low = np.minimum.reduceat(self.mid, first)
        self.c_cvd = self.cvd[last]                       # CVD at the candle's last tick
        self.tick_candle = np.repeat(np.arange(len(first)), last - first + 1)
        self.has_prev = np.zeros(len(first), dtype=bool)  # previous candle exists AND is adjacent
        if len(first) > 1:
            self.has_prev[1:] = np.diff(self.c_start) == CANDLE_MS

    def at_or_before(self, t_ms):
        """Index of the last tick with timestamp <= t_ms (-1 if none)."""
        return int(np.searchsorted(self.ts, t_ms, side="right")) - 1


def load_chunk(con, start_ms, end_ms):
    """Load ticks in [start_ms, end_ms) from DuckDB as a Market."""
    data = con.execute(
        """
        SELECT timestamp_ms, bid_price, ask_price
        FROM historical_ticks
        WHERE timestamp_ms >= ? AND timestamp_ms < ?
        ORDER BY timestamp_ms
        """,
        [int(start_ms), int(end_ms)],
    ).fetchnumpy()
    return Market(data["timestamp_ms"], data["bid_price"], data["ask_price"])


def load_news(path):
    """
    Read news_events.csv (columns: datetime_utc,event). Returns a sorted list
    of event times in epoch milliseconds, or None if the file does not exist.
    Example row:  2026-11-06 13:30:00,US NFP
    """
    p = Path(path)
    if not p.is_absolute():
        p = BASE_DIR / p
    if not p.exists():
        return None
    out = []
    with open(p, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            raw = (row.get("datetime_utc") or "").strip()
            if not raw:
                continue
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            out.append(int(dt.timestamp() * 1000))
    return sorted(out)


# =====================================================================
# THE FILTERS (each returns a FilterResult: PASS / FAIL / UNAVAILABLE)
# FAIL        = checked and the condition is not met (or not enough history)
# UNAVAILABLE = cannot be computed with the data/settings we have
# =====================================================================
def _session_tz(cfg):
    if cfg.session_mode == "EST_FIXED":
        return timezone(timedelta(hours=-5))
    from zoneinfo import ZoneInfo
    return ZoneInfo("America/New_York")


def session_label(ts_ms, cfg):
    """Returns '08:00-11:30', '13:30-16:00' or None if outside allowed windows."""
    local = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).astimezone(_session_tz(cfg))
    t = local.time()
    for start, end in cfg.session_windows:
        if start <= t < end:
            return f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}"
    return None


# Filter 9
def f_session(ts_ms, cfg):
    label = session_label(ts_ms, cfg)
    local = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).astimezone(_session_tz(cfg))
    stamp = local.strftime("%H:%M")
    if label:
        return FilterResult("SESSION", PASS, f"{stamp} ({cfg.session_mode}) inside {label}")
    return FilterResult("SESSION", FAIL, f"{stamp} ({cfg.session_mode}) outside 08:00-11:30 / 13:30-16:00")


# Filter 2
def f_news(ts_ms, news_ms, cfg):
    if news_ms is None:
        return FilterResult("NEWS", UNAVAILABLE,
                            f"no calendar file ({cfg.news_file}); cannot check the blackout window")
    before = cfg.news_before_s * 1000
    after = cfg.news_after_s * 1000
    for ev in news_ms:
        if ev - before <= ts_ms <= ev + after:
            return FilterResult("NEWS", FAIL, "inside news blackout (-5 min / +10 min)")
    return FilterResult("NEWS", PASS, "outside every blackout window")


# Filter 1
def f_spread(mkt, i, cfg):
    t = int(mkt.ts[i])
    if mkt.n == 0 or int(mkt.ts[0]) > t - cfg.spread_window_ms:
        return FilterResult("SPREAD", FAIL, "less than 5 minutes of tick history")
    j = int(np.searchsorted(mkt.ts, t - cfg.spread_window_ms, side="left"))
    count = i - j
    if count < 1:
        return FilterResult("SPREAD", FAIL, "no ticks in the previous 5 minutes")
    avg = (mkt.cs_spread[i] - mkt.cs_spread[j]) / count    # previous 5 min, current tick excluded
    live = float(mkt.spread[i])
    limit = avg * cfg.spread_multiplier
    text = f"live {live:.3f} vs limit {limit:.3f} (5m avg {avg:.3f} x {cfg.spread_multiplier})"
    if live > limit:
        return FilterResult("SPREAD", FAIL, text)
    return FilterResult("SPREAD", PASS, text)


# Filter 8
def htf_slope(mkt, i, cfg):
    """Linear-regression slope of CVD over the last 4 COMPLETED 15m candles. Returns (slope, error_text)."""
    t = int(mkt.ts[i])
    last = int(np.searchsorted(mkt.c_start, t - CANDLE_MS, side="right")) - 1   # last completed candle
    k = cfg.htf_periods
    if last < k - 1:
        return None, "fewer than 4 completed 15m candles of history"
    sl = slice(last - k + 1, last + 1)
    if np.any(np.diff(mkt.c_start[sl]) != CANDLE_MS):
        return None, "gap inside the last 4 completed 15m candles"
    y = mkt.c_cvd[sl].astype(float)
    x = np.arange(k, dtype=float)
    slope = ((x - x.mean()) * (y - y.mean())).sum() / ((x - x.mean()) ** 2).sum()
    return float(slope), None


def f_mtf(mkt, i, direction, cfg):
    slope, err = htf_slope(mkt, i, cfg)
    if err:
        return FilterResult("MTF_SLOPE", FAIL, err)
    if slope > 0:
        bias = "BULLISH"
    elif slope < 0:
        bias = "BEARISH"
    else:
        return FilterResult("MTF_SLOPE", FAIL, "15m CVD slope is flat (no trades when flat)")
    text = f"15m CVD slope {slope:.2f} = {bias}"
    if direction == "SELL" and bias == "BULLISH":
        return FilterResult("MTF_SLOPE", FAIL, text + " -> SELL disabled")
    if direction == "BUY" and bias == "BEARISH":
        return FilterResult("MTF_SLOPE", FAIL, text + " -> BUY disabled")
    return FilterResult("MTF_SLOPE", PASS, text)


# Filter 6 (the trigger). Absorption needs traded volume, which we do not have.
def f_sweep(event):
    text = (f"{event['direction']} sweep of {event['level']:.3f}, broke by up to "
            f"{event['peak']:.3f}, reclaimed in {(event['t_end'] - event['t0']) / 1000:.1f}s")
    return FilterResult("SWEEP_RECLAIM", UNAVAILABLE,
                        text + "; ABSORPTION cannot be verified (needs traded volume)")


# Filter 3
def cvd_acceleration(mkt, i, cfg):
    """(CVD change over last 15s - CVD change over the 15s before) / 15. None if not enough history."""
    w = cfg.accel_window_s * 1000
    t = int(mkt.ts[i])
    if mkt.n == 0 or t - 2 * w < int(mkt.ts[0]):
        return None
    c0 = mkt.cvd[i]
    c1 = mkt.cvd[mkt.at_or_before(t - w)]
    c2 = mkt.cvd[mkt.at_or_before(t - 2 * w)]
    return float(((c0 - c1) - (c1 - c2)) / cfg.accel_window_s)


def f_velocity(accel, direction, cfg, threshold=None):
    thr = cfg.accel_threshold if threshold is None else threshold
    if accel is None:
        return FilterResult("CVD_VELOCITY", FAIL, "less than 30 seconds of tick history")
    if thr is None:
        return FilterResult("CVD_VELOCITY", UNAVAILABLE,
                            f"acceleration {accel:+.3f}; the PDFs give no threshold - set accel_threshold")
    text = f"acceleration {accel:+.3f} vs threshold {thr}"
    if direction == "BUY":
        return FilterResult("CVD_VELOCITY", PASS if accel >= thr else FAIL, text + " (BUY needs >= +thr)")
    return FilterResult("CVD_VELOCITY", PASS if accel <= -thr else FAIL, text + " (SELL needs <= -thr)")


# Filter 4 - only computes if a real order book is supplied.
def f_tobi(direction, cfg, bid_volumes=None, ask_volumes=None):
    if not bid_volumes or not ask_volumes:
        return FilterResult("TOBI", UNAVAILABLE,
                            "no order-book depth (Exness market_book_add fails; no historical depth exists)")
    b, a = sum(bid_volumes[:5]), sum(ask_volumes[:5])
    if b + a <= 0:
        return FilterResult("TOBI", FAIL, "empty book")
    ratio = b / (b + a) if direction == "BUY" else a / (b + a)
    text = f"top-5 dominance {ratio:.2f} (needs >= {cfg.tobi_min})"
    return FilterResult("TOBI", PASS if ratio >= cfg.tobi_min else FAIL, text)


# Filter 5 - only computes if a real footprint is supplied.
def f_stacked(direction, cfg, footprint=None):
    """footprint: list of (price, buy_volume, sell_volume) ordered by price."""
    if not footprint:
        return FilterResult("STACKED_IMBALANCE", UNAVAILABLE,
                            "needs traded volume per price level (not in quote data)")
    run = best = 0
    for _, buy_v, sell_v in footprint:
        if direction == "BUY":
            ok = (buy_v > 0 and sell_v == 0) or (sell_v > 0 and buy_v / sell_v >= cfg.stacked_min_ratio)
        else:
            ok = (sell_v > 0 and buy_v == 0) or (buy_v > 0 and sell_v / buy_v >= cfg.stacked_min_ratio)
        run = run + 1 if ok else 0
        best = max(best, run)
    text = f"{best} consecutive steps at >= {cfg.stacked_min_ratio}:1 (needs {cfg.stacked_min_steps})"
    return FilterResult("STACKED_IMBALANCE", PASS if best >= cfg.stacked_min_steps else FAIL, text)


# Filter 7
def f_volume_profile():
    return FilterResult("VOLUME_PROFILE", UNAVAILABLE,
                        "needs traded volume for VAH/VAL/POC; PDFs also leave value-area %, 'near' and POC width undefined")


# =====================================================================
# SWEEP + RECLAIM DETECTION (Filter 6 trigger)
# =====================================================================
def scan_sweeps(mkt, cfg, scan_from, scan_to):
    """
    Walks ticks scan_from..scan_to-1 and returns a list of events.

    Rules (all measured on the MID price, same basis as the candle levels):
      * Reference = high/low of the PREVIOUS COMPLETED 15m candle, and only
        if that candle is directly adjacent (no market gap in between).
      * BREAK: price goes beyond the level by more than
        sweep_min_break_spreads x current spread, coming from the inside.
      * RECLAIM: price returns to the level within 30 s of the break.
      * One open event per side at a time. After an event ends, a new one
        can only start once price has come back inside the level.
    """
    ts = mkt.ts.tolist()
    mid = mkt.mid.tolist()
    spr = mkt.spread.tolist()
    tc = mkt.tick_candle.tolist()
    c_hi = mkt.c_high.tolist()
    c_lo = mkt.c_low.tolist()
    has_prev = mkt.has_prev.tolist()
    window = cfg.sweep_window_s * 1000
    k = cfg.sweep_min_break_spreads

    events = []
    hi_ep = None
    lo_ep = None
    hi_armed = lo_armed = False
    cur_c = -1
    hi = lo = None

    for i in range(scan_from, scan_to):
        c = tc[i]
        if c != cur_c:                     # new 15m candle: levels change, must be "inside" again before a break
            cur_c = c
            hi_armed = lo_armed = False
            if has_prev[c]:
                hi, lo = c_hi[c - 1], c_lo[c - 1]
            else:
                hi = lo = None
        t = ts[i]
        m = mid[i]

        if hi_ep is not None:
            if t - hi_ep["t0"] > window:
                hi_ep["status"] = "failed"
                events.append(hi_ep)
                hi_ep = None
            elif m <= hi_ep["level"]:
                hi_ep["status"] = "reclaimed"
                hi_ep["end_idx"] = i
                hi_ep["t_end"] = t
                events.append(hi_ep)
                hi_ep = None
                hi_armed = True
            elif m - hi_ep["level"] > hi_ep["peak"]:
                hi_ep["peak"] = m - hi_ep["level"]

        if lo_ep is not None:
            if t - lo_ep["t0"] > window:
                lo_ep["status"] = "failed"
                events.append(lo_ep)
                lo_ep = None
            elif m >= lo_ep["level"]:
                lo_ep["status"] = "reclaimed"
                lo_ep["end_idx"] = i
                lo_ep["t_end"] = t
                events.append(lo_ep)
                lo_ep = None
                lo_armed = True
            elif lo_ep["level"] - m > lo_ep["peak"]:
                lo_ep["peak"] = lo_ep["level"] - m

        if hi is None:
            continue
        dist = k * spr[i]

        if hi_ep is None:
            if m <= hi:
                hi_armed = True
            elif hi_armed and m > hi + dist:
                hi_ep = {"direction": "HIGH", "level": hi, "break_idx": i, "t0": t,
                         "peak": m - hi, "status": "pending", "end_idx": None, "t_end": None}
                hi_armed = False
        if lo_ep is None:
            if m >= lo:
                lo_armed = True
            elif lo_armed and m < lo - dist:
                lo_ep = {"direction": "LOW", "level": lo, "break_idx": i, "t0": t,
                         "peak": lo - m, "status": "pending", "end_idx": None, "t_end": None}
                lo_armed = False

    for ep in (hi_ep, lo_ep):
        if ep is not None:
            events.append(ep)              # still pending at the end of the scan range
    events.sort(key=lambda e: e["break_idx"])
    return events


# =====================================================================
# TRADE FILLS (used by the backtest; the same logic a demo runner would follow)
# =====================================================================
def simulate_fills(mkt, i, direction, cfg):
    """
    Returns the fill picture for a signal at tick i, or None if it cannot be filled.
      entry : first tick at/after signal + latency. BUY pays the ask, SELL gets the bid.
      plain exit : hold_seconds after the fill (+ latency). BUY sells at the bid, SELL buys at the ask.
      eject exit : Filter 10 - from 4 s after the fill, if CVD over the last 2 s points against the
                   position, close at the first tick after latency.
    """
    n = mkt.n
    sig_ts = int(mkt.ts[i])
    fi = int(np.searchsorted(mkt.ts, sig_ts + cfg.latency_ms, side="left"))
    if fi >= n or int(mkt.ts[fi]) - sig_ts > cfg.max_fill_delay_ms:
        return None
    buy = direction == "BUY"
    entry_px = float(mkt.ask[fi] if buy else mkt.bid[fi])
    entry_ts = int(mkt.ts[fi])

    hold_ms = int(cfg.hold_seconds * 1000)
    exit_decision = entry_ts + hold_ms
    xi = int(np.searchsorted(mkt.ts, exit_decision + cfg.latency_ms, side="left"))
    if xi >= n or int(mkt.ts[xi]) - exit_decision > cfg.max_fill_delay_ms + cfg.latency_ms:
        return None
    plain = (int(mkt.ts[xi]), float(mkt.bid[xi] if buy else mkt.ask[xi]))

    eject = None
    lo = int(np.searchsorted(mkt.ts, entry_ts + int(cfg.early_eject_after_s * 1000), side="left"))
    hi = int(np.searchsorted(mkt.ts, exit_decision, side="left"))
    if hi > lo:
        ks = np.arange(lo, hi)
        win = int(cfg.early_eject_cvd_window_s * 1000)
        prior = np.searchsorted(mkt.ts, mkt.ts[ks] - win, side="right") - 1
        prior = np.maximum(prior, 0)
        change = mkt.cvd[ks] - mkt.cvd[prior]
        flipped = change < 0 if buy else change > 0
        if flipped.any():
            k = int(ks[int(flipped.argmax())])
            ei = int(np.searchsorted(mkt.ts, int(mkt.ts[k]) + cfg.latency_ms, side="left"))
            if ei < n and int(mkt.ts[ei]) - int(mkt.ts[k]) <= cfg.max_fill_delay_ms:
                eject = (int(mkt.ts[ei]), float(mkt.bid[ei] if buy else mkt.ask[ei]))
    return {"entry_ts": entry_ts, "entry_px": entry_px, "plain": plain, "eject": eject}


# =====================================================================
# CANDIDATES AND THE AND-GATE
# =====================================================================
@dataclass
class Candidate:
    ts: int
    direction: str
    event: dict
    results: list                      # filter results except CVD_VELOCITY (threshold is applied later)
    accel: float = None
    fill: dict = None

    def all_results(self, cfg, threshold=None):
        """Full list in PDF evaluation order, with the velocity filter slotted in."""
        out = []
        for r in self.results:
            out.append(r)
            if r.name == "SWEEP_RECLAIM":
                out.append(f_velocity(self.accel, self.direction, cfg, threshold))
        return out


def authorize(results, enabled=None, allow_unavailable=False):
    """
    The AND-gate. Returns (authorized, failed_results, skipped_results).
      * every enabled filter must be PASS
      * UNAVAILABLE counts as NOT passed, unless allow_unavailable=True
        (research mode only - the skipped filters are always reported)
    """
    failed, skipped = [], []
    for r in results:
        if enabled is not None and r.name not in enabled:
            continue
        if r.status == PASS:
            continue
        if r.status == UNAVAILABLE and allow_unavailable:
            skipped.append(r)
            continue
        failed.append(r)
    return (len(failed) == 0), failed, skipped


def explain(cand, cfg, allow_unavailable=False, threshold=None):
    """Plain-text decision with a reason for every filter."""
    results = cand.all_results(cfg, threshold)
    ok, failed, skipped = authorize(results, None, allow_unavailable)
    when = datetime.fromtimestamp(cand.ts / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [f"{when}  {cand.direction}  -> {'AUTHORIZED' if ok else 'REJECTED'}"]
    for r in results:
        lines.append(f"    {r.status:<11} {r.name:<18} {r.reason}")
    return "\n".join(lines)


def evaluate_candidate(mkt, event, cfg, news_ms):
    """Run every filter for one sweep/reclaim event. Direction: HIGH swept -> SELL, LOW swept -> BUY."""
    i = event["end_idx"]
    direction = "SELL" if event["direction"] == "HIGH" else "BUY"
    t = int(mkt.ts[i])
    results = [
        f_session(t, cfg),
        f_news(t, news_ms, cfg),
        f_spread(mkt, i, cfg),
        f_mtf(mkt, i, direction, cfg),
        f_sweep(event),
        f_tobi(direction, cfg),
        f_stacked(direction, cfg),
        f_volume_profile(),
    ]
    return Candidate(ts=t, direction=direction, event=event, results=results,
                     accel=cvd_acceleration(mkt, i, cfg),
                     fill=simulate_fills(mkt, i, direction, cfg))


def process_chunk(mkt, cfg, news_ms, scan_from_ms, scan_to_ms):
    """Scan one chunk of ticks. Returns (candidates, sweep_events_in_range)."""
    scan_from = int(np.searchsorted(mkt.ts, scan_from_ms, side="left"))
    scan_to = int(np.searchsorted(mkt.ts, scan_to_ms, side="left"))
    events = scan_sweeps(mkt, cfg, scan_from, scan_to)
    candidates = [evaluate_candidate(mkt, e, cfg, news_ms) for e in events if e["status"] == "reclaimed"]
    return candidates, events


# =====================================================================
# RISK MANAGER (Blueprint section 7)
# =====================================================================
class RiskManager:
    """
    * Stops for the day when realized loss reaches max_daily_loss_pct of the day's starting balance.
    * Caps trades per day at max_trades_per_day.
    * One position at a time.
    Days are UTC days. Only realized P&L counts, so the final losing trade can take the
    day slightly past 2%.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.balance = cfg.start_balance
        self.day = None
        self.day_start_balance = cfg.start_balance
        self.day_pnl = 0.0
        self.day_trades = 0
        self.busy_until = 0

    def _roll(self, ts_ms):
        day = ts_ms // DAY_MS
        if day != self.day:
            self.day = day
            self.day_start_balance = self.balance
            self.day_pnl = 0.0
            self.day_trades = 0

    def can_trade(self, ts_ms):
        self._roll(ts_ms)
        if ts_ms < self.busy_until:
            return False, "position already open"
        if self.day_pnl <= -self.cfg.max_daily_loss_pct / 100.0 * self.day_start_balance:
            return False, "daily loss limit reached"
        if self.day_trades >= self.cfg.max_trades_per_day:
            return False, "daily trade ceiling reached"
        return True, ""

    def opened(self, ts_ms, exit_ts_ms):
        self._roll(ts_ms)
        self.day_trades += 1
        self.busy_until = exit_ts_ms

    def closed(self, pnl_usd):
        self.balance += pnl_usd
        self.day_pnl += pnl_usd


# =====================================================================
# STATUS REPORT:  python main.py
# =====================================================================
def main():
    cfg = Config()
    print("XAUUSD ORDER FLOW BOT - STRATEGY ENGINE STATUS")
    print("=" * 60)
    print("NO TRADES ARE PLACED. This is a status report only.\n")

    if not DB_PATH.exists():
        print("ERROR: database not found:", DB_PATH)
        return 1

    con = duckdb.connect(str(DB_PATH), read_only=True)
    n, t0, t1 = con.execute(
        "SELECT COUNT(*), MIN(timestamp_ms), MAX(timestamp_ms) FROM historical_ticks").fetchone()
    if not n:
        print("Database has no ticks.")
        return 1
    fmt = lambda ms: datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    days = (t1 - t0) / DAY_MS
    print(f"TICKS: {n:,}   FROM {fmt(t0)}   TO {fmt(t1)}   (~{days:.1f} days)")
    print("BLUEPRINT NEEDS 2-3 YEARS. Current data is far too small to trust any statistic.\n")

    news_ms = load_news(cfg.news_file)
    print("FILTER AVAILABILITY")
    rows = [
        ("1  Spread guard (x1.20, 5m avg)", "READY"),
        ("2  News blackout (-5/+10 min)", "READY" if news_ms is not None else "UNAVAILABLE - supply news_events.csv"),
        ("3  CVD velocity", "UNAVAILABLE - no threshold set (PDFs give none)" if cfg.accel_threshold is None else "READY (proxy CVD)"),
        ("4  TOBI top-5 >= 0.70", "UNAVAILABLE - no order-book depth"),
        ("5  Stacked imbalance 3x 3:1", "UNAVAILABLE - no traded volume per price"),
        ("6  Sweep + reclaim", "TRIGGER READY - absorption UNAVAILABLE (no traded volume)"),
        ("7  Volume profile VAH/VAL/POC", "UNAVAILABLE - no traded volume"),
        ("8  15m CVD slope alignment", "READY (proxy CVD)"),
        ("9  Session filter", f"READY ({cfg.session_mode})"),
        ("10 Early eject (exit rule)", "READY in backtest (proxy CVD); not connected to MT5"),
    ]
    for name, state in rows:
        print(f"  {name:<34} {state}")
    print("\nAND-GATE: because filters 4, 5, 6(absorption) and 7 are UNAVAILABLE, the strict gate")
    print("authorizes ZERO trades. That is correct behaviour, not a bug.\n")

    last_day = (t1 // DAY_MS) * DAY_MS
    mkt = load_chunk(con, last_day - 75 * 60 * 1000, last_day + DAY_MS + 5 * 60 * 1000)
    con.close()
    cands, events = process_chunk(mkt, cfg, news_ms, last_day, last_day + DAY_MS)
    done = [e for e in events if e["status"] != "pending"]
    reclaimed = [e for e in done if e["status"] == "reclaimed"]
    print(f"LAST DAY IN DATABASE ({fmt(last_day)[:10]}): {len(events)} sweep events, "
          f"{len(reclaimed)} reclaimed within {cfg.sweep_window_s}s")
    strict_ok = sum(1 for c in cands if authorize(c.all_results(cfg))[0])
    print(f"CANDIDATE SIGNALS: {len(cands)}   AUTHORIZED UNDER STRICT AND-GATE: {strict_ok}\n")
    for c in cands[-3:]:
        print(explain(c, cfg))
        print()
    print("Next: python backtest.py --help")
    return 0


if __name__ == "__main__":
    sys.exit(main())
