#!/usr/bin/env python3
"""
Mirror Trader — mirrors the main swing trader in opposite direction.

No strategy logic. Reads the main bot's HTTP status endpoint and
mirrors every trade: when main buys, mirror sells and vice versa.
Mirror PnL directly from main's balance change — if main wins,
mirror loses by the same amount.
"""

import requests
import time
import sys
import signal
import json
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


def get_main_status():
    try:
        r = requests.get(MAIN_URL, timeout=3)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


class MirrorState:
    def __init__(self, symbol):
        self.symbol = symbol
        self.active = None  # {"dir": "buy"/"sell", "entry": price, "open_time": ms}

    def open_opposite(self, main_dir, main_entry):
        """Open mirror position opposite to main. Use main's entry price."""
        mirror_dir = "sell" if main_dir == "buy" else "buy"
        self.active = {
            "dir": mirror_dir,
            "entry": main_entry,  # Mirror at main's entry price
            "open_time": now_ts(),
            "main_dir": main_dir,
        }
        print(f"  🪞 {fmt_dt(now_ts())} {self.symbol:>10s} MIRROR {mirror_dir:>5s} @ {main_entry:.2f}  "
              f"(main was {main_dir})")
        return True

    def close_with_result(self, main_won):
        """Close mirror knowing main's result. Mirror result = opposite."""
        global balance, trades_log
        if not self.active:
            return
        t = self.active

        # Mirror result is opposite of main
        mirror_won = not main_won
        result = "win" if mirror_won else "loss"
        pnl = balance * RISK * (1 if mirror_won else -1)
        balance += pnl

        duration = (now_ts() - t["open_time"]) / 1000
        icon = "✅" if mirror_won else "❌"
        print(f"  {icon} {fmt_dt(now_ts())} {self.symbol:>10s} MIRROR EXIT {t['dir']:>5s}  "
              f"@ {t['entry']:.2f}  PnL={pnl:+.2f} ({'main won' if main_won else 'main lost'})  "
              f"Bal=${balance:.2f}  held={duration:.0f}s")
        trades_log.append({
            "symbol": self.symbol, "dir": t["dir"],
            "entry": t["entry"], "result": result,
            "pnl": pnl, "duration_s": duration,
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
    print("  Mirrors PnL from main's balance changes")
    print(f"  Start: ${START_BAL:.2f}  Risk: {RISK*100:.0f}%")
    print(f"  Main:  {MAIN_URL}")
    print("=" * 70)

    mirrors = {}
    last_print = 0
    prev_main_bal = None
    prev_main_active = {}  # symbol -> {"dir": str, "entry": float}

    while running:
        status = get_main_status()
        ts = now_ts()

        if status is None:
            if ts - last_print > 10000:
                print(f"  [{fmt_dt(ts)}] Main bot not reachable — retrying...")
                last_print = ts
            time.sleep(1)
            continue

        main_bal = status.get("balance", 0)
        if prev_main_bal is None:
            prev_main_bal = main_bal

        # Current main active trades
        current_active = {}
        for s in status.get("symbols", []):
            sym = s["symbol"]
            if s.get("active") and s.get("in_trade"):
                current_active[sym] = {
                    "dir": s["in_trade"]["dir"],
                    "entry": s["in_trade"]["entry"],
                }

        # Ensure mirror state exists
        for s in status.get("symbols", []):
            sn = s["symbol"]
            if sn not in mirrors:
                mirrors[sn] = MirrorState(sn)

        # Detect new trades opened and trades closed
        for sym, mirror in mirrors.items():
            was_active = sym in prev_main_active
            is_active = sym in current_active

            if is_active and not was_active:
                # Main just opened a trade — open opposite mirror
                main_dir = current_active[sym]["dir"]
                main_entry = current_active[sym]["entry"]
                mirror.open_opposite(main_dir, main_entry)

            elif was_active and not is_active:
                # Main just closed a trade — close mirror with opposite result
                # Determine result: if main balance went down, main lost
                main_won = main_bal > prev_main_bal
                mirror.close_with_result(main_won)

        prev_main_active = current_active
        prev_main_bal = main_bal

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
            # Show main bot balance for comparison
            parts.append(f"MainBal=${main_bal:.2f}")
            print("  ".join(parts))
            last_print = ts

        time.sleep(1)

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
