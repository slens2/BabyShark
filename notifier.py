# -*- coding: utf-8 -*-
import os
import json
import time
import requests

class Notifier:
    def __init__(self, cfg):
        self._enabled = bool(((cfg.get("discord") or {}).get("enabled", False)))
        # Ưu tiên ENV, fallback config
        env_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
        cfg_url = ((cfg.get("discord") or {}).get("webhook_url") or "").strip()
        self._webhook = env_url or cfg_url

    def enabled(self):
        return self._enabled and bool(self._webhook)

    def text(self, content: str):
        if not self.enabled():
            return
        try:
            payload = {"content": content[:1999]}
            requests.post(self._webhook, json=payload, timeout=8)
        except Exception as e:
            print(f"[NOTIFIER] Error send text: {e!r}")

    def send_file(self, path: str, title: str = ""):
        if not self.enabled():
            return
        try:
            with open(path, "rb") as f:
                files = {"file": (os.path.basename(path), f, "text/plain")}
                data = {"payload_json": json.dumps({"content": title[:1999]})}
                requests.post(self._webhook, data=data, files=files, timeout=15)
        except Exception as e:
            print(f"[NOTIFIER] Error send file: {e!r}")