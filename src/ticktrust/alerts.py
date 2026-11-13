"""Incident alerts to Slack (or any webhook), with a cooldown so a flapping feed is not spam."""

from __future__ import annotations

import os
from collections.abc import Callable

from .models import Incident, Kind

EMOJI = {
    Kind.DEPEG: "🚨",
    Kind.QUORUM: "🛑",
    Kind.OUTAGE: "📴",
    Kind.STALE: "⏳",
    Kind.FROZEN: "🧊",
    Kind.OUTLIER: "⚠️",
}


def message(event: str, inc: Incident) -> str:
    where = f"{inc.pair}" + (f" · {inc.source}" if inc.source else "")
    if event == "resolved":
        mins = ((inc.end or inc.start) - inc.start) / 60
        return f"✅ Resolved: {inc.kind.value} on {where} after {mins:.1f} min"
    return f"{EMOJI.get(inc.kind, '⚠️')} {inc.kind.value} on {where}: {inc.detail}"


def _post(webhook: str, text: str) -> None:
    import requests

    requests.post(webhook, json={"text": text}, timeout=5)


class Alerter:
    """Callable for Monitor(alert=...). Sends open/resolve messages, one per key per cooldown."""

    def __init__(
        self,
        webhook: str | None = None,
        cooldown_s: float = 300,
        post: Callable[[str, str], None] = _post,
        kinds: set[Kind] | None = None,
    ):
        self.webhook = webhook or os.getenv("TICKTRUST_SLACK_WEBHOOK")
        self.cooldown_s = cooldown_s
        self.post = post
        self.kinds = kinds or set(Kind)
        self.sent: list[str] = []
        self._last: dict[tuple, float] = {}

    def __call__(self, event: str, inc: Incident) -> None:
        if inc.kind not in self.kinds:
            return
        key = (event, inc.kind, inc.pair, inc.source)
        t = inc.end if event == "resolved" and inc.end else inc.start
        if t - self._last.get(key, float("-inf")) < self.cooldown_s:
            return
        self._last[key] = t
        text = message(event, inc)
        self.sent.append(text)
        if self.webhook:
            try:
                self.post(self.webhook, text)
            except Exception:
                pass  # alerting must never take the price service down
