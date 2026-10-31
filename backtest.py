"""
XAUUSD ORDER FLOW BOT - BACKTEST
=================================
Replays the ticks in xauusd_ticks.duckdb through the engine in main.py.

Costs are included: you pay the real bid/ask spread on every trade, there is a
fill delay (latency), extra slippage and optional commission.

IMPORTANT HONESTY NOTES
  * Strict mode (the default) applies the PDF's AND-gate exactly. Because TOBI,
    stacked imbalance, absorption and the volume profile cannot be computed from
    quote data, strict mode authorizes ZERO trades. That is correct.
  * --allow-unavailable runs a RESEARCH version with the unavailable filters
    skipped. It is NOT the PDF strategy and the report says so.
  * CVD is a tick-direction proxy, not the PDF's tick-rule CVD.
  * Calibrating a threshold on the same data you test on is in-sample.
    Treat such results as a guide, never as proof.

Examples
  python backtest.py
  python backtest.py --allow-unavailable
  python backtest.py --allow-unavailable --ablation
  python backtest.py --allow-unavailable --hold 30 --accel-percentile 75
"""

import argparse
import sys
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timezone

import duckdb
import numpy as np

import main as engine
from main import (Config, DAY_MS, DB_PATH, ENTRY_FILTERS, PASS, FAIL, UNAVAILABLE,
                  RiskManager, authorize, load_chunk, load_news, process_chunk, session_label)

WARMUP_MS = 75 * 60 * 1000      # history loaded before each day (4 completed candles + slack)
LOOKAHEAD_MS = 5 * 60 * 1000    # ticks loaded after each day so late signals can still exit


# ---------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="Backtest the XAUUSD order flow strategy.")
    p.add_argument("--db", default=str(DB_PATH))
    p.add_argument("--from", dest="date_from", help="first UTC day, YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", help="last UTC day, YYYY-MM-DD")
    p.add_argument("--allow-unavailable", action="store_true",
                   help="RESEARCH MODE: skip filters that cannot be computed")
    p.add_argument("--ablation", action="store_true", help="also run with each filter removed")
    p.add_argument("--hold", type=float, help="hold time in seconds (PDF: 10-16)")
    p.add_argument("--accel-threshold", type=float, help="CVD acceleration threshold")
    p.add_argument("--accel-percentile", type=float,
                   help="set the threshold to this percentile of |acceleration| over all candidates (in-sample!)")
    p.add_argument("--min-break-spreads", type=float, help="sweep break distance in spreads (0 = off)")
    p.add_argument("--latency-ms", type=int)
    p.add_argument("--slippage", type=float, help="extra $ per ounce per side")
    p.add_argument("--commission", type=float, help="$ per lot per side")
    p.add_argument("--session-mode", choices=["NEW_YORK", "EST_FIXED"])
    return p.parse_args()


def build_config(a):
    cfg = Config()
    if a.hold is not None:
        cfg = replace(cfg, hold_seconds=a.hold)
    if a.accel_threshold is not None:
        cfg = replace(cfg, accel_threshold=a.accel_threshold)
    if a.min_break_spreads is not None:
        cfg = replace(cfg, sweep_min_break_spreads=a.min_break_spreads)
    if a.latency_ms is not None:
        cfg = replace(cfg, latency_ms=a.latency_ms)
    if a.slippage is not None:
        cfg = replace(cfg, slippage_usd_per_side=a.slippage)
    if a.commission is not None:
        cfg = replace(cfg, commission_usd_per_lot_per_side=a.commission)
    if a.session_mode:
        cfg = replace(cfg, session_mode=a.session_mode)
    return cfg


