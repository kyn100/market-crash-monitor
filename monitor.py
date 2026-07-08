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

# Market tops that preceded major crashes / corrections (onset date, label, drawdown)
EPISODES = [
    ("1973-01-11", "1973-74 bear", "-48%"),
    ("1980-11-28", "1980-82 bear", "-27%"),
    ("1987-08-25", "1987 Black Monday", "-34%"),
    ("1990-07-16", "1990 bear", "-20%"),
    ("2000-03-24", "Dot-com crash", "-49%"),
    ("2007-10-09", "Financial Crisis", "-57%"),
    ("2011-04-29", "2011 correction", "-19%"),
    ("2018-09-20", "Q4-2018 correction", "-20%"),
    ("2020-02-19", "COVID crash", "-34%"),
    ("2022-01-03", "2022 bear", "-25%"),
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

    def compute(self):
        if self.error or not self.dates:
            return
        self.mdates, self.mvalues = build_metric(self.dates, self.values, self.disp)
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
            moves = []
            for d, v in zip(self.mdates, self.mvalues):
                if d < cutoff:
                    continue
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
    """Compare current headline readings vs each pre-crash market top."""
    rows = []
    for date_s, label, dd in EPISODES:
        onset = dt.date.fromisoformat(date_s)
        comps, worse = [], 0
        for sid in HEADLINE_IDS:
            ind = by_id.get(sid)
            if not ind or ind.current is None or ind.dir == 0:
                continue
            then = ind.metric_at(onset)
            if then is None:
                continue
            cutoff = onset - dt.timedelta(days=int(15 * 365.25))
            window = sorted(v for d, v in zip(ind.mdates, ind.mvalues) if cutoff <= d <= onset)
            spread = (window[-1] - window[0]) if len(window) > 10 else None
            tol = 0.05 * spread if spread else 0.0
            is_worse = (ind.dir * (ind.current - then)) >= -tol
            if is_worse:
                worse += 1
            comps.append((sid, ind.name, then, ind.current, is_worse))
        pct = round(100 * worse / len(comps)) if comps else None
        rows.append({"date": onset, "label": label, "dd": dd, "pct": pct,
                     "n": len(comps), "comps": comps})
    return rows


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


def write_conclusions(score, flag, n_on, checklist, episodes, by_id, cat_scores):
    """Deterministic reasoning -> prose conclusions."""
    paras = []
    on_items = [c for c in checklist if c["on"]]
    off_items = [c for c in checklist if not c["on"]]

    p = (f"Composite crash-risk score is {score}/100 ({flag}). "
         f"{n_on} of {len(checklist)} classic pre-crash conditions are currently present.")
    paras.append(p)

    if on_items:
        names = "; ".join(f"{c['name'].lower()} ({c['detail']})" for c in on_items[:6])
        paras.append("Warning signs currently active: " + names + ".")
    if off_items:
        names = ", ".join(c["name"].lower() for c in off_items[:6])
        paras.append("Notably absent: " + names +
                     ". Historically, major crashes (2000, 2007) were preceded by several of these "
                     "turning on together, not in isolation.")

    scored = [e for e in episodes if e["pct"] is not None and e["n"] >= 5]
    if scored:
        best = max(scored, key=lambda e: e["pct"])
        paras.append(
            f"Closest historical analog: {best['label']} (market top {best['date']}, eventual drawdown {best['dd']}). "
            f"Today's readings are at least as stressed as that top on {best['pct']}% of {best['n']} comparable indicators. "
            "A high match means conditions RESEMBLE that pre-crash moment; it is a similarity measure, not a prediction.")

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


def render_report(indicators, by_id, cat_scores, checklist, episodes, score, flag,
                  msg, n_on, conclusions, history, prev_score, fred_key_missing, failures):
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

    # episode table
    ep_rows = ""
    for ep in episodes:
        pct = f"{ep['pct']}%" if ep["pct"] is not None else "-"
        bar_w = ep["pct"] or 0
        bcol = "#e53935" if (ep["pct"] or 0) >= 70 else ("#e6c200" if (ep["pct"] or 0) >= 45 else "#1db954")
        ep_rows += f"""<tr><td>{e(ep['label'])}</td><td>{ep['date']}</td><td>{e(ep['dd'])}</td>
          <td>{ep['n']}</td>
          <td><div class="bar"><div style="width:{bar_w}%;background:{bcol}"></div></div> {pct}</td></tr>"""

    # then-vs-now detail for headline indicators
    then_head = "".join(f"<th>{e(lbl)}</th>" for _, lbl, _ in EPISODES)
    then_rows = ""
    for sid in HEADLINE_IDS:
        ind = by_id.get(sid)
        if not ind or ind.current is None:
            continue
        cells = ""
        for date_s, _, _ in EPISODES:
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
  .concl {{ background:#161c26; border:1px solid #232b39; border-left:4px solid {fc};
           border-radius:10px; padding:6px 20px; font-size:14.5px; line-height:1.65; }}
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
  </div>
</div>

<h2>Conclusions</h2>
<div class="concl">{concl_html}</div>

<h2>Classic pre-crash checklist ({n_on}/{len(checklist)} present)</h2>
<table><tr><th>Condition</th><th>Status</th><th>Current reading</th><th>Historical precedent</th></tr>
{chk_rows}</table>

<h2>Similarity to past pre-crash moments</h2>
<div class="muted" style="margin-bottom:8px">Share of comparable headline indicators that look at least as stressed today as at each historical market top.</div>
<table><tr><th>Episode</th><th>Market top</th><th>Drawdown</th><th># compared</th><th>Similarity</th></tr>
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
    conclusions = write_conclusions(score, flag, n_on, checklist, episodes, by_id, cat_scores)

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
                  msg, n_on, conclusions, history, prev_score, fred_key_missing, failures)

    print(f"FLAG: {flag}  (score {score}/100, {n_alert} alerts, {n_watch} watches, "
          f"{len(failures)} unavailable)")
    print(f"Report written to: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
