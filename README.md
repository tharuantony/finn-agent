# Finn – Covenant Check Agent via E-Mail

Send Finn an e-mail with the business plan attached. Finn checks the financial covenants of four loan
contracts and replies in the same thread with a summary, a table, the source of every number and the
calculation path. **All numbers are calculated in Python code, not by the LLM.**

> Finn only answers **while it is running in a terminal**. The mailbox credentials are already in `.env`.
> Please run only **one** Finn at a time.

---

## 1. Start Finn

Requires **Python 3.10+** (https://www.python.org/downloads/; on Windows tick *"Add Python to PATH"*).

### macOS / Linux (Terminal) or Windows (Git Bash)

```bash
git clone https://github.com/<your-username>/finn-agent.git
cd finn-agent
python3 -m venv .venv
source .venv/bin/activate          # Git Bash: source .venv/Scripts/activate
pip install -r requirements.txt
python -m finn.main run
```

### Windows (PowerShell)

```powershell
git clone https://github.com/<your-username>/finn-agent.git
cd finn-agent
py -m venv .venv
.venv\Scripts\Activate.ps1         # if blocked: Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
pip install -r requirements.txt
python -m finn.main run
```

When Finn is running you see:
```
Finn läuft – überwacht agent.finn.demo@gmail.com alle 20s (Ctrl+C beendet)
```
Keep the window open. **Ctrl + C** stops Finn. Next time, only the `cd`, `activate` and `run` lines are needed.

---

## 2. Try it

From any other e-mail account send:

| | |
|---|---|
| **To** | `agent.finn.demo@gmail.com` |
| **Subject** | `Covenant-Prüfung Wirtschaftsplan 2027` |
| **Attachment** | [`samples/Wirtschaftsplan_2027.xlsx`](samples/Wirtschaftsplan_2027.xlsx) |

The reply arrives in the same thread within about 30–60 seconds (check spam if not).

**Expected result (31.12.2027):**

| Contract | Covenant | Plan | Threshold | Headroom | |
|---|---|---|---|---|---|
| V1 Sparkasse | no financial covenants | – | – | – | – |
| V2 Förderbank | Economic equity ratio | 34.32 % | ≥ 27.5 % | 6.82 pp | ✅ |
| V3 Landesbank | DSCR | 1.56 | ≥ 1.30 | 0.26 | ✅ |
| V4 Syndicated loan | Equity ratio | 30.7 % | ≥ 30.0 % | 0.7 pp | ⚠️ tight |
| V4 Syndicated loan | Dynamic leverage | 3.37x | ≤ 3.50x | 0.13 | ⚠️ tight |

Also try: a mail without attachment, a broken Excel file, or a modified plan.

---

## Other commands

| Command | Purpose |
|---|---|
| `python -m finn.main check samples/Wirtschaftsplan_2027.xlsx` | Result in the terminal, no e-mail |
| `python -m pytest -q` | Run the 19 tests |

## How it works

`finn/mail_io.py` (IMAP/SMTP) → `finn/contracts.py` (reviewed covenant register `covenants.json`, every quote verified in the PDF) → `finn/plan_reader.py` (Excel with cell references and consistency checks) → `finn/calc.py` (calculation per contract definition) → `finn/report.py` (reply mail). The LLM (`finn/llm.py`, optional) only writes text. Assumptions and design decisions: [NOTE.md](NOTE.md).

All data is synthetic.
