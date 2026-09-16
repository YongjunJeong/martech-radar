"""Pushing new signals to a webhook.

Two rules keep this from becoming spam. A signal is only ever sent once:
its identity — kind, company, vendors, and the run the change started in —
is remembered in the database, so a migration that stays visible for six
weekly runs produces one message, not six. And nothing here may hurt the
batch: a webhook that is down is a warning line, never an exception, and
the identity is recorded only after the send succeeded, so a failed
delivery is retried on the next batch instead of being silently lost.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, UTC
from typing import Any
from collections.abc import Callable

from . import config as cfg
from . import signals as sig
from .store import Store
from .strings import translator

Transport = Callable[[str, dict[str, Any]], None]


def signal_key(signal: sig.Signal) -> str:
    """Stable identity: the same event keeps the same key across runs.

    `since_run` is part of it on purpose — a vendor removed, re-adopted
    and removed again is two events, and both deserve a message.
    """
    since = min((c.since_run or 0 for c in signal.changes), default=0)
    return f"{signal.kind}:{signal.target_id}:{','.join(signal.vendors)}:{since}"


def _post_json(webhook: str, payload: dict[str, Any]) -> None:
    request = urllib.request.Request(
        webhook, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=10):
        pass


def _slack_payload(found: list[sig.Signal], t, max_items: int) -> dict[str, Any]:
    lines = [f"*{t('notify.header', n=len(found))}*"]
    for signal in found[:max_items]:
        lines.append(f"• [{signal.kind}] {signal.company} ({signal.industry}) — "
                     f"{signal.headline}")
    if len(found) > max_items:
        lines.append(t("notify.more", n=len(found) - max_items))
    return {"text": "\n".join(lines)}


def notify_new_signals(store: Store, language: str | None = None,
                       webhook: str | None = None,
                       dry_run: bool = False,
                       transport: Transport | None = None,
                       progress: Callable[[str], None] | None = None,
                       ) -> dict[str, Any]:
    """Send every not-yet-notified signal of the configured kinds.

    Returns {"pending": n, "sent": n, "error": ...}; never raises for
    delivery problems. With `dry_run`, nothing is sent or recorded.
    """
    say = progress or (lambda _msg: None)
    settings = cfg.load()
    notify_cfg = settings.notify
    language = language or settings.ui.language
    webhook = webhook if webhook is not None else notify_cfg.webhook
    t = translator(language)

    wanted = set(notify_cfg.kinds) & set(sig.PRIORITY)
    unknown = set(notify_cfg.kinds) - set(sig.PRIORITY)
    if unknown:
        say(f"  [notify] ignoring unknown kinds: {sorted(unknown)}")

    already = store.notified_keys()
    found = [s for s in sig.all_signals(store, language=language)
             if s.kind in wanted and signal_key(s) not in already]
    report: dict[str, Any] = {"pending": len(found), "sent": 0}
    if not found:
        return report
    if dry_run:
        for signal in found:
            say(f"  [notify] would send: [{signal.kind}] {signal.company}")
        return report
    if not webhook:
        report["error"] = "no webhook configured"
        return report

    if notify_cfg.format == "json":
        payload: dict[str, Any] = {"signals": [s.as_dict() for s in found[:100]]}
    else:
        payload = _slack_payload(found, t, notify_cfg.max_items)

    send = transport or _post_json
    try:
        send(webhook, payload)
    except Exception as exc:  # noqa: BLE001 — a dead webhook must not kill a batch
        report["error"] = f"{type(exc).__name__}: {exc}"
        say(f"  [notify] delivery failed, will retry next batch: {report['error']}")
        return report

    # Recorded only now: a failed delivery above stays pending.
    store.record_notified(
        [(signal_key(s), s.kind, s.target_id) for s in found],
        sent_at=datetime.now(UTC).isoformat())
    report["sent"] = len(found)
    say(f"  [notify] sent {len(found)} new signal(s)")
    return report
