#!/usr/bin/env python3
"""
Historical backtest using the same logic as the live trader.

Loads previously saved 1s CSV data and runs the swing pattern strategy.
Mirrors live_trader.py logic for consistency.

Usage:
    python3 backtest.py btc_1s_100k.csv
    python3 backtest.py btc_1s_100k.csv --risk 0.05 --threshold 10.0 --window 15
"""
import sys, json, csv
import numpy as np
from datetime import datetime, timezone

def run(csv_path, risk=0.10, threshold=8.0, window=10, atr_period=14, start=100.0):
    # Load data
    rows = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append({
                "t": int(datetime.strptime(r["Datetime"], "%Y-%m-%d %H:%M:%S").timestamp() * 1000),
                "o": float(r["Open"]), "h": float(r["High"]),
                "l": float(r["Low"]), "c": float(r["Close"]),
                "v": float(r["Volume"]), "n": int(r["Trades"]) if "Trades" in r else 0
            })

    n = len(rows)
    H = np.array([r["h"] for r in rows])
    L = np.array([r["l"] for r in rows])
    C = np.array([r["c"] for r in rows])
    O = np.array([r["o"] for r in rows])

    # PIP from MEXC exchange info (pass symbol or default)
    pip = 0.01
    try:
        import requests as _req
        _r = _req.get("https://api.mexc.com/api/v3/exchangeInfo", timeout=10)
        for _s in _r.json()["symbols"]:
            if _s["symbol"] == csv_path.split("_")[0].upper():
                pip = 10 ** -_s["quotePrecision"]
                break
    except Exception:
        pass

    # ATR
    tr = np.maximum(H - L, np.maximum(np.abs(H - np.roll(C, 1)), np.abs(L - np.roll(C, 1))))
    tr[0] = H[0] - L[0]
    atr = np.full(n, np.nan)
    atr[atr_period-1] = np.mean(tr[:atr_period])
    alpha = 2.0 / (atr_period + 1)
    for i in range(atr_period, n):
        atr[i] = alpha * tr[i] + (1 - alpha) * atr[i-1]

    # Swing pivots
    highs, lows = [], []
    w = window
    for i in range(w, n - w):
        if H[i] == max(H[i-w:i+w+1]):
            highs.append({"idx": i, "val": H[i], "type": "high"})
        if L[i] == min(L[i-w:i+w+1]):
            lows.append({"idx": i, "val": L[i], "type": "low"})

    pivots = sorted(highs + lows, key=lambda x: x["idx"])
    cleaned = []
    if pivots:
        cleaned.append(pivots[0])
        for p in pivots[1:]:
            lp = cleaned[-1]
            if p["type"] == lp["type"]:
                if p["type"] == "high" and p["val"] > lp["val"]: cleaned[-1] = p
                elif p["type"] == "low" and p["val"] < lp["val"]: cleaned[-1] = p
            else: cleaned.append(p)

    # Signals
    sig_map = {}
    for i in range(len(cleaned) - 4):
        p1, p2, p3, p4 = cleaned[i:i+4]
        if (p1["type"] == "high" and p2["type"] == "low" and p3["type"] == "high" and p4["type"] == "low"
            and p3["val"] > p1["val"] and p4["val"] < p2["val"]):
            dist = p2["val"] - p4["val"] + pip
            if dist > 0:
                sig_map.setdefault(p4["idx"] + w + 1, []).append(
                    {"entry": p2["val"], "sl": p2["val"] + dist, "tp": p2["val"] - dist,
                     "dist": dist, "dir": "sell", "pidx": p4["idx"]})
        elif (p1["type"] == "low" and p2["type"] == "high" and p3["type"] == "low" and p4["type"] == "high"
              and p3["val"] < p1["val"] and p4["val"] > p2["val"]):
            dist = p4["val"] - p2["val"] + pip
            if dist > 0:
                sig_map.setdefault(p4["idx"] + w + 1, []).append(
                    {"entry": p2["val"], "sl": p2["val"] - dist, "tp": p2["val"] + dist,
                     "dist": dist, "dir": "buy", "pidx": p4["idx"]})

    # Simulation
    trades = []
    active = None
    pending = []
    balance = start

    for idx in range(n):
        o, h, l, c = O[idx], H[idx], L[idx], C[idx]

        if idx in sig_map:
            for s in sig_map[idx]:
                ap = atr[idx] / pip
                dp = s["dist"] / pip
                if ap > 0 and dp / ap >= threshold:
                    sc = dict(s)
                    sc.update({"atr_pips": ap, "dist_pips": dp, "dist_atr": dp/ap})
                    pending.append(sc)

        seq = [("o", o), ("l", l), ("h", h)] if c >= o else [("o", o), ("h", h), ("l", l)]
        result = None

        for tag, price in seq:
            if active:
                if active["dir"] == "sell":
                    if price >= active["sl"]: result = "loss"; break
                    if price <= active["tp"]: result = "win"; break
                else:
                    if price <= active["sl"]: result = "loss"; break
                    if price >= active["tp"]: result = "win"; break
            else:
                consumed = []
                for sig in pending:
                    filled = (sig["dir"] == "sell" and price <= sig["entry"]) or \
                             (sig["dir"] == "buy" and price >= sig["entry"])
                    blown = (sig["dir"] == "sell" and price >= sig["sl"]) or \
                            (sig["dir"] == "buy" and price <= sig["sl"])
                    if blown: consumed.append(sig); continue
                    if filled:
                        active = sig; consumed.append(sig)
                        if (sig["dir"] == "sell" and price <= sig["tp"]) or \
                           (sig["dir"] == "buy" and price >= sig["tp"]): result = "win"
                        break
                for s in consumed:
                    if s in pending: pending.remove(s)
                if result: break

        for sig in list(pending):
            if (sig["dir"] == "sell" and h >= sig["sl"]) or \
               (sig["dir"] == "buy" and l <= sig["sl"]):
                if sig in pending: pending.remove(sig)

        if result and active:
            nr = (active["dist_pips"] / active["dist_pips"]) if result == "win" else \
                 (-active["dist_pips"] / active["dist_pips"])
            balance += balance * risk * nr
            trades.append({
                "result": result, "dir": active["dir"],
                "entry": active["entry"], "exit": active["tp"] if result == "win" else active["sl"],
                "dist_pips": active["dist_pips"], "atr_pips": active["atr_pips"],
                "dist_atr": active["dist_atr"],
                "pnl_pct": nr * risk * 100, "balance": balance,
            })
            active = None

    return trades, balance, cleaned, len(sig_map)

