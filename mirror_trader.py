#!/usr/bin/env python3
"""
Mirror Trader — mirrors the main swing trader in opposite direction.

No strategy logic. Reads the main bot's HTTP status endpoint and
mirrors every trade: when main buys, mirror sells and vice versa.
When the main trade closes, the mirror trade closes immediately
at the current market price.
"""

import requests
import time
import sys
import signal
import json
import math
from datetime import datetime, timezone

MAIN_URL = "http://localhost:8080/status"
MEXC_API = "https://api.mexc.com/api/v3/ticker/price"
START_BAL = 1000.0
RISK = 0.10

running = True
balance = START_BAL
trades_log = []


def now_ts():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def fmt_dt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]


def fetch_price(sym):
    try:
        r = requests.get(MEXC_API, params={"symbol": sym}, timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return None


def get_main_status():
    """Fetch the main bot's status. Returns None on failure."""
    try:
        r = requests.get(MAIN_URL, timeout=3)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


class MirrorState:
    """Tracks mirror positions for one symbol."""

    def __init__(self, symbol):
        self.symbol = symbol
        self.active = None  # { "dir": "buy"/"sell", "entry": price, "open_time": ms }

    def open_opposite(self, main_dir):
        """Open mirror position opposite to main bot's trade."""
        price = fetch_price(self.symbol)
        if price is None:
            return False
        mirror_dir = "sell" if main_dir == "buy" else "buy"
        self.active = {
            "dir": mirror_dir,
            "entry": price,
            "open_time": now_ts(),
        }
        print(f"  🪞 {fmt_dt(now_ts())} {self.symbol:>10s} MIRROR {mirror_dir:>5s} @ {price:.2f}  "
              f"(main was {main_dir})")
        return True

    def close(self):
        """Close mirror position at current market price."""
        global balance, trades_log
        if not self.active:
            return
        price = fetch_price(self.symbol)
        if price is None:
            return
        t = self.active
        # Mirror PnL: opposite direction logic
        if t["dir"] == "buy":
            pnl_pct = (price / t["entry"] - 1)
        else:
            pnl_pct = (t["entry"] / price - 1)
        pnl = balance * RISK * (1 if pnl_pct > 0 else -1)
        balance += pnl

        result = "win" if pnl > 0 else "loss"
        duration = (now_ts() - t["open_time"]) / 1000
        icon = "✅" if result == "win" else "❌"
        print(f"  {icon} {fmt_dt(now_ts())} {self.symbol:>10s} MIRROR EXIT {t['dir']:>5s}  "
              f"${t['entry']:.2f} -> ${price:.2f}  "
              f"PnL={pnl:+.2f} ({pnl_pct*100:+.2f}%)  Bal=${balance:.2f}  held={duration:.0f}s")
        trades_log.append({
            "symbol": self.symbol, "dir": t["dir"],
            "entry": t["entry"], "exit": price,
            "result": result, "pnl": pnl,
            "pnl_pct": pnl_pct * 100, "duration_s": duration,
        })
        self.active = None

    def summary(self):
        return f"{self.symbol}={('ACTIVE ' + self.active['dir']) if self.active else 'IDLE'}"


def handle_sigint(sig, frame):
    global running
    print("\n  ⏹️  Mirror shutting down...")
    running = False


def main():
    global running, balance
    signal.signal(signal.SIGINT, handle_sigint)

    print("=" * 70)
    print("  MIRROR TRADER — opposes main swing bot")
    print("  No strategy — mirrors every trade in opposite direction")
    print(f"  Start: ${START_BAL:.2f}  Risk: {RISK*100:.0f}%")
    print(f"  Main:  {MAIN_URL}")
    print("=" * 70)

    mirrors = {}  # symbol -> MirrorState
    last_print = 0

    while running:
        status = get_main_status()
        ts = now_ts()

        if status is None:
            if ts - last_print > 10000:
                print(f"  [{fmt_dt(ts)}] Main bot not reachable — retrying...")
                last_print = ts
            time.sleep(1)
            continue

        # Get main bot's active trades per symbol
        main_active = {}
        for s in status.get("symbols", []):
            sym = s["symbol"]
            if s.get("active") and s.get("in_trade"):
                main_active[sym] = s["in_trade"]["dir"]

        # Ensure mirror state exists for each symbol
        for sym in status.get("symbols", []):
            sn = sym["symbol"]
            if sn not in mirrors:
                mirrors[sn] = MirrorState(sn)

        # Open/close mirrors based on main bot's state
        for sym, mirror in list(mirrors.items()):
            if sym in main_active:
                # Main has an active trade
                main_dir = main_active[sym]
                if mirror.active is None:
                    # No mirror yet — open opposite
                    mirror.open_opposite(main_dir)
                elif mirror.active["dir"] == main_dir:
                    # Mirror has same direction as main (shouldn't happen) — close and re-open
                    mirror.close()
                    mirror.open_opposite(main_dir)
            else:
                # Main has no active trade — close mirror if open
                if mirror.active is not None:
                    mirror.close()

        # Print status every 10s
        if ts - last_print > 10000:
            n = len(trades_log)
            wins = sum(1 for t in trades_log if t["result"] == "win")
            wr = wins / n * 100 if n else 0.0
            growth = (balance / START_BAL - 1) * 100
            parts = [f"  [{fmt_dt(ts)}]  ${balance:<8.2f}  {n}tr  {wr:.0f}%  "
                     f"Growth={growth:+7.2f}%"]
            for m in mirrors.values():
                parts.append(m.summary())
            print("  ".join(parts))
            last_print = ts

        time.sleep(1)

    # Summary
    n = len(trades_log)
    wins = sum(1 for t in trades_log if t["result"] == "win")
    wr = wins / n * 100 if n else 0.0
    growth = (balance / START_BAL - 1) * 100
    print(f"\n{'=' * 70}")
    print(f"  MIRROR SESSION SUMMARY")
    print(f"{'=' * 70}")
    print(f"  Trades:   {n}")
    print(f"  Win rate: {wr:.1f}%")
    print(f"  Start:    ${START_BAL:.2f}")
    print(f"  End:      ${balance:.2f}")
    print(f"  Return:   {growth:+.2f}%")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
