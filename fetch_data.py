"""
Fetch 1 year of OHLCV data from Binance Futures (no API key required).
Data is cached to data/<symbol>_<interval>.csv and updated incrementally.
"""
import asyncio
import os
import time
import logging
from datetime import datetime

import aiohttp
import pandas as pd

logger = logging.getLogger("fetch_data")

FAPI_BASE = "https://fapi.binance.com/fapi/v1/klines"
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

INTERVAL_MS = {
    "1m":   60_000,
    "5m":   300_000,
    "15m":  900_000,
    "1h":   3_600_000,
    "4h":   14_400_000,
    "1d":   86_400_000,
}

KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades",
    "taker_buy_base", "taker_buy_quote", "ignore",
]


def _cache_path(symbol: str, interval: str) -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, f"{symbol.lower()}_{interval}.csv")


async def _fetch_batch(
    session: aiohttp.ClientSession,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    limit: int = 1500,
) -> list:
    params = {
        "symbol": symbol,
        "interval": interval,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": limit,
    }
    async with session.get(FAPI_BASE, params=params, timeout=aiohttp.ClientTimeout(total=30)) as r:
        r.raise_for_status()
        return await r.json()


async def fetch_klines(symbol: str, interval: str, days: int = 365) -> pd.DataFrame:
    """
    Fetch `days` of klines. Returns a DataFrame with numeric columns.
    Uses cached CSV if available and fresh enough; otherwise fetches incrementally.
    """
    step_ms = INTERVAL_MS[interval]
    now_ms = int(time.time() * 1000)
    target_start = now_ms - days * 86_400_000
    target_end = now_ms - step_ms  # exclude the currently-open candle

    cached = _cache_path(symbol, interval)
    old_df = None

    if os.path.exists(cached):
        old_df = pd.read_csv(cached, dtype={"open_time": int, "close_time": int})
        cache_end = int(old_df["close_time"].iloc[-1])
        gap = target_end - cache_end
        if gap < step_ms * 3:
            logger.info(f"Cache hit: {interval} ({len(old_df)} bars)")
            old_df = _cast_numeric(old_df)
            return old_df[old_df["open_time"] >= target_start].reset_index(drop=True)
        fetch_start = cache_end + 1
        logger.info(
            f"Updating {interval} from {datetime.fromtimestamp(fetch_start/1000):%Y-%m-%d %H:%M}"
        )
    else:
        fetch_start = target_start
        total_bars = (target_end - target_start) // step_ms
        logger.info(
            f"Fetching {days}d of {interval} data for {symbol} (~{total_bars:,} bars)"
        )

    rows: list = []
    current = fetch_start
    total = max((target_end - fetch_start) // step_ms, 1)
    fetched = 0

    async with aiohttp.ClientSession() as session:
        while current < target_end:
            batch = await _fetch_batch(session, symbol, interval, current, target_end)
            if not batch:
                break
            rows.extend(batch)
            fetched += len(batch)
            current = batch[-1][0] + step_ms
            pct = min(fetched / total * 100, 100)
            print(f"\r  [{interval}] {fetched:,}/{total:,} bars  ({pct:.0f}%)", end="", flush=True)
            await asyncio.sleep(0.05)  # stay well within rate limits

    print()

    new_df = pd.DataFrame(rows, columns=KLINE_COLS)

    if old_df is not None:
        df = pd.concat([old_df, new_df], ignore_index=True)
    else:
        df = new_df

    df = (
        df.drop_duplicates("open_time")
        .sort_values("open_time")
        .reset_index(drop=True)
    )
    # Trim to requested window
    df = df[df["open_time"] >= target_start].reset_index(drop=True)
    df = _cast_numeric(df)
    df.to_csv(cached, index=False)
    logger.info(f"Saved {len(df):,} {interval} bars → {cached}")
    return df


def _cast_numeric(df: pd.DataFrame) -> pd.DataFrame:
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["open_time"] = df["open_time"].astype(int)
    df["close_time"] = df["close_time"].astype(int)
    return df


async def fetch_all(symbol: str = "BTCUSDT", days: int = 365):
    """Fetch 5m and 1h data concurrently. Returns (df_5m, df_1h)."""
    print(f"\nFetching {days}-day historical data for {symbol}…")
    df_5m, df_1h = await asyncio.gather(
        fetch_klines(symbol, "5m", days),
        fetch_klines(symbol, "1h", days),
    )
    print(
        f"Data ready: 5m={len(df_5m):,} bars  "
        f"1h={len(df_1h):,} bars\n"
    )
    return df_5m, df_1h


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    df5, df1 = asyncio.run(fetch_all())
    print(df5.tail(3))
