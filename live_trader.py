#!/usr/bin/env python3
"""
MEXC 1-Second Swing Pattern Live Trader

Polls MEXC ticker every second, builds 1s OHLCV candles in real-time,
detects swing pivots, fires 4-pivot patterns, manages TP/SL positions.

Usage:
    python3 live_trader.py BTCUSDT
    python3 live_trader.py ETHUSDT --risk 0.05 --threshold 10.0
"""

import requests
import time
import sys
import json
import signal
import os
from datetime import datetime, timezone
from collections import deque

# ─── Configuration ───────────────────────────────────────────────
MEXC_BASE = "https://api.mexc.com"
SYMBOL = "BTCUSDT"
PIP = 0.01        # minimum price tick
SPREAD = 0.0      # zero-slip assumption
ATR_PERIOD = 14   # ATR EMA period
SWING_WINDOW = 10 # ± bars for pivot detection
ATR_THRESH = 8.0  # min dist/ATR to enter
RISK = 0.10       # fraction of balance risked per trade
START_BAL = 100.0

# ─── State ───────────────────────────────────────────────────────
running = True
balance = START_BAL
active_trade = None       # current open trade dict or None
pending_signals = []      # signals waiting for price fill
completed_candles = deque()  # deque of dicts {t, o, h, l, c, v, n}
current_candle = None     # in-progress 1s candle
prev_price = None
all_pivots = []           # confirmed pivots [{idx, val, type}]
all_trades_log = []       # completed trades
last_pivot_idx = 0        # candle index of last processed pivot
atr_values = deque(maxlen=ATR_PERIOD)  # rolling TR for ATR
current_atr = None
candle_counter = 0        # total completed candle count
signal_count = 0
day_start_bal = START_BAL

# ─── Helpers ─────────────────────────────────────────────────────

def now_ts():
    return int(datetime.now(timezone.utc).timestamp() * 1000)

def fmt_price(p):
    if abs(p) >= 1000: return f"{p:.2f}"
    if abs(p) >= 1: return f"{p:.4f}"
    return f"{p:.8f}"

def fmt_dt(ms):
    return datetime.fromtimestamp(ms/1000, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]

