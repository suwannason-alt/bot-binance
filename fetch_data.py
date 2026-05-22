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


async def _fetch_range(session: aiohttp.ClientSession,
                       symbol: str, interval: str,
                       start_ms: int, end_ms: int,
                       label: str = "") -> list:
    """Fetch all bars in [start_ms, end_ms] across multiple batches."""
    step_ms = INTERVAL_MS[interval]
    rows: list = []
    current = start_ms
    total = max((end_ms - start_ms) // step_ms, 1)
    fetched = 0
    while current < end_ms:
        batch = await _fetch_batch(session, symbol, interval, current, end_ms)
        if not batch:
            break
        rows.extend(batch)
        fetched += len(batch)
        current = batch[-1][0] + step_ms
        pct = min(fetched / total * 100, 100)
        tag = f"[{interval}{' '+label if label else ''}]"
        print(f"\r  {tag} {fetched:,}/{total:,} bars  ({pct:.0f}%)", end="", flush=True)
        await asyncio.sleep(0.05)
    print()
    return rows


async def fetch_klines(symbol: str, interval: str, days: int = 365) -> pd.DataFrame:
    """
    Fetch `days` of klines. Returns a DataFrame with numeric columns.
    Uses cached CSV if available; extends backward and/or forward as needed.
    """
    step_ms = INTERVAL_MS[interval]
    now_ms = int(time.time() * 1000)
    target_start = now_ms - days * 86_400_000
    target_end = now_ms - step_ms  # exclude the currently-open candle

    cached = _cache_path(symbol, interval)
    parts: list[pd.DataFrame] = []

    if os.path.exists(cached):
        old_df = pd.read_csv(cached, dtype={"open_time": int, "close_time": int})
        old_df = _cast_numeric(old_df)
        cache_start = int(old_df["open_time"].iloc[0])
        cache_end   = int(old_df["close_time"].iloc[-1])

        needs_older = target_start < cache_start - step_ms
        needs_newer = (target_end - cache_end) >= step_ms * 3

        if not needs_older and not needs_newer:
            logger.info(f"Cache hit: {interval} ({len(old_df)} bars)")
            return old_df[old_df["open_time"] >= target_start].reset_index(drop=True)

        async with aiohttp.ClientSession() as session:
            if needs_older:
                logger.info(
                    f"Back-filling {interval} "
                    f"{datetime.fromtimestamp(target_start/1000):%Y-%m-%d} → "
                    f"{datetime.fromtimestamp(cache_start/1000):%Y-%m-%d}"
                )
                rows = await _fetch_range(session, symbol, interval,
                                          target_start, cache_start, label="back-fill")
                if rows:
                    parts.append(_cast_numeric(pd.DataFrame(rows, columns=KLINE_COLS)))

            parts.append(old_df)

            if needs_newer:
                logger.info(
                    f"Updating {interval} from "
                    f"{datetime.fromtimestamp(cache_end/1000):%Y-%m-%d %H:%M}"
                )
                rows = await _fetch_range(session, symbol, interval,
                                          cache_end + 1, target_end, label="update")
                if rows:
                    parts.append(_cast_numeric(pd.DataFrame(rows, columns=KLINE_COLS)))
    else:
        total_bars = (target_end - target_start) // step_ms
        logger.info(f"Fetching {days}d of {interval} data for {symbol} (~{total_bars:,} bars)")
        async with aiohttp.ClientSession() as session:
            rows = await _fetch_range(session, symbol, interval, target_start, target_end)
        parts.append(_cast_numeric(pd.DataFrame(rows, columns=KLINE_COLS)))

    df = pd.concat(parts, ignore_index=True)
    df = (
        df.drop_duplicates("open_time")
        .sort_values("open_time")
        .reset_index(drop=True)
    )
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
