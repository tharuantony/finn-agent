"""LLM-assisted covenant extraction (offline step, run once per new contract).

Reads every PDF in contracts/, asks the LLM to return the financial covenants in a
fixed JSON schema with verbatim quotes, verifies each quote against the PDF text,
and compares the result with the reviewed register covenants.json.

    python scripts/extract_covenants.py            # writes out/covenants_extracted.json + diff
"""
from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from finn.contracts import find_quote, raw_pdf_text  # noqa: E402
from finn.main import _load_env  # noqa: E402

SCHEMA = """{
  "has_covenants": bool,
  "no_covenant_reason": string | null,
  "covenants": [{
     "name": string,                       // e.g. "Eigenkapitalquote"
     "operator": ">=" | "<=",
     "threshold": string,                  // decimal with dot, e.g. "30.0"
     "unit": "%" | "x" | "",
     "rounding_decimals": int | null,      // null if the contract has no rounding rule
     "definition": string,                 // full definition in your words incl. all inclusions/exclusions
     "sources": [{"ref": string, "role": "Grenzwert"|"Definition"|"Ausschluss"|"Rundung"|"Formel",
                  "quote": string}]        // quote = VERBATIM, max 15 words, from the contract text
  }]
}"""

SYSTEM = ("Du bist ein erfahrener Finanzreferent. Lies den Kreditvertrag und extrahiere ausschließlich "
          "Financial Covenants (Finanzkennzahlen, die die Kreditnehmerin einhalten MUSS). Kennzahlen, die nur "
          "als Hintergrund (z. B. Präambel) oder für ein Margenraster genannt werden, sind KEINE Covenants. "
          "Achte genau auf Definitionen in § 1 und in Anlagen: was zählt dazu, was ist ausgenommen "
          "(Sonderposten, Gesellschafterdarlehen, Rangrücktritt, Sondertilgungen, Aufzinsung). "
          "Rechne nichts. Antworte NUR mit JSON nach diesem Schema:\n" + SCHEMA)


def main():
    _load_env()
    try:
        import anthropic
    except ImportError:
        sys.exit("pip install anthropic")
    if not os.getenv("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY fehlt (.env)")
    client = anthropic.Anthropic()
    model = os.getenv("FINN_MODEL", "claude-sonnet-5-5")
    reviewed = {c["file"]: c for c in json.loads((ROOT / "covenants.json").read_text(encoding="utf-8"))["contracts"]}
    out, problems = {}, []
    for pdf in sorted((ROOT / "contracts").glob("*.pdf")):
        pages = raw_pdf_text(str(pdf))
        text = "\n\n".join(f"=== Seite {i} ===\n{t}" for i, t in enumerate(pages, 1))
        msg = client.messages.create(model=model, max_tokens=4000, system=SYSTEM,
                                     messages=[{"role": "user", "content": text}])
        raw = "".join(b.text for b in msg.content if b.type == "text")
        data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        for cv in data.get("covenants", []):
            for s in cv.get("sources", []):
                s["page"] = find_quote(str(pdf), s["quote"])
                if s["page"] is None:
                    problems.append(f"{pdf.name}: Zitat nicht im PDF: {s['quote']!r}")
        out[pdf.name] = data
        # compare with reviewed register
        rev = reviewed.get(pdf.name)
        if rev is None:
            problems.append(f"{pdf.name}: nicht im geprüften Register – bitte manuell ergänzen")
            continue
        if bool(data["has_covenants"]) != rev["has_covenants"]:
            problems.append(f"{pdf.name}: has_covenants LLM={data['has_covenants']} Register={rev['has_covenants']}")
        got = sorted((c["operator"], Decimal(c["threshold"])) for c in data.get("covenants", []))
        exp = sorted((c["operator"], Decimal(c["threshold"])) for c in rev["covenants"])
        if got != exp:
            problems.append(f"{pdf.name}: Grenzwerte LLM={got} Register={exp}")
        print(f"✓ {pdf.name}: {len(data.get('covenants', []))} Covenant(s)")
    (ROOT / "out").mkdir(exist_ok=True)
    (ROOT / "out" / "covenants_extracted.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nAbweichungen zum geprüften Register:" if problems else "\nKeine Abweichungen zum geprüften Register.")
    for p in problems:
        print("  -", p)


if __name__ == "__main__":
    main()
