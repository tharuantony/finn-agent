"""Contract register: load the reviewed covenants.json and verify every quote
against the actual PDF text. Page numbers come from the PDF, not from the LLM."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from pypdf import PdfReader

_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    text = text.replace("­", "").replace(" ", " ")
    text = text.replace("„", '"').replace("“", '"').replace("”", '"')
    return _WS.sub(" ", text).strip().lower()


@lru_cache(maxsize=16)
def pdf_pages(path: str) -> tuple[str, ...]:
    """Normalised text of each page (index 0 = page 1)."""
    reader = PdfReader(path)
    return tuple(_norm(p.extract_text() or "") for p in reader.pages)


def raw_pdf_text(path: str) -> list[str]:
    reader = PdfReader(path)
    return [p.extract_text() or "" for p in reader.pages]


def find_quote(path: str, quote: str) -> int | None:
    """Return the 1-based page where the quote appears (also across a page break)."""
    q = _norm(quote)
    pages = pdf_pages(path)
    for i, page in enumerate(pages, 1):
        if q in page:
            return i
    for i in range(len(pages) - 1):  # quote spanning two pages (footer in between is removed below)
        joined = _strip_footer(pages[i]) + " " + pages[i + 1]
        if q in joined:
            return i + 1
    return None


def _strip_footer(page: str) -> str:
    # Footer lines start with the contract header and end with "seite x von y" + disclaimer
    m = re.search(r"(darlehensvertrag|kreditvertrag|konsortialkreditvertrag)[^\n]{0,120}seite \d+ von \d+.*$", page)
    return page[: m.start()] if m else page


@dataclass
class Source:
    ref: str
    quote: str
    role: str = ""
    page: int | None = None

    @property
    def verified(self) -> bool:
        return self.page is not None

    def cite(self) -> str:
        page = f"S. {self.page}" if self.page else "Seite nicht verifiziert"
        return f"{self.ref}, {page}"


@dataclass
class Covenant:
    key: str
    name: str
    calc: str
    operator: str
    threshold: str
    unit: str
    decimals: int
    definition: str
    sources: list[Source]
    params: dict = field(default_factory=dict)
    rounding_rule_in_contract: bool = True
    plan_basis_note: dict | None = None
    margin_grid: dict | None = None
    self_test: dict | None = None

    @property
    def threshold_source(self) -> Source:
        return next((s for s in self.sources if s.role == "Grenzwert"), self.sources[0])


@dataclass
class Contract:
    id: str
    file: str
    path: Path
    lender: str
    title: str
    plan_loan_label: str
    has_covenants: bool
    covenants: list[Covenant]
    no_covenant_reason: str = ""
    evidence: list[Source] = field(default_factory=list)

    def all_sources(self) -> list[Source]:
        out = list(self.evidence)
        for c in self.covenants:
            out += c.sources
        return out


class RegisterError(Exception):
    pass


def load_register(register_path: Path, contracts_dir: Path) -> tuple[list[Contract], list[str]]:
    """Load covenants.json, check files exist and verify quotes. Returns (contracts, warnings)."""
    data = json.loads(Path(register_path).read_text(encoding="utf-8"))
    contracts: list[Contract] = []
    warnings: list[str] = []
    for c in data["contracts"]:
        path = Path(contracts_dir) / c["file"]
        if not path.exists():
            raise RegisterError(f"Vertrag fehlt im Ordner: {path}")
        covs = []
        for cv in c.get("covenants", []):
            covs.append(Covenant(
                key=cv["key"], name=cv["name"], calc=cv["calc"], operator=cv["operator"],
                threshold=cv["threshold"], unit=cv["unit"], decimals=int(cv["decimals"]),
                definition=cv["definition"], params=cv.get("params", {}),
                sources=[Source(**s) for s in cv["sources"]],
                rounding_rule_in_contract=cv.get("rounding_rule_in_contract", True),
                plan_basis_note=cv.get("plan_basis_note"), margin_grid=cv.get("margin_grid"),
                self_test=cv.get("self_test"),
            ))
        contract = Contract(
            id=c["id"], file=c["file"], path=path, lender=c["lender"], title=c["title"],
            plan_loan_label=c.get("plan_loan_label", ""), has_covenants=c["has_covenants"],
            covenants=covs, no_covenant_reason=c.get("no_covenant_reason", ""),
            evidence=[Source(**s) for s in c.get("evidence", [])],
        )
        if contract.has_covenants != bool(contract.covenants):
            raise RegisterError(f"{contract.id}: has_covenants passt nicht zur Covenant-Liste")
        for src in contract.all_sources():
            src.page = find_quote(str(path), src.quote)
            if src.page is None:
                warnings.append(f"{contract.id} {src.ref}: Zitat nicht im PDF gefunden: '{src.quote[:60]}'")
        for cv in contract.covenants:
            for extra in (cv.plan_basis_note, cv.margin_grid):
                if extra and extra.get("quote"):
                    extra["page"] = find_quote(str(path), extra["quote"])
        contracts.append(contract)
    return contracts, warnings
