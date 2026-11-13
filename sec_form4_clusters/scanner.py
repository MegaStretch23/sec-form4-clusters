"""Core scanner: EDGAR daily index -> Form 4 XML -> open-market purchases -> clusters.

Stdlib only (urllib, sqlite3, xml.etree). Free data only: SEC EDGAR, which requires a
User-Agent that identifies you (name + email). Requests are paced at ~7.5/s, under
EDGAR's published 10 requests/second ceiling.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

EDGAR_BASE = "https://www.sec.gov"
DAILY_INDEX_FMT = EDGAR_BASE + "/Archives/edgar/daily-index/{year}/QTR{q}/form.{ymd}.idx"
RATE_LIMIT_S = 0.13

DEFAULT_LOOKBACK_DAYS = 7
CLUSTER_WINDOW_DAYS = 14
MIN_INSIDERS = 3
MIN_AGGREGATE_USD = 500_000

THROTTLE_CODES = (403, 429)
THROTTLE_PAUSE_S = 600
THROTTLE_PAUSES_MAX = 2


class SecThrottled(RuntimeError):
    """EDGAR kept rate-limiting this IP after every allowed pause."""


class Client:
    """Tiny paced HTTP client for EDGAR."""

    def __init__(self, user_agent: str):
        self.user_agent = user_agent
        self._last = 0.0
        self._streak = 0
        self._pauses = 0

    def _get_once(self, url: str, accept: str) -> bytes:
        wait = RATE_LIMIT_S - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(
            url, headers={"User-Agent": self.user_agent, "Accept": accept,
                          "Accept-Encoding": "identity"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        finally:
            self._last = time.monotonic()

    def get(self, url: str, accept: str = "*/*") -> bytes:
        """GET with a pause-and-retry when EDGAR rate-limits the IP (403/429 streak)."""
        while True:
            try:
                data = self._get_once(url, accept)
                self._streak = 0
                return data
            except urllib.error.HTTPError as e:
                if e.code not in THROTTLE_CODES:
                    self._streak = 0
                    raise
                self._streak += 1
                if e.code != 429 and self._streak < 3:
                    raise
                if self._pauses >= THROTTLE_PAUSES_MAX:
                    raise SecThrottled(f"EDGAR still answering {e.code} after "
                                       f"{THROTTLE_PAUSES_MAX} pauses") from e
                self._pauses += 1
                self._streak = 0
                print(f"[throttle] EDGAR {e.code}: pausing {THROTTLE_PAUSE_S}s "
                      f"({self._pauses}/{THROTTLE_PAUSES_MAX})", flush=True)
                time.sleep(THROTTLE_PAUSE_S)


# ---------- SQLite cache ----------

def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS filings (
            accession TEXT PRIMARY KEY, cik TEXT NOT NULL, issuer_name TEXT, ticker TEXT,
            filed_date TEXT NOT NULL, xml_url TEXT, parsed_at TEXT, parse_error TEXT);
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, accession TEXT NOT NULL,
            owner_cik TEXT, owner_name TEXT, is_director INTEGER, is_officer INTEGER,
            officer_title TEXT, is_ten_pct INTEGER, other_relationship TEXT,
            transaction_date TEXT, transaction_code TEXT, shares REAL, price REAL,
            dollar_value REAL, footnote_10b5_1 INTEGER);
        CREATE INDEX IF NOT EXISTS idx_filings_filed ON filings(filed_date);
        CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(transaction_date);
    """)
    conn.commit()
    return conn


# ---------- fetch: EDGAR daily index ----------

# form.idx columns: Form Type, Company Name, CIK, Date Filed (YYYYMMDD), File Name
_INDEX_LINE_RE = re.compile(r"^(\S+)\s+(.+?)\s+(\d{6,10})\s+(\d{8})\s+(\S+)\s*$")
_ACCESSION_RE = re.compile(r"(\d{10}-\d{2}-\d{6})")


def quarter_for(d: dt.date) -> int:
    return (d.month - 1) // 3 + 1


def parse_index(text: str) -> list[dict]:
    """Parse an EDGAR form.idx body; keep exact Form '4' rows (amendments 4/A are skipped)."""
    out, in_data = [], False
    for raw in text.splitlines():
        if raw.startswith("---"):
            in_data = True
            continue
        if not in_data or not raw.strip():
            continue
        m = _INDEX_LINE_RE.match(raw)
        if not m:
            continue
        form, name, cik, ymd, fname = m.groups()
        if form != "4":
            continue
        acc = _ACCESSION_RE.search(fname)
        if not acc:
            continue
        out.append({"cik": cik.zfill(10), "issuer_name": name.strip(),
                    "accession": acc.group(1),
                    "filed_date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}",
                    "index_url": urljoin(EDGAR_BASE, "/" + fname.lstrip("/"))})
    return out


