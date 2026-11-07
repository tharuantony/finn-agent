"""Finn command line.

  python -m finn.main run              poll the mailbox and answer new requests (Ctrl+C to stop)
  python -m finn.main once             process unread mails once and exit
  python -m finn.main eml FILE.eml     offline: process a saved .eml, write reply to out/ (no sending)
  python -m finn.main check FILE.xlsx  print the covenant table for a plan file
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from .mail_io import Attachment, IncomingMail, Mailbox, build_message, parse_message
from .pipeline import ROOT, process

log = logging.getLogger("finn")
STATE = ROOT / ".finn_state.json"


def _load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _state() -> set[str]:
    return set(json.loads(STATE.read_text())) if STATE.exists() else set()


def _save_state(done: set[str]):
    STATE.write_text(json.dumps(sorted(done)))


def _mailbox() -> Mailbox:
    addr, pw = os.getenv("FINN_EMAIL"), os.getenv("FINN_APP_PASSWORD")
    if not addr or not pw:
        sys.exit("FINN_EMAIL und FINN_APP_PASSWORD fehlen – siehe .env.example")
    return Mailbox(addr, pw.replace(" ", ""), os.getenv("FINN_IMAP_HOST", "imap.gmail.com"),
                   os.getenv("FINN_SMTP_HOST", "smtp.gmail.com"), int(os.getenv("FINN_SMTP_PORT", "465")))


def _allowed(mail: IncomingMail) -> bool:
    allow = [a.strip().lower() for a in os.getenv("FINN_ALLOWED_SENDERS", "").split(",") if a.strip()]
    return not allow or any(mail.sender_addr == a or mail.sender_addr.endswith("@" + a.lstrip("@")) for a in allow)


def poll_once(mb: Mailbox) -> int:
    done = _state()
    handled = 0
    for mail in mb.fetch_unseen():
        key = mail.message_id or (mail.uid or b"").decode()
        if key in done:
            mb.mark_seen(mail.uid)
            continue
        if not _allowed(mail):
            log.info("skip: sender %s not in FINN_ALLOWED_SENDERS", mail.sender_addr)
            mb.mark_seen(mail.uid)
            continue
        try:
            reply = process(mail, mb.address)
            if reply:
                mid = mb.send(reply)
                log.info("📤 Antwort an %s gesendet (%s)", reply.to_addr, mid)
                handled += 1
        except Exception:  # noqa: BLE001 – never crash the loop on one bad mail
            log.exception("Fehler bei Mail %s", key)
        finally:
            done.add(key)
            _save_state(done)
            mb.mark_seen(mail.uid)
    return handled


def main(argv=None):
    _load_env()
    logging.basicConfig(level=os.getenv("FINN_LOG_LEVEL", "INFO"), format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(prog="finn")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("once")
    e = sub.add_parser("eml")
    e.add_argument("file")
    c = sub.add_parser("check")
    c.add_argument("file")
    c.add_argument("--year", type=int, default=2027)
    a = ap.parse_args(argv)

    if a.cmd == "check":
        mail = IncomingMail("<local>", "", "local@example.com", "Covenant-Prüfung Wirtschaftsplan", "", "",
                            attachments=[Attachment(Path(a.file).name, Path(a.file).read_bytes())])
        out = process(mail, "finn@example.com")
        print(out.text)
        return
    if a.cmd == "eml":
        mail = parse_message(Path(a.file).read_bytes())
        out = process(mail, os.getenv("FINN_EMAIL", "finn@example.com"))
        if out is None:
            print("Mail übersprungen (eigene/automatische Mail).")
            return
        od = ROOT / "out"
        od.mkdir(exist_ok=True)
        (od / "reply.html").write_text(out.html, encoding="utf-8")
        (od / "reply.eml").write_bytes(bytes(build_message(out, os.getenv("FINN_EMAIL", "finn@example.com"))))
        for att in out.attachments:
            (od / att.filename).write_bytes(att.data)
        print(f"Antwort geschrieben: {od/'reply.html'}, {od/'reply.eml'}")
        return

    mb = _mailbox()
    if a.cmd == "once":
        print(f"{poll_once(mb)} Auftrag/Aufträge beantwortet")
        return
    interval = int(os.getenv("FINN_POLL_SECONDS", "20"))
    log.info("Finn läuft – überwacht %s alle %ss (Ctrl+C beendet)", mb.address, interval)
    backoff = interval
    while True:
        try:
            poll_once(mb)
            backoff = interval
        except KeyboardInterrupt:
            raise
        except Exception as ex:  # noqa: BLE001 – network hiccups: retry with backoff
            log.warning("Postfach nicht erreichbar (%s) – neuer Versuch in %ss", ex, backoff)
            backoff = min(backoff * 2, 300)
        time.sleep(backoff)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nFinn beendet.")
