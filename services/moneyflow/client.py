from __future__ import annotations

"""Unauthenticated Binance Spot client; no order methods exist."""

from services.common.binance_public import BinancePublicClient


class MoneyFlowClient:
    def __init__(self, *, spot=None):
        self.spot = spot or BinancePublicClient(user_agent="bitcoin-moneyflow/1.0")

    def spot_exchange_symbol(self, symbol: str) -> dict:
        data = self.spot.exchange_info(symbol)
        rows = data.get("symbols") or []
        if len(rows) != 1:
            raise RuntimeError(f"Spot exchangeInfo did not return exactly one {symbol} market")
        return rows[0]

    def spot_depth(self, symbol: str, limit: int) -> dict:
        return self.spot.get("/api/v3/depth", {"symbol": symbol, "limit": limit})

    def spot_aggregate_trades(self, symbol: str, limit: int) -> list:
        return self.spot.get("/api/v3/aggTrades", {"symbol": symbol, "limit": limit})

    def klines(self, symbol: str, interval: str, limit: int) -> list:
        return self.spot.get("/api/v3/klines", {
            "symbol": symbol, "interval": interval, "limit": limit})