# ── Main ──
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)

    csv_path = sys.argv[1]
    risk = float(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[2] == "--risk" else 0.10
    thresh = float(sys.argv[5]) if len(sys.argv) > 5 and sys.argv[4] == "--threshold" else 8.0
    window = int(sys.argv[7]) if len(sys.argv) > 7 and sys.argv[6] == "--window" else 10

    print("="*60)
    print(f"  Swing Pattern Backtest — {csv_path}")
    print(f"  Risk: {risk*100:.0f}%  Threshold: {thresh}x  Window: {window}s")
    print("="*60)

    trades, final, pivots, n_sigs = run(csv_path, risk=risk, threshold=thresh, window=window)

    if not trades:
        print("  No trades generated.")
    else:
        wins = [t for t in trades if t["result"] == "win"]
        losses = [t for t in trades if t["result"] == "loss"]
        wr = len(wins) / len(trades) * 100
        peak = max(t["balance"] for t in trades)
        pk = 100.0; mdd = 0
        for t in trades:
            if t["balance"] > pk: pk = t["balance"]
            dd = (pk - t["balance"]) / pk * 100
            mdd = max(mdd, dd)

        print(f"  Trades:   {len(trades)}")
        print(f"  WR:       {wr:.1f}%")
        print(f"  Start:    $100.00")
        print(f"  Final:    ${final:,.2f}")
        print(f"  Return:   {(final/100-1)*100:+.2f}%")
        print(f"  MaxDD:    {mdd:.1f}%")

        buys = [t for t in trades if t["dir"] == "buy"]
        sells = [t for t in trades if t["dir"] == "sell"]
        if buys: print(f"  Longs:    {len(buys)} WR: {sum(1 for t in buys if t['result']=='win')/len(buys)*100:.1f}%")
        if sells: print(f"  Shorts:   {len(sells)} WR: {sum(1 for t in sells if t['result']=='win')/len(sells)*100:.1f}%")

        if wins and losses:
            pf = abs(sum(t["pnl_pct"] for t in wins) / sum(t["pnl_pct"] for t in losses))
            print(f"  PF:       {pf:.2f}")

        print(f"  AvgDist:  {np.mean([t['dist_pips'] for t in trades]):.1f}p")
        print(f"  AvgATR:   {np.mean([t['atr_pips'] for t in trades]):.1f}p")

    print("="*60)
