#!/usr/bin/env python3
"""
MEXC 1s Multi-Asset Swing Pattern Live Trader

Monitors multiple symbols on a single shared balance. Each symbol gets its
own candle stream, pivot detection, and position slot. When any trade closes,
the global balance updates — all symbols trade from the same account.

Usage:
    python3 live_trader.py BTCUSDT ETHUSDT
    python3 live_trader.py BTCUSDT ETHUSDT SOLUSDT --risk 0.05 --threshold 10.0
"""

import requests
import time
import sys
import signal
import math
import os
import threading
import http.server
from datetime import datetime, timezone
from collections import deque

MEXC_BASE = "https://api.mexc.com"
SPREAD = 0.0

ATR_PERIOD = 14
SWING_WINDOW = 10
ATR_THRESH = 8.0
MIN_DIFF = 0.0
RISK = 0.10
START_BAL = 100.0

running = True
balance = START_BAL
all_trades_log = []


# ─── Helpers ─────────────────────────────────────────────────────

def now_ts():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def fmt_dt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]


def fmt_price(p):
    if abs(p) >= 1000:
        return f"{p:.2f}"
    if abs(p) >= 1:
        return f"{p:.4f}"
    return f"{p:.8f}"


def fetch_pip(sym):
    try:
        r = requests.get(f"{MEXC_BASE}/api/v3/exchangeInfo", timeout=10)
        for s in r.json()["symbols"]:
            if s["symbol"] == sym:
                return 10 ** -s["quotePrecision"]
    except Exception:
        pass
    return 0.01


