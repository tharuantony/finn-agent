"""Build the reply mail (HTML + plain text) and the machine-readable audit file.
All numbers are inserted from calculation results; the LLM may only provide the
short summary paragraph, which is checked by llm.numbers_are_grounded()."""
from __future__ import annotations

import html
import json
from dataclasses import dataclass
from decimal import Decimal

from .calc import Result
from .contracts import Contract
from .fmt import de, headroom, op, ratio, teur, threshold
from .plan_reader import Plan


@dataclass
class Report:
    subject_prefix: str
    html: str
    text: str
    audit_json: str
    facts: dict


def facts_for_summary(contracts: list[Contract], results: list[Result], plan: Plan) -> dict:
    """The only information the LLM sees when writing the summary — already formatted."""
    items = []
    for r in results:
        c = r.covenant
        items.append({
            "vertrag": r.contract_id, "covenant": c.name,
            "wert": ratio(r.value, c.unit, c.decimals),
            "grenzwert": f"{op(c.operator)} {threshold(c.threshold, c.unit)}",
            "headroom": headroom(r.headroom, c.unit, c.decimals),
            "eingehalten": r.met, "knapp": r.tight, "puffer": r.buffer_text,
        })
    return {
        "planjahr": plan.year, "stichtag": f"31.12.{plan.year}",
        "planstatus": plan.status,
        "ohne_covenants": [f"{c.id} {c.lender}" for c in contracts if not c.has_covenants],
        "covenants": items,
        "alle_eingehalten": all(r.met for r in results),
        "knappe": [f"{r.contract_id} {r.covenant.name}" for r in results if r.tight],
        "verletzt": [f"{r.contract_id} {r.covenant.name}" for r in results if not r.met],
    }


def template_summary(facts: dict) -> str:
    """Deterministic fallback summary (used without API key or if the LLM output fails the check)."""
    n = len(facts["covenants"])
    if facts["verletzt"]:
        head = f"Auf Basis des Wirtschaftsplans {facts['planjahr']} wird/werden {len(facts['verletzt'])} von {n} Covenants zum {facts['stichtag']} nicht eingehalten: " + ", ".join(facts["verletzt"]) + "."
    else:
        head = f"Alle {n} Financial Covenants werden zum Planstichtag {facts['stichtag']} eingehalten."
    if facts["knappe"]:
        tight = [c for c in facts["covenants"] if c["knapp"]]
        head += " Knapp sind " + " und ".join(f"{c['vertrag']} {c['covenant']} ({c['wert']}, Headroom {c['headroom']})" for c in tight) + "."
    if facts["ohne_covenants"]:
        head += " " + ", ".join(facts["ohne_covenants"]) + " enthält keine Financial Covenants."
    return head


def _e(s) -> str:
    return html.escape(str(s))


