"""Tests for the calculation core, the traps in the contracts, and the mail flow (offline)."""
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path

import openpyxl
import pytest

from finn import llm
from finn.calc import dscr_self_test, evaluate, round_half_up
from finn.contracts import load_register
from finn.mail_io import Attachment, IncomingMail, build_message, parse_message
from finn.pipeline import process
from finn.plan_reader import PlanError, read_plan

ROOT = Path(__file__).resolve().parent.parent
PLAN = ROOT / "samples" / "Wirtschaftsplan_2027.xlsx"


@pytest.fixture(scope="module")
def register():
    contracts, warnings = load_register(ROOT / "covenants.json", ROOT / "contracts")
    return contracts, warnings


@pytest.fixture(scope="module")
def results(register):
    contracts, _ = register
    plan = read_plan(PLAN, 2027)
    return {cv.key: evaluate(c, cv, plan) for c in contracts for cv in c.covenants}


# ---------------------------------------------------------------- register / contracts
def test_all_quotes_found_in_pdfs(register):
    _, warnings = register
    assert warnings == []


def test_contract_1_has_no_covenants(register):
    contracts, _ = register
    v1 = next(c for c in contracts if c.id == "V1")
    assert not v1.has_covenants and v1.evidence[0].page == 1  # ratios only in the preamble


def test_threshold_pages(register):
    contracts, _ = register
    pages = {cv.key: cv.threshold_source.page for c in contracts for cv in c.covenants}
    assert pages == {"V2_WEKQ": 6, "V3_DSCR": 5, "V4_EKQ": 6, "V4_DVG": 7}


def test_dscr_worked_example_from_contract(register):
    contracts, _ = register
    cv = next(cv for c in contracts for cv in c.covenants if cv.key == "V3_DSCR")
    got, expected = dscr_self_test(cv)
    assert got == expected == Decimal("1.54")


# ---------------------------------------------------------------- plan reader
def test_plan_values_and_checks():
    plan = read_plan(PLAN, 2027)
    assert plan.net_income() == 10700
    assert plan.total_assets() == 313200
    assert plan.equity() == 96100
    assert all(ok for _, ok, _ in plan.checks)


def test_plan_reader_survives_inserted_row(tmp_path):
    wb = openpyxl.load_workbook(PLAN)
    wb["GuV"].insert_rows(5)
    wb["GuV"]["A5"] = "Neue Zeile ohne Bedeutung"
    p = tmp_path / "plan.xlsx"
    wb.save(p)
    assert read_plan(p, 2027).net_income() == 10700


def test_unbalanced_plan_is_rejected(tmp_path):
    wb = openpyxl.load_workbook(PLAN)
    wb["Bilanz"]["D12"] = 99999
    p = tmp_path / "plan.xlsx"
    wb.save(p)
    with pytest.raises(PlanError, match="nicht ausgeglichen"):
        read_plan(p, 2027)


def test_missing_year_column():
    with pytest.raises(PlanError, match="Plan-2030"):
        read_plan(PLAN, 2030)


# ---------------------------------------------------------------- covenant values (hand-checked)
def test_v2_economic_equity_ratio(results):
    r = results["V2_WEKQ"]
    assert r.value == Decimal("34.32") and r.met  # (96.100 + 50 % × 22.800) / 313.200


def test_v3_dscr(results):
    r = results["V3_DSCR"]
    assert r.value == Decimal("1.56") and r.headroom == Decimal("0.26") and r.met
    debt_service = [i for i in r.inputs if i.name == "= Schuldendienst"][0].amount
    assert debt_service == 23680  # 4.930 interest + 18.750 scheduled repayment, NO 3.000 Sondertilgung


def test_v4_equity_ratio_tight(results):
    r = results["V4_EKQ"]
    assert r.value == Decimal("30.7") and r.met and r.tight  # Sonderposten NOT equity


def test_v4_leverage_excludes_subordinated_shareholder_loan(results):
    r = results["V4_DVG"]
    assert r.value == Decimal("3.37") and r.met and r.tight
    assert r.margin == Decimal("1.25")
    # trap: including the 8.000 T€ shareholder loan would give (156.200 − 12.600) / 40.200 = 3,57 → breach
    assert round_half_up(Decimal(143600) / Decimal(40200), 2) == Decimal("3.57")


def test_rounding_is_commercial():
    assert round_half_up(Decimal("1.305"), 2) == Decimal("1.31")
    assert round_half_up(Decimal("29.95"), 1) == Decimal("30.0")


# ---------------------------------------------------------------- LLM guard
def test_number_guard_rejects_invented_numbers():
    facts = {"covenants": [{"wert": "3,37x", "headroom": "0,13"}], "planjahr": 2027}
    assert llm.numbers_are_grounded("Der Verschuldungsgrad liegt bei 3,37x (Headroom 0,13).", facts)
    assert not llm.numbers_are_grounded("Der Verschuldungsgrad liegt bei 3,4x.", facts)


def test_summary_falls_back_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    text, src = llm.write_summary({"x": 1}, "Fallback")
    assert (text, src) == ("Fallback", "template")


# ---------------------------------------------------------------- mail flow (offline)
def _request(with_attachment=True, sender="Dr. Katrin Möller <k.moeller@example.org>"):
    m = EmailMessage()
    m["From"] = sender
    m["To"] = "agent.finn.demo@gmail.com"
    m["Subject"] = "Covenant-Prüfung Wirtschaftsplan 2027"
    m["Message-ID"] = "<abc@example.org>"
    m.set_content("Hallo Finn, bitte prüfe die Covenants. Danke, Katrin")
    if with_attachment:
        m.add_attachment(PLAN.read_bytes(), maintype="application", subtype="octet-stream",
                         filename="Wirtschaftsplan_2027.xlsx")
    return parse_message(bytes(m))


def test_end_to_end_reply(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = process(_request(), "agent.finn.demo@gmail.com")
    assert out.to_addr == "k.moeller@example.org"
    assert out.subject == "AW: Covenant-Prüfung Wirtschaftsplan 2027"
    assert out.in_reply_to == "<abc@example.org>"
    assert "3,37x" in out.html and "keine Financial Covenants" in out.html
    msg = build_message(out, "agent.finn.demo@gmail.com")
    assert msg["In-Reply-To"] == "<abc@example.org>" and msg["Auto-Submitted"] == "auto-replied"


def test_missing_attachment_asks_for_file(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = process(_request(with_attachment=False), "agent.finn.demo@gmail.com")
    assert "Excel" in out.text


def test_own_mail_is_ignored():
    mail = _request(sender="Finn <agent.finn.demo@gmail.com>")
    assert process(mail, "agent.finn.demo@gmail.com") is None


def test_broken_excel_gives_error_reply_not_crash(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    mail = IncomingMail("<x@y>", "Test", "t@example.org", "Covenant-Prüfung", "", "",
                        attachments=[Attachment("Wirtschaftsplan_2027.xlsx", b"not an excel file")])
    out = process(mail, "agent.finn.demo@gmail.com")
    assert "nicht abschließen" in out.text
