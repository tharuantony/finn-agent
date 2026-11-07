"""Deterministic calculation core. No LLM in here.

One function per contract definition. Each returns a Result holding the value,
the formula as text and every input with its Excel cell reference, so the reply
can show the full calculation path."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from .contracts import Contract, Covenant
from .fmt import teur
from .plan_reader import Plan, Value

ZERO = Decimal(0)


def round_half_up(x: Decimal, decimals: int) -> Decimal:
    return x.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)


@dataclass
class Input:
    name: str
    amount: Decimal
    refs: list[str]  # Excel cell references, e.g. ["GuV!D4"]
    note: str = ""


@dataclass
class Result:
    contract_id: str
    covenant: Covenant
    raw: Decimal                    # unrounded ratio (percent for % covenants)
    value: Decimal                  # rounded per contract
    inputs: list[Input]
    formula: str
    assumptions: list[str] = field(default_factory=list)
    headroom: Decimal = ZERO
    met: bool = False
    tight: bool = False
    buffer_text: str = ""
    margin: Decimal | None = None


def _sum(values: list[Value]) -> Decimal:
    return sum((v.amount for v in values), ZERO)


# ---------------------------------------------------------------- building blocks
def ebitda(plan: Plan) -> tuple[Decimal, list[Input], list[str]]:
    """EBITDA as defined in V3 Anlage 3 Abs. 2 and V4 § 1 (identical wording):
    net income + income taxes + interest expense − interest income + depreciation
    ± extraordinary items (§ 285 Nr. 31 HGB)."""
    g = plan.guv
    ni = plan.net_income()
    gv_refs = [g[k].ref for k in ["umsatz", "eigenleistungen", "sbe", "material", "personal", "afa",
                                  "sba", "zinsertrag", "zinsaufwand", "steuern"]]
    taxes = -g["steuern"].amount
    int_exp = -g["zinsaufwand"].amount
    int_inc = g["zinsertrag"].amount
    dep = -g["afa"].amount
    value = ni + taxes + int_exp - int_inc + dep
    inputs = [
        Input("Jahresüberschuss (im Code aus den GuV-Positionen berechnet)", ni, gv_refs),
        Input("+ Steuern vom Einkommen und vom Ertrag", taxes, [g["steuern"].ref]),
        Input("+ Zinsen und ähnliche Aufwendungen (inkl. Aufzinsung)", int_exp, [g["zinsaufwand"].ref]),
        Input("− Sonstige Zinsen und ähnliche Erträge", int_inc, [g["zinsertrag"].ref]),
        Input("+ Abschreibungen", dep, [g["afa"].ref]),
    ]
    assumptions = ["Der Wirtschaftsplan weist keine außerordentlichen Posten (§ 285 Nr. 31 HGB) aus; Bereinigung = 0."]
    return value, inputs, assumptions


# ---------------------------------------------------------------- covenant formulas
def economic_equity_ratio(plan: Plan, cov: Covenant) -> Result:
    share = Decimal(cov.params.get("sonderposten_share", "0.5"))
    eq = plan.equity()
    sp = plan.bilanz["sonderposten"].amount
    ta = plan.total_assets()
    raw = (eq + share * sp) / ta * 100
    inputs = [
        Input("Eigenkapital (A.)", eq, [v.ref for v in plan.equity_values()]),
        Input(f"+ {share * 100:.0f} % Sonderposten", share * sp, [plan.bilanz["sonderposten"].ref],
              f"Sonderposten gesamt {teur(sp)}"),
        Input("Bilanzsumme", ta, [v.ref for v in plan.asset_values()]),
    ]
    return Result("", cov, raw, raw, inputs,
                  "(Eigenkapital + 50 % Sonderposten) / Bilanzsumme",
                  ["Gesellschafterdarlehen (nachrangig) bleibt außen vor (§ 1 Abs. 1)."])


def equity_ratio(plan: Plan, cov: Covenant) -> Result:
    eq = plan.equity()
    ta = plan.total_assets()
    raw = eq / ta * 100
    inputs = [
        Input("Eigenkapital (A.)", eq, [v.ref for v in plan.equity_values()]),
        Input("Bilanzsumme", ta, [v.ref for v in plan.asset_values()]),
    ]
    return Result("", cov, raw, raw, inputs, "Eigenkapital / Bilanzsumme",
                  ["Sonderposten und Gesellschafterdarlehen sind laut Vertrag kein Eigenkapital."])


def dscr(plan: Plan, cov: Covenant) -> Result:
    e, e_inputs, assumptions = ebitda(plan)
    taxes = -plan.guv["steuern"].amount
    # Interest on financial debt: per-loan interest from the loan schedule (excludes pension unwinding by construction)
    interest = _sum([l.interest for l in plan.loans])
    scheduled = _sum([l.scheduled_repayment for l in plan.loans])
    special = _sum([l.special_repayment for l in plan.loans])
    debt_service = interest + scheduled
    raw = (e - taxes) / debt_service
    inputs = e_inputs + [
        Input("= EBITDA", e, []),
        Input("− Ertragsteuern", taxes, [plan.guv["steuern"].ref]),
        Input("Zinsaufwand für Finanzverbindlichkeiten (alle Darlehen inkl. Gesellschafterdarlehen)", interest,
              [l.interest.ref for l in plan.loans],
              f"entspricht GuV-Zinsaufwand {teur(-plan.guv['zinsaufwand'].amount)} − Aufzinsung Pensionen {teur(-plan.guv['aufzinsung'].amount)}"),
        Input("+ planmäßige Tilgungen", scheduled, [l.scheduled_repayment.ref for l in plan.loans]),
        Input("= Schuldendienst", debt_service, []),
    ]
    assumptions += [
        f"Sondertilgungen ({teur(special)}) sind laut Anlage 3 Abs. 4 lit. b nicht Teil des Schuldendienstes.",
        f"Aufzinsung der Pensionsrückstellungen ({teur(-plan.guv['aufzinsung'].amount)}) ist nicht enthalten (Anlage 3 Abs. 4 lit. a).",
        "Das Gesellschafterdarlehen zählt trotz Rangrücktritt zu den Finanzverbindlichkeiten (Anlage 3 Abs. 3).",
        "Tilgungen der Altdarlehen sind laut Wirtschaftsplan vollständig planmäßige Annuitätentilgung.",
    ]
    return Result("", cov, raw, raw, inputs, "(EBITDA − Ertragsteuern) / (Zinsen + planmäßige Tilgung)", assumptions)


def net_leverage(plan: Plan, cov: Covenant) -> Result:
    e, e_inputs, assumptions = ebitda(plan)
    bank = plan.bilanz["vb_kreditinstitute"]
    sh = plan.bilanz["vb_gesellschafter"]
    cash = plan.bilanz["kasse"]
    sub_note = plan.notes.get("subordination")
    sh_subordinated = sub_note is not None and "freiem Vermögen" in sub_note.label
    exclude = cov.params.get("exclude_subordinated_shareholder_loans", True) and sh_subordinated
    fin_debt = bank.amount + (ZERO if exclude else sh.amount)
    cash_free = "cash_free" in plan.notes
    liquid = cash.amount if cash_free else ZERO
    net_debt = fin_debt - liquid
    raw = net_debt / e
    inputs = [
        Input("Verbindlichkeiten gegenüber Kreditinstituten", bank.amount, [bank.ref]),
        Input("Gesellschafterdarlehen (nachrangig, ausgenommen)" if exclude else "+ Gesellschafterdarlehen",
              ZERO if exclude else sh.amount, [sh.ref] + ([sub_note.ref] if sub_note else []),
              f"Buchwert {teur(sh.amount)}"),
        Input("− Liquide Mittel (frei verfügbar)", liquid, [cash.ref] + ([plan.notes["cash_free"].ref] if cash_free else [])),
        Input("= Nettofinanzverbindlichkeiten", net_debt, []),
    ] + e_inputs + [Input("= EBITDA", e, [])]
    if exclude:
        assumptions.append(f"Gesellschafterdarlehen ({teur(sh.amount)}) ist ein Nachrangiges Gesellschafterdarlehen "
                           f"(qualifizierter Rangrücktritt § 39 Abs. 2 InsO, Zahlung nur aus freiem Vermögen, "
                           f"siehe {sub_note.ref}) und daher keine Finanzverbindlichkeit.")
    else:
        assumptions.append("Kein Nachweis für Nachrangigkeit gefunden – Gesellschafterdarlehen wurde einbezogen (konservativ).")
    if not cash_free:
        assumptions.append("Keine Angabe zur freien Verfügbarkeit der liquiden Mittel – konservativ nicht abgezogen.")
    assumptions.append("Keine Finanzierungsleasingverhältnisse (nur Operating Leasing, Bilanz Erläuterung 4).")
    return Result("", cov, raw, raw, inputs, "(Finanzverbindlichkeiten − liquide Mittel) / EBITDA", assumptions)


CALCS = {
    "economic_equity_ratio": economic_equity_ratio,
    "equity_ratio": equity_ratio,
    "dscr": dscr,
    "net_leverage": net_leverage,
}

TIGHT_SHARE = Decimal("0.05")  # headroom < 5 % of the threshold => "knapp"


def evaluate(contract: Contract, cov: Covenant, plan: Plan) -> Result:
    if cov.calc not in CALCS:
        raise KeyError(f"Keine Berechnungsfunktion für '{cov.calc}' ({contract.id})")
    r = CALCS[cov.calc](plan, cov)
    r.contract_id = contract.id
    r.value = round_half_up(r.raw, cov.decimals)
    threshold = Decimal(cov.threshold)
    if cov.operator == ">=":
        r.headroom = r.value - threshold
        r.met = r.value >= threshold
    elif cov.operator == "<=":
        r.headroom = threshold - r.value
        r.met = r.value <= threshold
    else:
        raise ValueError(f"Unbekannter Operator {cov.operator}")
    r.tight = r.met and r.headroom < threshold * TIGHT_SHARE
    r.buffer_text = _buffer(cov, plan, r, threshold)
    if cov.margin_grid:
        for step in cov.margin_grid["steps"]:
            if step["max"] is None or r.value <= Decimal(step["max"]):
                r.margin = Decimal(step["margin"])
                break
    return r


def _buffer(cov: Covenant, plan: Plan, r: Result, threshold: Decimal) -> str:
    """Plain-language sensitivity: how much may the driver move before the threshold is hit."""
    ta = plan.total_assets()
    if cov.calc == "equity_ratio":
        b = plan.equity() - threshold / 100 * ta
        return f"Eigenkapital-Puffer bis zur Schwelle (bei unveränderter Bilanzsumme): {teur(b)}"
    if cov.calc == "economic_equity_ratio":
        share = Decimal(cov.params.get("sonderposten_share", "0.5"))
        b = plan.equity() + share * plan.bilanz["sonderposten"].amount - threshold / 100 * ta
        return f"Puffer im wirtschaftlichen Eigenkapital (bei unveränderter Bilanzsumme): {teur(b)}"
    if cov.calc == "net_leverage":
        e, _, _ = ebitda(plan)
        net_debt = r.raw * e
        e_min = net_debt / threshold
        return f"Ein EBITDA-Rückgang um rd. {teur(e - e_min)} (sonst unverändert) würde die Schwelle erreichen."
    if cov.calc == "dscr":
        e, _, _ = ebitda(plan)
        taxes = -plan.guv["steuern"].amount
        ds = (e - taxes) / r.raw
        e_min = threshold * ds + taxes
        return f"Ein EBITDA-Rückgang um rd. {teur(e - e_min)} (sonst unverändert) würde die Schwelle erreichen."
    return ""


def dscr_self_test(cov: Covenant) -> tuple[Decimal, Decimal]:
    """Recompute the contract's own worked example (V3 Anlage 3 Abs. 8)."""
    t = cov.self_test
    val = (Decimal(t["ebitda"]) - Decimal(t["taxes"])) / (Decimal(t["interest"]) + Decimal(t["scheduled_repayments"]))
    return round_half_up(val, cov.decimals), Decimal(t["expected"])
