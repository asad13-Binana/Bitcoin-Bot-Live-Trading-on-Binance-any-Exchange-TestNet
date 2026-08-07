from __future__ import annotations
"""Unauthenticated Binance Spot and USD-M clients; no order methods exist."""

from services.common.binance_public import BinancePublicClient


class MoneyFlowClient:
    def __init__(self, *, spot=None, futures=None):
        self.spot = spot or BinancePublicClient(user_agent="bitcoin-moneyflow/1.0")
        self.futures = futures or BinancePublicClient(
            base="https://fapi.binance.com", user_agent="bitcoin-moneyflow/1.0")

    def spot_exchange_symbol(self, symbol: str) -> dict:
        data = self.spot.exchange_info(symbol)
        rows = data.get("symbols") or []
        if len(rows) != 1:
            raise RuntimeError(f"Spot exchangeInfo did not return exactly one {symbol} market")
        return rows[0]

    def futures_exchange_symbol(self, symbol: str) -> dict | None:
        try:
            data = self.futures.get("/fapi/v1/exchangeInfo")
        except Exception:
            raise
        rows = [row for row in (data.get("symbols") or []) if row.get("symbol") == symbol]
        return rows[0] if rows else None

    def spot_depth(self, symbol: str, limit: int) -> dict:
        return self.spot.get("/api/v3/depth", {"symbol": symbol, "limit": limit})

    def futures_depth(self, symbol: str, limit: int) -> dict:
        return self.futures.get("/fapi/v1/depth", {"symbol": symbol, "limit": limit})

    def spot_aggregate_trades(self, symbol: str, limit: int) -> list:
        return self.spot.get("/api/v3/aggTrades", {"symbol": symbol, "limit": limit})

    def futures_taker(self, symbol: str) -> dict:
        rows = self.futures.get("/futures/data/takerlongshortRatio", {
            "symbol": symbol, "period": "5m", "limit": 1})
        return rows[-1] if rows else {}

    def futures_open_interest(self, symbol: str) -> dict:
        return self.futures.get("/fapi/v1/openInterest", {"symbol": symbol})

    def futures_premium(self, symbol: str) -> dict:
        return self.futures.get("/fapi/v1/premiumIndex", {"symbol": symbol})

    def klines(self, symbol: str, interval: str, limit: int) -> list:
        return self.spot.get("/api/v3/klines", {
            "symbol": symbol, "interval": interval, "limit": limit})