def fetch_price(sym):
    try:
        r = requests.get(f"{MEXC_BASE}/api/v3/ticker/price",
                         params={"symbol": sym}, timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return None


# ─── Symbol State ────────────────────────────────────────────────

class SymbolState:
    """Holds all live state for a single symbol."""

    def __init__(self, symbol, min_diff=0.0):
        self.symbol = symbol
        self.pip = fetch_pip(symbol)
        self.min_diff = min_diff

        # Candle builder
        self.current_candle = None
        self.completed_candles = deque()
        self.candle_counter = 0
        self.prev_price = None

        # ATR
        self.atr_values = deque(maxlen=ATR_PERIOD)
        self.current_atr = None

        # Pivots / signals / position
        self.all_pivots = []
        self.last_pivot_idx = 0
        self.pending_signals = []
        self.signal_count = 0
        self.active_trade = None

    # ── Candle builder ──

    def tick_price(self, price, ts):
        sec = (ts // 1000) * 1000

        if self.current_candle is None:
            self.current_candle = {
                "t": sec, "o": price, "h": price, "l": price,
                "c": price, "v": 0.0, "n": 1
            }
            self.prev_price = price
            return

        if sec > self.current_candle["t"]:
            final = dict(self.current_candle)
            self.completed_candles.append(final)
            self.candle_counter += 1
            tr = max(final["h"] - final["l"],
                     abs(final["h"] - self.prev_price),
                     abs(final["l"] - self.prev_price))
            self._update_atr(tr)
            self.prev_price = final["c"]

            self.current_candle = {
                "t": sec, "o": price, "h": price, "l": price,
                "c": price, "v": 0.0, "n": 1
            }
        else:
            c = self.current_candle
            if price > c["h"]:
                c["h"] = price
            if price < c["l"]:
                c["l"] = price
            c["c"] = price
            c["v"] += 1
            c["n"] += 1

    def _update_atr(self, tr):
        self.atr_values.append(tr)
        if len(self.atr_values) == ATR_PERIOD:
            self.current_atr = sum(self.atr_values) / ATR_PERIOD
        elif len(self.atr_values) > ATR_PERIOD:
            alpha = 2.0 / (ATR_PERIOD + 1)
            self.current_atr = alpha * tr + (1 - alpha) * self.current_atr

    # ── Swing pivots ──

    def detect_swings(self):
        w = SWING_WINDOW
        n = len(self.completed_candles)
        new = []
        start = max(self.last_pivot_idx, w)
        end = n - w

        for i in range(start, end):
            hs = [self.completed_candles[j]["h"] for j in range(i - w, i + w + 1)]
            ls = [self.completed_candles[j]["l"] for j in range(i - w, i + w + 1)]
            ci = self.completed_candles[i]

            if ci["h"] == max(hs):
                new.append({"idx": i, "val": ci["h"], "type": "high", "time": ci["t"]})
            if ci["l"] == min(ls):
                new.append({"idx": i, "val": ci["l"], "type": "low", "time": ci["t"]})

        if new:
            self.all_pivots.extend(new)
            cleaned = []
            if self.all_pivots:
                cleaned.append(self.all_pivots[0])
                for p in self.all_pivots[1:]:
                    lp = cleaned[-1]
                    if p["type"] == lp["type"]:
                        if p["type"] == "high" and p["val"] > lp["val"]:
                            cleaned[-1] = p
                        elif p["type"] == "low" and p["val"] < lp["val"]:
                            cleaned[-1] = p
                    else:
                        cleaned.append(p)
            self.all_pivots.clear()
            self.all_pivots.extend(cleaned)
            self.last_pivot_idx = end

        return len(new)

    # ── Pattern matcher ──

    def check_patterns(self):
        n = len(self.all_pivots)
        for i in range(n - 4):
            p1, p2, p3, p4 = self.all_pivots[i:i + 4]

            if (p1["type"] == "high" and p2["type"] == "low" and
                    p3["type"] == "high" and p4["type"] == "low" and
                    p3["val"] > p1["val"] and p4["val"] < p2["val"]):
                dist = p2["val"] - p4["val"] + self.pip
                if dist > 0:
                    self._emit_signal("sell", p2["val"], p2["val"] + dist,
                                      p2["val"] - dist, dist, p4)

            elif (p1["type"] == "low" and p2["type"] == "high" and
                  p3["type"] == "low" and p4["type"] == "high" and
                  p3["val"] < p1["val"] and p4["val"] > p2["val"]):
                dist = p4["val"] - p2["val"] + self.pip
                if dist > 0:
                    self._emit_signal("buy", p2["val"], p2["val"] - dist,
                                      p2["val"] + dist, dist, p4)

    def _emit_signal(self, d, entry, sl, tp, dist, p4):
        if dist < self.min_diff:
            return
        if self.current_atr and self.current_atr > 0:
            ap = self.current_atr / self.pip
            dp = dist / self.pip
            if dp / ap >= ATR_THRESH:
                sig = {
                    "entry": entry, "sl": sl, "tp": tp, "dist": dist,
                    "dir": d, "atr_pips": ap, "dist_pips": dp,
                    "dist_atr": dp / ap, "symbol": self.symbol,
                }
                # Dedup: skip if same dir+entry already pending
                exists = any(s["dir"] == d and abs(s["entry"] - entry) < 0.001 for s in self.pending_signals)
                if not exists:
                    self.pending_signals.append(sig)
                    self.signal_count += 1
                    log_signal(sig)

    # ── Position check ──

    def check_price(self, price):
        """Check fills/exits for this symbol. Returns True if a trade closed."""
        global balance

        # Active trade — check TP/SL
        t = self.active_trade
        if t:
            if t["dir"] == "sell":
                if price >= t["sl"]:
                    close_trade(self, "loss", t["sl"])
                    return True
                if price <= t["tp"]:
                    close_trade(self, "win", t["tp"])
                    return True
            else:
                if price <= t["sl"]:
                    close_trade(self, "loss", t["sl"])
                    return True
                if price >= t["tp"]:
                    close_trade(self, "win", t["tp"])
                    return True
            return False

        # No active trade — check pending signals
        if not self.pending_signals:
            return False

        still = []
        for sig in self.pending_signals:
            d = sig["dir"]

            # Limit order: track approach direction
            # Buy limit fills when price DROPS to entry from above
            # Sell limit fills when price RISES to entry from below
            if d == "buy":
                if price > sig["entry"]:
                    sig["_seen_above"] = True
                filled = sig.get("_seen_above") and price <= sig["entry"]
            else:
                if price < sig["entry"]:
                    sig["_seen_below"] = True
                filled = sig.get("_seen_below") and price >= sig["entry"]

            # Blown: price hit SL before limit filled
            blown = (d == "sell" and price >= sig["sl"]) or \
                    (d == "buy" and price <= sig["sl"])
            if blown:
                continue
            if filled:
                # LIMIT ORDER: fill at exact pivot entry, use signal's TP/SL
                self.active_trade = {
                    "dir": sig["dir"], "entry": sig["entry"],
                    "tp": sig["tp"], "sl": sig["sl"],
                    "dist": sig["dist"], "dist_pips": sig["dist_pips"],
                    "atr_pips": sig["atr_pips"], "dist_atr": sig["dist_atr"],
                    "symbol": sig["symbol"], "open_time": now_ts(),
                }
                log_entry(sig, sig["entry"])
                # Check if TP hit immediately at fill
                if (d == "sell" and price <= sig["tp"]) or \
                   (d == "buy" and price >= sig["tp"]):
                    self.pending_signals = still
                    close_trade(self, "win", sig["tp"])
                    return True
                self.pending_signals = still
                return False
            still.append(sig)

        self.pending_signals = still
        return False


# ─── Logging ─────────────────────────────────────────────────────

def log_signal(sig):
    ts = fmt_dt(now_ts())
    print(f"  ⚡ {ts} {sig['symbol']:>10s} {sig['dir']:>5s} "
          f"entry={fmt_price(sig['entry']):>10s} TP={fmt_price(sig['tp']):>10s} "
          f"SL={fmt_price(sig['sl']):>10s}  dist={sig['dist_pips']:.0f}p "
          f"ATR={sig['atr_pips']:.1f}p D/ATR={sig['dist_atr']:.1f}x")


def log_entry(sig, price):
    ts = fmt_dt(now_ts())
    # Limit order: entry at signal pivot, TP/SL from signal (1:1 around pivot)
    print(f"  🚀 {ts} {sig['symbol']:>10s} ENTRY {sig['dir']:>5s} @ {fmt_price(price)} "
          f"TP={fmt_price(sig['tp'])} SL={fmt_price(sig['sl'])} "
          f"risk={RISK * 100:.0f}%")


def log_exit(rec):
    ts = fmt_dt(rec["close_time"])
    icon = "✅" if rec["result"] == "win" else "❌"
    print(f"  {icon} {ts} {rec['symbol']:>10s} EXIT  {rec['dir']:>5s}  "
          f"${rec['entry']:.2f} -> ${rec['exit']:.2f}  "
          f"P&L={rec['pnl']:+.2f} ({rec['pnl_pct']:+.2f}%)  Bal=${rec['balance']:.2f}  "
          f"held={rec['duration_s']:.0f}s")


def print_status(states):
    global balance
    stats = compute_stats()
    growth = (balance / START_BAL - 1) * 100
    active = sum(1 for s in states if s.active_trade)
    candles = sum(s.candle_counter for s in states)
    s = f"  [{fmt_dt(now_ts())}]  ${balance:<8.2f}  {stats['trades']:>3d}tr  "
    s += f"{stats['wr']:>5.1f}%  Sharpe={stats['sharpe']:>5.1f}  "
    s += f"Growth={growth:+7.2f}%  {candles}c  Active={active}  {len(states)}sym"
    # Show per-symbol break
    for st in states:
        s += f"  {st.symbol}={st.candle_counter}c/{st.signal_count}s"
    print(s)


# ─── Stats ───────────────────────────────────────────────────────

def compute_stats():
    n = len(all_trades_log)
    if n == 0:
        return {"trades": 0, "wr": 0.0, "sharpe": 0.0}

    wins = sum(1 for t in all_trades_log if t["result"] == "win")
    wr = wins / n * 100
    returns = [t["pnl_pct"] for t in all_trades_log]
    mu = sum(returns) / n
    var = sum((r - mu) ** 2 for r in returns) / n
    std = math.sqrt(var) if var > 0 else 1e-10

    first_ts = all_trades_log[0]["close_time"]
    last_ts = all_trades_log[-1]["close_time"]
    elapsed_years = (last_ts - first_ts) / (365.25 * 24 * 3600 * 1000) if n >= 2 else 1
    tpy = n / elapsed_years if elapsed_years > 0 else 0
    sharpe = (mu / std) * math.sqrt(tpy) if tpy > 0 else 0.0

    return {"trades": n, "wr": wr, "sharpe": sharpe}


# ─── Global Trade Actions ────────────────────────────────────────

def close_trade(state, result, exit_price):
    global balance, all_trades_log
    t = state.active_trade
    nr = 1 if result == "win" else -1
    pnl = balance * RISK * nr
    balance += pnl

    rec = {
        "symbol": t["symbol"], "dir": t["dir"],
        "entry": t["entry"], "exit": exit_price,
        "result": result, "dist_pips": t["dist_pips"],
        "atr_pips": t["atr_pips"], "dist_atr": t["dist_atr"],
        "pnl": pnl, "pnl_pct": nr * RISK * 100,
        "balance": balance, "open_time": t["open_time"],
        "close_time": now_ts(),
        "duration_s": (now_ts() - t["open_time"]) / 1000,
    }
    all_trades_log.append(rec)
    log_exit(rec)
    state.active_trade = None


# ─── Main ────────────────────────────────────────────────────────

def handle_sigint(sig, frame):
    global running
    print("\n\n  ⏹️  Shutting down...")
    running = False


def print_footer(states, start_time, poll_count):
    stats = compute_stats()
    print(f"\n{'=' * 80}")
    print(f"  SESSION SUMMARY")
    print(f"{'=' * 80}")
    print(f"  Duration:   {poll_count}s ({poll_count / 3600:.1f}h)")
    print(f"  Symbols:    {len(states)}")
    for s in states:
        print(f"    {s.symbol:>10s}: {s.candle_counter}c  {s.signal_count}sig  "
              f"pip={s.pip}")
    print(f"  Trades:     {stats['trades']}")
    print(f"  Win rate:   {stats['wr']:.1f}%")
    print(f"  Sharpe:     {stats['sharpe']:.2f}")
    print(f"  Start:      ${START_BAL:.2f}")
    print(f"  End:        ${balance:.2f}")
    print(f"  Return:     {(balance / START_BAL - 1) * 100:+.2f}%")

    if all_trades_log:
        for sym in sorted(set(t["symbol"] for t in all_trades_log)):
            sym_trades = [t for t in all_trades_log if t["symbol"] == sym]
            sym_wins = sum(1 for t in sym_trades if t["result"] == "win")
            print(f"  {sym:>10s}: {len(sym_trades)}tr WR: {sym_wins / len(sym_trades) * 100:.1f}%")

        logfile = f"logs/trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        os.makedirs("logs", exist_ok=True)
        with open(logfile, "w") as f:
            json.dump(all_trades_log, f, indent=2, default=str)
        print(f"  Trade log:  {logfile}")
    print()



# ─── HTTP Status Server ─────────────────────────────────────────

def start_status_server_thread():
    """Minimal HTTP server on $PORT (Render) or 8080."""
    import json, socketserver
    from http.server import BaseHTTPRequestHandler
    port = int(os.environ.get("PORT", "8080"))
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/status", "/"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                import __main__ as m
                stats = m.compute_stats()
                syms = []
                for s in m._all_states:
                    syms.append({
                        "symbol": s.symbol, "pip": s.pip,
                        "candles": s.candle_counter, "signals": s.signal_count,
                        "active": s.active_trade is not None,
                        "in_trade": {
                            "dir": s.active_trade["dir"],
                            "entry": s.active_trade["entry"],
                            "tp": s.active_trade["tp"],
                            "sl": s.active_trade["sl"],
                            "dist_pips": s.active_trade["dist_pips"],
                            "atr_pips": s.active_trade["atr_pips"],
                            "dist_atr": s.active_trade["dist_atr"],
                        } if s.active_trade else None,
                    })
                body = json.dumps({
                    "status": "running",
                    "balance": m.balance,
                    "start_balance": m.START_BAL,
                    "growth_pct": (m.balance / m.START_BAL - 1) * 100 if m.START_BAL > 0 else 0,
                    "trades": stats["trades"],
                    "win_rate": round(stats["wr"], 1),
                    "sharpe": round(stats["sharpe"], 2),
                    "symbols": syms,
                })
                self.wfile.write(body.encode())
            else:
                self.send_response(404); self.end_headers()
        def log_message(self, *a): pass
    try:
        with socketserver.TCPServer(("0.0.0.0", port), H) as srv:
            print(f"  HTTP status on :{port}")
            srv.serve_forever()
    except OSError as e:
        print(f"  [HTTP server] {e}", file=sys.stderr)


def main():
    global running, SYMBOLS, PIP, ATR_THRESH, RISK, SWING_WINDOW, ATR_PERIOD, START_BAL, balance, MIN_DIFF, _all_states

    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} SYMBOL1 [SYMBOL2 ...] [options]")
        print(f"  python3 {sys.argv[0]} BTCUSDT ETHUSDT SOLUSDT")
        sys.exit(1)

    # Symbols are positional args before any --
    symbols = []
    i = 1
    while i < len(sys.argv) and not sys.argv[i].startswith("--"):
        symbols.append(sys.argv[i].upper())
        i += 1
    while i < len(sys.argv):
        a = sys.argv[i]
        if a == "--risk" and i + 1 < len(sys.argv):
            RISK = float(sys.argv[i + 1]); i += 2
        elif a == "--threshold" and i + 1 < len(sys.argv):
            ATR_THRESH = float(sys.argv[i + 1]); i += 2
        elif a == "--window" and i + 1 < len(sys.argv):
            SWING_WINDOW = int(sys.argv[i + 1]); i += 2
        elif a == "--atr-period" and i + 1 < len(sys.argv):
            ATR_PERIOD = int(sys.argv[i + 1]); i += 2
        elif a == "--min-diff" and i + 1 < len(sys.argv):
            MIN_DIFF = float(sys.argv[i + 1]); i += 2
        elif a == "--start" and i + 1 < len(sys.argv):
            START_BAL = float(sys.argv[i + 1]); balance = START_BAL; i += 2
        else:
            i += 1

    signal.signal(signal.SIGINT, handle_sigint)

    # Init symbol states
    states = [SymbolState(sym, min_diff=MIN_DIFF) for sym in symbols]

    # Start HTTP status server thread
    global _all_states
    _all_states = states
    t = threading.Thread(target=start_status_server_thread, daemon=True)
    t.start()



    print("=" * 80)
    print(f"  MEXC 1s Multi-Asset Swing Trader")
    print(f"  Symbols: {', '.join(symbols)}")
    print(f"  Start: ${START_BAL:.2f}  Risk: {RISK * 100:.0f}%  "
          f"Threshold: {ATR_THRESH}x  Window: {SWING_WINDOW}s  MinDiff: ${MIN_DIFF:.0f}")
    print("=" * 80)
    print(f"  {'Time':<15} {'Bal':<10} {'Stats':<50}")
    print("-" * 80)

    poll_count = 0
    last_status_ts = 0

    while running:
        for state in states:
            price = fetch_price(state.symbol)
            ts = now_ts()

            if price is not None:
                state.tick_price(price, ts)
                if state.detect_swings() > 0:
                    state.check_patterns()
                state.check_price(price)

        poll_count += 1
        if now_ts() - last_status_ts > 5000:
            print_status(states)
            last_status_ts = now_ts()

        time.sleep(1)

    print_footer(states, 0, poll_count)


if __name__ == "__main__":
    main()