# ---------------------------------------------------------------------
def collect(con, cfg, news_ms, date_from, date_to):
    """Walk the database one UTC day at a time. Returns candidates and sweep events."""
    t0, t1 = con.execute("SELECT MIN(timestamp_ms), MAX(timestamp_ms) FROM historical_ticks").fetchone()
    if t0 is None:
        return [], [], 0
    first_day = (t0 // DAY_MS) * DAY_MS
    last_day = (t1 // DAY_MS) * DAY_MS
    if date_from:
        first_day = max(first_day, int(datetime.strptime(date_from, "%Y-%m-%d")
                                       .replace(tzinfo=timezone.utc).timestamp() * 1000))
    if date_to:
        last_day = min(last_day, int(datetime.strptime(date_to, "%Y-%m-%d")
                                     .replace(tzinfo=timezone.utc).timestamp() * 1000))
    all_c, all_e, days_done = [], [], 0
    day = first_day
    while day <= last_day:
        mkt = load_chunk(con, day - WARMUP_MS, day + DAY_MS + LOOKAHEAD_MS)
        if mkt.n:
            cands, events = process_chunk(mkt, cfg, news_ms, day, day + DAY_MS)
            all_c.extend(cands)
            all_e.extend(events)
            days_done += 1
            if days_done % 10 == 0:
                print(f"  ...{days_done} days processed", flush=True)
        day += DAY_MS
    return all_c, all_e, days_done


# ---------------------------------------------------------------------
def run_variant(cands, cfg, enabled, allow_unavailable, threshold, use_eject):
    """Apply the AND-gate and risk rules to the stored candidates and simulate the account."""
    risk = RiskManager(cfg)
    trades = []
    blocked = Counter()
    for c in sorted(cands, key=lambda x: x.ts):
        ok, _, _ = authorize(c.all_results(cfg, threshold), enabled, allow_unavailable)
        if not ok:
            continue
        if c.fill is None:
            blocked["no fill possible (data gap)"] += 1
            continue
        allowed, why = risk.can_trade(c.ts)
        if not allowed:
            blocked[why] += 1
            continue
        fill = c.fill
        exit_ts, exit_px = (fill["eject"] if (use_eject and fill["eject"]) else fill["plain"])
        reason = "early eject" if (use_eject and fill["eject"]) else "timed exit"
        move = (exit_px - fill["entry_px"]) if c.direction == "BUY" else (fill["entry_px"] - exit_px)
        costs = 2 * cfg.slippage_usd_per_side + 2 * cfg.commission_usd_per_lot_per_side / cfg.contract_oz
        pnl = (move - costs) * cfg.contract_oz * cfg.lot_size
        risk.opened(c.ts, exit_ts)
        risk.closed(pnl)
        trades.append({"ts": fill["entry_ts"], "exit_ts": exit_ts, "dir": c.direction, "pnl": pnl,
                       "move": move, "reason": reason,
                       "session": session_label(c.ts, cfg) or "outside sessions",
                       "day": c.ts // DAY_MS})
    return trades, blocked


def metrics(trades, cfg):
    n = len(trades)
    if n == 0:
        return {"n": 0}
    pnl = np.array([t["pnl"] for t in trades])
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gross_win, gross_loss = wins.sum(), -losses.sum()
    equity = cfg.start_balance + np.cumsum(pnl)
    peak = np.maximum.accumulate(np.concatenate(([cfg.start_balance], equity)))[1:]
    dd = peak - equity
    return {
        "n": n,
        "win_rate": len(wins) / n * 100,
        "pf": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "net": pnl.sum(),
        "expectancy": pnl.mean(),
        "avg_win": wins.mean() if len(wins) else 0.0,
        "avg_loss": losses.mean() if len(losses) else 0.0,
        "max_dd": dd.max(),
        "max_dd_pct": (dd / peak).max() * 100,
    }


def fmt_row(label, m):
    if m["n"] == 0:
        return f"{label:<34} {'0':>6}"
    pf = "inf" if m["pf"] == float("inf") else f"{m['pf']:.2f}"
    return (f"{label:<34} {m['n']:>6} {m['win_rate']:>6.1f}% {pf:>6} {m['net']:>9.2f} "
            f"{m['expectancy']:>8.3f} {m['avg_win']:>8.3f} {m['avg_loss']:>8.3f} {m['max_dd']:>8.2f}")


TABLE_HEAD = (f"{'VARIANT':<34} {'TRADES':>6} {'WIN%':>7} {'PF':>6} {'NET$':>9} "
              f"{'EXPECT$':>8} {'AVGWIN$':>8} {'AVGLOSS$':>8} {'MAXDD$':>8}")


# ---------------------------------------------------------------------
def main():
    a = parse_args()
    cfg = build_config(a)
    news_ms = load_news(cfg.news_file)

    print("XAUUSD ORDER FLOW BACKTEST")
    print("=" * 70)
    con = duckdb.connect(a.db, read_only=True)
    print("Scanning ticks day by day...")
    cands, events, days = collect(con, cfg, news_ms, a.date_from, a.date_to)
    con.close()

    # ---- data summary and honesty banner
    print(f"\nDAYS PROCESSED: {days}")
    if days < 250:
        print(f"WARNING: the Blueprint requires 2-3 years (~500-750 trading days). You have {days}. "
              "No statistic below is trustworthy yet.")
    print("CVD: tick-direction PROXY (not the PDF's tick-rule CVD).")

    # ---- sweep statistics
    done = [e for e in events if e["status"] != "pending"]
    rec = [e for e in done if e["status"] == "reclaimed"]
    print("\nLIQUIDITY SWEEP EVENTS (corrected logic: 15m adjacent candle, mid price, "
          f"min break {cfg.sweep_min_break_spreads} x spread)")
    print(f"  breaks detected: {len(events)}   high-side: {sum(e['direction']=='HIGH' for e in events)}"
          f"   low-side: {sum(e['direction']=='LOW' for e in events)}")
    print(f"  completed: {len(done)}   reclaimed within {cfg.sweep_window_s}s: {len(rec)}   "
          f"failed: {len(done) - len(rec)}   pending at end: {len(events) - len(done)}")
    if done:
        print(f"  reclaim rate: {len(rec) / len(done) * 100:.1f}%   (absorption NOT verified)")
    print(f"  candidate signals (reclaims): {len(cands)}")

    # ---- velocity threshold
    threshold = cfg.accel_threshold
    accels = [abs(c.accel) for c in cands if c.accel is not None]
    if accels:
        pcts = {p: float(np.percentile(accels, p)) for p in (50, 75, 90)}
        print("\nCVD ACCELERATION |value| percentiles over candidates: "
              + "  ".join(f"p{p}={v:.3f}" for p, v in pcts.items()))
    if a.accel_percentile is not None and accels:
        threshold = float(np.percentile(accels, a.accel_percentile))
        print(f"ACCEL THRESHOLD SET TO p{a.accel_percentile:g} = {threshold:.3f}  (IN-SAMPLE calibration)")
    if threshold is None:
        print("ACCEL THRESHOLD: not set (PDFs give none) -> CVD_VELOCITY is UNAVAILABLE")

    # ---- why candidates are rejected (all filters, strict view)
    fail_count, unavail_count = Counter(), Counter()
    for c in cands:
        for r in c.all_results(cfg, threshold):
            if r.status == FAIL:
                fail_count[r.name] += 1
            elif r.status == UNAVAILABLE:
                unavail_count[r.name] += 1
    print("\nFILTER OUTCOMES ACROSS ALL CANDIDATES")
    for name in ENTRY_FILTERS:
        total = len(cands)
        f, u = fail_count[name], unavail_count[name]
        print(f"  {name:<18} failed {f:>5}   unavailable {u:>5}   passed {total - f - u:>5}")

    # ---- strict run
    enabled = set(ENTRY_FILTERS)
    print("\n" + "=" * 70)
    if not a.allow_unavailable:
        trades, blocked = run_variant(cands, cfg, enabled, False, threshold, True)
        print("STRICT AND-GATE (the PDF rule): every filter must PASS")
        print(f"TRADES AUTHORIZED: {len(trades)}")
        if not trades:
            never = sorted({n for n in ENTRY_FILTERS if unavail_count[n] == len(cands) and cands})
            print("No trade can be authorized. Filters that are UNAVAILABLE for every candidate:")
            print("  " + ", ".join(never))
            print("Rerun with --allow-unavailable for a RESEARCH backtest (not the PDF strategy).")
        return 0

    skipped = sorted({n for n in ENTRY_FILTERS if cands and unavail_count[n] == len(cands)})
    print("*** RESEARCH MODE - NOT THE PDF STRATEGY ***")
    print("Filters SKIPPED because they cannot be computed: " + (", ".join(skipped) or "none"))
    print("=" * 70)

    trades, blocked = run_variant(cands, cfg, enabled, True, threshold, True)
    m = metrics(trades, cfg)

    print("\nCOST ASSUMPTIONS (edit in main.py -> Config, or use flags)")
    print(f"  spread: real Dukascopy bid/ask   latency: {cfg.latency_ms} ms   slippage: "
          f"${cfg.slippage_usd_per_side}/oz/side   commission: ${cfg.commission_usd_per_lot_per_side}/lot/side")
    print(f"  lot {cfg.lot_size} x {cfg.contract_oz:g} oz   start balance ${cfg.start_balance:,.0f}   "
          f"hold {cfg.hold_seconds:g}s   early eject after {cfg.early_eject_after_s:g}s")
    print("  NOTE: Dukascopy spreads are not Exness spreads. Real costs will differ.")

    print("\nRESULT")
    print(TABLE_HEAD)
    print(fmt_row("RESEARCH BASELINE", m))
    if m["n"]:
        print(f"\n  max drawdown: ${m['max_dd']:.2f} ({m['max_dd_pct']:.1f}% of peak equity)")
        print(f"  trades per day: {m['n'] / max(days, 1):.1f}   (Blueprint target 5-12)")
        if blocked:
            print("  signals blocked by risk rules: " + ", ".join(f"{k}: {v}" for k, v in blocked.items()))
        by_day = Counter(t["day"] for t in trades)
        meeting = sum(1 for v in by_day.values() if v >= cfg.target_min_trades_per_day)
        print(f"  days with at least {cfg.target_min_trades_per_day} trades: {meeting} of {days} "
              f"(the 5-trade minimum cannot be forced; only the ceiling is enforced)")

        print("\nBY SESSION (New York time windows)" if cfg.session_mode == "NEW_YORK"
              else "\nBY SESSION (fixed EST windows)")
        groups = defaultdict(list)
        for t in trades:
            groups[t["session"]].append(t)
        print(TABLE_HEAD)
        for k in sorted(groups):
            print(fmt_row("session " + k, metrics(groups[k], cfg)))

        print("\nBY EXIT TYPE")
        groups = defaultdict(list)
        for t in trades:
            groups[t["reason"]].append(t)
        print(TABLE_HEAD)
        for k in sorted(groups):
            print(fmt_row(k, metrics(groups[k], cfg)))

        print("\nBY DIRECTION")
        groups = defaultdict(list)
        for t in trades:
            groups[t["dir"]].append(t)
        print(TABLE_HEAD)
        for k in sorted(groups):
            print(fmt_row(k, metrics(groups[k], cfg)))

    # ---- with / without each filter
    if a.ablation:
        print("\nWITH / WITHOUT EACH FILTER  (research mode, same costs)")
        print(TABLE_HEAD)
        print(fmt_row("baseline (all computable filters)", m))
        computable = [n for n in ENTRY_FILTERS if not (cands and unavail_count[n] == len(cands))
                      and n != "SWEEP_RECLAIM"]
        for name in computable:
            t2, _ = run_variant(cands, cfg, enabled - {name}, True, threshold, True)
            print(fmt_row(f"without {name}", metrics(t2, cfg)))
        t2, _ = run_variant(cands, cfg, enabled, True, threshold, False)
        print(fmt_row("without EARLY_EJECT (filter 10)", metrics(t2, cfg)))
        t2, _ = run_variant(cands, cfg, set(), True, threshold, False)
        print(fmt_row("sweep trigger only, no filters", metrics(t2, cfg)))

    print("\nREMINDERS")
    print("  * This is a historical simulation. It proves nothing about live results.")
    print("  * The win rate is whatever the data shows. The PDFs' 68-78% is a claim, not a result.")
    print("  * Strict PDF mode needs order-book depth / traded volume that are not available yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
