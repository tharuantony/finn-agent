"""Read the business plan (Wirtschaftsplan) from Excel.

Rows are found by their label (column A), the plan column by its header, so the
reader survives inserted rows or moved columns. Every value keeps its cell
reference so the reply can cite it. Totals are recomputed in code, never taken
from Excel formulas."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import openpyxl


class PlanError(Exception):
    pass


@dataclass(frozen=True)
class Value:
    amount: Decimal
    sheet: str
    cell: str
    label: str

    @property
    def ref(self) -> str:
        return f"{self.sheet}!{self.cell}"


@dataclass
class Loan:
    label: str
    row: int
    opening: Value
    drawdowns: Value
    scheduled_repayment: Value
    special_repayment: Value
    interest: Value
    is_shareholder_loan: bool
    is_bank_loan: bool

    @property
    def closing(self) -> Decimal:
        return (self.opening.amount + self.drawdowns.amount
                - self.scheduled_repayment.amount - self.special_repayment.amount)


@dataclass
class Plan:
    year: int
    file: str
    guv: dict[str, Value]
    bilanz: dict[str, Value]
    loans: list[Loan]
    notes: dict[str, Value] = field(default_factory=dict)
    status: str = ""
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    # ---- derived values (all computed here, in code) ----
    def net_income(self) -> Decimal:
        g = self.guv
        keys = ["umsatz", "eigenleistungen", "sbe", "material", "personal", "afa", "sba",
                "zinsertrag", "zinsaufwand", "steuern"]
        return sum((g[k].amount for k in keys), Decimal(0))

    def total_assets(self) -> Decimal:
        b = self.bilanz
        keys = ["immat", "sachanlagen", "finanzanlagen", "vorraete", "forderungen_lul",
                "sonst_vg", "kasse", "arap"]
        return sum((b[k].amount for k in keys), Decimal(0))

    def total_liabilities(self) -> Decimal:
        b = self.bilanz
        keys = ["gez_kapital", "kapitalruecklage", "gewinnruecklagen", "bilanzgewinn", "sonderposten",
                "pensionen", "steuerrst", "sonst_rst", "vb_kreditinstitute", "vb_gesellschafter",
                "vb_lul", "sonst_vb", "prap"]
        return sum((b[k].amount for k in keys), Decimal(0))

    def equity(self) -> Decimal:
        b = self.bilanz
        return sum((b[k].amount for k in ["gez_kapital", "kapitalruecklage", "gewinnruecklagen", "bilanzgewinn"]), Decimal(0))

    def equity_values(self) -> list[Value]:
        return [self.bilanz[k] for k in ["gez_kapital", "kapitalruecklage", "gewinnruecklagen", "bilanzgewinn"]]

    def asset_values(self) -> list[Value]:
        return [self.bilanz[k] for k in ["immat", "sachanlagen", "finanzanlagen", "vorraete",
                                         "forderungen_lul", "sonst_vg", "kasse", "arap"]]


# label prefix (case-insensitive startswith) per needed item
GUV_ROWS = {
    "umsatz": "Umsatzerlöse",
    "eigenleistungen": "Andere aktivierte Eigenleistungen",
    "sbe": "Sonstige betriebliche Erträge",
    "material": "Materialaufwand",
    "personal": "Personalaufwand",
    "afa": "Abschreibungen",
    "sba": "Sonstige betriebliche Aufwendungen",
    "zinsertrag": "Sonstige Zinsen und ähnliche Erträge",
    "zinsaufwand": "Zinsen und ähnliche Aufwendungen",
    "aufzinsung": "davon aus der Aufzinsung",
    "steuern": "Steuern vom Einkommen und vom Ertrag",
}
BILANZ_ROWS = {
    "immat": "Immaterielle Vermögensgegenstände",
    "sachanlagen": "Sachanlagen",
    "finanzanlagen": "Finanzanlagen",
    "vorraete": "Vorräte",
    "forderungen_lul": "Forderungen aus Lieferungen",
    "sonst_vg": "Sonstige Vermögensgegenstände",
    "kasse": "Kassenbestand",
    "arap": "Aktiver Rechnungsabgrenzungsposten",
    "gez_kapital": "Gezeichnetes Kapital",
    "kapitalruecklage": "Kapitalrücklage",
    "gewinnruecklagen": "Gewinnrücklagen",
    "bilanzgewinn": "Bilanzgewinn",
    "sonderposten": "B. Sonderposten",
    "pensionen": "Rückstellungen für Pensionen",
    "steuerrst": "Steuerrückstellungen",
    "sonst_rst": "Sonstige Rückstellungen",
    "vb_kreditinstitute": "Verbindlichkeiten gegenüber Kreditinstituten",
    "vb_gesellschafter": "Verbindlichkeiten gegenüber Gesellschafter",
    "vb_lul": "Verbindlichkeiten aus Lieferungen",
    "sonst_vb": "Sonstige Verbindlichkeiten",
    "prap": "E. Passiver Rechnungsabgrenzungsposten",
}
LOAN_COLS = {
    "opening": "Stand 01.01",
    "drawdowns": "Auszahlungen",
    "scheduled_repayment": "Planmäßige Tilgung",
    "special_repayment": "Sondertilgung",
    "interest": "Zinsaufwand",
}


def _find_sheet(wb, *names):
    for ws in wb.worksheets:
        if any(n.lower() in ws.title.lower() for n in names):
            return ws
    raise PlanError(f"Tabellenblatt nicht gefunden: {names} (vorhanden: {wb.sheetnames})")


def _plan_column(ws, year: int) -> int:
    for row in ws.iter_rows(min_row=1, max_row=8):
        for c in row:
            if isinstance(c.value, str) and str(year) in c.value and "plan" in c.value.lower():
                return c.column
    raise PlanError(f"Keine Plan-{year}-Spalte im Blatt '{ws.title}' gefunden")


def _find_row(ws, prefix: str) -> int:
    hits = [c.row for c in ws["A"] if isinstance(c.value, str) and c.value.strip().lower().startswith(prefix.lower())]
    if not hits:
        raise PlanError(f"Zeile '{prefix}' im Blatt '{ws.title}' nicht gefunden")
    return hits[0]


def _num(ws, ws_cached, row: int, col: int) -> Value:
    cell = ws.cell(row=row, column=col)
    v = cell.value
    if isinstance(v, str) and v.startswith("="):
        v = ws_cached.cell(row=row, column=col).value  # cached result if the file was saved by Excel
        if v is None:
            raise PlanError(f"{ws.title}!{cell.coordinate} ist eine Formel ohne gespeicherten Wert")
    if v is None:
        v = 0
    if not isinstance(v, (int, float)):
        raise PlanError(f"{ws.title}!{cell.coordinate} ist keine Zahl: {v!r}")
    label = str(ws.cell(row=row, column=1).value or "").strip()
    return Value(Decimal(str(v)), ws.title, cell.coordinate, label)


def read_plan(path: str | Path, year: int = 2027) -> Plan:
    path = Path(path)
    try:
        wb = openpyxl.load_workbook(path, data_only=False)
        wbc = openpyxl.load_workbook(path, data_only=True)
    except Exception as e:  # noqa: BLE001
        raise PlanError(f"Excel-Datei kann nicht gelesen werden: {e}") from e

    ws_g, ws_b, ws_f = _find_sheet(wb, "GuV", "Erfolgsplan"), _find_sheet(wb, "Bilanz"), _find_sheet(wb, "Finanzierung", "Darlehen")
    cg, cb, cf = wbc[ws_g.title], wbc[ws_b.title], wbc[ws_f.title]

    col_g, col_b = _plan_column(ws_g, year), _plan_column(ws_b, year)
    guv = {k: _num(ws_g, cg, _find_row(ws_g, p), col_g) for k, p in GUV_ROWS.items()}
    bilanz = {k: _num(ws_b, cb, _find_row(ws_b, p), col_b) for k, p in BILANZ_ROWS.items()}

    # Darlehensspiegel: header row with column names
    header_row, cols = None, {}
    for row in ws_f.iter_rows(min_row=1, max_row=10):
        names = {c.column: str(c.value) for c in row if c.value}
        if any("Tilgung" in n for n in names.values()):
            header_row = row[0].row
            for key, pref in LOAN_COLS.items():
                match = [col for col, n in names.items() if n.lower().startswith(pref.lower())]
                if not match:
                    raise PlanError(f"Spalte '{pref}' im Darlehensspiegel fehlt")
                cols[key] = match[0]
            break
    if header_row is None:
        raise PlanError("Kopfzeile des Darlehensspiegels nicht gefunden")

    loans: list[Loan] = []
    section = ""
    for r in range(header_row + 1, ws_f.max_row + 1):
        label = ws_f.cell(row=r, column=1).value
        if not isinstance(label, str) or not label.strip():
            continue
        label = label.strip()
        low = label.lower()
        if low.startswith("summe darlehen gesamt"):
            break
        if low.startswith("summe") or low.startswith("abgleich"):
            continue
        if ws_f.cell(row=r, column=cols["opening"]).value is None:
            section = low  # section header such as "Verbindlichkeiten gegenüber Kreditinstituten"
            continue
        vals = {k: _num(ws_f, cf, r, c) for k, c in cols.items()}
        loans.append(Loan(label=label, row=r, **vals,
                          is_shareholder_loan="gesellschafter" in low,
                          is_bank_loan="kreditinstitut" in section))
    if not loans:
        raise PlanError("Keine Darlehen im Darlehensspiegel gefunden")

    notes = {}
    for c in ws_b["A"]:
        if isinstance(c.value, str):
            t = c.value
            if "Rangrücktritt" in t and "39" in t:
                notes["subordination"] = Value(Decimal(0), ws_b.title, c.coordinate, t)
            if t.lstrip().startswith("3)") and "frei verfügbar" in t:
                notes["cash_free"] = Value(Decimal(0), ws_b.title, c.coordinate, t)
    status = ""
    for ws in wb.worksheets[:1]:
        for c in ws["A"]:
            if isinstance(c.value, str) and c.value.startswith("Stand"):
                status = c.value

    plan = Plan(year=year, file=path.name, guv=guv, bilanz=bilanz, loans=loans, notes=notes, status=status)
    _run_checks(plan)
    return plan


def _run_checks(plan: Plan) -> None:
    """Consistency checks. A failing check is reported in the reply; a broken balance aborts."""
    a, p = plan.total_assets(), plan.total_liabilities()
    plan.checks.append(("Bilanz: Aktiva = Passiva", a == p, f"Aktiva {a} / Passiva {p}"))
    if a != p:
        raise PlanError(f"Planbilanz nicht ausgeglichen: Aktiva {a} T€ ≠ Passiva {p} T€")

    bank_closing = sum((l.closing for l in plan.loans if l.is_bank_loan), Decimal(0))
    bil_bank = plan.bilanz["vb_kreditinstitute"].amount
    plan.checks.append(("Darlehensspiegel Banken = Bilanz", bank_closing == bil_bank,
                        f"{bank_closing} vs {bil_bank} ({plan.bilanz['vb_kreditinstitute'].ref})"))

    sh_closing = sum((l.closing for l in plan.loans if l.is_shareholder_loan), Decimal(0))
    bil_sh = plan.bilanz["vb_gesellschafter"].amount
    plan.checks.append(("Darlehensspiegel Gesellschafter = Bilanz", sh_closing == bil_sh,
                        f"{sh_closing} vs {bil_sh} ({plan.bilanz['vb_gesellschafter'].ref})"))

    interest = sum((l.interest.amount for l in plan.loans), Decimal(0))
    guv_int = -plan.guv["zinsaufwand"].amount + plan.guv["aufzinsung"].amount  # both negative in GuV
    plan.checks.append(("Zinsen Darlehensspiegel = GuV-Zinsaufwand ohne Aufzinsung", interest == guv_int,
                        f"{interest} vs {guv_int}"))