def fetch_price(sym):
    try:
        r = requests.get(f"{MEXC_BASE}/api/v3/ticker/price",
                        params={"symbol": sym}, timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return None

# ─── ATR ─────────────────────────────────────────────────────────

def compute_tr(prev_c, c):
    """True Range for a completed candle."""
    hl = c["h"] - c["l"]
    hpc = abs(c["h"] - prev_c)
    lpc = abs(c["l"] - prev_c)
    return max(hl, hpc, lpc)

def update_atr(tr):
    global current_atr, atr_values
    atr_values.append(tr)
    if len(atr_values) == ATR_PERIOD:
        current_atr = sum(atr_values) / ATR_PERIOD
    elif len(atr_values) > ATR_PERIOD:
        alpha = 2.0 / (ATR_PERIOD + 1)
        current_atr = alpha * tr + (1 - alpha) * current_atr

# ─── Candle Builder ──────────────────────────────────────────────

def tick_price(price, ts):
    """Process a new price tick — update current 1s candle."""
    global current_candle, prev_price, completed_candles, candle_counter

    sec = (ts // 1000) * 1000

    if current_candle is None:
        # Start first candle
        current_candle = {"t": sec, "o": price, "h": price, "l": price,
                          "c": price, "v": 0.0, "n": 1}
        prev_price = price
        return

    if sec > current_candle["t"]:
        # Finalize previous candle
        final_candle = dict(current_candle)
        completed_candles.append(final_candle)
        candle_counter += 1
        tr = compute_tr(prev_price, final_candle)
        update_atr(tr)
        prev_price = final_candle["c"]

        # Start new candle
        current_candle = {"t": sec, "o": price, "h": price, "l": price,
                          "c": price, "v": 0.0, "n": 1}
    else:
        # Update current candle
        c = current_candle
        if price > c["h"]: c["h"] = price
        if price < c["l"]: c["l"] = price
        c["c"] = price
        c["v"] += 1  # tick count as volume proxy
        c["n"] += 1

# ─── Swing Pivot Detection ──────────────────────────────────────

def detect_swings():
    """Scan completed candles for new swing pivots."""
    global all_pivots, last_pivot_idx
    w = SWING_WINDOW
    n = len(completed_candles)
    new_pivots = []

    # Only scan candles where we have enough context (w bars before + after)
    # and that we haven't checked yet
    start = max(last_pivot_idx, w)
    end = n - w

    for i in range(start, end):
        highs = [completed_candles[j]["h"] for j in range(i-w, i+w+1)]
        lows = [completed_candles[j]["l"] for j in range(i-w, i+w+1)]
        ci = completed_candles[i]

        is_high = ci["h"] == max(highs)
        is_low = ci["l"] == min(lows)

        if is_high or is_low:
            typ = "high" if is_high else "low"
            val = ci["h"] if is_high else ci["l"]
            new_pivots.append({"idx": i, "val": val, "type": typ,
                               "time": ci["t"]})

    if new_pivots:
        # Merge into existing alternating pivots
        all_pivots.extend(new_pivots)
        # Clean to alternating sequence only
        cleaned = []
        if all_pivots:
            cleaned.append(all_pivots[0])
            for p in all_pivots[1:]:
                lp = cleaned[-1]
                if p["type"] == lp["type"]:
                    if p["type"] == "high" and p["val"] > lp["val"]:
                        cleaned[-1] = p
                    elif p["type"] == "low" and p["val"] < lp["val"]:
                        cleaned[-1] = p
                else:
                    cleaned.append(p)
        all_pivots.clear()
        all_pivots.extend(cleaned)
        last_pivot_idx = end

    return len(new_pivots)

# ─── Pattern Matcher ─────────────────────────────────────────────

def check_patterns():
    """Look for 4-pivot patterns in the pivot list."""
    global pending_signals, signal_count, current_atr
    n = len(all_pivots)
    signals = []

    for i in range(n - 4):
        p1, p2, p3, p4 = all_pivots[i], all_pivots[i+1], all_pivots[i+2], all_pivots[i+3]

        # SELL: H-L-H-L, p3 higher high, p4 lower low
        if (p1["type"] == "high" and p2["type"] == "low" and
            p3["type"] == "high" and p4["type"] == "low" and
            p3["val"] > p1["val"] and p4["val"] < p2["val"]):
            dist = p2["val"] - p4["val"] + PIP
            if dist > 0:
                signals.append({
                    "entry": p2["val"],
                    "sl": p2["val"] + dist,
                    "tp": p2["val"] - dist,
                    "dist": dist,
                    "dir": "sell",
                    "detect_idx": p4["idx"],
                    "detect_time": p4["time"],
                    "p1": p1, "p2": p2, "p3": p3, "p4": p4,
                })

        # BUY: L-H-L-H, p3 lower low, p4 higher high
        elif (p1["type"] == "low" and p2["type"] == "high" and
              p3["type"] == "low" and p4["type"] == "high" and
              p3["val"] < p1["val"] and p4["val"] > p2["val"]):
            dist = p4["val"] - p2["val"] + PIP
            if dist > 0:
                signals.append({
                    "entry": p2["val"],
                    "sl": p2["val"] - dist,
                    "tp": p2["val"] + dist,
                    "dist": dist,
                    "dir": "buy",
                    "detect_idx": p4["idx"],
                    "detect_time": p4["time"],
                    "p1": p1, "p2": p2, "p3": p3, "p4": p4,
                })

    for sig in signals:
        if current_atr and current_atr > 0:
            atr_pips = current_atr / PIP
            dist_pips = sig["dist"] / PIP
            if dist_pips / atr_pips >= ATR_THRESH:
                sig["atr_pips"] = atr_pips
                sig["dist_pips"] = dist_pips
                sig["dist_atr"] = dist_pips / atr_pips
                # Entry will trigger when price reaches entry level
                pending_signals.append(sig)
                signal_count += 1
                log_signal(sig)

# ─── Position Manager ────────────────────────────────────────────

def check_fill_and_exit(price):
    """Check if pending signals fill or if active trade hits TP/SL."""
    global active_trade, pending_signals, balance

    # Check active trade first
    if active_trade:
        d = active_trade["dir"]
        if d == "sell":
            if price <= active_trade["tp"]:
                close_trade("win", active_trade["tp"])
                return
            if price >= active_trade["sl"]:
                close_trade("loss", active_trade["sl"])
                return
        else:  # buy
            if price >= active_trade["tp"]:
                close_trade("win", active_trade["tp"])
                return
            if price <= active_trade["sl"]:
                close_trade("loss", active_trade["sl"])
                return

    # Check pending signals for fill (or blow-out)
    if not active_trade and pending_signals:
        still_pending = []
        for sig in pending_signals:
            d = sig["dir"]
            filled = (d == "sell" and price <= sig["entry"]) or \
                     (d == "buy" and price >= sig["entry"])
            blown = (d == "sell" and price >= sig["sl"]) or \
                    (d == "buy" and price <= sig["sl"])

            if blown:
                continue  # signal expired
            if filled:
                open_trade(sig, price)
                # Check if TP hit immediately
                if active_trade:
                    if d == "sell" and price <= sig["tp"]:
                        close_trade("win", sig["tp"])
                    elif d == "buy" and price >= sig["tp"]:
                        close_trade("win", sig["tp"])
                return
            still_pending.append(sig)
        pending_signals = still_pending

    # Purge signals that got blown during the bar
    pending_signals = [s for s in pending_signals if
                       not ((s["dir"] == "sell" and price >= s["sl"]) or
                            (s["dir"] == "buy" and price <= s["sl"]))]

def open_trade(sig, fill_price):
    global active_trade
    active_trade = {
        "dir": sig["dir"],
        "entry": fill_price,
        "tp": sig["tp"],
        "sl": sig["sl"],
        "dist": sig["dist"],
        "dist_pips": sig["dist_pips"],
        "atr_pips": sig["atr_pips"],
        "dist_atr": sig["dist_atr"],
        "open_time": now_ts(),
    }
    log_entry(sig, fill_price)

def close_trade(result, exit_price):
    global active_trade, balance
    t = active_trade
    if result == "win":
        nr = (t["dist_pips"] - SPREAD) / t["dist_pips"]
    else:
        nr = -(t["dist_pips"] + SPREAD) / t["dist_pips"]

    pnl = balance * RISK * nr
    balance += pnl

    trade_record = {
        "dir": t["dir"],
        "entry": t["entry"],
        "exit": exit_price,
        "result": result,
        "dist_pips": t["dist_pips"],
        "atr_pips": t["atr_pips"],
        "dist_atr": t["dist_atr"],
        "pnl": pnl,
        "pnl_pct": nr * RISK * 100,
        "balance": balance,
        "open_time": t["open_time"],
        "close_time": now_ts(),
        "duration_s": (now_ts() - t["open_time"]) / 1000,
    }
    all_trades_log.append(trade_record)
    log_exit(trade_record)
    active_trade = None

# ─── Logging ─────────────────────────────────────────────────────

def log_signal(sig):
    d = sig["dir"]
    ts = fmt_dt(sig["detect_time"])
    print(f"  ⚡ {ts} SIGNAL {d:>5s}  entry={fmt_price(sig['entry']):>10s} "
          f"TP={fmt_price(sig['tp']):>10s} SL={fmt_price(sig['sl']):>10s}  "
          f"dist={sig['dist_pips']:.0f}p ATR={sig['atr_pips']:.1f}p "
          f"D/ATR={sig['dist_atr']:.1f}x")

def log_entry(sig, price):
    ts = fmt_dt(now_ts())
    print(f"  🚀 {ts} ENTRY {sig['dir']:>5s} @ {fmt_price(price)} "
          f"TP={fmt_price(sig['tp'])} SL={fmt_price(sig['sl'])} "
          f"risk={RISK*100:.0f}%")

def log_exit(t):
    ts = fmt_dt(t["close_time"])
    icon = "✅" if t["result"] == "win" else "❌"
    print(f"  {icon} {ts} EXIT  {t['dir']:>5s}  entry={fmt_price(t['entry']):>10s} "
          f"exit={fmt_price(t['exit']):>10s}  "
          f"P&L={t['pnl']:+.2f} ({t['pnl_pct']:+.2f}%)  Bal=${t['balance']:.2f}  "
          f"held={t['duration_s']:.0f}s")

def print_status():
    """Print current status line."""
    if not active_trade:
        line = f"  [{fmt_dt(now_ts())}] Bal=${balance:.2f}  Candles={candle_counter}  "
        line += f"Pivots={len(all_pivots)}  Signals={signal_count}  Trades={len(all_trades_log)}  "
        line += f"ATR={current_atr/PIP:.1f}p" if current_atr else ""
        print(line)

# ─── Main Loop ───────────────────────────────────────────────────

def handle_sigint(sig, frame):
    global running
    print("\n\n  ⏹️  Shutting down...")
    running = False

def main():
    global running, SYMBOL, PIP, ATR_THRESH, RISK, SWING_WINDOW, ATR_PERIOD, START_BAL, balance

    # Parse args
    if len(sys.argv) > 1:
        SYMBOL = sys.argv[1].upper()

    i = 2
    while i < len(sys.argv):
        a = sys.argv[i]
        if a == "--risk" and i+1 < len(sys.argv):
            RISK = float(sys.argv[i+1]); i += 2
        elif a == "--threshold" and i+1 < len(sys.argv):
            ATR_THRESH = float(sys.argv[i+1]); i += 2
        elif a == "--window" and i+1 < len(sys.argv):
            SWING_WINDOW = int(sys.argv[i+1]); i += 2
        elif a == "--atr-period" and i+1 < len(sys.argv):
            ATR_PERIOD = int(sys.argv[i+1]); i += 2
        elif a == "--start" and i+1 < len(sys.argv):
            START_BAL = float(sys.argv[i+1]); balance = START_BAL; i += 2
        else:
            print(f"Unknown: {a}"); sys.exit(1)

    signal.signal(signal.SIGINT, handle_sigint)

    # Set PIP from symbol
    if "PEPE" in SYMBOL: PIP = 0.00000001
    elif "EUR" in SYMBOL: PIP = 0.0001
    elif "DOGE" in SYMBOL: PIP = 0.00001
    else: PIP = 0.01

    print("=" * 72)
    print(f"  MEXC 1s Swing Trader — {SYMBOL}")
    print(f"  Start: ${START_BAL:.2f}  Risk: {RISK*100:.0f}%  "
          f"Threshold: {ATR_THRESH}x  Window: {SWING_WINDOW}s")
    print("=" * 72)
    print(f"  {'Time':<15} {'Price':<12} {'Status':<40}")
    print("-" * 72)

    poll_count = 0
    last_status_ts = 0

    while running:
        price = fetch_price(SYMBOL)
        ts = now_ts()

        if price is not None:
            tick_price(price, ts)

            # Detect swing pivots on new completed candles
            new_pivots = detect_swings()

            # Check for patterns if we have new pivots
            if new_pivots > 0:
                check_patterns()

            # Check fills and exits
            check_fill_and_exit(price)

        # Status update every 10 seconds
        if ts - last_status_ts > 10000:
            print_status()
            last_status_ts = ts

        poll_count += 1
        time.sleep(1)

    # Print final summary
    print(f"\n{'='*72}")
    print(f"  SESSION SUMMARY — {SYMBOL}")
    print(f"{'='*72}")
    print(f"  Duration:      {poll_count}s ({poll_count/3600:.1f}h)")
    print(f"  Candles:       {candle_counter}")
    print(f"  Signals:       {signal_count}")
    print(f"  Trades:        {len(all_trades_log)}")
    wins = len([t for t in all_trades_log if t["result"] == "win"])
    if all_trades_log:
        print(f"  Win rate:      {wins/len(all_trades_log)*100:.1f}%")
    print(f"  Start:         ${START_BAL:.2f}")
    print(f"  End:           ${balance:.2f}")
    print(f"  Return:        {(balance/START_BAL-1)*100:+.2f}%")

    if all_trades_log:
        buys = [t for t in all_trades_log if t["dir"] == "buy"]
        sells = [t for t in all_trades_log if t["dir"] == "sell"]
        if buys: print(f"  Longs:         {len(buys)} WR: {sum(1 for t in buys if t['result']=='win')/len(buys)*100:.1f}%")
        if sells: print(f"  Shorts:        {len(sells)} WR: {sum(1 for t in sells if t['result']=='win')/len(sells)*100:.1f}%")

        # Save trade log
        logfile = f"trades_{SYMBOL}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(logfile, "w") as f:
            json.dump(all_trades_log, f, indent=2, default=str)
        print(f"  Trade log:     {logfile}")

    print()

if __name__ == "__main__":
    main()
