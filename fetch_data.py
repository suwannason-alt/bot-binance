"""
Binance Futures OHLCV data fetcher with persistent CSV cache.

Data is downloaded from the public ``/fapi/v1/klines`` endpoint (no API key
required) and cached to ``data/<symbol>_<interval>.csv``.  Subsequent calls
extend the cache forward (and optionally backward) rather than re-downloading
the entire history.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import List, Tuple

import aiohttp
import pandas as pd

logger = logging.getLogger("fetch_data")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FAPI_BASE = "https://fapi.binance.com/fapi/v1/klines"
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

INTERVAL_MS: dict[str, int] = {
    "1m":  60_000,
    "5m":  300_000,
    "15m": 900_000,
    "1h":  3_600_000,
    "4h":  14_400_000,
    "1d":  86_400_000,
}

KLINE_COLS: List[str] = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades",
    "taker_buy_base", "taker_buy_quote", "ignore",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _cache_path(symbol: str, interval: str) -> str:
    """Return the local CSV cache path, creating the data directory if needed.

    Args:
        symbol:   Trading pair (e.g. ``"BTCUSDT"``).
        interval: Candle interval string (e.g. ``"5m"``).

    Returns:
        Absolute path to the CSV file.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, f"{symbol.lower()}_{interval}.csv")


