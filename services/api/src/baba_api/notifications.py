"""Outbound notification adapters.

Each `kind` (webhook, slack, telegram, smtp) has its own `send_*` function
that takes a decrypted config dict + a payload and returns nothing on
success or raises with a descriptive message on failure. The router
catches the exception and surfaces it to the operator + records it in
`notification_channels.last_error` for diagnostic context.

We deliberately keep this module small + dependency-light: httpx is
already pulled in for ai_provider and go2rtc, and SMTP uses the stdlib.
No celery/queue: the v1 rule engine will fire deliveries inline from
event-manager; if latency becomes an issue we can add a worker tier
later without changing this adapter shape.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.mime.text import MIMEText
from email.utils import formatdate
from typing import Any

import httpx

log = logging.getLogger(__name__)


# Common request timeout for all HTTP-based channels. Five seconds is
# slow enough to traverse a residential VPN once and short enough that
# a stuck webhook doesn't pile up across a notification burst.
_HTTP_TIMEOUT = 5.0


class NotificationError(Exception):
    """Raised on a send failure. Caller logs + records the message."""


async def send_webhook(config: dict[str, Any], payload: dict[str, Any]) -> None:
    """Generic JSON POST. Operator supplies the URL and any custom headers."""
    url = config.get("url")
    if not url:
        raise NotificationError("webhook config missing 'url'")
    method = (config.get("method") or "POST").upper()
    headers = dict(config.get("headers") or {})
    headers.setdefault("content-type", "application/json")
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.request(method, url, headers=headers, json=payload)
    except httpx.HTTPError as e:
        raise NotificationError(f"webhook network error: {e}") from e
    if r.status_code >= 400:
        raise NotificationError(
            f"webhook returned {r.status_code}: {r.text[:200]}",
        )


async def send_slack(config: dict[str, Any], payload: dict[str, Any]) -> None:
    """Slack Incoming Webhook — same JSON shape as the official endpoint
    accepts. We don't normalise to blocks/attachments; the message body
    is a single string in `text`. Operator picks the channel via the
    URL itself (Slack ties webhook URL → channel)."""
    url = config.get("url")
    if not url or not url.startswith("https://hooks.slack.com/"):
        raise NotificationError(
            "slack config requires 'url' starting with https://hooks.slack.com/",
        )
    body = {"text": _format_message(payload)}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.post(url, json=body)
    except httpx.HTTPError as e:
        raise NotificationError(f"slack network error: {e}") from e
    if r.status_code != 200:
        raise NotificationError(
            f"slack returned {r.status_code}: {r.text[:200]}",
        )


async def send_telegram(config: dict[str, Any], payload: dict[str, Any]) -> None:
    """Telegram Bot API. Requires bot_token + chat_id from BotFather +
    a chat the bot is a member of. Uses sendMessage with HTML parse
    mode — keeps formatting consistent with Slack's plain-text approach
    while still allowing **bold** for emphasis if the message contains it."""
    token = config.get("bot_token")
    chat_id = config.get("chat_id")
    if not token or not chat_id:
        raise NotificationError("telegram config needs 'bot_token' + 'chat_id'")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = {
        "chat_id": chat_id,
        "text": _format_message(payload),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.post(url, json=body)
    except httpx.HTTPError as e:
        raise NotificationError(f"telegram network error: {e}") from e
    if r.status_code != 200:
        # Telegram returns a JSON error body even on 200; check both
        # status and the `ok` field for full diagnostic.
        raise NotificationError(
            f"telegram returned {r.status_code}: {r.text[:200]}",
        )
    try:
        body_json = r.json()
        if not body_json.get("ok", False):
            raise NotificationError(
                f"telegram api error: {body_json.get('description', '?')}",
            )
    except ValueError as e:
        raise NotificationError(f"telegram returned non-JSON body: {e}") from e


def send_smtp_blocking(config: dict[str, Any], payload: dict[str, Any]) -> None:
    """SMTP send. Blocking — caller is expected to run via asyncio.to_thread.
    smtplib is stdlib, ssl too; no extra dep for the most common deployment
    pattern (operator has an existing SMTP relay)."""
    host = config.get("host")
    port = int(config.get("port") or 587)
    username = config.get("username")
    password = config.get("password")
    from_addr = config.get("from")
    to_addr = config.get("to")
    use_tls = bool(config.get("use_tls", True))
    if not host or not from_addr or not to_addr:
        raise NotificationError("smtp config needs 'host', 'from', 'to'")

    msg = MIMEText(_format_message(payload), "plain", "utf-8")
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = payload.get("subject") or "BABA notification"
    msg["Date"] = formatdate(localtime=True)

    try:
        if use_tls and port == 465:
            # Implicit TLS: connect with SMTP_SSL, no STARTTLS needed.
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=10, context=ctx) as s:
                if username and password:
                    s.login(username, password)
                s.send_message(msg)
        else:
            # STARTTLS path or plain SMTP. Most modern relays want STARTTLS
            # on 587; some lab setups bypass entirely on 25.
            with smtplib.SMTP(host, port, timeout=10) as s:
                if use_tls:
                    s.starttls(context=ssl.create_default_context())
                if username and password:
                    s.login(username, password)
                s.send_message(msg)
    except (OSError, smtplib.SMTPException) as e:
        raise NotificationError(f"smtp send failed: {e}") from e


def _format_message(payload: dict[str, Any]) -> str:
    """Render a payload to plain text. For now the rule engine isn't
    live, so the only payloads are operator-initiated test messages —
    keep it simple. Once the rule engine fires real events we'll grow
    a templating layer (Jinja or just a dict-key registry)."""
    title = payload.get("title") or "BABA"
    body = payload.get("body") or ""
    extras = []
    for k, v in payload.items():
        if k in ("title", "body", "subject"):
            continue
        if isinstance(v, (str, int, float, bool)):
            extras.append(f"{k}: {v}")
    parts = [title]
    if body:
        parts.append("")
        parts.append(body)
    if extras:
        parts.append("")
        parts.extend(extras)
    return "\n".join(parts)


# Single dispatch point. Async fns awaited directly; the smtp one is
# offloaded to a thread because smtplib is blocking and we don't want
# a stuck SMTP relay to peg the event loop.
async def dispatch(kind: str, config: dict[str, Any], payload: dict[str, Any]) -> None:
    if kind == "webhook":
        await send_webhook(config, payload)
    elif kind == "slack":
        await send_slack(config, payload)
    elif kind == "telegram":
        await send_telegram(config, payload)
    elif kind == "smtp":
        import asyncio

        await asyncio.to_thread(send_smtp_blocking, config, payload)
    else:
        raise NotificationError(f"unknown channel kind {kind!r}")
