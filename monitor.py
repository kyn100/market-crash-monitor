"""
US Stock Market Crash / Correction Early-Warning Monitor
=========================================================
Fetches 100 economic & market indicators (FRED + Yahoo Finance), scores each
against conditions observed before past US stock market crashes/corrections
(1973, 1980, 1987, 1990, 2000, 2007, 2011, 2018, 2020, 2022), computes a
composite warning flag (GREEN / YELLOW / ORANGE / RED), and writes a
self-contained HTML dashboard: report.html

Runs on Python 3 standard library only. Designed to be run twice a day.

Data sources:
  - FRED API (https://fred.stlouisfed.org) -- requires a FREE api key in config.json
  - Yahoo Finance chart API -- no key required

NOT investment advice. Signals are probabilistic; most warnings do not end in crashes.
"""

import bisect
import concurrent.futures
import csv
import datetime as dt
import html
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
REPORT_PATH = os.path.join(APP_DIR, "report.html")
HISTORY_PATH = os.path.join(APP_DIR, "history.csv")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# ---------------------------------------------------------------------------
# Indicator definitions: exactly 100.
# (series_id, source, display name, category, display transform, unit, risk_dir)
#   display transform: 'level' | 'yoy' (% vs 1y ago) | 'chg3m' (avg monthly change, 3m)
#   risk_dir: +1 = high values risky, -1 = low/falling values risky, 0 = informational
# ---------------------------------------------------------------------------
CAT_CURVE = "Yield Curve & Rates"
CAT_INFL = "Inflation"
CAT_CREDIT = "Credit & Financial Stress"
CAT_MONEY = "Banking & Money"
CAT_LABOR = "Labor Market"
CAT_GROWTH = "Growth & Activity"
CAT_HOUSING = "Housing"
CAT_CONSUMER = "Consumer"
CAT_FISCAL = "Fiscal & Corporate"
CAT_MARKET = "Markets"

SERIES = [
    # --- Yield Curve & Rates (12) ---
    ("DGS3MO", "fred", "3-Month Treasury Yield", CAT_CURVE, "level", "%", 0),
    ("DGS1", "fred", "1-Year Treasury Yield", CAT_CURVE, "level", "%", 0),
    ("DGS2", "fred", "2-Year Treasury Yield", CAT_CURVE, "level", "%", 0),
    ("DGS10", "fred", "10-Year Treasury Yield", CAT_CURVE, "level", "%", 0),
    ("DGS30", "fred", "30-Year Treasury Yield", CAT_CURVE, "level", "%", 0),
    ("T10Y3M", "fred", "Yield Curve: 10y minus 3m", CAT_CURVE, "level", "pp", -1),
    ("T10Y2Y", "fred", "Yield Curve: 10y minus 2y", CAT_CURVE, "level", "pp", -1),
    ("DFF", "fred", "Fed Funds Effective Rate", CAT_CURVE, "level", "%", 0),
    ("DPRIME", "fred", "Bank Prime Loan Rate", CAT_CURVE, "level", "%", 0),
    ("MORTGAGE30US", "fred", "30-Year Mortgage Rate", CAT_CURVE, "level", "%", 1),
    ("DFII10", "fred", "10-Year Real Yield (TIPS)", CAT_CURVE, "level", "%", 1),
    ("T10YIE", "fred", "10-Year Inflation Breakeven", CAT_CURVE, "level", "%", 0),

    # --- Inflation (8) ---
    ("CPIAUCSL", "fred", "CPI Inflation (YoY)", CAT_INFL, "yoy", "%", 1),
    ("CPILFESL", "fred", "Core CPI Inflation (YoY)", CAT_INFL, "yoy", "%", 1),
    ("PCEPI", "fred", "PCE Inflation (YoY)", CAT_INFL, "yoy", "%", 1),
    ("PCEPILFE", "fred", "Core PCE Inflation (YoY)", CAT_INFL, "yoy", "%", 1),
    ("PPIACO", "fred", "Producer Prices (YoY)", CAT_INFL, "yoy", "%", 1),
    ("T5YIE", "fred", "5-Year Inflation Breakeven", CAT_INFL, "level", "%", 0),
    ("T5YIFR", "fred", "5y5y Forward Inflation Expectation", CAT_INFL, "level", "%", 0),
    ("MICH", "fred", "Consumer 1y Inflation Expectations (UMich)", CAT_INFL, "level", "%", 1),

    # --- Credit & Financial Stress (12) ---
    ("BAMLH0A0HYM2", "fred", "High-Yield Credit Spread (OAS)", CAT_CREDIT, "level", "pp", 1),
    ("BAMLC0A0CM", "fred", "Investment-Grade Credit Spread (OAS)", CAT_CREDIT, "level", "pp", 1),
    ("BAMLH0A3HYC", "fred", "CCC & Lower Credit Spread (OAS)", CAT_CREDIT, "level", "pp", 1),
    ("BAA10Y", "fred", "Baa Corporate vs 10y Treasury Spread", CAT_CREDIT, "level", "pp", 1),
    ("NFCI", "fred", "Chicago Fed Financial Conditions Index", CAT_CREDIT, "level", "idx", 1),
    ("ANFCI", "fred", "Adjusted Financial Conditions Index", CAT_CREDIT, "level", "idx", 1),
    ("STLFSI4", "fred", "St. Louis Fed Financial Stress Index", CAT_CREDIT, "level", "idx", 1),
    ("DRCCLACBS", "fred", "Credit Card Delinquency Rate", CAT_CREDIT, "level", "%", 1),
    ("DRSFRMACBS", "fred", "Mortgage Delinquency Rate", CAT_CREDIT, "level", "%", 1),
    ("DRBLACBS", "fred", "Business Loan Delinquency Rate", CAT_CREDIT, "level", "%", 1),
    ("DRTSCILM", "fred", "Banks Tightening C&I Loans (large firms, SLOOS)", CAT_CREDIT, "level", "%", 1),
    ("DRTSCIS", "fred", "Banks Tightening C&I Loans (small firms, SLOOS)", CAT_CREDIT, "level", "%", 1),

    # --- Banking & Money (9) ---
    ("BUSLOANS", "fred", "Commercial & Industrial Loans (YoY)", CAT_MONEY, "yoy", "%", -1),
    ("TOTBKCR", "fred", "Total Bank Credit (YoY)", CAT_MONEY, "yoy", "%", -1),
    ("DPSACBW027SBOG", "fred", "Bank Deposits (YoY)", CAT_MONEY, "yoy", "%", -1),
    ("TOTRESNS", "fred", "Bank Reserves at Fed (YoY)", CAT_MONEY, "yoy", "%", -1),
    ("M2SL", "fred", "M2 Money Supply (YoY)", CAT_MONEY, "yoy", "%", -1),
    ("M2REAL", "fred", "Real M2 Money Supply (YoY)", CAT_MONEY, "yoy", "%", -1),
    ("WALCL", "fred", "Fed Balance Sheet (YoY)", CAT_MONEY, "yoy", "%", 0),
    ("RRPONTSYD", "fred", "Fed Reverse Repo Usage ($B)", CAT_MONEY, "level", "$B", 0),
    ("WTREGEN", "fred", "Treasury General Account ($B)", CAT_MONEY, "level", "$B", 0),

    # --- Labor Market (14) ---
    ("UNRATE", "fred", "Unemployment Rate", CAT_LABOR, "level", "%", 1),
    ("SAHMREALTIME", "fred", "Sahm Rule Recession Indicator", CAT_LABOR, "level", "pp", 1),
    ("U6RATE", "fred", "Underemployment Rate (U-6)", CAT_LABOR, "level", "%", 1),
    ("ICSA", "fred", "Initial Jobless Claims", CAT_LABOR, "level", "k", 1),
    ("IC4WSA", "fred", "Initial Claims (4-week avg)", CAT_LABOR, "level", "k", 1),
    ("PAYEMS", "fred", "Payroll Growth (3m avg, k/month)", CAT_LABOR, "chg3m", "k/mo", -1),
    ("MANEMP", "fred", "Manufacturing Employment (YoY)", CAT_LABOR, "yoy", "%", -1),
    ("TEMPHELPS", "fred", "Temp-Help Employment (YoY, leading)", CAT_LABOR, "yoy", "%", -1),
    ("JTSJOL", "fred", "Job Openings (YoY)", CAT_LABOR, "yoy", "%", -1),
    ("JTSQUR", "fred", "Quits Rate (worker confidence)", CAT_LABOR, "level", "%", -1),
    ("UEMPMED", "fred", "Median Unemployment Duration (weeks)", CAT_LABOR, "level", "wk", 1),
    ("EMRATIO", "fred", "Employment-Population Ratio", CAT_LABOR, "level", "%", -1),
    ("AWHMAN", "fred", "Avg Weekly Hours, Manufacturing (leading)", CAT_LABOR, "level", "hrs", -1),
    ("CES0500000003", "fred", "Avg Hourly Earnings (YoY)", CAT_LABOR, "yoy", "%", 0),

    # --- Growth & Activity (12) ---
    ("GDPC1", "fred", "Real GDP (YoY)", CAT_GROWTH, "yoy", "%", -1),
    ("INDPRO", "fred", "Industrial Production (YoY)", CAT_GROWTH, "yoy", "%", -1),
    ("TCU", "fred", "Capacity Utilization", CAT_GROWTH, "level", "%", -1),
    ("RSAFS", "fred", "Retail Sales (YoY)", CAT_GROWTH, "yoy", "%", -1),
    ("CMRMTSPL", "fred", "Real Manufacturing & Trade Sales (YoY)", CAT_GROWTH, "yoy", "%", -1),
    ("DGORDER", "fred", "Durable Goods Orders (YoY)", CAT_GROWTH, "yoy", "%", -1),
    ("NEWORDER", "fred", "Core Capital Goods Orders (YoY)", CAT_GROWTH, "yoy", "%", -1),
    ("AMTMNO", "fred", "Manufacturers' New Orders (YoY)", CAT_GROWTH, "yoy", "%", -1),
    ("CFNAI", "fred", "Chicago Fed National Activity Index", CAT_GROWTH, "level", "idx", -1),
    ("ISRATIO", "fred", "Business Inventories/Sales Ratio", CAT_GROWTH, "level", "x", 1),
    ("TSIFRGHT", "fred", "Freight Transportation Index (YoY)", CAT_GROWTH, "yoy", "%", -1),
    ("TRUCKD11", "fred", "Truck Tonnage (YoY)", CAT_GROWTH, "yoy", "%", -1),

    # --- Housing (7) ---
    ("HOUST", "fred", "Housing Starts (YoY)", CAT_HOUSING, "yoy", "%", -1),
    ("PERMIT", "fred", "Building Permits (YoY, leading)", CAT_HOUSING, "yoy", "%", -1),
    ("HOUST1F", "fred", "Single-Family Starts (YoY)", CAT_HOUSING, "yoy", "%", -1),
    ("PERMIT1", "fred", "Single-Family Permits (YoY)", CAT_HOUSING, "yoy", "%", -1),
    ("HSN1F", "fred", "New Home Sales (YoY)", CAT_HOUSING, "yoy", "%", -1),
    ("CSUSHPINSA", "fred", "Case-Shiller Home Prices (YoY)", CAT_HOUSING, "yoy", "%", -1),
    ("MSACSR", "fred", "Months' Supply of New Homes", CAT_HOUSING, "level", "mo", 1),

    # --- Consumer (8) ---
    ("UMCSENT", "fred", "Consumer Sentiment (UMich)", CAT_CONSUMER, "level", "idx", -1),
    ("PSAVERT", "fred", "Personal Savings Rate", CAT_CONSUMER, "level", "%", 0),
    ("PCE", "fred", "Consumer Spending (YoY)", CAT_CONSUMER, "yoy", "%", -1),
    ("DSPIC96", "fred", "Real Disposable Income (YoY)", CAT_CONSUMER, "yoy", "%", -1),
    ("PI", "fred", "Personal Income (YoY)", CAT_CONSUMER, "yoy", "%", 0),
    ("TOTALSL", "fred", "Total Consumer Credit (YoY)", CAT_CONSUMER, "yoy", "%", 0),
    ("REVOLSL", "fred", "Revolving Credit / Card Debt (YoY)", CAT_CONSUMER, "yoy", "%", 1),
    ("TDSP", "fred", "Household Debt Service Ratio", CAT_CONSUMER, "level", "%", 1),

    # --- Fiscal & Corporate (6) ---
    ("GFDEBTN", "fred", "Federal Debt (YoY)", CAT_FISCAL, "yoy", "%", 0),
    ("FYFSGDA188S", "fred", "Federal Surplus/Deficit (% of GDP)", CAT_FISCAL, "level", "%", 0),
    ("A091RC1Q027SBEA", "fred", "Federal Interest Payments (YoY)", CAT_FISCAL, "yoy", "%", 1),
    ("MTSDS133FMS", "fred", "Monthly Federal Surplus/Deficit ($M)", CAT_FISCAL, "level", "$M", 0),
    ("CP", "fred", "Corporate Profits (YoY)", CAT_FISCAL, "yoy", "%", -1),
    ("CPATAX", "fred", "Corporate Profits After Tax (YoY)", CAT_FISCAL, "yoy", "%", -1),

    # --- FX & Commodities via FRED (2) ---
    ("DTWEXBGS", "fred", "Broad US Dollar Index (YoY)", CAT_MARKET, "yoy", "%", 1),
    ("DCOILWTICO", "fred", "WTI Crude Oil (YoY)", CAT_MARKET, "yoy", "%", 1),

    # --- Markets via Yahoo Finance (10) ---
    ("^GSPC", "yahoo", "S&P 500 (YoY)", CAT_MARKET, "yoy", "%", -1),
    ("^IXIC", "yahoo", "Nasdaq Composite (YoY)", CAT_MARKET, "yoy", "%", -1),
    ("^DJI", "yahoo", "Dow Jones Industrial (YoY)", CAT_MARKET, "yoy", "%", -1),
    ("^RUT", "yahoo", "Russell 2000 Small Caps (YoY)", CAT_MARKET, "yoy", "%", -1),
    ("^VIX", "yahoo", "VIX Volatility Index", CAT_MARKET, "level", "", 1),
    ("^SOX", "yahoo", "Semiconductor Index (YoY, cycle proxy)", CAT_MARKET, "yoy", "%", -1),
    ("XLF", "yahoo", "Financials Sector ETF (YoY)", CAT_MARKET, "yoy", "%", -1),
    ("GC=F", "yahoo", "Gold (YoY)", CAT_MARKET, "yoy", "%", 0),
    ("HG=F", "yahoo", "Copper (YoY, growth proxy)", CAT_MARKET, "yoy", "%", -1),
    ("BTC-USD", "yahoo", "Bitcoin (YoY, risk appetite)", CAT_MARKET, "yoy", "%", 0),
]
assert len(SERIES) == 100, f"expected 100 indicators, got {len(SERIES)}"

