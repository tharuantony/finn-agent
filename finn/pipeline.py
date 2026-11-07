"""End-to-end flow for one incoming mail: understand → read contracts → read plan → calculate → reply."""
from __future__ import annotations

import html
import logging
import re
import tempfile
from pathlib import Path

from . import llm
from .calc import dscr_self_test, evaluate
from .contracts import RegisterError, load_register
from .mail_io import Attachment, IncomingMail, OutgoingMail
from .plan_reader import PlanError, read_plan
from .report import build_report, facts_for_summary, template_summary

log = logging.getLogger("finn")

ROOT = Path(__file__).resolve().parent.parent


def _reply_subject(subject: str) -> str:
    return subject if re.match(r"^(aw|re|antw):", subject, re.I) else f"AW: {subject}"


def _quote(mail: IncomingMail) -> str:
    body = html.escape(mail.body.strip()).replace("\n", "<br>")
    return (f"<div style='border-top:1px solid #E1E1E1;margin-top:16px;padding-top:10px;font-family:Calibri,Arial;"
            f"font-size:12.5px;color:#444'><b>Von:</b> {html.escape(mail.sender_name)} &lt;{html.escape(mail.sender_addr)}&gt;<br>"
            f"<b>Gesendet:</b> {html.escape(mail.date)}<br><b>Betreff:</b> {html.escape(mail.subject)}<br><br>{body}</div>")


def greeting(mail: IncomingMail, salutation: str | None = None) -> str:
    if salutation:
        return f"Hallo {salutation},"
    return f"Guten Tag {mail.sender_name}," if mail.sender_name else "Guten Tag,"


def _simple_reply(mail: IncomingMail, text: str) -> OutgoingMail:
    hello = greeting(mail)
    full = f"{hello}\n\n{text}\n\nViele Grüße\nFinn"
    return OutgoingMail(mail.sender_addr, mail.sender_name, _reply_subject(mail.subject), full,
                        "<div style='font-family:Calibri,Arial;font-size:14px'>" + html.escape(full).replace("\n", "<br>")
                        + "</div>" + _quote(mail), mail.message_id, mail.references)


def process(mail: IncomingMail, own_address: str, contracts_dir: Path | None = None,
            register: Path | None = None) -> OutgoingMail | None:
    contracts_dir = contracts_dir or ROOT / "contracts"
    register = register or ROOT / "covenants.json"

    if mail.sender_addr == own_address.lower() or mail.auto_generated:
        log.info("skip: own or auto-generated mail from %s", mail.sender_addr)
        return None

    log.info("📥 Auftrag von %s: '%s' (%d Anhang/Anhänge)", mail.sender_addr, mail.subject, len(mail.attachments))
    xlsx = [a for a in mail.attachments if a.filename.lower().endswith((".xlsx", ".xlsm"))]
    intent = llm.classify_request(mail.subject, mail.body, [a.filename for a in mail.attachments])
    log.info("   Auftrag erkannt: %s (via %s)", intent, intent.get("source"))

    if not xlsx:
        if not intent["is_covenant_request"]:
            return _simple_reply(mail, "ich bin Finn und prüfe Financial Covenants. Schicke mir bitte einen "
                                       "Wirtschaftsplan als Excel-Anhang (z. B. Wirtschaftsplan_2027.xlsx) mit dem Betreff "
                                       "„Covenant-Prüfung Wirtschaftsplan 2027“.")
        return _simple_reply(mail, "für die Covenant-Prüfung brauche ich den Wirtschaftsplan als Excel-Datei (.xlsx) "
                                   "im Anhang. In deiner Mail habe ich keine Excel-Datei gefunden. Schick sie mir bitte noch einmal.")
    if len(xlsx) > 1:
        preferred = [a for a in xlsx if "wirtschaftsplan" in a.filename.lower()]
        xlsx = preferred or xlsx
        log.info("   mehrere Excel-Anhänge, verwende %s", xlsx[0].filename)
    att = xlsx[0]
    year = intent.get("plan_year") or _year_from_name(att.filename) or 2027

    try:
        contracts, warnings = load_register(register, contracts_dir)
        log.info("📂 %d Verträge gelesen, %d mit Covenants, %d Fundstellen-Warnungen",
                 len(contracts), sum(c.has_covenants for c in contracts), len(warnings))
        for c in contracts:
            for cv in c.covenants:
                if cv.self_test:
                    got, exp = dscr_self_test(cv)
                    if got != exp:
                        raise RegisterError(f"Selbsttest {c.id}: Beispielrechnung ergibt {got}, Vertrag sagt {exp}")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / Path(att.filename).name
            p.write_bytes(att.data)
            plan = read_plan(p, year)
        log.info("📊 Wirtschaftsplan %s gelesen (Plan %s), Prüfungen: %s", plan.file, year,
                 ", ".join(f"{n}={'ok' if ok else 'ABWEICHUNG'}" for n, ok, _ in plan.checks))
        results = [evaluate(c, cv, plan) for c in contracts for cv in c.covenants]
        for r in results:
            log.info("🧮 %s %-34s %s (Headroom %s) %s", r.contract_id, r.covenant.name, r.value, r.headroom,
                     "OK" if r.met else "VERLETZT")
    except (PlanError, RegisterError, KeyError, ValueError, ZeroDivisionError) as e:
        log.exception("Prüfung fehlgeschlagen")
        return _simple_reply(mail, f"die Covenant-Prüfung konnte ich leider nicht abschließen:\n\n{e}\n\n"
                                   "Bitte prüfe die Datei (Blätter GuV, Bilanz, Finanzierung; Spalte Plan "
                                   f"{year}) und schicke sie erneut. Es wurden keine Zahlen geschätzt.")

    facts = facts_for_summary(contracts, results, plan)
    summary, src = llm.write_summary(facts, template_summary(facts))
    log.info("✍️  Zusammenfassung via %s", src)
    rep = build_report(contracts, results, plan, summary, greeting(mail, intent.get("sender_salutation")), warnings)
    audit_name = f"Covenant-Pruefung_Plan{plan.year}.json"
    return OutgoingMail(
        to_addr=mail.sender_addr, to_name=mail.sender_name, subject=_reply_subject(mail.subject),
        text=rep.text, html=rep.html + _quote(mail), in_reply_to=mail.message_id, references=mail.references,
        attachments=[Attachment(audit_name, rep.audit_json.encode("utf-8"))],
    )


def _year_from_name(name: str) -> int | None:
    m = re.search(r"(20[2-4]\d)", name)
    return int(m.group(1)) if m else None
