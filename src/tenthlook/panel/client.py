"""Minimal OpenAI-compatible chat client (stdlib only). Works with Ollama's `/v1` endpoint.

The API key is held in memory for the run and sent only to the endpoint the user entered.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class ChatClient:
    base_url: str  # e.g. http://127.0.0.1:11434/v1  or  https://api.example.com/v1
    model: str
    api_key: str = ""
    temperature: float = 0.0
    seed: int | None = 0
    max_tokens: int = 400
    timeout: float = 180.0
    keep_alive: str | None = None  # Ollama only: how long the model stays loaded after a request (e.g. "5m")
    json_mode: bool = False  # ask for a JSON object (OpenAI-style response_format; Ollama supports it)

    def complete(self, messages: list[dict]) -> tuple[str, dict]:
        body = {"model": self.model, "messages": messages, "temperature": self.temperature,
                "max_tokens": self.max_tokens, "stream": False}
        if self.seed is not None:
            body["seed"] = self.seed
        if self.keep_alive is not None:
            body["keep_alive"] = self.keep_alive
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(self.base_url.rstrip("/") + "/chat/completions",
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})})
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code} from endpoint: {e.read()[:300]!r}") from None
        choice = data["choices"][0]
        text = choice["message"].get("content") or ""
        return text, {"latency_s": round(time.perf_counter() - t0, 3), "usage": data.get("usage"),
                      "finish_reason": choice.get("finish_reason"), "model_reported": data.get("model")}