CATEGORY_WEIGHTS = {
    CAT_CURVE: 12,
    CAT_CREDIT: 18,
    CAT_MONEY: 6,
    CAT_LABOR: 18,
    CAT_INFL: 6,
    CAT_GROWTH: 12,
    CAT_HOUSING: 8,
    CAT_CONSUMER: 8,
    CAT_FISCAL: 2,
    CAT_MARKET: 10,
}

# Types of bad markets: each has a distinct causal mechanism, hence distinct precursors
# name, description, member episodes, flaggable (shocks have no common causal
# mechanism, so "shared factors" across them are coincidence - never flagged)
CRASH_TYPES = {
    "tightening": ("Inflation & Fed-Tightening Bears",
                   "Inflation (or policy normalization) forces the Fed to tighten until growth cracks",
                   "1973-74, 1980-82, 1990, Q4-2018, 2022", True),
    "credit": ("Credit / Banking Crisis",
               "Leverage and lending excesses unwind - the 2008 pattern",
               "2007-09", True),
    "bubble": ("Valuation Bubble Bust",
               "Extreme valuations in a hot sector collapse - the 2000 pattern",
               "2000-02", True),
    "shock": ("Exogenous Shocks",
              "External events (war, pandemic, market structure) with little macro warning",
              "1987, 2011, 2020", False),
}

# Sub-category threat watches: the top-10 critical threats that are not
# themselves a major category. Each tripwire is (series_id, label, test):
#   "status"    -> the indicator's own WATCH/ALERT scoring
#   "pct_hi:90" -> current reading at/above that 15-year percentile
#   "pct_lo:10" -> current reading at/below that 15-year percentile
# parent None = unprecedented risk with no parent category (threshold watch only).
SUBCARDS = [
    ("oil_shock", "tightening", "Oil / Energy Shock Watch",
     "A supply-driven oil spike lands on the inflation squeeze (1973, 1979, 1990 pattern)",
     [("DCOILWTICO", "oil up sharply YoY", "status"),
      ("DCOILWTICO", "oil at 15-year extreme", "pct_hi:90"),
      ("PPIACO", "producer prices hot", "status"),
      ("MICH", "inflation expectations elevated", "status")]),
    ("labor_cascade", "tightening", "Labor-Market Cascade",
     "The self-reinforcing layoff spiral that turns slowdowns into recessions - the confirmation stage",
     [("SAHMREALTIME", "Sahm rule", "status"),
      ("IC4WSA", "jobless claims trending up", "status"),
      ("UNRATE", "unemployment off its low", "status"),
      ("TEMPHELPS", "temp-help employment falling", "status"),
      ("JTSQUR", "quits rate depressed", "status"),
      ("PAYEMS", "payroll growth weak", "status")]),
    ("housing_bust", "credit", "Housing Bust Watch",
     "Affordability breaks, prices fall, construction stops - the 2006-09 on-ramp",
     [("MSACSR", "months' supply elevated", "status"),
      ("CSUSHPINSA", "home-price growth stalling", "status"),
      ("PERMIT", "permits contracting", "status"),
      ("HOUST", "starts contracting", "status"),
      ("HSN1F", "new-home sales falling", "status"),
      ("DRSFRMACBS", "mortgage delinquencies rising", "status"),
      ("MORTGAGE30US", "mortgage rates punitive", "status")]),
    ("ai_bubble", "bubble", "AI / Tech-Concentration Watch",
     "Mega-cap concentration + the AI capex cycle unwinding, 2000-style. Valuation and "
     "concentration data are not in the indicator set - this watch tracks momentum breaks "
     "in the leadership complex",
     [("^IXIC", "Nasdaq momentum breaking", "status"),
      ("^SOX", "semiconductors breaking", "status"),
      ("^GSPC", "S&P drawdown / below 200-day", "status"),
      ("^VIX", "volatility regime shift", "status"),
      ("BTC-USD", "risk appetite collapsing (Bitcoin)", "pct_lo:15")]),
    ("sovereign", None, "Sovereign Debt / Fiscal Stress Watch",
     "A bond-market revolt against US debt. NO US PRECEDENT - no historical signature is "
     "possible; this is a raw threshold watch on the ingredients",
     [("A091RC1Q027SBEA", "interest burden accelerating", "status"),
      ("GFDEBTN", "debt growth extreme", "pct_hi:85"),
      ("MTSDS133FMS", "deficits at 15-year extremes", "pct_lo:10"),
      ("DGS30", "30-year yield at 15-year extreme", "pct_hi:90"),
      ("T10YIE", "inflation compensation demanded", "pct_hi:90"),
      ("GC=F", "gold spike (confidence hedge)", "pct_hi:90")]),
    ("dollar", None, "Dollar-Confidence Watch",
     "Disorderly flight from the dollar / reserve-status erosion. NO US PRECEDENT - "
     "raw threshold watch only",
     [("DTWEXBGS", "dollar falling sharply", "pct_lo:10"),
      ("GC=F", "gold spike", "pct_hi:90"),
      ("T10YIE", "inflation compensation extreme", "pct_hi:85"),
      ("DGS30", "long yields spiking", "pct_hi:90")]),
    ("geopolitical", "shock", "Geopolitical Rupture (Taiwan, war)",
     "A Taiwan blockade would sever advanced-semiconductor supply overnight - plausibly a "
     "bigger real-economy shock than any oil embargo. Would show first in semiconductors, "
     "VIX and gold as reactions, not warnings",
     []),
    ("cyber", "shock", "Cyber / Pandemic / Infrastructure",
     "An attack on payments, clearing or the grid - or a new pandemic. Instant stop with "
     "no macro warning (COVID 2020 precedent)",
     []),
]

# Market tops preceding major crashes / corrections (onset, label, drawdown, type)
EPISODES = [
    ("1973-01-11", "1973-74 bear", "-48%", "tightening"),
    ("1980-11-28", "1980-82 bear", "-27%", "tightening"),
    ("1987-08-25", "1987 Black Monday", "-34%", "shock"),
    ("1990-07-16", "1990 bear", "-20%", "tightening"),
    ("2000-03-24", "Dot-com crash", "-49%", "bubble"),
    ("2007-10-09", "Financial Crisis", "-57%", "credit"),
    ("2011-04-29", "2011 correction", "-19%", "shock"),
    ("2018-09-20", "Q4-2018 correction", "-20%", "tightening"),
    ("2020-02-19", "COVID crash", "-34%", "shock"),
    ("2022-01-03", "2022 bear", "-25%", "tightening"),
]

# Indicators used for the "then vs now" historical comparison table
HEADLINE_IDS = ["UNRATE", "T10Y3M", "BAMLH0A0HYM2", "IC4WSA", "NFCI", "INDPRO",
                "PERMIT", "UMCSENT", "CPIAUCSL", "DFF", "^VIX", "^GSPC",
                "RSAFS", "TCU", "M2REAL"]


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------
def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"fred_api_key": ""}, f, indent=2)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"fred_api_key": ""}


def http_get(url, timeout=45):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last_err = e
            time.sleep(2.0 * (attempt + 1))
    raise last_err


def fetch_fred(series_id, api_key):
    url = ("https://api.stlouisfed.org/fred/series/observations?"
           + urllib.parse.urlencode({
               "series_id": series_id, "api_key": api_key,
               "file_type": "json", "observation_start": "1919-01-01"}))
    data = json.loads(http_get(url))
    obs = data.get("observations", [])
    dates, values = [], []
    for o in obs:
        v = o.get("value", ".")
        if v in (".", "", None):
            continue
        try:
            values.append(float(v))
            dates.append(dt.date.fromisoformat(o["date"]))
        except ValueError:
            continue
    return dates, values


def fetch_yahoo(symbol):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           + urllib.parse.quote(symbol)
           + "?range=max&interval=1d&events=history")
    data = json.loads(http_get(url))
    result = data["chart"]["result"][0]
    ts = result.get("timestamp") or []
    closes = result["indicators"]["quote"][0].get("close") or []
    dates, values = [], []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        dates.append(dt.datetime.fromtimestamp(t, dt.timezone.utc).date())
        values.append(float(c))
    return dates, values


# ---------------------------------------------------------------------------
# Series math helpers
# ---------------------------------------------------------------------------
def last_on_or_before(dates, values, target):
    """Value of the last observation on or before target date, else None."""
    lo, hi = 0, len(dates) - 1
    if hi < 0 or dates[0] > target:
        return None, None
    pos = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if dates[mid] <= target:
            pos = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if pos < 0:
        return None, None
    return dates[pos], values[pos]


def build_metric(dates, values, disp):
    """Transform raw series into the displayed/scored metric series."""
    if disp == "level":
        return list(dates), list(values)
    if disp == "yoy":
        out_d, out_v = [], []
        for i, d in enumerate(dates):
            target = d - dt.timedelta(days=365)
            pd_, pv = last_on_or_before(dates, values, target)
            if pd_ is None or (target - pd_).days > 190 or pv == 0:
                continue
            out_d.append(d)
            out_v.append((values[i] / pv - 1.0) * 100.0)
        return out_d, out_v
    if disp == "chg3m":  # average monthly change over trailing 3 observations
        out_d, out_v = [], []
        for i in range(3, len(dates)):
            out_d.append(dates[i])
            out_v.append((values[i] - values[i - 3]) / 3.0)
        return out_d, out_v
    return list(dates), list(values)


def percentile_of(sorted_vals, x):
    """Percentile rank (0-100) of x within sorted_vals."""
    if not sorted_vals:
        return None
    lo, hi = 0, len(sorted_vals)
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_vals[mid] <= x:
            lo = mid + 1
        else:
            hi = mid
    return 100.0 * lo / len(sorted_vals)