def build_report(contracts: list[Contract], results: list[Result], plan: Plan, summary: str,
                 greeting: str, warnings: list[str]) -> Report:
    by_id = {c.id: c for c in contracts}
    rows, foot, calc_blocks, text_rows = [], [], [], []
    n = 0
    ordered: list = []
    for c in contracts:
        if not c.has_covenants:
            ordered.append(c)
        ordered += [r for r in results if r.contract_id == c.id]
    for item in ordered:
        if isinstance(item, Contract):
            c = item
            rows.append(f"<tr><td><div style='color:#666;font-size:11px'>{_e(c.id)} · {_e(c.lender)}</div>"
                        f"<b>keine Financial Covenants</b></td><td colspan='4' style='color:#666' align='center'>–</td>"
                        f"<td style='font-size:11px'>{_e(c.evidence[0].cite())}</td></tr>")
            text_rows.append(f"{c.id} {c.lender}: keine Financial Covenants")
            continue
        r = item
        c, cv = by_id[r.contract_id], r.covenant
        n += 1
        bg = "#FFF4D6" if r.tight else ("#FDE7E9" if not r.met else "")
        status = "✅ ja" if r.met and not r.tight else ("⚠️ ja, knapp" if r.met else "❌ nein")
        thr = f"{op(cv.operator)} {threshold(cv.threshold, cv.unit)}"
        rows.append(
            f"<tr style='background:{bg}'><td><div style='color:#666;font-size:11px'>{_e(c.id)} · {_e(c.lender)}</div>"
            f"<b>{_e(cv.name)}</b><sup>{n}</sup></td>"
            f"<td align='right'><b>{ratio(r.value, cv.unit, cv.decimals)}</b></td><td align='right'>{thr}</td>"
            f"<td align='right'>{headroom(r.headroom, cv.unit, cv.decimals)}</td><td>{status}</td>"
            f"<td style='font-size:11px'>{_e(cv.threshold_source.cite())}</td></tr>")
        text_rows.append(f"{c.id} {cv.name}: {ratio(r.value, cv.unit, cv.decimals)} | Grenze {thr} | "
                         f"Headroom {headroom(r.headroom, cv.unit, cv.decimals)} | {status}")
        cites = "; ".join(f"{s.role}: {s.cite()}" for s in cv.sources)
        plan_refs = sorted({ref for i in r.inputs for ref in i.refs}, key=_ref_key)
        foot.append(f"<li><sup>{n}</sup> <b>{_e(c.file)}</b> – {_e(cites)}. Planwerte: {_e(plan.file)} "
                    f"({_e(_compress(plan_refs))}).</li>")
        calc_blocks.append(_calc_block(n, c, r, plan))

    hints = []
    hints.append(f"Grundlage: Planbilanz zum 31.12.{plan.year} und Erfolgsplan {plan.year} aus <i>{_e(plan.file)}</i>. "
                 f"{_e(plan.status)}" if plan.status else f"Grundlage: {_e(plan.file)}")
    for r in results:
        if r.tight and r.buffer_text:
            hints.append(f"{_e(r.contract_id)} {_e(r.covenant.name)}: {_e(r.buffer_text)}")
    seen_notes = set()
    for r in results:
        pn = r.covenant.plan_basis_note
        if pn and (r.contract_id, pn["ref"]) not in seen_notes:
            seen_notes.add((r.contract_id, pn["ref"]))
            page = f", S. {pn['page']}" if pn.get("page") else ""
            hints.append(f"{_e(r.contract_id)} ({_e(pn['ref'])}{page}): {_e(pn['text'])}")
        if r.margin is not None:
            g = r.covenant.margin_grid
            hints.append(f"{_e(r.contract_id)} Margenraster ({_e(g['ref'])}): Bei einem Verschuldungsgrad von "
                         f"{ratio(r.value, r.covenant.unit, r.covenant.decimals)} läge die Marge bei {de(r.margin, 2)} % p. a. "
                         f"(reine Preiswirkung, kein Kündigungsgrund).")
    for c in contracts:
        if not c.has_covenants:
            hints.append(f"{_e(c.id)} {_e(c.lender)}: {_e(c.no_covenant_reason)} (geprüft: "
                         f"{_e('; '.join(s.cite() for s in c.evidence))})")
    failed = [ch for ch in plan.checks if not ch[1]]
    hints.append("Plausibilitätsprüfung des Wirtschaftsplans: " + (
        "Bilanz ausgeglichen, Darlehensspiegel stimmt mit Bilanz und GuV überein." if not failed
        else "Abweichungen: " + "; ".join(f"{_e(a)} ({_e(b)})" for a, _, b in failed)))
    for w in warnings:
        hints.append(f"Hinweis Fundstelle: {_e(w)}")

    hello = _e(greeting)
    body = f"""<div style="font-family:Calibri,Arial,sans-serif;font-size:14px;color:#1F1F1F;max-width:860px">
<p>{hello}</p>
<p>die Covenant-Prüfung für den Wirtschaftsplan {plan.year} ist abgeschlossen.</p>
<div style="background:#F3F8FD;border-left:3px solid #0F6CBD;padding:8px 12px;margin:0 0 12px"><b>Ergebnis:</b> {_e(summary)}</div>
<table cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-size:13px;border:1px solid #DADADA" border="1">
<thead style="background:#F3F3F3"><tr><th align="left">Vertrag · Covenant</th><th>Plan {plan.year}</th><th>Grenzwert</th><th>Headroom</th><th>eingehalten</th><th align="left">Fundstelle Grenzwert</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<ol style="list-style:none;padding-left:0;font-size:11.5px;color:#555">{''.join(foot)}</ol>
<h4 style="margin:14px 0 6px">Hinweise</h4><ul>{''.join(f'<li>{h}</li>' for h in hints)}</ul>
<h4 style="margin:14px 0 6px">Rechenweg und Quellen</h4>{''.join(calc_blocks)}
<p>Die vollständige Herleitung liegt zusätzlich als JSON-Datei bei.</p>
<p>Mit freundlichen Grüßen</p>
<div style="font-size:12.5px"><b>Finn</b><br>Referent Finanzierung (digital)<br>
<span style="font-size:11px;color:#888">Diese Nachricht wurde automatisch erstellt. Alle Kennzahlen stammen aus einem deterministischen Rechenkern (Code), nicht aus dem Sprachmodell. Werte in T€.</span></div>
</div>"""

    text = (f"{html.unescape(hello)}\n\ndie Covenant-Prüfung für den Wirtschaftsplan {plan.year} ist abgeschlossen.\n\n"
            f"Ergebnis: {summary}\n\n" + "\n".join(text_rows) +
            "\n\nDetails, Fundstellen und Rechenweg siehe HTML-Ansicht bzw. JSON-Anhang.\n\nMit freundlichen Grüßen\nFinn")

    audit = {
        "plan_file": plan.file, "plan_year": plan.year, "plan_status": plan.status,
        "plan_checks": [{"check": a, "ok": b, "detail": c} for a, b, c in plan.checks],
        "contracts_without_covenants": [{"id": c.id, "file": c.file, "reason": c.no_covenant_reason,
                                         "evidence": [s.cite() for s in c.evidence]} for c in contracts if not c.has_covenants],
        "results": [{
            "contract": r.contract_id, "file": by_id[r.contract_id].file, "covenant": r.covenant.name,
            "definition": r.covenant.definition, "formula": r.formula,
            "value_unrounded": str(r.raw), "value": str(r.value), "operator": r.covenant.operator,
            "threshold": r.covenant.threshold, "headroom": str(r.headroom), "met": r.met, "tight": r.tight,
            "contract_sources": [{"ref": s.ref, "page": s.page, "role": s.role, "quote": s.quote} for s in r.covenant.sources],
            "inputs": [{"name": i.name, "amount_teur": str(i.amount), "cells": i.refs, "note": i.note} for i in r.inputs],
            "assumptions": r.assumptions, "sensitivity": r.buffer_text,
            "margin_pct": str(r.margin) if r.margin is not None else None,
        } for r in results],
        "warnings": warnings,
    }
    return Report("AW: ", body, text, json.dumps(audit, ensure_ascii=False, indent=2), {})