def _cast_numeric(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure OHLCV columns are typed correctly after CSV round-trips.

    Args:
        df: Raw kline DataFrame.

    Returns:
        The same DataFrame with OHLCV columns cast to ``float`` and
        ``open_time`` / ``close_time`` cast to ``int``.
    """
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    df["open_time"] = df["open_time"].astype(int)
    df["close_time"] = df["close_time"].astype(int)
    return df


async def _fetch_batch(
    session: aiohttp.ClientSession,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    limit: int = 1500,
) -> list:
    """Fetch a single batch of klines from the Binance Futures REST API.

    Args:
        session:   Active ``aiohttp`` client session.
        symbol:    Trading pair symbol.
        interval:  Candle interval string.
        start_ms:  Batch start time in milliseconds since epoch.
        end_ms:    Batch end time in milliseconds since epoch.
        limit:     Maximum candles per request (Binance max is 1 500).

    Returns:
        Raw list of kline rows as returned by the API.

    Raises:
        aiohttp.ClientResponseError: On non-2xx HTTP responses.
    """
    params = {
        "symbol":    symbol,
        "interval":  interval,
        "startTime": start_ms,
        "endTime":   end_ms,
        "limit":     limit,
    }
    async with session.get(
        FAPI_BASE,
        params=params,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as response:
        response.raise_for_status()
        return await response.json()


async def _fetch_range(
    session: aiohttp.ClientSession,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    label: str = "",
) -> list:
    """Fetch all kline bars in ``[start_ms, end_ms]`` across multiple batches.

    Args:
        session:  Active ``aiohttp`` client session.
        symbol:   Trading pair symbol.
        interval: Candle interval string.
        start_ms: Range start in milliseconds since epoch.
        end_ms:   Range end in milliseconds since epoch.
        label:    Optional display label for the progress line.

    Returns:
        Concatenated list of all raw kline rows in the requested range.
    """
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
        tag = f"[{interval}{' ' + label if label else ''}]"
        print(f"\r  {tag} {fetched:,}/{total:,} bars  ({pct:.0f}%)", end="", flush=True)
        await asyncio.sleep(0.05)

    print()
    return rows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def fetch_klines(
    symbol: str,
    interval: str,
    days: int = 365,
) -> pd.DataFrame:
    """Return a DataFrame with ``days`` of kline history for ``symbol``.

    Uses the local CSV cache and only downloads what is missing:

    - **Cache hit**: data already covers the requested window → return immediately.
    - **Forward fill**: new bars available → extend cache forward.
    - **Back-fill**: requested start is earlier than cache start → extend backward.

    Args:
        symbol:   Trading pair (e.g. ``"BTCUSDT"``).
        interval: Candle interval (e.g. ``"5m"``, ``"1h"``).
        days:     Number of calendar days of history to return.

    Returns:
        DataFrame with columns ``open_time``, ``open``, ``high``, ``low``,
        ``close``, ``volume``, ``close_time``, plus the remaining Binance
        kline fields.  The currently-open candle is excluded.
    """
    step_ms = INTERVAL_MS[interval]
    now_ms = int(time.time() * 1000)
    target_start = now_ms - days * 86_400_000
    target_end = now_ms - step_ms  # exclude the currently-open candle

    cache_file = _cache_path(symbol, interval)
    parts: List[pd.DataFrame] = []

    if os.path.exists(cache_file):
        cached_df = _cast_numeric(
            pd.read_csv(cache_file, dtype={"open_time": int, "close_time": int})
        )
        cache_start = int(cached_df["open_time"].iloc[0])
        cache_end = int(cached_df["close_time"].iloc[-1])

        needs_older = target_start < cache_start - step_ms
        needs_newer = (target_end - cache_end) >= step_ms * 3

        if not needs_older and not needs_newer:
            logger.info(f"Cache hit: {interval} ({len(cached_df)} bars)")
            return cached_df[cached_df["open_time"] >= target_start].reset_index(drop=True)

        async with aiohttp.ClientSession() as session:
            if needs_older:
                logger.info(
                    f"Back-filling {interval} "
                    f"{datetime.fromtimestamp(target_start / 1000):%Y-%m-%d} → "
                    f"{datetime.fromtimestamp(cache_start / 1000):%Y-%m-%d}"
                )
                rows = await _fetch_range(
                    session, symbol, interval, target_start, cache_start, label="back-fill"
                )
                if rows:
                    parts.append(_cast_numeric(pd.DataFrame(rows, columns=KLINE_COLS)))

            parts.append(cached_df)

            if needs_newer:
                logger.info(
                    f"Updating {interval} from "
                    f"{datetime.fromtimestamp(cache_end / 1000):%Y-%m-%d %H:%M}"
                )
                rows = await _fetch_range(
                    session, symbol, interval, cache_end + 1, target_end, label="update"
                )
                if rows:
                    parts.append(_cast_numeric(pd.DataFrame(rows, columns=KLINE_COLS)))
    else:
        total_bars = (target_end - target_start) // step_ms
        logger.info(f"Fetching {days}d of {interval} data for {symbol} (~{total_bars:,} bars)")
        async with aiohttp.ClientSession() as session:
            rows = await _fetch_range(session, symbol, interval, target_start, target_end)
        parts.append(_cast_numeric(pd.DataFrame(rows, columns=KLINE_COLS)))

    df = (
        pd.concat(parts, ignore_index=True)
        .drop_duplicates("open_time")
        .sort_values("open_time")
        .reset_index(drop=True)
    )
    df = _cast_numeric(df[df["open_time"] >= target_start].reset_index(drop=True))
    df.to_csv(cache_file, index=False)
    logger.info(f"Saved {len(df):,} {interval} bars → {cache_file}")
    return df


async def fetch_all(
    symbol: str = "BTCUSDT",
    days: int = 365,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch 5m and 1h klines concurrently.

    Args:
        symbol: Trading pair symbol (default ``"BTCUSDT"``).
        days:   Number of calendar days of history to fetch.

    Returns:
        Tuple ``(df_5m, df_1h)`` of DataFrames in chronological order.
    """
    print(f"\nFetching {days}-day historical data for {symbol}…")
    df_5m, df_1h = await asyncio.gather(
        fetch_klines(symbol, "5m", days),
        fetch_klines(symbol, "1h", days),
    )
    print(f"Data ready: 5m={len(df_5m):,} bars  1h={len(df_1h):,} bars\n")
    return df_5m, df_1h


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _df5, _df1 = asyncio.run(fetch_all())
    print(_df5.tail(3))
