# US Market Crash / Correction Monitor

**Live dashboard:** https://kyn100.github.io/market-crash-monitor/ — refreshed twice
a day (12:30 & 21:30 UTC) by GitHub Actions; the FRED API key is stored as an
encrypted repository secret (`FRED_API_KEY`), never in the code.

Tracks **100 economic and market indicators**, compares them with conditions seen
before past US stock market crashes and corrections (1973, 1980, 1987, 1990, 2000,
2007, 2011, 2018, 2020, 2022), and produces a composite warning flag:

- **GREEN** — no crash signature
- **YELLOW** — some warning signs
- **ORANGE** — multiple pre-crash conditions present
- **RED** — crash-signature conditions widespread

## Files
| File | Purpose |
|---|---|
| `run_now.bat` | Run once and open the dashboard |
| `report.html` | The dashboard (open in any browser; auto-refreshes hourly if left open) |
| `setup_schedule.bat` | Register the twice-a-day schedule (8:00 & 17:00) |
| `config.json` | Put your free FRED API key here |
| `history.csv` | Log of every run (score + flag over time) |
| `monitor.py` | The program (Python 3, standard library only) |

## One-time setup (2 minutes)
Without a FRED key the monitor runs in **market-only mode** (~12 of 100 indicators).
To activate everything:

1. Create a free account and request a key at
   https://fredaccount.stlouisfed.org/apikeys
2. Open `config.json` and paste the key:
   `{"fred_api_key": "your_key_here"}`
3. Double-click `run_now.bat`

## How scoring works
- Every indicator is ranked against its **own 15-year history** (percentile) plus
  its 6-month trend, direction-adjusted (e.g. high credit spreads = risky,
  low building permits = risky).
- **Crash-specific rules** override where research supports hard thresholds:
  Sahm rule (>= 0.50), yield-curve inversion & re-steepening, high-yield spread
  widening off its 12-month low, jobless-claims momentum, S&P drawdown/200-day
  average, VIX regime, permits contraction, sentiment extremes, CFNAI -0.70.
- A 12-item "classic pre-crash checklist" and a similarity match against ten
  historical market tops feed the composite score (0–100).

**Not investment advice.** Most warning signals produce false positives, and
shock-driven crashes (1987, 2020) can arrive without macro warning.
