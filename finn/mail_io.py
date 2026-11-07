"""Mailbox access: IMAP for incoming, SMTP for replies (works with Gmail + app password)."""
from __future__ import annotations

import email
import html
import imaplib
import logging
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import formataddr, getaddresses, make_msgid, parseaddr

log = logging.getLogger("finn.mail")


@dataclass
class Attachment:
    filename: str
    data: bytes


@dataclass
class IncomingMail:
    message_id: str
    sender_name: str
    sender_addr: str
    subject: str
    body: str
    date: str
    references: str = ""
    cc: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    uid: bytes | None = None
    auto_generated: bool = False


@dataclass
class OutgoingMail:
    to_addr: str
    to_name: str
    subject: str
    text: str
    html: str
    in_reply_to: str
    references: str
    attachments: list[Attachment] = field(default_factory=list)


def parse_message(raw: bytes, uid: bytes | None = None) -> IncomingMail:
    msg = email.message_from_bytes(raw, policy=default_policy)
    reply_to = msg.get("Reply-To")
    name, addr = parseaddr(str(reply_to or msg.get("From", "")))
    if not name:
        name = parseaddr(str(msg.get("From", "")))[0]
    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = ""
    if body_part is not None:
        body = body_part.get_content()
        if body_part.get_content_subtype() == "html":
            import re
            body = html.unescape(re.sub(r"<[^>]+>", " ", body))
    atts = []
    for part in msg.iter_attachments():
        fn = part.get_filename()
        if fn:
            atts.append(Attachment(fn, part.get_payload(decode=True) or b""))
    auto = (str(msg.get("Auto-Submitted", "no")).lower() != "no"
            or str(msg.get("Precedence", "")).lower() in {"bulk", "junk", "auto_reply", "list"}
            or "mailer-daemon" in addr.lower() or "noreply" in addr.lower().replace("-", ""))
    return IncomingMail(
        message_id=str(msg.get("Message-ID", "")).strip(),
        sender_name=name, sender_addr=addr.lower(), subject=str(msg.get("Subject", "")).strip(),
        body=body, date=str(msg.get("Date", "")), references=str(msg.get("References", "")).strip(),
        cc=[a for _, a in getaddresses([str(msg.get("Cc", ""))]) if a],
        attachments=atts, uid=uid, auto_generated=auto,
    )


def build_message(out: OutgoingMail, from_addr: str, from_name: str = "Finn") -> EmailMessage:
    m = EmailMessage()
    m["From"] = formataddr((from_name, from_addr))
    m["To"] = formataddr((out.to_name, out.to_addr)) if out.to_name else out.to_addr
    m["Subject"] = out.subject
    m["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1])
    m["Auto-Submitted"] = "auto-replied"  # prevents reply loops with other bots
    if out.in_reply_to:
        m["In-Reply-To"] = out.in_reply_to
        m["References"] = (out.references + " " + out.in_reply_to).strip()
    m.set_content(out.text)
    m.add_alternative(out.html, subtype="html")
    for a in out.attachments:
        maintype, subtype = ("application", "json") if a.filename.endswith(".json") else ("application", "octet-stream")
        m.add_attachment(a.data, maintype=maintype, subtype=subtype, filename=a.filename)
    return m


class Mailbox:
    def __init__(self, address: str, password: str, imap_host="imap.gmail.com", smtp_host="smtp.gmail.com",
                 smtp_port=465, folder="INBOX"):
        self.address, self.password = address, password
        self.imap_host, self.smtp_host, self.smtp_port, self.folder = imap_host, smtp_host, smtp_port, folder

    def fetch_unseen(self) -> list[IncomingMail]:
        out = []
        with imaplib.IMAP4_SSL(self.imap_host) as im:
            im.login(self.address, self.password)
            im.select(self.folder)
            typ, data = im.uid("search", None, "UNSEEN")
            for uid in (data[0].split() if data and data[0] else []):
                typ, msg_data = im.uid("fetch", uid, "(BODY.PEEK[])")  # PEEK: stays unread until processed
                raw = next((part[1] for part in msg_data if isinstance(part, tuple)), None)
                if raw:
                    out.append(parse_message(raw, uid))
        return out

    def mark_seen(self, uid: bytes) -> None:
        with imaplib.IMAP4_SSL(self.imap_host) as im:
            im.login(self.address, self.password)
            im.select(self.folder)
            im.uid("store", uid, "+FLAGS", "(\\Seen)")

    def send(self, out: OutgoingMail) -> str:
        m = build_message(out, self.address)
        with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port) as s:
            s.login(self.address, self.password)
            s.send_message(m)
        return str(m["Message-ID"])
