"""LLM layer: reads and writes language, never computes.

Used for (1) understanding the incoming request and (2) writing the short
summary sentence. Works without an API key (deterministic fallbacks), so the
agent never fails because the model is unavailable."""
from __future__ import annotations

import json
import logging
import os
import re

log = logging.getLogger("finn.llm")

MODEL = os.getenv("FINN_MODEL", "claude-sonnet-5-5")


def _client():
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic package not installed – using fallbacks")
        return None
    return anthropic.Anthropic(api_key=key)


def _ask(system: str, user: str, max_tokens: int = 600) -> str | None:
    client = _client()
    if client is None:
        return None
    try:
        msg = client.messages.create(model=MODEL, max_tokens=max_tokens, system=system,
                                     messages=[{"role": "user", "content": user}])
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    except Exception as e:  # noqa: BLE001
        log.warning("LLM call failed (%s) – using fallback", e)
        return None


def _json(text: str | None) -> dict | None:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


# ------------------------------------------------------------------ request understanding
def classify_request(subject: str, body: str, attachments: list[str]) -> dict:
    """Return {'is_covenant_request': bool, 'plan_year': int|None, 'first_name': str|None}."""
    system = ("Du bist Finn, digitaler Referent Finanzierung. Klassifiziere eine eingehende E-Mail. "
              "Antworte NUR mit JSON: {\"is_covenant_request\": bool, \"plan_year\": int|null, "
              "\"sender_salutation\": string|null}. is_covenant_request ist true, wenn um eine Prüfung "
              "von Covenants/Finanzkennzahlen gegen einen Wirtschaftsplan gebeten wird. "
              "sender_salutation: wie der Absender angeredet werden möchte, aus der Signatur abgeleitet "
              "(z. B. 'Frau Dr. Möller' oder 'Katrin'), sonst null.")
    user = f"Betreff: {subject}\nAnhänge: {', '.join(attachments)}\n\n{body[:3000]}"
    data = _json(_ask(system, user, 200))
    fallback = _heuristic(subject, body, attachments)
    if not data:
        return fallback
    return {
        "is_covenant_request": bool(data.get("is_covenant_request", fallback["is_covenant_request"])),
        "plan_year": data.get("plan_year") or fallback["plan_year"],
        "sender_salutation": data.get("sender_salutation"),
        "source": "llm",
    }


def _heuristic(subject: str, body: str, attachments: list[str]) -> dict:
    text = f"{subject} {body} {' '.join(attachments)}".lower()
    is_req = any(k in text for k in ["covenant", "finanzkennzahl", "kennzahlen", "wirtschaftsplan"])
    years = re.findall(r"\b(20[2-4]\d)\b", f"{subject} {' '.join(attachments)} {body}")
    return {"is_covenant_request": is_req, "plan_year": int(years[0]) if years else None,
            "sender_salutation": None, "source": "heuristic"}


# ------------------------------------------------------------------ summary writing
_NUM = re.compile(r"\d+(?:[.,]\d+)*")


def numbers_are_grounded(text: str, facts: dict) -> bool:
    """Guard: every number in the LLM text must already exist in the computed facts."""
    allowed = set(_NUM.findall(json.dumps(facts, ensure_ascii=False)))
    found = _NUM.findall(text)
    bad = [n for n in found if n not in allowed]
    if bad:
        log.warning("LLM summary rejected, ungrounded numbers: %s", bad)
    return not bad


def write_summary(facts: dict, fallback: str) -> tuple[str, str]:
    """Return (summary, source). The LLM gets only pre-formatted facts."""
    system = ("Du bist Finn, digitaler Referent Finanzierung bei Stadtwerken. Schreibe 2–3 sachliche Sätze "
              "auf Deutsch als Ergebnis einer Covenant-Prüfung für die Finanzleitung. Nenne, ob alle Covenants "
              "eingehalten werden, welche am knappsten sind (mit Wert und Headroom) und welcher Vertrag keine "
              "Covenants hat. Verwende AUSSCHLIESSLICH Zahlen, die wörtlich in den Fakten stehen. Rechne nichts. "
              "Keine Anrede, keine Grußformel, kein Markdown.")
    text = _ask(system, json.dumps(facts, ensure_ascii=False), 300)
    if text and numbers_are_grounded(text, facts):
        return text, "llm"
    return fallback, "template"
