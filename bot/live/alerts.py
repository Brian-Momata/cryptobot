"""Telegram messages (outbound only) and the dead-man's-switch heartbeat.

Sending must never crash the bot or leak the token: failures are logged with the error type only.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request


class Alerts:
    def __init__(self, telegram_token: str = "", chat_id: str = "", healthcheck_url: str = "", log=print):
        self.token, self.chat_id, self.healthcheck_url, self.log = telegram_token, chat_id, healthcheck_url, log

    def send(self, text: str) -> None:
        self.log(f"ALERT: {text}")
        if not (self.token and self.chat_id):
            return
        data = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text}).encode()
        try:
            with urllib.request.urlopen(f"https://api.telegram.org/bot{self.token}/sendMessage", data, timeout=15) as r:
                if not json.load(r).get("ok"):
                    self.log("Telegram send failed: not ok")
        except Exception as e:  # noqa: BLE001 - alerts must never take the bot down
            self.log(f"Telegram send failed: {type(e).__name__}")

    def heartbeat(self, failed: bool = False) -> None:
        """Ping healthchecks.io (or similar). If the pings stop, it alerts you: a dead bot
        can't send its own alert."""
        if not self.healthcheck_url:
            return
        url = self.healthcheck_url.rstrip("/") + ("/fail" if failed else "")
        try:
            urllib.request.urlopen(url, timeout=10).close()
        except Exception as e:  # noqa: BLE001
            self.log(f"Heartbeat failed: {type(e).__name__}")