def _calc_block(n: int, c: Contract, r: Result, plan: Plan) -> str:
    cv = r.covenant
    lines = []
    for i in r.inputs:
        refs = _compress(i.refs)
        note = f" – {i.note}" if i.note else ""
        strong = i.name.startswith("=")
        nm = f"<b>{_e(i.name)}</b>" if strong else _e(i.name)
        lines.append(f"<tr><td>{nm}{_e(note)}</td><td align='right' style='white-space:nowrap'>{teur(i.amount)}</td>"
                     f"<td style='color:#666;font-size:11px'>{_e(refs)}</td></tr>")
    res = (f"{ratio(r.value, cv.unit, cv.decimals)} {op(cv.operator)} {threshold(cv.threshold, cv.unit)} "
           f"→ Headroom {headroom(r.headroom, cv.unit, cv.decimals)} "
           f"(ungerundet {de(r.raw, 4)}{' %' if cv.unit == '%' else ''}"
           f"{'; Rundung laut Vertrag' if cv.rounding_rule_in_contract else '; Vertrag enthält keine Rundungsregel, Vergleich ungerundet'})")
    ass = "".join(f"<li>{_e(a)}</li>" for a in r.assumptions)
    return f"""<div style="border:1px solid #E1E1E1;border-radius:6px;padding:8px 12px;margin:8px 0;font-size:12.5px">
<b><sup>{n}</sup> {_e(c.id)} · {_e(cv.name)}</b> – {_e(c.file)}<br>
<span style="color:#444">Definition laut Vertrag: {_e(cv.definition)}</span><br>
Formel: <code>{_e(r.formula)}</code>
<table cellpadding="3" style="font-size:12px;margin-top:4px">{''.join(lines)}</table>
<div><b>Ergebnis:</b> {_e(res)}</div>
<ul style="margin:4px 0 0;color:#555;font-size:11.5px">{ass}</ul></div>"""


def _ref_key(ref: str):
    sheet, cell = ref.split("!")
    col = "".join(ch for ch in cell if ch.isalpha())
    row = int("".join(ch for ch in cell if ch.isdigit()))
    return (sheet, col, row)


def _compress(refs: list[str]) -> str:
    """['GuV!D4','GuV!D5','GuV!D6'] -> 'GuV!D4–D6'."""
    if not refs:
        return "berechnet"
    refs = sorted(set(refs), key=_ref_key)
    out, run = [], [refs[0]]
    for ref in refs[1:]:
        s0, c0, r0 = _ref_key(run[-1])
        s1, c1, r1 = _ref_key(ref)
        if s0 == s1 and c0 == c1 and r1 == r0 + 1:
            run.append(ref)
        else:
            out.append(run)
            run = [ref]
    out.append(run)
    parts = []
    for run in out:
        if len(run) == 1:
            parts.append(run[0])
        else:
            parts.append(f"{run[0]}–{run[-1].split('!')[1]}")
    return ", ".join(parts)
