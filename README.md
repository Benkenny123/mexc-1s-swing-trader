# MEXC 1s Swing Trader

Real-time 1-second swing pattern trading bot for MEXC exchange.

Detects 4-pivot swing patterns (H-L-H-L / L-H-L-H) on 1-second OHLCV candles built from live price ticks. Enters at the pivot point with 1:1 RR, risking 10% per trade.

## How it works

```
MEXC ticker/price (poll 1s) → 1s OHLCV builder → swing pivot detector
  → 4-pivot pattern matcher → position manager → P&L
```

- Polls `GET /api/v3/ticker/price` every second
- Builds 1-second OHLCV candles in real-time
- Detects swing pivots over a rolling ±10s window
- Fires when a 4-pivot pattern is confirmed with dist/ATR ≥ 8x
- Manages TP/SL entry on the next tick
- Logs every trade, prints live stats

## Usage

```bash
python3 live_trader.py BTCUSDT
python3 live_trader.py ETHUSDT --risk 0.05
python3 live_trader.py PEPEUSDT --threshold 12.0
```

## Parameters

| Flag | Default | Description |
|------|---------|-------------|
| `--risk` | 0.10 | Risk per trade (% of balance) |
| `--threshold` | 8.0 | Min dist/ATR for entry |
| `--window` | 10 | Swing pivot lookback (seconds) |
| `--atr-period` | 14 | ATR calculation period |
| `--start` | 100.0 | Starting account balance |

## Files

- `live_trader.py` — Main live trading bot
- `backtest.py` — Historical backtest on saved data
- `requirements.txt` — Dependencies

## Strategy

The 4-pivot swing pattern:

**Sell setup:** High → Low → Higher High → Lower Low
Entry at pivot-2 low, TP at entry - dist, SL at entry + dist

**Buy setup:** Low → High → Lower Low → Higher High
Entry at pivot-2 high, TP at entry + dist, SL at entry - dist

Only trades when `dist / ATR ≥ threshold` (default 8.0).