class Indicator:
    def __init__(self, sid, src, name, cat, disp, unit, direction):
        self.sid, self.src, self.name = sid, src, name
        self.cat, self.disp, self.unit, self.dir = cat, disp, unit, direction
        self.dates, self.values = [], []       # raw
        self.mdates, self.mvalues = [], []     # metric (displayed/scored)
        self.error = None
        self.status = "N/A"     # OK / WATCH / ALERT / N/A / INFO
        self.points = None
        self.pctile = None
        self.note = ""
        self.current = None
        self.current_date = None
        self.year_ago = None
        self.trend6m = None

    def metric_at(self, target):
        d, v = last_on_or_before(self.mdates, self.mvalues, target)
        if d is None or (target - d).days > 400:
            return None
        return v

    def as_of(self, asof):
        """Re-evaluate this indicator using only observations up to `asof`,
        applying the exact same percentile windows and special rules.
        Metric arrays are sliced from the parent (all transforms are
        backward-looking, so slicing is exact) to keep bulk screening fast."""
        snap = Indicator(self.sid, self.src, self.name, self.cat,
                         self.disp, self.unit, self.dir)
        if self.error or not self.dates:
            snap.error = self.error or "no data"
            return snap
        lo = bisect.bisect_right(self.dates, asof)
        snap.dates = self.dates[:lo]
        snap.values = self.values[:lo]
        if not snap.dates:
            snap.error = "series did not exist yet"
            return snap
        if (asof - snap.dates[-1]).days > 550:
            snap.error = "no data near this date"
            return snap
        if not self.mdates:
            self.mdates, self.mvalues = build_metric(self.dates, self.values, self.disp)
        ml = bisect.bisect_right(self.mdates, asof)
        snap.mdates = self.mdates[:ml]
        snap.mvalues = self.mvalues[:ml]
        try:
            snap._score()
        except Exception as ex:
            snap.error = f"compute failed: {type(ex).__name__}"
            snap.status = "N/A"
        return snap

    def compute(self):
        if self.error or not self.dates:
            return
        self.mdates, self.mvalues = build_metric(self.dates, self.values, self.disp)
        self._score()

    def _score(self):
        if not self.mdates:
            self.error = "no usable data"
            return
        self.current = self.mvalues[-1]
        self.current_date = self.mdates[-1]
        self.year_ago = self.metric_at(self.current_date - dt.timedelta(days=365))
        v6 = self.metric_at(self.current_date - dt.timedelta(days=183))
        if v6 is not None:
            self.trend6m = self.current - v6

        # 15-year window for percentile scoring
        cutoff = self.current_date - dt.timedelta(days=int(15 * 365.25))
        window = [v for d, v in zip(self.mdates, self.mvalues) if d >= cutoff]
        if len(window) >= 24:
            self.pctile = percentile_of(sorted(window), self.current)

        if self.dir == 0:
            self.status = "INFO"
            return
        self.generic_score()
        self.special_score()

    def generic_score(self):
        pts = 0
        if self.pctile is not None:
            adj = self.pctile if self.dir > 0 else 100.0 - self.pctile
            if adj >= 80:
                pts += 1
            if adj >= 90:
                pts += 1
            if adj >= 97:
                pts += 1
        # trend: is the 6-month move in the risky direction and large vs history?
        if self.trend6m is not None:
            cutoff = self.current_date - dt.timedelta(days=int(15 * 365.25))
            i0 = bisect.bisect_left(self.mdates, cutoff)
            recent = list(zip(self.mdates[i0:], self.mvalues[i0:]))
            step = max(1, len(recent) // 500)  # sample dense daily series
            moves = []
            for d, v in recent[::step]:
                pv = self.metric_at(d - dt.timedelta(days=183))
                if pv is not None:
                    moves.append(v - pv)
            if len(moves) >= 24:
                p = percentile_of(sorted(moves), self.trend6m)
                adj = p if self.dir > 0 else 100.0 - p
                if adj >= 85:
                    pts += 1
        self.points = pts
        self.status = "OK" if pts == 0 else ("WATCH" if pts <= 2 else "ALERT")

    # -- crash-specific rules with researched thresholds --------------------
    def special_score(self):
        sid, cur = self.sid, self.current
        if cur is None:
            return
        set_ = self._set_status

        if sid == "SAHMREALTIME":
            if cur >= 0.50:
                set_("ALERT", "Sahm rule TRIGGERED (>=0.50) - has marked the start of every US recession since 1970")
            elif cur >= 0.30:
                set_("WATCH", "Sahm rule approaching trigger (0.50)")
            else:
                set_("OK", "Sahm rule not triggered (trigger at 0.50)")
        elif sid == "T10Y3M":
            recent_min = min(self.mvalues[-540:]) if len(self.mvalues) > 10 else cur
            if recent_min < -0.10 and cur > 0.10:
                set_("ALERT", "Curve re-steepening AFTER inversion - the pattern seen months before the 2001, 2008 and 2020 downturns")
            elif cur < 0:
                set_("WATCH", "Yield curve inverted - preceded 2000, 2007, 2020 crashes by 6-18 months")
        elif sid == "BAMLH0A0HYM2":
            low12 = min([v for d, v in zip(self.mdates, self.mvalues)
                         if d >= self.current_date - dt.timedelta(days=365)] or [cur])
            rise = cur - low12
            if cur >= 5.0 or rise >= 1.5:
                set_("ALERT", f"HY spreads blowing out (+{rise:.2f}pp off 12m low) - classic crash accelerant (2000, 2008, 2020)")
            elif rise >= 0.75:
                set_("WATCH", f"HY spreads widening (+{rise:.2f}pp off 12m low)")
            elif cur <= 3.5:
                self.note = self.note or f"Spreads calm (+{rise:.2f}pp off 12m low); credit market sees low near-term default risk"
        elif sid in ("IC4WSA", "ICSA"):
            low12 = min([v for d, v in zip(self.mdates, self.mvalues)
                         if d >= self.current_date - dt.timedelta(days=365)] or [cur])
            pct = (cur / low12 - 1) * 100 if low12 else 0
            if pct >= 25:
                set_("ALERT", f"Claims +{pct:.0f}% off 12m low - labor deterioration underway")
            elif pct >= 12:
                set_("WATCH", f"Claims +{pct:.0f}% off 12m low")
        elif sid == "UNRATE":
            low12 = min([v for d, v in zip(self.mdates, self.mvalues)
                         if d >= self.current_date - dt.timedelta(days=365)] or [cur])
            rise = cur - low12
            if rise >= 0.5:
                set_("ALERT", f"Unemployment +{rise:.1f}pp off 12m low - recessions become self-reinforcing past +0.5pp")
            elif rise >= 0.3:
                set_("WATCH", f"Unemployment +{rise:.1f}pp off 12m low")
        elif sid in ("NFCI", "ANFCI"):
            if cur > 0.4:
                set_("ALERT", "Financial conditions much tighter than average (2008-style stress)")
            elif cur > 0.0:
                set_("WATCH", "Financial conditions tighter than long-run average")
            else:
                self.note = self.note or "Negative = looser than average; calm"
        elif sid == "STLFSI4":
            if cur > 1.0:
                set_("ALERT", "Severe financial stress")
            elif cur > 0.0:
                set_("WATCH", "Above-average financial stress")
        elif sid == "CFNAI":
            m3 = statistics.fmean(self.mvalues[-3:]) if len(self.mvalues) >= 3 else cur
            if m3 < -0.70:
                set_("ALERT", f"CFNAI 3m avg {m3:.2f} below -0.70, the historical recession threshold")
            elif m3 < -0.35:
                set_("WATCH", f"CFNAI 3m avg {m3:.2f} signals below-trend growth")
            else:
                self.note = self.note or f"3m avg {m3:.2f} (recession threshold -0.70)"
        elif sid == "^VIX":
            if cur >= 33:
                set_("ALERT", "VIX in crisis regime (>33)")
            elif cur >= 25:
                set_("WATCH", "VIX elevated (>25)")
        elif sid == "^GSPC":
            ath = max(self.values)
            dd = (self.values[-1] / ath - 1) * 100
            ma200 = statistics.fmean(self.values[-200:]) if len(self.values) >= 200 else None
            below = ma200 is not None and self.values[-1] < ma200
            pts = 0
            if dd <= -20:
                pts = 4
            elif dd <= -15:
                pts = 3
            elif dd <= -10:
                pts = 2
            elif dd <= -5:
                pts = 1
            if below:
                pts += 1
            st = "OK" if pts <= 1 else ("WATCH" if pts <= 3 else "ALERT")
            pos = "below" if below else "above"
            set_(st, f"Drawdown from all-time high: {dd:.1f}%; price {pos} 200-day average")
            self.points = pts
        elif sid == "PERMIT":
            if cur <= -18:
                set_("ALERT", "Permits collapsing - housing led the economy down in 1973, 1980, 1990, 2007")
            elif cur <= -8:
                set_("WATCH", "Permits contracting - housing is the most reliable leading sector")
        elif sid == "UMCSENT":
            if cur <= 55:
                set_("ALERT", "Sentiment at recessionary extreme")
            elif cur <= 65:
                set_("WATCH", "Sentiment at levels seen around past recessions")

    def _set_status(self, status, note):
        order = {"OK": 0, "WATCH": 1, "ALERT": 2}
        # specials can raise or lower relative to generic scoring: they win
        self.status = status
        self.note = note
        if self.points is None:
            self.points = order[status] * 2


# ---------------------------------------------------------------------------
# Composite scoring, checklist, conclusions
# ---------------------------------------------------------------------------
STATUS_SCORE = {"OK": 0.0, "WATCH": 50.0, "ALERT": 100.0}


def category_scores(indicators):
    cats = {}
    for ind in indicators:
        if ind.status in STATUS_SCORE:
            cats.setdefault(ind.cat, []).append(STATUS_SCORE[ind.status])
    return {c: statistics.fmean(v) for c, v in cats.items() if v}


def build_checklist(by_id):
    """The 12 classic pre-crash conditions, each ON/OFF with historical context."""
    items = []

    def add(name, on, detail, precedent):
        items.append({"name": name, "on": bool(on), "detail": detail, "precedent": precedent})

    def ind(sid):
        return by_id.get(sid)

    i = ind("SAHMREALTIME")
    add("Sahm rule triggered", i and i.status == "ALERT",
        i.note if i and i.note else "n/a",
        "Marked the start of every recession since 1970")

    i = ind("UNRATE")
    add("Unemployment rising off its low", i and i.status in ("WATCH", "ALERT"),
        f"Now {i.current:.1f}%" if i and i.current is not None else "n/a",
        "Rose ahead of 2001, 2008, 2020 bear markets")

    i = ind("IC4WSA")
    add("Jobless claims trending up", i and i.status in ("WATCH", "ALERT"),
        f"4-wk avg {i.current:,.0f}" if i and i.current is not None else "n/a",
        "Claims turned up months before 1990, 2001, 2008 recessions")

    i = ind("T10Y3M")
    on = i and i.status in ("WATCH", "ALERT")
    add("Yield curve inverted / re-steepening", on,
        f"10y-3m = {i.current:+.2f}pp" if i and i.current is not None else "n/a",
        "Inverted before 1990, 2001, 2008, 2020; the crash usually comes on re-steepening")

    i = ind("BAMLH0A0HYM2")
    add("Credit spreads widening", i and i.status in ("WATCH", "ALERT"),
        f"HY OAS {i.current:.2f}pp" if i and i.current is not None else "n/a",
        "Spreads blew out ahead of the worst of 2000-02 and 2008")

    i = ind("NFCI")
    add("Financial conditions tightening", i and i.status in ("WATCH", "ALERT"),
        f"NFCI {i.current:+.2f}" if i and i.current is not None else "n/a",
        "Positive NFCI accompanied 1990, 2008, 2020 stress")

    i = ind("^GSPC")
    add("Equity market breaking down", i and i.status in ("WATCH", "ALERT"),
        i.note if i and i.note else "n/a",
        "A >10% drawdown below the 200-day average began most bear markets")

    i = ind("^VIX")
    add("Volatility regime shift", i and i.status in ("WATCH", "ALERT"),
        f"VIX {i.current:.1f}" if i and i.current is not None else "n/a",
        "VIX >25 persisted through 2000-02, 2008-09, 2020, 2022")

    i = ind("PERMIT")
    add("Housing rolling over", i and i.status in ("WATCH", "ALERT"),
        f"Permits {i.current:+.1f}% YoY" if i and i.current is not None else "n/a",
        "Housing peaked ~2 years before the 2007 top; led 1973, 1980, 1990")

    i = ind("UMCSENT")
    add("Consumer sentiment collapsed", i and i.status in ("WATCH", "ALERT"),
        f"UMich {i.current:.1f}" if i and i.current is not None else "n/a",
        "Sub-65 readings clustered around 1980, 2008, 2022 lows")

    i = ind("INDPRO")
    add("Industrial production contracting", i is not None and i.current is not None and i.current < 0,
        f"{i.current:+.1f}% YoY" if i and i.current is not None else "n/a",
        "Negative YoY IP accompanied every recession since 1950")

    i = ind("DFF")
    on = False
    detail = "n/a"
    if i and i.current is not None:
        v18 = i.metric_at(i.current_date - dt.timedelta(days=548))
        if v18 is not None:
            on = (i.current - v18) >= 1.0
            detail = f"Fed funds {i.current:.2f}% ({i.current - v18:+.2f}pp over 18m)"
    add("Fed tightening cycle underway", on, detail,
        "Aggressive hiking preceded 1973, 1980, 1987, 2000, 2007, 2022 declines")

    return items


def episode_similarity(by_id):
    """Apples-to-apples pairwise comparison: for every scoreable indicator that
    existed at each market top, is today's reading better, similar, or worse
    (direction-adjusted, 5%-of-historical-range tolerance band)?"""
    rows = []
    for date_s, label, dd, _etype in EPISODES:
        onset = dt.date.fromisoformat(date_s)
        better = similar = worse = 0
        drivers = []
        for ind in by_id.values():
            if ind.dir == 0 or ind.current is None or ind.error:
                continue
            then = ind.metric_at(onset)
            if then is None:
                continue
            cutoff = onset - dt.timedelta(days=int(15 * 365.25))
            window = sorted(v for d, v in zip(ind.mdates, ind.mvalues)
                            if cutoff <= d <= onset)
            spread = (window[-1] - window[0]) if len(window) > 10 else None
            tol = 0.05 * spread if spread else max(abs(then) * 0.10, 1e-9)
            diff = ind.dir * (ind.current - then)
            if diff > tol:
                worse += 1
                drivers.append((diff / tol, ind.name))
            elif diff < -tol:
                better += 1
            else:
                similar += 1
        n = better + similar + worse
        if n:
            net = (worse - better) / n
            if net > 0.15:
                verdict = "worse today"
            elif net < -0.15:
                verdict = "better today"
            else:
                verdict = "about the same"
            pct = round(100 * (worse + similar) / n)
        else:
            verdict, pct, net = "insufficient data", None, 0.0
        drivers.sort(reverse=True)
        rows.append({"date": onset, "label": label, "dd": dd, "n": n,
                     "better": better, "similar": similar, "worse": worse,
                     "net": net, "verdict": verdict, "pct": pct,
                     "drivers": [name for _, name in drivers[:3]]})
    return rows


def healthy_dates(sp):
    """Control sample of certified 'healthy market' moments: two dates per year
    since 1965, excluding anything within 1y before / 2y after a crash onset,
    and excluding dates where the S&P fell >15% within the following year."""
    out = []
    last = sp.dates[-1] if sp and sp.dates else dt.date.today()
    onsets = [dt.date.fromisoformat(ds) for ds, *_ in EPISODES]
    for y in range(1965, dt.date.today().year + 1):
        for m in (3, 9):
            d = dt.date(y, m, 1)
            if d + dt.timedelta(days=365) > last:
                continue  # too recent to certify as healthy
            if any(o - dt.timedelta(days=365) <= d <= o + dt.timedelta(days=730)
                   for o in onsets):
                continue
            if sp and sp.dates:
                _, pv = last_on_or_before(sp.dates, sp.values, d)
                if pv is None:
                    continue
                i0 = bisect.bisect_right(sp.dates, d)
                i1 = bisect.bisect_right(sp.dates, d + dt.timedelta(days=365))
                fwd = sp.values[i0:i1]
                if not fwd or min(fwd) / pv - 1 < -0.15:
                    continue
            out.append(d)
    return out


def factor_analysis(indicators, by_id, checklist_today):
    """Empirically find the factors that past crashes shared but healthy markets
    lacked: evaluate every indicator condition (WATCH/ALERT) and checklist rule
    at all crash tops AND at the healthy control dates, rank by separation,
    then check which of the top factors are flashing today."""
    sp = by_id.get("^GSPC")
    bull_dates = healthy_dates(sp)

    def states_at(d):
        by_ep = {ind.sid: ind.as_of(d) for ind in indicators}
        st = {}
        for i in by_ep.values():
            if i.dir != 0 and i.status in ("OK", "WATCH", "ALERT"):
                st[("ind", i.sid)] = i.status in ("WATCH", "ALERT")
        for c in build_checklist(by_ep):
            if c["detail"] != "n/a":
                st[("chk", c["name"])] = c["on"]
        return st

    crash_states = [(etype, states_at(dt.date.fromisoformat(ds)))
                    for ds, _lbl, _dd, etype in EPISODES]
    bull_states = [states_at(d) for d in bull_dates]

    today_states = {}
    for i in indicators:
        if i.dir != 0 and i.status in ("OK", "WATCH", "ALERT"):
            today_states[("ind", i.sid)] = i.status in ("WATCH", "ALERT")
    for c in checklist_today:
        if c["detail"] != "n/a":
            today_states[("chk", c["name"])] = c["on"]

    keys = set()
    for _t, s in crash_states:
        keys.update(s)
    for s in bull_states:
        keys.update(s)

    def screen(states, min_crash_n, min_gap):
        """Rank factors by separation between the given crash states and healthy dates."""
        out = []
        for k in keys:
            cvals = [s[k] for s in states if k in s]
            bvals = [s[k] for s in bull_states if k in s]
            if len(cvals) < min_crash_n or len(bvals) < 10:
                continue
            crate = sum(cvals) / len(cvals)
            brate = sum(bvals) / len(bvals)
            if crate - brate <= min_gap:
                continue
            kind, key = k
            name = by_id[key].name if kind == "ind" else key
            out.append({"kind": kind, "name": name,
                        "crash_on": sum(cvals), "crash_n": len(cvals),
                        "crash_rate": crate, "bull_rate": brate,
                        "gap": crate - brate, "today": today_states.get(k)})
        out.sort(key=lambda f: (-f["gap"], f["kind"]))
        # a checklist rule and its backing indicator produce identical stats -
        # keep only one of each such pair
        seen, deduped = set(), []
        for f in out:
            sig = (f["crash_on"], f["crash_n"], round(f["bull_rate"], 4), f["today"])
            if sig in seen:
                continue
            seen.add(sig)
            deduped.append(f)
        return deduped

    def summarize(factors):
        known = [f for f in factors if f["today"] is not None]
        today_pct = (100 * sum(1 for f in known if f["today"]) / len(known)) if known else 0.0
        crash_avg = 100 * statistics.fmean(f["crash_rate"] for f in factors) if factors else 0.0
        bull_avg = 100 * statistics.fmean(f["bull_rate"] for f in factors) if factors else 0.0
        span = max(crash_avg - bull_avg, 1e-9)
        position = min(max((today_pct - bull_avg) / span, 0.0), 1.0)
        return today_pct, crash_avg, bull_avg, position

    # overall signature (all crash types blended)
    factors = screen([s for _t, s in crash_states], min_crash_n=5, min_gap=0.10)[:20]
    today_pct, crash_avg, bull_avg, position = summarize(factors)

    # one signature + flag per crash type
    pos_flags = ((0.25, "GREEN"), (0.50, "YELLOW"), (0.75, "ORANGE"), (9.9, "RED"))
    types = []
    for tkey, (tname, tdesc, teps, flaggable) in CRASH_TYPES.items():
        tstates = [s for et, s in crash_states if et == tkey]
        # factor must have flashed at >=60% of this type's tops and rarely in bulls
        tf = screen(tstates, min_crash_n=max(1, (len(tstates) + 1) // 2),
                    min_gap=0.25) if flaggable else []
        tf = [f for f in tf if f["crash_rate"] >= 0.60][:10]
        entry = {"key": tkey, "name": tname, "desc": tdesc, "episodes": teps,
                 "n_eps": len(tstates), "factors": tf, "flag": None,
                 "today_pct": None, "crash_avg": None, "bull_avg": None,
                 "position": None}
        if flaggable and len(tf) >= 3:
            tp, ca, ba, pos = summarize(tf)
            entry.update({"today_pct": tp, "crash_avg": ca, "bull_avg": ba,
                          "position": pos,
                          "flag": next(fl for th, fl in pos_flags if pos < th)})
        types.append(entry)

    return {"factors": factors, "n_bull": len(bull_dates), "n_crash": len(crash_states),
            "today_pct": today_pct, "crash_avg": crash_avg, "bull_avg": bull_avg,
            "position": position, "types": types}


def build_subcards(by_id):
    """Evaluate the sub-category threat watches against today's indicators."""
    pos_flags = ((0.25, "GREEN"), (0.50, "YELLOW"), (0.75, "ORANGE"), (9.9, "RED"))
    out = []
    for key, parent, name, desc, wires in SUBCARDS:
        trips = []
        for sid, label, test in wires:
            ind = by_id.get(sid)
            on = None
            if ind is not None and not ind.error and ind.current is not None:
                if test == "status":
                    if ind.status in ("OK", "WATCH", "ALERT"):
                        on = ind.status in ("WATCH", "ALERT")
                elif test.startswith("pct_hi:"):
                    if ind.pctile is not None:
                        on = ind.pctile >= float(test.split(":")[1])
                elif test.startswith("pct_lo:"):
                    if ind.pctile is not None:
                        on = ind.pctile <= float(test.split(":")[1])
            trips.append({"label": label, "on": on})
        known = [t for t in trips if t["on"] is not None]
        entry = {"key": key, "parent": parent, "name": name, "desc": desc,
                 "trips": trips, "n_on": sum(1 for t in known if t["on"]),
                 "n_known": len(known), "position": None, "flag": None}
        if wires and len(known) >= max(2, len(wires) // 2):
            entry["position"] = entry["n_on"] / len(known)
            entry["flag"] = next(fl for th, fl in pos_flags if entry["position"] < th)
        out.append(entry)
    return out


def episode_snapshots(indicators):
    """Run the full scoring engine as of each historical market top."""
    snaps = []
    for date_s, label, dd, etype in EPISODES:
        asof = dt.date.fromisoformat(date_s)
        by_ep = {ind.sid: ind.as_of(asof) for ind in indicators}
        cs = category_scores(by_ep.values())
        chk = build_checklist(by_ep)
        sc, fl, _msg, non = overall_flag(cs, chk)
        n_scored = sum(1 for i in by_ep.values() if i.status in STATUS_SCORE)
        snaps.append({"label": label, "date": asof, "dd": dd, "etype": etype,
                      "score": sc, "flag": fl, "n_on": non, "n_chk": len(chk),
                      "cat_scores": cs, "n_scored": n_scored})
    return snaps


def overall_flag(cat_scores, checklist):
    total_w = sum(CATEGORY_WEIGHTS.get(c, 5) for c in cat_scores)
    cat_part = (sum(cat_scores[c] * CATEGORY_WEIGHTS.get(c, 5) for c in cat_scores) / total_w
                if total_w else 0.0)
    n_on = sum(1 for c in checklist if c["on"])
    chk_part = 100.0 * n_on / max(len(checklist), 1)
    score = 0.75 * cat_part + 0.25 * chk_part
    if score < 20:
        flag, msg = "GREEN", "No crash signature detected"
    elif score < 40:
        flag, msg = "YELLOW", "Some classic warning signs present - stay vigilant"
    elif score < 60:
        flag, msg = "ORANGE", "Multiple pre-crash conditions present - elevated risk"
    else:
        flag, msg = "RED", "Crash-signature conditions are widespread"
    return round(score, 1), flag, msg, n_on


def write_conclusions(score, flag, n_on, checklist, episodes, by_id, cat_scores,
                      ep_snaps=None, factor_summary=None, subcards=None):
    """Deterministic reasoning -> prose conclusions."""
    paras = []
    on_items = [c for c in checklist if c["on"]]
    off_items = [c for c in checklist if not c["on"]]

    p = (f"Composite crash-risk score is {score}/100 ({flag}). "
         f"{n_on} of {len(checklist)} classic pre-crash conditions are currently present.")
    paras.append(p)

    if factor_summary and factor_summary["factors"]:
        fs = factor_summary
        pos = fs["position"]
        if pos < 0.25:
            verdict = "much closer to typical healthy-market conditions than to a pre-crash top"
        elif pos < 0.5:
            verdict = "closer to healthy-market conditions than to a pre-crash top, but drifting"
        elif pos < 0.75:
            verdict = "closer to typical pre-crash conditions than to a healthy market"
        else:
            verdict = "at levels typical of past pre-crash tops"
        flashing = [f["name"] for f in fs["factors"] if f["today"]][:6]
        p = (f"THE CRASH-SIGNATURE TEST (the most direct better-or-worse yardstick): of the "
             f"{len(fs['factors'])} factors that best separate pre-crash tops from healthy bull markets, "
             f"{fs['today_pct']:.0f}% are flashing today, versus {fs['crash_avg']:.0f}% at a typical pre-crash "
             f"top and {fs['bull_avg']:.0f}% in a typical healthy market - {verdict}.")
        if flashing:
            p += " Signature factors flashing now: " + ", ".join(flashing) + "."
        paras.append(p)
        typed = [t for t in fs.get("types", [])]
        if typed:
            bits = []
            for t in typed:
                if t["flag"]:
                    bits.append(f"{t['name']}: {t['flag']} ({t['today_pct']:.0f}% of its signature flashing)")
                else:
                    bits.append(f"{t['name']}: no early-warning signature exists - not flaggable in advance")
            paras.append("Flags by type of bad market - " + "; ".join(bits) + ".")
        if subcards:
            sbits = []
            for s in subcards:
                if s["flag"]:
                    sbits.append(f"{s['name']}: {s['flag']} ({s['n_on']}/{s['n_known']} tripwires)")
            if sbits:
                paras.append("Sub-category threat watches - " + "; ".join(sbits) + ".")

    if on_items:
        names = "; ".join(f"{c['name'].lower()} ({c['detail']})" for c in on_items[:6])
        paras.append("Warning signs currently active: " + names + ".")
    if off_items:
        names = ", ".join(c["name"].lower() for c in off_items[:6])
        paras.append("Notably absent: " + names +
                     ". Historically, major crashes (2000, 2007) were preceded by several of these "
                     "turning on together, not in isolation.")

    scored = [e for e in episodes if e["pct"] is not None and e["n"] >= 8]
    if scored:
        b = sum(1 for e in scored if e["verdict"] == "better today")
        w = sum(1 for e in scored if e["verdict"] == "worse today")
        s = len(scored) - b - w
        paras.append(
            f"Better or worse than the bad moments? Comparing only the indicators that existed at each top: "
            f"today's conditions are HEALTHIER than {b}, ABOUT THE SAME as {s}, and WORSE than {w} of the "
            f"{len(scored)} pre-crash moments (majority vote of shared indicators; see the verdict table).")
        best = max(scored, key=lambda e: e["pct"])
        paras.append(
            f"Closest historical analog: {best['label']} (market top {best['date']}, eventual drawdown {best['dd']}). "
            f"Today's readings are at least as stressed as that top on {best['pct']}% of {best['n']} comparable indicators. "
            "A high match means conditions RESEMBLE that pre-crash moment; it is a similarity measure, not a prediction.")

    reliable = [s for s in (ep_snaps or []) if s["n_scored"] >= 15]
    if reliable:
        ep_scores = sorted(s["score"] for s in reliable)
        med = ep_scores[len(ep_scores) // 2]
        higher = sum(1 for s in reliable if s["score"] > score)
        paras.append(
            f"Running today's exact rulebook at {len(reliable)} past market tops: the median pre-crash "
            f"score was {med:.0f}/100 (range {ep_scores[0]:.0f}-{ep_scores[-1]:.0f}). Today's {score} is "
            f"lower than {higher} of {len(reliable)} pre-crash readings"
            + (" - current conditions score below what this framework showed on the eve of most historical crashes."
               if higher >= len(reliable) * 0.6 else
               " - current conditions score in the same range this framework showed on the eve of past crashes, which argues for caution.")
        )

    worst_cats = sorted(cat_scores.items(), key=lambda kv: -kv[1])[:3]
    if worst_cats and worst_cats[0][1] > 0:
        paras.append("Most stressed areas right now: "
                     + ", ".join(f"{c} ({s:.0f}/100)" for c, s in worst_cats if s > 0) + ".")

    sahm = by_id.get("SAHMREALTIME")
    hy = by_id.get("BAMLH0A0HYM2")
    calm_bits = []
    if sahm and sahm.status == "OK":
        calm_bits.append("the labor market has not crossed recession thresholds (Sahm rule off)")
    if hy and hy.status == "OK":
        calm_bits.append("credit markets are not pricing distress")
    if calm_bits and flag in ("GREEN", "YELLOW"):
        paras.append("The strongest reassurance: " + " and ".join(calm_bits) +
                     ". Since 1970 no major (>30%) crash has unfolded with both of these calm.")

    paras.append("Caveats: most warning signals produce false positives; corrections of 5-10% happen in most years "
                 "regardless of conditions; exogenous shocks (1987 program trading, COVID) can strike without "
                 "macro warning. This dashboard measures resemblance to past pre-crash conditions - it cannot "
                 "time the market and is not investment advice.")
    return paras


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
def append_history(score, flag, n_alert, n_watch, sp_dd):
    new = not os.path.exists(HISTORY_PATH)
    with open(HISTORY_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["run_time", "score", "flag", "alerts", "watches", "sp500_drawdown_pct"])
        w.writerow([dt.datetime.now().strftime("%Y-%m-%d %H:%M"), score, flag,
                    n_alert, n_watch, f"{sp_dd:.1f}" if sp_dd is not None else ""])


def read_history(limit=20):
    if not os.path.exists(HISTORY_PATH):
        return []
    with open(HISTORY_PATH, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows[-limit:]


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------
FLAG_COLORS = {"GREEN": "#1db954", "YELLOW": "#e6c200", "ORANGE": "#ff7a00", "RED": "#e53935"}
STATUS_COLORS = {"OK": "#1db954", "WATCH": "#e6c200", "ALERT": "#e53935",
                 "INFO": "#5b7a99", "N/A": "#666"}


CAT_SHORT = {CAT_CURVE: "Curve", CAT_INFL: "Inflation", CAT_CREDIT: "Credit",
             CAT_MONEY: "Money", CAT_LABOR: "Labor", CAT_GROWTH: "Growth",
             CAT_HOUSING: "Housing", CAT_CONSUMER: "Consumer",
             CAT_FISCAL: "Fiscal", CAT_MARKET: "Markets"}


def svg_gauge(score, flag, w=220):
    """Semicircular dial: green/yellow/orange/red zones, needle at score."""
    cx, cy, r = 110, 108, 82
    h = round(w * 132 / 220)

    def pt(f, rad=r):
        a = math.pi * f
        return cx - rad * math.cos(a), cy - rad * math.sin(a)

    def arc(f1, f2, color):
        x1, y1 = pt(f1)
        x2, y2 = pt(f2)
        return (f'<path d="M {x1:.1f} {y1:.1f} A {r} {r} 0 0 1 {x2:.1f} {y2:.1f}" '
                f'fill="none" stroke="{color}" stroke-width="17"/>')

    zones = (arc(0.00, 0.20, FLAG_COLORS["GREEN"]) + arc(0.20, 0.40, FLAG_COLORS["YELLOW"])
             + arc(0.40, 0.60, FLAG_COLORS["ORANGE"]) + arc(0.60, 1.00, FLAG_COLORS["RED"]))
    nx, ny = pt(min(max(score, 0), 100) / 100.0, r - 22)
    fc = FLAG_COLORS[flag]
    ticks = ""
    for v in (0, 20, 40, 60, 80, 100):
        tx, ty = pt(v / 100.0, r + 14)
        ticks += (f'<text x="{tx:.0f}" y="{ty:.0f}" font-size="9" fill="#8b97a6" '
                  f'text-anchor="middle">{v}</text>')
    return f'''<svg viewBox="0 0 220 132" width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg">
{zones}{ticks}
<line x1="{cx}" y1="{cy}" x2="{nx:.1f}" y2="{ny:.1f}" stroke="#dfe6ee" stroke-width="3.5" stroke-linecap="round"/>
<circle cx="{cx}" cy="{cy}" r="6" fill="#dfe6ee"/>
<text x="{cx}" y="{cy - 24}" font-size="26" font-weight="800" fill="{fc}" text-anchor="middle">{score:.0f}</text>
<text x="{cx}" y="{cy + 20}" font-size="13" font-weight="700" fill="{fc}" text-anchor="middle" letter-spacing="2">{flag}</text>
</svg>'''


def svg_donut(n_on, total, w=132):
    """Donut: share of pre-crash checklist signals currently on."""
    cx = cy = 66
    r = 48
    c = 2 * math.pi * r
    on_len = c * n_on / max(total, 1)
    col = "#e53935" if n_on else "#1db954"
    return f'''<svg viewBox="0 0 132 132" width="{w}" height="{w}" xmlns="http://www.w3.org/2000/svg">
<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#1db954" stroke-width="15" opacity="0.35"/>
<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{col}" stroke-width="15"
 stroke-dasharray="{on_len:.1f} {c:.1f}" transform="rotate(-90 {cx} {cy})" stroke-linecap="butt"/>
<text x="{cx}" y="{cy - 2}" font-size="24" font-weight="800" fill="#dfe6ee" text-anchor="middle">{n_on}/{total}</text>
<text x="{cx}" y="{cy + 18}" font-size="10" fill="#8b97a6" text-anchor="middle">signals on</text>
</svg>'''


def svg_radar(cat_scores, flag, w=270):
    """Spider chart of risk by category (0-100 per axis)."""
    cats = [c for c in CATEGORY_WEIGHTS if c in cat_scores]
    n = len(cats)
    if n < 3:
        return ""
    cx, cy, R = 135, 118, 78
    h = round(w * 240 / 270)
    fc = FLAG_COLORS[flag]

    def pt(i, frac):
        a = -math.pi / 2 + 2 * math.pi * i / n
        return cx + R * frac * math.cos(a), cy + R * frac * math.sin(a)

    rings = ""
    for frac in (0.25, 0.5, 0.75, 1.0):
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in (pt(i, frac) for i in range(n)))
        rings += f'<polygon points="{pts}" fill="none" stroke="#2a3140" stroke-width="1"/>'
    axes, labels = "", ""
    for i, cat in enumerate(cats):
        x, y = pt(i, 1.0)
        axes += f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" stroke="#2a3140" stroke-width="1"/>'
        lx, ly = pt(i, 1.0)
        lx = cx + (lx - cx) * 1.22
        ly = cy + (ly - cy) * 1.22 + 3
        labels += (f'<text x="{lx:.1f}" y="{ly:.1f}" font-size="9.5" fill="#9fb0c3" '
                   f'text-anchor="middle">{html.escape(CAT_SHORT.get(cat, cat))}</text>')
    data_pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in
                        (pt(i, min(cat_scores[c], 100) / 100.0) for i, c in enumerate(cats)))
    dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.6" fill="{fc}"/>' for x, y in
                   (pt(i, min(cat_scores[c], 100) / 100.0) for i, c in enumerate(cats)))
    return f'''<svg viewBox="0 0 270 240" width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg">
{rings}{axes}
<polygon points="{data_pts}" fill="{fc}" fill-opacity="0.22" stroke="{fc}" stroke-width="2"/>
{dots}{labels}
</svg>'''


def svg_trend(history):
    """Composite score across recent runs, dots colored by flag."""
    hs = history[-20:]
    if not hs:
        return ""
    W, H = 330, 150
    x0, x1, y0, y1 = 34, W - 14, 14, H - 30

    def sx(i):
        return x0 + (x1 - x0) * (i / max(len(hs) - 1, 1))

    def sy(v):
        return y1 - (y1 - y0) * (min(max(v, 0), 100) / 100.0)

    grid = ""
    for v, lbl in ((0, "0"), (20, "20"), (40, "40"), (60, "60"), (100, "100")):
        gy = sy(v)
        grid += (f'<line x1="{x0}" y1="{gy:.1f}" x2="{x1}" y2="{gy:.1f}" stroke="#2a3140" '
                 f'stroke-width="1" stroke-dasharray="3 4"/>'
                 f'<text x="{x0 - 6}" y="{gy + 3:.1f}" font-size="9" fill="#8b97a6" text-anchor="end">{lbl}</text>')
    pts, dots = [], ""
    for i, h in enumerate(hs):
        try:
            v = float(h.get("score", "") or 0)
        except ValueError:
            v = 0.0
        x, y = sx(i), sy(v)
        pts.append(f"{x:.1f},{y:.1f}")
        col = FLAG_COLORS.get(h.get("flag", ""), "#8b97a6")
        dots += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.4" fill="{col}"><title>{html.escape(h.get("run_time",""))}: {v:g} ({html.escape(h.get("flag",""))})</title></circle>'
    line = (f'<polyline points="{" ".join(pts)}" fill="none" stroke="#6ab7ff" stroke-width="2"/>'
            if len(pts) > 1 else "")
    lbl_a = html.escape((hs[0].get("run_time", "") or "")[:10])
    lbl_b = html.escape((hs[-1].get("run_time", "") or "")[:16])
    return f'''<svg viewBox="0 0 {W} {H}" width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">
{grid}{line}{dots}
<text x="{x0}" y="{H - 8}" font-size="9" fill="#8b97a6">{lbl_a}</text>
<text x="{x1}" y="{H - 8}" font-size="9" fill="#8b97a6" text-anchor="end">{lbl_b}</text>
</svg>'''


def svg_signature(crash_avg, today_pct, bull_avg, flag, w=None):
    """Three bars: share of crash-signature factors flashing at a typical
    pre-crash top, today, and in a typical healthy market."""
    rows = [("Typical pre-crash top", crash_avg, "#e53935"),
            ("TODAY", today_pct, FLAG_COLORS[flag]),
            ("Typical healthy market", bull_avg, "#1db954")]
    rh, x0, W = 34, 186, 600
    w = w or W
    xmax = W - 62
    H = len(rows) * rh + 28

    def sx(v):
        return x0 + (xmax - x0) * min(max(v, 0), 100) / 100.0

    grid = ""
    for v in (25, 50, 75, 100):
        gx = sx(v)
        grid += (f'<line x1="{gx:.1f}" y1="8" x2="{gx:.1f}" y2="{H - 20}" '
                 f'stroke="#2a3140" stroke-width="1" stroke-dasharray="3 4"/>'
                 f'<text x="{gx:.1f}" y="{H - 6}" font-size="9" fill="#8b97a6" '
                 f'text-anchor="middle">{v}%</text>')
    bars = ""
    for i, (label, v, col) in enumerate(rows):
        y = 10 + i * rh
        fw = "800" if label == "TODAY" else "400"
        lc = "#ffffff" if label == "TODAY" else "#9fb0c3"
        outline = ' stroke="#dfe6ee" stroke-width="1.6"' if label == "TODAY" else ""
        bars += (f'<text x="{x0 - 8}" y="{y + 15}" font-size="11.5" fill="{lc}" '
                 f'font-weight="{fw}" text-anchor="end">{label}</text>'
                 f'<rect x="{x0}" y="{y}" width="{max(sx(v) - x0, 2):.1f}" height="19" rx="4" '
                 f'fill="{col}"{outline}/>'
                 f'<text x="{sx(v) + 6:.1f}" y="{y + 14}" font-size="11.5" fill="#dfe6ee" '
                 f'font-weight="{fw}">{v:.0f}%</text>')
    return (f'<svg viewBox="0 0 {W} {H}" width="{w}" height="{round(w * H / W)}" '
            f'xmlns="http://www.w3.org/2000/svg">{grid}{bars}</svg>')


def svg_meter(position, w=340, left_label="typical healthy market",
              right_label="eve of this type of crash"):
    """Distance meter: how far today's conditions have travelled from typical
    healthy-market conditions (0%) toward conditions typical on the eve of
    this type of crash (100%)."""
    W, H = 360, 78
    x0, x1, y, bh = 16, W - 16, 38, 13
    pos = min(max(position, 0.0), 1.0)
    px = x0 + (x1 - x0) * pos
    zones = ""
    for f0, f1, col in ((0.00, 0.25, FLAG_COLORS["GREEN"]),
                        (0.25, 0.50, FLAG_COLORS["YELLOW"]),
                        (0.50, 0.75, FLAG_COLORS["ORANGE"]),
                        (0.75, 1.00, FLAG_COLORS["RED"])):
        zx = x0 + (x1 - x0) * f0
        zw = (x1 - x0) * (f1 - f0)
        zones += f'<rect x="{zx:.1f}" y="{y}" width="{zw:.1f}" height="{bh}" fill="{col}" opacity="0.85"/>'
    marker = (f'<polygon points="{px:.1f},{y - 3} {px - 7:.1f},{y - 15} {px + 7:.1f},{y - 15}" fill="#ffffff"/>'
              f'<line x1="{px:.1f}" y1="{y - 2}" x2="{px:.1f}" y2="{y + bh + 2}" stroke="#ffffff" stroke-width="2"/>'
              f'<text x="{min(max(px, x0 + 18), x1 - 18):.1f}" y="{y - 20}" font-size="15" font-weight="800" '
              f'fill="#ffffff" text-anchor="middle">{pos * 100:.0f}%</text>')
    labels = (f'<text x="{x0}" y="{y + bh + 16}" font-size="9.5" fill="#8b97a6">{html.escape(left_label)}</text>'
              f'<text x="{x1}" y="{y + bh + 16}" font-size="9.5" fill="#8b97a6" '
              f'text-anchor="end">{html.escape(right_label)}</text>')
    return (f'<svg viewBox="0 0 {W} {H}" width="{w}" height="{round(w * H / W)}" '
            f'xmlns="http://www.w3.org/2000/svg"><rect x="{x0}" y="{y}" width="{x1 - x0}" '
            f'height="{bh}" rx="6" fill="#232b39"/>{zones}{marker}{labels}</svg>')


def svg_rank(ep_snaps, score, flag):
    """Horizontal bar ranking: TODAY among all historical pre-crash scores."""
    entries = ([{"label": "TODAY", "score": score, "flag": flag, "today": True}]
               + [{"label": s["label"], "score": s["score"], "flag": s["flag"],
                   "today": False} for s in ep_snaps])
    entries.sort(key=lambda x: -x["score"])
    rh, x0, W = 24, 168, 620
    xmax = W - 52
    H = len(entries) * rh + 34

    def sx(v):
        return x0 + (xmax - x0) * min(max(v, 0), 100) / 100.0

    grid = ""
    for v in (20, 40, 60, 80, 100):
        gx = sx(v)
        grid += (f'<line x1="{gx:.1f}" y1="10" x2="{gx:.1f}" y2="{H - 24}" '
                 f'stroke="#2a3140" stroke-width="1" stroke-dasharray="3 4"/>'
                 f'<text x="{gx:.1f}" y="{H - 10}" font-size="9" fill="#8b97a6" '
                 f'text-anchor="middle">{v}</text>')
    bars = ""
    for i, en in enumerate(entries):
        y = 12 + i * rh
        col = FLAG_COLORS[en["flag"]]
        outline = ' stroke="#dfe6ee" stroke-width="1.6"' if en["today"] else ""
        weight = "800" if en["today"] else "400"
        lab_col = "#ffffff" if en["today"] else "#9fb0c3"
        bars += (f'<text x="{x0 - 8}" y="{y + 13}" font-size="11" fill="{lab_col}" '
                 f'font-weight="{weight}" text-anchor="end">{html.escape(en["label"])}</text>'
                 f'<rect x="{x0}" y="{y}" width="{max(sx(en["score"]) - x0, 2):.1f}" height="16" '
                 f'rx="4" fill="{col}"{outline}/>'
                 f'<text x="{sx(en["score"]) + 6:.1f}" y="{y + 13}" font-size="11" '
                 f'fill="#dfe6ee" font-weight="{weight}">{en["score"]:g}</text>')
    return (f'<svg viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
            f'xmlns="http://www.w3.org/2000/svg">{grid}{bars}</svg>')


def fmt(v, unit=""):
    if v is None:
        return "-"
    if abs(v) >= 10000:
        s = f"{v:,.0f}"
    elif abs(v) >= 100:
        s = f"{v:,.1f}"
    else:
        s = f"{v:,.2f}"
    return s + (" " + unit if unit and unit not in ("%", "pp") else unit if unit else "")


def render_episode_cards(ep_snaps, score, flag, n_on, n_chk, cat_scores, n_scored_today):
    """Side-by-side cards: TODAY plus every historical market top, same visuals."""
    e = html.escape

    def card(title, sub, dd, sc, fl, on, chk, cs, n_scored, highlight=False):
        fc = FLAG_COLORS[fl]
        border = f"border:2px solid {fc};" if highlight else ""
        badge = (f'<span class="chip" style="background:{fc}">{fl}</span>')
        ddtxt = f'<span class="epdd">{e(dd)}</span>' if dd else ""
        radar = svg_radar(cs, fl, w=196) or '<div class="muted" style="padding:30px 0">too few categories</div>'
        return f"""<div class="epcard" style="{border}">
          <div class="ephead"><b>{e(title)}</b> {badge}</div>
          <div class="epmeta">{e(sub)} {ddtxt}</div>
          {svg_gauge(sc, fl, w=176)}
          <div class="eprow">{svg_donut(on, chk, w=84)}
            <div class="epstats">score <b>{sc:g}</b>/100<br>{on}/{chk} signals on<br>
            <span class="muted">{n_scored} indicators scored</span></div></div>
          {radar}
        </div>"""

    cards = ""
    today = {"label": "TODAY", "date": dt.date.today(), "dd": "", "score": score,
             "flag": flag, "n_on": n_on, "n_chk": n_chk, "cat_scores": cat_scores,
             "n_scored": n_scored_today, "etype": None}
    for i, s in enumerate([today] + ep_snaps):
        n_sc = s["n_scored"] if s["n_scored"] is not None else "-"
        sub = f"market top {s['date']}" if i else str(s["date"])
        if s.get("etype"):
            sub += f" · {CRASH_TYPES[s['etype']][0]}"
        cards += card(s["label"], sub,
                      s["dd"], s["score"], s["flag"], s["n_on"], s["n_chk"],
                      s["cat_scores"], n_sc, highlight=(i == 0))
    return cards


def render_report(indicators, by_id, cat_scores, checklist, episodes, score, flag,
                  msg, n_on, conclusions, history, prev_score, fred_key_missing,
                  failures, ep_snaps, factor_summary, subcards):
    e = html.escape
    now = dt.datetime.now().strftime("%A, %B %d %Y at %H:%M")
    fc = FLAG_COLORS[flag]
    n_ok = sum(1 for i in indicators if i.status == "OK")
    n_watch = sum(1 for i in indicators if i.status == "WATCH")
    n_alert = sum(1 for i in indicators if i.status == "ALERT")
    n_info = sum(1 for i in indicators if i.status == "INFO")
    n_na = sum(1 for i in indicators if i.status == "N/A")

    delta_txt = ""
    if prev_score is not None:
        d = score - prev_score
        arrow = "&#9650;" if d > 0 else ("&#9660;" if d < 0 else "&#9654;")
        delta_txt = f'<span class="delta">{arrow} {d:+.1f} vs previous run ({prev_score})</span>'

    key_banner = ""
    if fred_key_missing:
        key_banner = """
        <div class="banner">
          <b>FRED API key missing &mdash; running in market-only mode ({n} of 100 indicators live).</b><br>
          To activate all 100 indicators (takes ~2 minutes, free):
          <ol>
            <li>Create a free account at <a href="https://fredaccount.stlouisfed.org/apikeys">fredaccount.stlouisfed.org/apikeys</a> and click "Request API Key"</li>
            <li>Open <code>config.json</code> in this folder and paste the key: <code>{{"fred_api_key": "YOUR_KEY_HERE"}}</code></li>
            <li>Run the monitor again (double-click <code>run_now.bat</code>)</li>
          </ol>
        </div>""".format(n=sum(1 for i in indicators if not i.error))

    # checklist rows
    chk_rows = ""
    for c in checklist:
        dot = "&#9679;"
        col = "#e53935" if c["on"] else "#1db954"
        state = "PRESENT" if c["on"] else "absent"
        chk_rows += f"""<tr>
          <td><span style="color:{col}">{dot}</span> {e(c['name'])}</td>
          <td class="{'on' if c['on'] else 'off'}">{state}</td>
          <td>{e(c['detail'])}</td>
          <td class="muted">{e(c['precedent'])}</td></tr>"""

    # crash-signature factor table
    factor_rows = ""
    for f in (factor_summary["factors"] if factor_summary else []):
        kind = "rule" if f["kind"] == "chk" else "indicator"
        cp = 100 * f["crash_rate"]
        bp = 100 * f["bull_rate"]
        gp = 100 * f["gap"]
        if f["today"] is None:
            tchip = '<span class="chip" style="background:#666">n/a</span>'
        elif f["today"]:
            tchip = '<span class="chip" style="background:#e53935">FLASHING</span>'
        else:
            tchip = '<span class="chip" style="background:#1db954">clear</span>'
        factor_rows += f"""<tr>
          <td>{e(f['name'])} <span class="muted">({kind})</span></td>
          <td><div class="bar"><div style="width:{cp:.0f}%;background:#e53935"></div></div>
              {f['crash_on']}/{f['crash_n']} tops ({cp:.0f}%)</td>
          <td><div class="bar"><div style="width:{bp:.0f}%;background:#1db954"></div></div> {bp:.0f}%</td>
          <td><b>{gp:+.0f}pp</b></td>
          <td>{tchip}</td></tr>"""

    signature_html = ""
    if factor_summary and factor_summary["factors"]:
        fs = factor_summary
        signature_html = f"""
<h2>The crash signature: factors past crashes shared but healthy markets lacked</h2>
<div class="muted" style="margin-bottom:8px">Each factor below was checked at all {fs['n_crash']} pre-crash market
tops AND at {fs['n_bull']} certified healthy-market control dates (algorithmically chosen: at least 1 year from any
crash, no &gt;15% drawdown in the following year). Only factors that flash much more often before crashes than in
healthy markets make this list &mdash; ranked by that separation. The right column is the answer to "how are we
doing now" on the evidence that actually discriminates.</div>
<div class="vizcard" style="max-width:660px;margin-bottom:14px">
  <div class="vtitle">Share of the {len(fs['factors'])} crash-signature factors currently flashing</div>
  {svg_signature(fs['crash_avg'], fs['today_pct'], fs['bull_avg'], flag)}
  <div class="muted" style="margin-top:4px">Today sits {fs['position'] * 100:.0f}% of the way from typical
  healthy conditions to typical pre-crash conditions.</div>
</div>
<table><tr><th>Factor</th><th>Flashing before crashes</th><th>Flashing in healthy markets</th><th>Separation</th><th>TODAY</th></tr>
{factor_rows}</table>"""

    # THE headline section: distance to each type of bad market
    types_html = ""
    type_line = ""
    if factor_summary and factor_summary.get("types"):
        fs = factor_summary
        type_cards = ""
        line_bits = []
        for t in fs.get("types", []):
            fc2 = FLAG_COLORS.get(t["flag"], "#666")
            chip = (f'<span class="chip" style="background:{fc2}">{t["flag"] or "NOT FLAGGABLE"}</span>')
            chips = ""
            for f2 in t["factors"]:
                if f2["today"] is None:
                    c2, mark = "#666", "?"
                elif f2["today"]:
                    c2, mark = "#e53935", "&#9679;"
                else:
                    c2, mark = "#1db954", "&#9679;"
                chips += (f'<span class="fchip" style="border-color:{c2}">'
                          f'<span style="color:{c2}">{mark}</span> {e(f2["name"])}</span> ')
            if t["flag"]:
                line_bits.append(f'<span><b style="color:{fc2}">{e(t["name"])}</b>: '
                                 f'{t["position"] * 100:.0f}% of the way</span>')
                body = f"""{svg_meter(t['position'], w=330)}
                <div class="muted" style="margin:2px 0 6px">{t['today_pct']:.0f}% of this type's signature is
                flashing today (typical top of this type: {t['crash_avg']:.0f}%, typical healthy market:
                {t['bull_avg']:.0f}%)</div>"""
            else:
                body = ('<div class="muted" style="padding:14px 0">No reliable early-warning signature - '
                        'episodes of this type struck without shared macro precursors. '
                        'This risk cannot be flagged in advance; it is why diversification exists.</div>')
            type_cards += f"""<div class="epcard" style="text-align:left">
              <div class="ephead"><b>{e(t['name'])}</b> {chip}</div>
              <div class="epmeta">{e(t['desc'])}<br>Historical episodes: {e(t['episodes'])} ({t['n_eps']} of 10 tops)</div>
              {body}
              <div style="margin-top:4px">{chips}</div>
            </div>"""
        if line_bits:
            type_line = ('<div class="typeline">How far along toward each type of bad market: '
                         + " &nbsp;&middot;&nbsp; ".join(line_bits) + "</div>")
        sub_cards = ""
        for s in subcards or []:
            if s["parent"]:
                ptag = "sub-category of " + CRASH_TYPES[s["parent"]][0]
            else:
                ptag = "new category watch - no US precedent"
            fc3 = FLAG_COLORS.get(s["flag"], "#666")
            if s["flag"]:
                chip = f'<span class="chip" style="background:{fc3}">{s["flag"]}</span>'
                body = f"""{svg_meter(s['position'], w=300, left_label="no tripwires",
                                      right_label="all tripwires triggered")}
                <div class="muted" style="margin:2px 0 6px">{s['n_on']} of {s['n_known']} tripwires triggered.
                Threshold watch - not calibrated against historical tops.</div>"""
            elif s["trips"]:
                chip = '<span class="chip" style="background:#666">INSUFFICIENT DATA</span>'
                body = ""
            else:
                chip = '<span class="chip" style="background:#666">NOT FLAGGABLE</span>'
                body = ""
            tchips = ""
            for t in s["trips"]:
                if t["on"] is None:
                    c3, mark = "#666", "?"
                elif t["on"]:
                    c3, mark = "#e53935", "&#9679;"
                else:
                    c3, mark = "#1db954", "&#9679;"
                tchips += (f'<span class="fchip" style="border-color:{c3}">'
                           f'<span style="color:{c3}">{mark}</span> {e(t["label"])}</span> ')
            sub_cards += f"""<div class="epcard" style="text-align:left">
              <div class="ephead"><b>{e(s['name'])}</b> {chip}</div>
              <div class="epmeta">{e(ptag)}<br>{e(s['desc'])}</div>
              {body}
              <div style="margin-top:4px">{tchips}</div>
            </div>"""
        subs_html = ""
        if sub_cards:
            subs_html = f"""
<h2>Sub-category threat watches</h2>
<div class="muted" style="margin-bottom:8px">The ten most critical macro threats, mapped to the categories above.
Threats that ARE a major category (the inflation squeeze, a banking crisis) live in the cards above; the rest get
tripwire watches here. Unlike the meters above, these are raw tripwire counts - honest but uncalibrated, because
some of these risks (sovereign debt, dollar confidence) have never happened in US history.</div>
<div class="epgrid" style="grid-template-columns:repeat(auto-fill,minmax(350px,1fr))">{sub_cards}</div>"""
        types_html = f"""
<h2>How far are we from each type of bad market?</h2>
<div class="muted" style="margin-bottom:8px">Different bad markets have different causes, so each type gets its own
signature (factors flashing at &gt;=60% of that type's historical tops but rarely at {fs['n_bull']} healthy-market
control dates) and its own meter: 0% = conditions of a typical healthy market, 100% = conditions on the eve of that
type of crash. Flags: GREEN &lt;25% of the way, YELLOW &lt;50%, ORANGE &lt;75%, RED above.</div>
<div class="epgrid" style="grid-template-columns:repeat(auto-fill,minmax(350px,1fr))">{type_cards}</div>
{subs_html}"""

    # episode table: better / similar / worse today vs each market top
    verdict_style = {"better today": ("#1db954", "TODAY IS BETTER"),
                     "about the same": ("#e6c200", "ABOUT THE SAME"),
                     "worse today": ("#e53935", "TODAY IS WORSE"),
                     "insufficient data": ("#666", "N/A")}
    ep_rows = ""
    for ep in episodes:
        n = max(ep["n"], 1)
        wb = 100 * ep["better"] / n
        ws = 100 * ep["similar"] / n
        ww = 100 * ep["worse"] / n
        vcol, vtxt = verdict_style[ep["verdict"]]
        drv = ""
        if ep["drivers"] and ep["verdict"] != "better today":
            drv = ("<div class='note'>worst gaps today: "
                   + e(", ".join(ep["drivers"])) + "</div>")
        ep_rows += f"""<tr><td>{e(ep['label'])}</td><td>{ep['date']}</td><td>{e(ep['dd'])}</td>
          <td>{ep['n']}</td>
          <td><div class="stack">
                <div style="width:{wb:.0f}%;background:#1db954" title="better today: {ep['better']}"></div>
                <div style="width:{ws:.0f}%;background:#5b7a99" title="similar: {ep['similar']}"></div>
                <div style="width:{ww:.0f}%;background:#e53935" title="worse today: {ep['worse']}"></div>
              </div>
              <span class="muted">{ep['better']} better &middot; {ep['similar']} similar &middot; {ep['worse']} worse</span>
              {drv}</td>
          <td><span class="chip" style="background:{vcol}">{vtxt}</span></td></tr>"""

    # then-vs-now detail for headline indicators
    then_head = "".join(f"<th>{e(lbl)}</th>" for _, lbl, *_ in EPISODES)
    then_rows = ""
    for sid in HEADLINE_IDS:
        ind = by_id.get(sid)
        if not ind or ind.current is None:
            continue
        cells = ""
        for date_s, *_ in EPISODES:
            v = ind.metric_at(dt.date.fromisoformat(date_s))
            cells += f"<td>{fmt(v, ind.unit)}</td>"
        then_rows += (f"<tr><td class='sticky'>{e(ind.name)}</td>"
                      f"<td class='now'><b>{fmt(ind.current, ind.unit)}</b></td>{cells}</tr>")

    # category cards
    cat_cards = ""
    for cat in CATEGORY_WEIGHTS:
        if cat not in cat_scores:
            continue
        cs = cat_scores[cat]
        col = "#1db954" if cs < 25 else ("#e6c200" if cs < 50 else ("#ff7a00" if cs < 70 else "#e53935"))
        n_in = sum(1 for i in indicators if i.cat == cat and i.status in STATUS_SCORE)
        cat_cards += f"""<div class="card">
          <div class="cat-name">{e(cat)}</div>
          <div class="cat-score" style="color:{col}">{cs:.0f}</div>
          <div class="bar"><div style="width:{cs:.0f}%;background:{col}"></div></div>
          <div class="muted">{n_in} scored indicators &middot; weight {CATEGORY_WEIGHTS[cat]}%</div>
        </div>"""

    # full indicator table
    ind_rows = ""
    for ind in indicators:
        sc = STATUS_COLORS.get(ind.status, "#666")
        if ind.error:
            ind_rows += (f"<tr class='na'><td>{e(ind.name)}</td><td>{e(ind.cat)}</td>"
                         f"<td colspan='5' class='muted'>unavailable: {e(str(ind.error)[:80])}</td>"
                         f"<td><span class='chip' style='background:#444'>N/A</span></td></tr>")
            continue
        trend = "-"
        if ind.trend6m is not None:
            up = ind.trend6m > 0
            good = (ind.dir <= 0 and up) or (ind.dir > 0 and not up)
            tcol = "#1db954" if good else ("#e53935" if ind.dir != 0 else "#8aa")
            trend = f"<span style='color:{tcol}'>{'&#9650;' if up else '&#9660;'} {fmt(abs(ind.trend6m))}</span>"
        pct = f"{ind.pctile:.0f}" if ind.pctile is not None else "-"
        ind_rows += f"""<tr>
          <td>{e(ind.name)}</td><td class="muted">{e(ind.cat)}</td>
          <td><b>{fmt(ind.current, ind.unit)}</b></td>
          <td>{fmt(ind.year_ago, ind.unit)}</td>
          <td>{trend}</td><td>{pct}</td>
          <td class="muted">{ind.current_date}</td>
          <td><span class="chip" style="background:{sc}">{ind.status}</span>
              {('<div class="note">' + e(ind.note) + '</div>') if ind.note else ''}</td></tr>"""

    hist_rows = ""
    for h in reversed(history):
        hc = FLAG_COLORS.get(h.get("flag", ""), "#666")
        hist_rows += (f"<tr><td>{e(h.get('run_time',''))}</td>"
                      f"<td>{e(h.get('score',''))}</td>"
                      f"<td><span class='chip' style='background:{hc}'>{e(h.get('flag',''))}</span></td>"
                      f"<td>{e(h.get('alerts',''))}</td><td>{e(h.get('watches',''))}</td>"
                      f"<td>{e(h.get('sp500_drawdown_pct',''))}%</td></tr>")

    concl_html = "".join(f"<p>{e(p)}</p>" for p in conclusions)

    # --- visual conclusions -------------------------------------------------
    flashing = [f["name"] for f in factor_summary["factors"] if f["today"]] if factor_summary else []
    calm = []

    def _calm(sid, text):
        i2 = by_id.get(sid)
        if i2 is not None and i2.status == "OK":
            calm.append(text)

    _calm("SAHMREALTIME", "Sahm rule off - unemployment not signalling recession")
    _calm("BAMLH0A0HYM2", "Credit spreads calm - markets pricing no distress")
    _calm("NFCI", "Financial conditions still loose")
    _calm("DRTSCILM", "Banks not tightening lending (SLOOS)")
    _calm("IC4WSA", "Jobless claims not trending up")
    _calm("TEMPHELPS", "Temp-help employment holding (a leading signal)")
    _calm("PERMIT", "Building permits still holding up")

    verdict_rows = [x for x in episodes if x.get("pct") is not None and x["n"] >= 8]
    vb = sum(1 for x in verdict_rows if x["verdict"] == "better today")
    vw = sum(1 for x in verdict_rows if x["verdict"] == "worse today")
    vs_ = len(verdict_rows) - vb - vw
    best_analog = max(verdict_rows, key=lambda x: x["pct"]) if verdict_rows else None

    worst_type = None
    if factor_summary:
        flagged_types = [t for t in factor_summary.get("types", []) if t["flag"]]
        if flagged_types:
            worst_type = max(flagged_types, key=lambda t: t["position"])

    bl_parts = []
    if worst_type:
        bl_parts.append(f"Nearest threat: {worst_type['name']} - "
                        f"{worst_type['position'] * 100:.0f}% of the way from healthy conditions "
                        f"to how those episodes began.")
    red_subs = [s for s in (subcards or []) if s["flag"] == "RED"]
    if red_subs:
        s0 = red_subs[0]
        bl_parts.append(f"Sub-watch RED: {s0['name']} ({s0['n_on']}/{s0['n_known']} tripwires).")
    sahm_ok = by_id.get("SAHMREALTIME") is not None and by_id["SAHMREALTIME"].status == "OK"
    hy_ok = by_id.get("BAMLH0A0HYM2") is not None and by_id["BAMLH0A0HYM2"].status == "OK"
    if sahm_ok and hy_ok:
        bl_parts.append("The labor + credit confirmation stage that turns slowdowns into crashes has NOT begun.")
    else:
        bl_parts.append("Caution: the labor/credit confirmation signals are turning.")
    bottom_line = " ".join(bl_parts) or msg

    sig_pos_txt, sig_pos_col = "-", "#8b97a6"
    if factor_summary:
        p100 = factor_summary["position"] * 100
        sig_pos_txt = f"{p100:.0f}%"
        sig_pos_col = ("#1db954" if p100 < 25 else "#e6c200" if p100 < 50
                       else "#ff7a00" if p100 < 75 else "#e53935")
    tiles = f"""
      <div class="tile"><div class="tnum" style="color:{fc}">{score:g}</div>
        <div class="tlab">composite risk / 100<br><span class="chip" style="background:{fc}">{flag}</span></div></div>
      <div class="tile"><div class="tnum" style="color:{sig_pos_col}">{sig_pos_txt}</div>
        <div class="tlab">of the way from healthy to typical pre-crash conditions</div></div>
      <div class="tile"><div class="tnum">{n_on}/{len(checklist)}</div>
        <div class="tlab">classic pre-crash signals on</div></div>"""
    if best_analog:
        tiles += f"""
      <div class="tile"><div class="tnum" style="font-size:19px;line-height:1.35;padding-top:9px">{e(best_analog['label'])}</div>
        <div class="tlab">closest historical analog</div></div>"""
    tiles += f"""
      <div class="tile"><div class="tnum"><span style="color:#1db954">{vb}</span><span class="muted">&middot;</span><span style="color:#8b97a6">{vs_}</span><span class="muted">&middot;</span><span style="color:#e53935">{vw}</span></div>
        <div class="tlab">vs {len(verdict_rows)} past tops:<br>healthier &middot; same &middot; worse</div></div>"""

    fl_items = "".join(f"<li>{e(x)}</li>" for x in flashing[:7]) or "<li class='muted'>none</li>"
    calm_items = "".join(f"<li>{e(x)}</li>" for x in calm[:7]) or "<li class='muted'>none</li>"
    twocol = f"""<div class="twocol">
      <div class="wbox"><div class="wtitle" style="color:#e57373">&#9888;&#65039; Flashing now</div><ul class="rlist">{fl_items}</ul></div>
      <div class="wbox"><div class="wtitle" style="color:#1db954">&#10003; Still calm</div><ul class="glist">{calm_items}</ul></div>
    </div>"""

    fail_html = ""
    if failures:
        fail_html = ("<details><summary class='muted'>Data issues this run (" + str(len(failures))
                     + ")</summary><ul class='muted'>"
                     + "".join(f"<li>{e(f)}</li>" for f in failures) + "</ul></details>")

    doc = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="3600">
<title>Market Crash Monitor - {flag} ({score}/100)</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ background:#0e1117; color:#dfe6ee; font-family:'Segoe UI',system-ui,sans-serif;
         margin:0; padding:24px; max-width:1250px; margin-inline:auto; }}
  h1 {{ font-size:26px; margin:0 0 4px; }}
  h2 {{ font-size:19px; margin:34px 0 10px; border-bottom:1px solid #2a3140; padding-bottom:6px; }}
  a {{ color:#6ab7ff; }}
  .muted {{ color:#8b97a6; font-size:12.5px; }}
  .flagbox {{ margin:18px 0; padding:22px 26px; border-radius:14px; background:{fc}22;
             border:2px solid {fc}; display:flex; align-items:center; gap:26px; flex-wrap:wrap; }}
  .flag {{ font-size:44px; font-weight:800; color:{fc}; letter-spacing:2px; }}
  .score {{ font-size:30px; font-weight:700; }}
  .delta {{ color:#8b97a6; font-size:14px; }}
  .counts span {{ margin-right:14px; font-size:14px; }}
  .typeline {{ margin-top:10px; font-size:14px; color:#c6d0da; }}
  .typeline span {{ margin-right:6px; }}
  .banner {{ background:#3b2f13; border:1px solid #e6c200; border-radius:10px; padding:14px 18px; margin:14px 0; }}
  table {{ border-collapse:collapse; width:100%; font-size:13.5px; }}
  th {{ text-align:left; color:#9fb0c3; font-weight:600; padding:7px 10px; border-bottom:2px solid #2a3140;
       position:sticky; top:0; background:#0e1117; }}
  td {{ padding:7px 10px; border-bottom:1px solid #1d2430; vertical-align:top; }}
  tr:hover td {{ background:#161c26; }}
  .chip {{ padding:2px 9px; border-radius:20px; color:#0b0e13; font-weight:700; font-size:11.5px; }}
  .note {{ color:#9fb0c3; font-size:11.5px; margin-top:3px; max-width:340px; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(215px,1fr)); gap:12px; }}
  .card {{ background:#161c26; border:1px solid #232b39; border-radius:12px; padding:14px 16px; }}
  .cat-name {{ font-size:13px; color:#9fb0c3; }}
  .cat-score {{ font-size:30px; font-weight:800; margin:2px 0 6px; }}
  .bar {{ display:inline-block; width:120px; height:9px; background:#232b39; border-radius:6px; overflow:hidden; vertical-align:middle; }}
  .bar div {{ height:100%; }}
  .on {{ color:#e53935; font-weight:700; }}
  .off {{ color:#1db954; }}
  .now {{ background:#18222e; }}
  .sticky {{ position:sticky; left:0; background:#0e1117; }}
  .scrollx {{ overflow-x:auto; }}
  .vizrow {{ display:flex; flex-wrap:wrap; gap:14px; margin:14px 0 16px; align-items:stretch; }}
  .vizcard {{ background:#161c26; border:1px solid #232b39; border-radius:12px;
             padding:12px 14px 8px; text-align:center; display:flex; flex-direction:column;
             justify-content:space-between; }}
  .vtitle {{ font-size:12px; color:#9fb0c3; margin-bottom:6px; }}
  .epgrid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(228px,1fr)); gap:13px; }}
  .epcard {{ background:#161c26; border:1px solid #232b39; border-radius:12px;
            padding:12px 12px 6px; text-align:center; }}
  .ephead {{ font-size:14px; margin-bottom:2px; }}
  .epmeta {{ font-size:11.5px; color:#8b97a6; margin-bottom:6px; }}
  .epdd {{ color:#e57373; font-weight:700; }}
  .eprow {{ display:flex; align-items:center; justify-content:center; gap:10px; margin:2px 0 4px; }}
  .epstats {{ text-align:left; font-size:12px; line-height:1.5; }}
  .stack {{ display:flex; width:190px; height:11px; border-radius:6px; overflow:hidden;
           background:#232b39; margin-bottom:3px; }}
  .stack div {{ height:100%; }}
  .fchip {{ display:inline-block; border:1px solid; border-radius:14px; padding:1px 8px;
           font-size:11px; color:#c6d0da; margin:2px 0; }}
  .concl {{ background:#161c26; border:1px solid #232b39; border-left:4px solid {fc};
           border-radius:10px; padding:6px 20px; font-size:14.5px; line-height:1.65; }}
  .verdictline {{ background:{fc}18; border:1px solid {fc}66; border-left:5px solid {fc};
                 border-radius:10px; padding:14px 18px; font-size:16.5px; line-height:1.55;
                 font-weight:600; margin-bottom:14px; }}
  .tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
           gap:12px; margin-bottom:14px; }}
  .tile {{ background:#161c26; border:1px solid #232b39; border-radius:12px;
          padding:14px 12px 10px; text-align:center; }}
  .tnum {{ font-size:32px; font-weight:800; }}
  .tlab {{ font-size:11.5px; color:#8b97a6; margin-top:4px; line-height:1.45; }}
  .twocol {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr));
            gap:12px; margin-bottom:6px; }}
  .wbox {{ background:#161c26; border:1px solid #232b39; border-radius:12px; padding:12px 16px 8px; }}
  .wtitle {{ font-weight:700; font-size:13.5px; margin-bottom:6px; }}
  .wbox ul {{ margin:0 0 8px; padding-left:20px; }}
  .wbox li {{ font-size:13px; line-height:1.7; }}
  .rlist li::marker {{ color:#e57373; }}
  .glist li::marker {{ color:#1db954; }}
  details {{ margin:10px 0; }}
  .na td {{ opacity:.55; }}
</style></head><body>

<h1>US Market Crash / Correction Monitor</h1>
<div class="muted">Generated {now} &middot; 100 indicators tracked &middot; auto-refreshes hourly if left open</div>
{key_banner}

<div class="flagbox">
  <div class="flag">&#9873; {flag}</div>
  <div>
    <div class="score">{score} / 100 {delta_txt}</div>
    <div>{e(msg)}</div>
    <div class="counts muted">
      <span style="color:#e53935">&#9679; {n_alert} alert</span>
      <span style="color:#e6c200">&#9679; {n_watch} watch</span>
      <span style="color:#1db954">&#9679; {n_ok} ok</span>
      <span>&#9679; {n_info} informational</span>
      <span>&#9679; {n_na} unavailable</span>
    </div>
    {type_line}
  </div>
</div>
{types_html}

<h2>Conclusions</h2>
<div class="verdictline">{e(bottom_line)}</div>
<div class="tiles">{tiles}</div>
{twocol}
<details><summary class="muted">Full written analysis</summary>
<div class="concl">{concl_html}</div></details>
{signature_html}

<h2>Classic pre-crash checklist ({n_on}/{len(checklist)} present)</h2>
<table><tr><th>Condition</th><th>Status</th><th>Current reading</th><th>Historical precedent</th></tr>
{chk_rows}</table>

<h2>The same criteria applied at every major market top</h2>
<div class="muted" style="margin-bottom:8px">Each card re-runs today's full rulebook (percentile scoring,
thresholds, checklist, weights) using only the data that existed on the eve of that crash.
Pre-1990 tops have fewer available series (no VIX, credit-spread or JOLTS data yet), so compare their
scores with that in mind.</div>
<div class="vizcard" style="margin-bottom:14px;max-width:680px">
  <div class="vtitle">Where TODAY ranks among the pre-crash scores</div>
  {svg_rank(ep_snaps, score, flag)}
</div>
<div class="epgrid">{render_episode_cards(ep_snaps, score, flag, n_on, len(checklist), cat_scores,
                                       sum(1 for i in indicators if i.status in STATUS_SCORE))}</div>

<h2>Better or worse than at past market tops?</h2>
<div class="muted" style="margin-bottom:8px">Apples-to-apples: each row compares today against that market top using
ONLY the indicators that existed at both dates (direction-adjusted, small differences count as "similar").
This is the fairest way to compare across eras, because older tops had fewer data series.</div>
<table><tr><th>Episode</th><th>Market top</th><th>Drawdown</th><th># compared</th><th>Today vs that moment</th><th>Verdict</th></tr>
{ep_rows}</table>

<h2>Then vs now: headline indicators at past market tops</h2>
<div class="scrollx"><table>
<tr><th class="sticky">Indicator</th><th>NOW</th>{then_head}</tr>
{then_rows}</table></div>

<h2>Risk by category</h2>
<div class="cards">{cat_cards}</div>

<h2>All 100 indicators</h2>
<table>
<tr><th>Indicator</th><th>Category</th><th>Current</th><th>1y ago</th><th>6m trend</th><th>15y %ile</th><th>As of</th><th>Status</th></tr>
{ind_rows}</table>

<h2>Run history</h2>
<div class="vizcard" style="max-width:380px;margin-bottom:12px">
  <div class="vtitle">Composite score, recent runs</div>{svg_trend(history)}</div>
<table><tr><th>Run</th><th>Score</th><th>Flag</th><th>Alerts</th><th>Watches</th><th>S&amp;P drawdown</th></tr>
{hist_rows}</table>

{fail_html}
<p class="muted" style="margin-top:28px">Sources: Federal Reserve Economic Data (FRED), Yahoo Finance.
Scoring: each indicator is ranked against its own 15-year history (percentile + 6-month trend), with
crash-specific threshold rules (Sahm rule, curve inversion, credit-spread widening, claims momentum, drawdown/200-day
rules) overriding where research supports explicit levels. Composite = weighted category risk (75%) + checklist (25%).
Not investment advice.</p>
</body></html>"""
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(doc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    cfg = load_config()
    api_key = (cfg.get("fred_api_key") or "").strip() or os.environ.get("FRED_API_KEY", "").strip()
    fred_key_missing = not api_key

    indicators = [Indicator(*s) for s in SERIES]
    print(f"Fetching {len(indicators)} indicators "
          f"({'FRED key OK' if api_key else 'NO FRED KEY - market-only mode'})...")

    def fetch(ind):
        try:
            if ind.src == "fred":
                if not api_key:
                    ind.error = "FRED API key not set in config.json"
                    return ind
                ind.dates, ind.values = fetch_fred(ind.sid, api_key)
            else:
                ind.dates, ind.values = fetch_yahoo(ind.sid)
            if not ind.dates:
                ind.error = "no data returned"
        except Exception as ex:
            ind.error = f"{type(ex).__name__}: {ex}"
        return ind

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(fetch, indicators))

    for ind in indicators:
        try:
            ind.compute()
        except Exception as ex:
            ind.error = ind.error or f"compute failed: {type(ex).__name__}: {ex}"
            ind.status = "N/A"

    by_id = {i.sid: i for i in indicators}
    cat_scores = category_scores(indicators)
    checklist = build_checklist(by_id)
    episodes = episode_similarity(by_id)
    score, flag, msg, n_on = overall_flag(cat_scores, checklist)
    print("Re-running the rulebook at 10 historical market tops...")
    ep_snaps = episode_snapshots(indicators)
    print("Screening factors against healthy-market control dates...")
    factor_summary = factor_analysis(indicators, by_id, checklist)
    subcards = build_subcards(by_id)
    conclusions = write_conclusions(score, flag, n_on, checklist, episodes, by_id,
                                    cat_scores, ep_snaps, factor_summary, subcards)

    history = read_history()
    prev_score = None
    if history:
        try:
            prev_score = float(history[-1]["score"])
        except (ValueError, KeyError):
            pass

    sp = by_id.get("^GSPC")
    sp_dd = None
    if sp and sp.values:
        sp_dd = (sp.values[-1] / max(sp.values) - 1) * 100

    n_alert = sum(1 for i in indicators if i.status == "ALERT")
    n_watch = sum(1 for i in indicators if i.status == "WATCH")
    append_history(score, flag, n_alert, n_watch, sp_dd)
    history = read_history()

    failures = [f"{i.sid} - {i.error}" for i in indicators if i.error]
    render_report(indicators, by_id, cat_scores, checklist, episodes, score, flag,
                  msg, n_on, conclusions, history, prev_score, fred_key_missing,
                  failures, ep_snaps, factor_summary, subcards)
    if factor_summary and factor_summary["factors"]:
        fs = factor_summary
        print(f"Crash signature: {fs['today_pct']:.0f}% of {len(fs['factors'])} factors flashing "
              f"(healthy avg {fs['bull_avg']:.0f}%, pre-crash avg {fs['crash_avg']:.0f}%, "
              f"position {fs['position'] * 100:.0f}%)")
        for t in fs.get("types", []):
            if t["flag"]:
                print(f"  {t['name']}: {t['flag']} ({t['today_pct']:.0f}% of its signature flashing)")
            else:
                print(f"  {t['name']}: not flaggable in advance")
        for s in subcards:
            if s["flag"]:
                print(f"    sub-watch {s['name']}: {s['flag']} ({s['n_on']}/{s['n_known']} tripwires)")

    print(f"FLAG: {flag}  (score {score}/100, {n_alert} alerts, {n_watch} watches, "
          f"{len(failures)} unavailable)")
    print(f"Report written to: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