def fetch_daily(client: Client, d: dt.date) -> list[dict]:
    url = DAILY_INDEX_FMT.format(year=d.year, q=quarter_for(d), ymd=d.strftime("%Y%m%d"))
    for attempt in range(3):
        try:
            return parse_index(client.get(url, "text/plain").decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):      # weekend / holiday: no index that day
                return []
            if 500 <= e.code < 600 and attempt < 2:
                time.sleep(3 + 5 * attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(3 + 5 * attempt)
    return []


def collect_window(client: Client, end: dt.date, lookback_days: int) -> list[dict]:
    seen, out = set(), []
    for i in range(lookback_days):
        for f in fetch_daily(client, end - dt.timedelta(days=i)):
            if f["accession"] not in seen:
                seen.add(f["accession"])
                out.append(f)
    return out


def find_primary_xml_url(client: Client, filing: dict) -> str | None:
    folder = (f"{EDGAR_BASE}/Archives/edgar/data/{int(filing['cik'])}/"
              f"{filing['accession'].replace('-', '')}/")
    try:
        data = json.loads(client.get(folder + "index.json", "application/json"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    names = [it.get("name", "") for it in data.get("directory", {}).get("item", [])]
    cands = [n for n in names if n.lower().endswith(".xml") and not n.lower().endswith("-index.xml")]
    if not cands:
        return None
    pref = [c for c in cands if any(t in c.lower() for t in ("form4", "ownership", "wf-form", "wk-form"))]
    return folder + (pref[0] if pref else cands[0])


# ---------- parse: Form 4 ownershipDocument ----------

def _text(el, path):
    if el is None:
        return None
    f = el.find(path)
    if f is None or f.text is None:
        return None
    s = f.text.strip()
    return s or None


def _flag(s):
    return s is not None and s.strip().lower() in ("1", "true")


def _num(s):
    try:
        return float(s.replace(",", "")) if s is not None else None
    except ValueError:
        return None


def parse_form4_xml(xml_bytes: bytes) -> dict | None:
    """Return issuer, first reporting owner and non-derivative transactions (None on bad XML).

    Only the first reportingOwner is attributed on joint filings. Derivative rows
    (option exercises, warrants) are intentionally ignored: this tool looks at open-market buys.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    owner = root.find("reportingOwner")
    if owner is None:
        return None
    rel = owner.find("reportingOwnerRelationship")
    owner_cik = _text(owner, "reportingOwnerId/rptOwnerCik") or ""
    aff = _flag(_text(root, "aff10b5One"))
    notes = {fn.get("id"): fn.text.lower() for fn in root.findall("footnotes/footnote")
             if fn.get("id") and fn.text}
    txs = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        shares = _num(_text(tx, "transactionAmounts/transactionShares/value"))
        price = _num(_text(tx, "transactionAmounts/transactionPricePerShare/value"))
        plan = aff or any("10b5-1" in notes.get(r.get("id"), "") for r in tx.iter("footnoteId"))
        txs.append({"date": _text(tx, "transactionDate/value"),
                    "code": _text(tx, "transactionCoding/transactionCode") or "",
                    "shares": shares, "price": price,
                    "dollar": (shares or 0) * (price or 0), "is_10b5_1": plan})
    return {"ticker": (_text(root, "issuer/issuerTradingSymbol") or "").upper(),
            "issuer_name": _text(root, "issuer/issuerName") or "",
            "owner_cik": owner_cik.zfill(10) if owner_cik else "",
            "owner_name": _text(owner, "reportingOwnerId/rptOwnerName") or "",
            "is_director": _flag(_text(rel, "isDirector")),
            "is_officer": _flag(_text(rel, "isOfficer")),
            "officer_title": _text(rel, "officerTitle") or "",
            "is_ten_pct": _flag(_text(rel, "isTenPercentOwner")),
            "other_relationship": _text(rel, "otherText") or "",
            "transactions": txs}


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _mark(conn, acc, err, xml_url=None):
    conn.execute("UPDATE filings SET parse_error=?, parsed_at=?, xml_url=COALESCE(?, xml_url) "
                 "WHERE accession=?", (err, _now(), xml_url, acc))
    conn.commit()


def store_filing(conn, client: Client, filing: dict) -> str:
    """Fetch + parse + store one filing. Returns cached/parsed/no_xml/parse_error/http_error."""
    conn.execute("INSERT OR IGNORE INTO filings (accession,cik,issuer_name,filed_date) VALUES (?,?,?,?)",
                 (filing["accession"], filing["cik"], filing["issuer_name"], filing["filed_date"]))
    row = conn.execute("SELECT parsed_at FROM filings WHERE accession=?", (filing["accession"],)).fetchone()
    if row and row[0]:
        return "cached"
    acc = filing["accession"]
    try:
        url = find_primary_xml_url(client, filing)
        if not url:
            _mark(conn, acc, "no_primary_xml")
            return "no_xml"
        body = client.get(url, "text/xml")
    except urllib.error.HTTPError as e:
        _mark(conn, acc, f"http_{e.code}")
        return "http_error"
    p = parse_form4_xml(body)
    if p is None:
        _mark(conn, acc, "parse_failed", url)
        return "parse_error"
    conn.execute("UPDATE filings SET ticker=?, issuer_name=?, xml_url=?, parsed_at=?, parse_error=NULL "
                 "WHERE accession=?", (p["ticker"] or None, p["issuer_name"] or filing["issuer_name"],
                                       url, _now(), acc))
    for t in p["transactions"]:
        conn.execute("""INSERT INTO transactions (accession,owner_cik,owner_name,is_director,is_officer,
            officer_title,is_ten_pct,other_relationship,transaction_date,transaction_code,shares,price,
            dollar_value,footnote_10b5_1) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (acc, p["owner_cik"], p["owner_name"], int(p["is_director"]), int(p["is_officer"]),
                      p["officer_title"], int(p["is_ten_pct"]), p["other_relationship"], t["date"],
                      t["code"], t["shares"], t["price"], t["dollar"], int(t["is_10b5_1"])))
    conn.commit()
    return "parsed"


# ---------- cluster ----------

def _role(r: dict) -> str:
    parts = []
    if r["is_ten_pct"]:
        parts.append("10%-owner")
    if r["is_officer"]:
        parts.append(r["officer_title"] or "Officer")
    if r["is_director"]:
        parts.append("Director")
    if r["other_relationship"]:
        parts.append(r["other_relationship"])
    return " / ".join(parts) or "?"


def _filing_url(cik: str, accession: str) -> str:
    return f"{EDGAR_BASE}/Archives/edgar/data/{int(cik or 0)}/{accession.replace('-', '')}/"


def find_clusters(conn, end: dt.date, window_days=CLUSTER_WINDOW_DAYS,
                  min_insiders=MIN_INSIDERS, min_aggregate_usd=MIN_AGGREGATE_USD) -> list[dict]:
    """Tickers where >= min_insiders distinct people made open-market purchases (code P)
    totalling >= min_aggregate_usd inside the window ending at `end`."""
    start = (end - dt.timedelta(days=window_days - 1)).isoformat()
    cur = conn.execute("""
        SELECT f.ticker, f.issuer_name, f.cik, t.accession, t.owner_cik, t.owner_name, t.is_director,
               t.is_officer, t.officer_title, t.is_ten_pct, t.other_relationship, t.transaction_date,
               t.shares, t.dollar_value, t.footnote_10b5_1
        FROM transactions t JOIN filings f ON t.accession=f.accession
        WHERE t.transaction_code='P' AND t.transaction_date BETWEEN ? AND ?
          AND f.ticker IS NOT NULL AND f.ticker NOT IN ('', 'NONE')""", (start, end.isoformat()))
    cols = ["ticker", "issuer", "filer_cik", "accession", "owner_cik", "owner_name", "is_director",
            "is_officer", "officer_title", "is_ten_pct", "other_relationship", "date", "shares",
            "dollar", "plan"]
    by_ticker: dict[str, list[dict]] = {}
    for row in cur:
        r = dict(zip(cols, row))
        r["shares"], r["dollar"] = r["shares"] or 0.0, r["dollar"] or 0.0
        by_ticker.setdefault(r["ticker"], []).append(r)
    clusters = []
    for ticker, txs in by_ticker.items():
        if len({t["owner_cik"] for t in txs if t["owner_cik"]}) < min_insiders:
            continue
        total = sum(t["dollar"] for t in txs)
        if total < min_aggregate_usd:
            continue
        people: dict[str, dict] = {}
        for t in txs:
            if not t["owner_cik"]:
                continue
            p = people.setdefault(t["owner_cik"], {"name": t["owner_name"], "role": _role(t),
                                                   "shares": 0.0, "dollar": 0.0, "any_10b5_1": False,
                                                   "filings": []})
            p["shares"] += t["shares"]
            p["dollar"] += t["dollar"]
            p["any_10b5_1"] = p["any_10b5_1"] or bool(t["plan"])
            u = _filing_url(t["filer_cik"], t["accession"])
            if u not in p["filings"]:
                p["filings"].append(u)
        plan_usd = sum(p["dollar"] for p in people.values() if p["any_10b5_1"])
        dates = [t["date"] for t in txs if t["date"]]
        clusters.append({"ticker": ticker, "issuer_name": txs[0]["issuer"],
                         "insider_count": len(people), "tx_count": len(txs),
                         "total_dollar": round(total, 2), "first_buy": min(dates), "last_buy": max(dates),
                         "plan_pct": round(plan_usd / total, 4) if total else 0.0,
                         "insiders": sorted(people.values(), key=lambda p: -p["dollar"])})
    clusters.sort(key=lambda c: -c["total_dollar"])
    for c in clusters:
        for p in c["insiders"]:
            p["shares"], p["dollar"] = round(p["shares"], 4), round(p["dollar"], 2)
    return clusters
