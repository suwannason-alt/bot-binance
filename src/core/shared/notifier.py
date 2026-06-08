"""Asynchronous Discord webhook notifier.

Replaces the old (placeholder) Telegram alert config with a Discord Channel
Webhook client built on ``aiohttp``.  Every public coroutine is **fail-safe**:
when the webhook is unset it is a no-op, and any network/HTTP error is logged
and swallowed so a notification failure can never stall or crash the live loop.

Target webhook is read from ``config.DISCORD_WEBHOOK_URL`` (env
``DISCORD_WEBHOOK_URL``).

Senders
-------
``send``               — raw message (plain ``content`` payload).
``send_funnel_report`` — the hourly 1H "Entry funnel" status block, wrapped in a
                         ``​```text`` code block so the column padding renders
                         monospaced in Discord (mirrors the live console logs).
``send_trade_open`` / ``send_trade_closed`` — trade-lifecycle alerts.

This module is **live-path only** — the vectorized backtest never imports it, so
the strategy logic stays identical across both execution paths (CLAUDE.md sync
contract is untouched).
"""
from __future__ import annotations

import logging

import aiohttp

import config

logger = logging.getLogger("notifier")

# Discord rejects a webhook ``content`` longer than 2000 chars with HTTP 400.
# Leave headroom for the code-fence wrapper we add around funnel text.
_MAX_CONTENT = 2000
_CODE_FENCE_BUDGET = _MAX_CONTENT - 16   # ```text\n … \n``` overhead + safety


def _wrap_code_block(body: str) -> str:
    """Wrap ``body`` in a Discord ```text fenced block, truncating if oversized.

    The ```text language tag keeps Discord from word-wrapping/markdown-parsing
    the line, so the funnel's column alignment survives intact.
    """
    if len(body) > _CODE_FENCE_BUDGET:
        body = body[: _CODE_FENCE_BUDGET - 1] + "…"   # ellipsis
    return f"```text\n{body}\n```"


async def send(content: str) -> None:
    """POST a raw ``content`` message to the Discord webhook (fire-and-forget).

    No-op when ``DISCORD_WEBHOOK_URL`` is unset.  Never raises — all transport
    and HTTP errors are logged at WARNING and swallowed.

    Args:
        content: Message text (already formatted; truncated to Discord's limit).
    """
    if not config.DISCORD_ENABLED:
        return
    if len(content) > _MAX_CONTENT:
        content = content[: _MAX_CONTENT - 1] + "…"
    payload = {"content": content}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                config.DISCORD_WEBHOOK_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                # 204 No Content is Discord's success code for webhooks.
                if resp.status >= 400:
                    text = await resp.text()
                    logger.warning(
                        "Discord webhook HTTP %s: %s", resp.status, text[:200]
                    )
    except Exception as exc:  # noqa: BLE001 — never let alerts break the loop
        logger.warning("Discord notify failed: %s", exc)


async def send_funnel_report(header: str, body: str) -> None:
    """Send the hourly 1H "Entry funnel" status report.

    Args:
        header: Markdown header line (rendered normally above the code block).
        body:   Multi-line funnel/context text, rendered inside a ```text block
                so the monospaced column padding lines up.
    """
    await send(f"{header}\n{_wrap_code_block(body)}")


async def send_trade_open(side: str, entry: float, sl: float, tp: float,
                          qty: float, reason: str = "") -> None:
    """Alert that a new position was opened."""
    emoji = "🟢" if side.upper() == "LONG" else "🔴"
    lines = [
        f"{emoji} **Trade OPENED** — {side.upper()} {config.SYMBOL}",
        f"Entry `{entry:,.2f}`  SL `{sl:,.2f}`  TP `{tp:,.2f}`  qty `{qty:.4f}`",
    ]
    if reason:
        lines.append(f"_{reason}_")
    await send("\n".join(lines))


async def send_trade_closed(side: str, close_reason: str, pnl: float,
                            balance: float, win_rate: float) -> None:
    """Alert that the open position was closed (SL/TP/BE/manual)."""
    emoji = "✅" if pnl >= 0 else "❌"
    await send(
        f"{emoji} **Trade CLOSED** [{close_reason}] — {side.upper()} {config.SYMBOL}\n"
        f"PnL `{pnl:+,.2f}`  balance `{balance:,.2f}`  win-rate `{win_rate:.1f}%`"
    )
