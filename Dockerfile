FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY live_trader.py .

ENTRYPOINT ["python3", "live_trader.py"]
CMD ["BTCUSDT", "ETHUSDT"]
