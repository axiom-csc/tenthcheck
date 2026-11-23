"""Hand-off to a person: a webhook (POST JSON, optional HMAC signature) or an email (SMTP + STARTTLS).

The summary is built deterministically from the conversation (no model call): the order facts, what the customer
asked first and last, the actions Panel took or queued in this conversation, and the last messages. Secrets come
from Config and are never logged; the summary contains no API keys or tokens.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import smtplib
import time
from email.message import EmailMessage

from tenthlook.panel.shopify.client import urllib_transport

log = logging.getLogger("panel.handoff")
PUBLIC_FACTS = ("order_id", "item", "purchase_date", "amount", "amount_paid", "on_sale", "has_receipt", "currency", "test")


def summarize(conversation_id: str, order: dict | None, transcript: list[dict], actions: list[dict], reason: str) -> dict:
    users = [m.get("content", "") for m in transcript if m.get("role") == "user"]
    return {"conversation_id": conversation_id, "reason": reason, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "order": {k: order.get(k) for k in PUBLIC_FACTS if order and k in order} if order else None,
            "customer_first_message": users[0] if users else "", "customer_last_message": users[-1] if users else "",
            "actions": [{k: a.get(k) for k in ("type", "amount", "percent", "status", "decision", "error")} for a in actions],
            "last_messages": [{"role": m.get("role"), "content": m.get("content", "")} for m in transcript[-10:]
                              if m.get("role") in ("user", "assistant")]}


def text_of(s: dict) -> str:
    lines = [f"Panel hand-off ({s['reason']}) for conversation {s['conversation_id']}", ""]
    if s.get("order"):
        lines.append("Order: " + ", ".join(f"{k}={v}" for k, v in s["order"].items()))
    lines += [f"First message: {s['customer_first_message']}", f"Last message: {s['customer_last_message']}", "", "Actions:"]
    lines += [f"- {a['type']} {a.get('amount') or a.get('percent') or ''}: {a['status']} {a.get('decision') or ''}".rstrip()
              for a in s["actions"]] or ["- none"]
    lines += ["", "Last messages:"] + [f"{m['role']}: {m['content']}" for m in s["last_messages"]]
    return "\n".join(lines)


class WebhookNotifier:
    def __init__(self, url: str, secret: str | None = None, transport=urllib_transport):
        self.url, self.secret, self.transport = url, secret, transport

    def send(self, summary: dict) -> dict:
        body = json.dumps(summary).encode()
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["X-Panel-Signature"] = "sha256=" + hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()
        status, _ = self.transport("POST", self.url, headers, body)
        if not 200 <= status < 300:
            raise RuntimeError(f"hand-off webhook returned HTTP {status}")
        log.info("hand-off sent by webhook for %s", summary["conversation_id"])
        return {"channel": "webhook", "status": status}


class EmailNotifier:
    def __init__(self, host, port, user, password, sender, to, smtp=smtplib.SMTP):
        self.host, self.port, self.user, self.password, self.sender, self.to, self.smtp = host, int(port), user, password, sender, to, smtp

    def send(self, summary: dict) -> dict:
        msg = EmailMessage()
        msg["Subject"] = f"[Panel] hand-off: {(summary.get('order') or {}).get('order_id', 'no order')} ({summary['reason']})"
        msg["From"], msg["To"] = self.sender, self.to
        msg.set_content(text_of(summary))
        with self.smtp(self.host, self.port, timeout=30) as s:
            s.starttls()
            if self.user:
                s.login(self.user, self.password)
            s.send_message(msg)
        log.info("hand-off sent by email for %s", summary["conversation_id"])
        return {"channel": "email"}


class LogNotifier:
    """No channel configured: the hand-off is only recorded (never claimed as sent to a person)."""

    def send(self, summary: dict) -> dict:
        log.warning("hand-off for %s NOT delivered: no webhook or email configured", summary["conversation_id"])
        raise RuntimeError("no hand-off channel configured")


def notifier_from_config(config) -> object:
    url = config.get("PANEL_HANDOFF_WEBHOOK")
    if url:
        return WebhookNotifier(url, config.get("PANEL_HANDOFF_WEBHOOK_SECRET"))
    if config.get("PANEL_SMTP_HOST"):
        return EmailNotifier(config.get("PANEL_SMTP_HOST"), config.get("PANEL_SMTP_PORT", "587"), config.get("PANEL_SMTP_USER"),
                             config.get("PANEL_SMTP_PASSWORD"), config.require("PANEL_HANDOFF_EMAIL_FROM"),
                             config.require("PANEL_HANDOFF_EMAIL_TO"))
    return LogNotifier()
