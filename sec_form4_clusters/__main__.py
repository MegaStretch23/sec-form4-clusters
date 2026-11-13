"""CLI: python -m sec_form4_clusters --user-agent "Your Name you@example.com" """
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

from . import __version__
from .scanner import (CLUSTER_WINDOW_DAYS, DEFAULT_LOOKBACK_DAYS, MIN_AGGREGATE_USD, MIN_INSIDERS,
                      Client, collect_window, find_clusters, init_db, store_filing)

PAID = {
    "apify": "https://apify.com/johnveds15/sec-form4-insider-clusters",
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sec_form4_clusters",
                                 description="Find SEC Form 4 insider-buying clusters (free EDGAR data).")
    ap.add_argument("--user-agent", default=os.environ.get("SEC_USER_AGENT"),
                    help='required by SEC EDGAR: "Your Name you@example.com" (or env SEC_USER_AGENT)')
    ap.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                    help="days of daily indexes to fetch (default %(default)s)")
    ap.add_argument("--end-date", help="YYYY-MM-DD, default today (UTC)")
    ap.add_argument("--window-days", type=int, default=CLUSTER_WINDOW_DAYS)
    ap.add_argument("--min-insiders", type=int, default=MIN_INSIDERS)
    ap.add_argument("--min-aggregate-usd", type=float, default=MIN_AGGREGATE_USD)
    ap.add_argument("--max-filings", type=int, default=0, help="stop after N filings (quick demo); 0 = all")
    ap.add_argument("--db", default=".form4_cache.db", help="SQLite cache (default %(default)s)")
    ap.add_argument("--out", help="write JSON here (default: stdout)")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args(argv)
    if not a.user_agent or "@" not in a.user_agent:
        ap.error('SEC requires a contact User-Agent, e.g. --user-agent "Jane Doe jane@example.com"')

    end = dt.date.fromisoformat(a.end_date) if a.end_date else dt.datetime.now(dt.timezone.utc).date()
    client = Client(a.user_agent)
    conn = init_db(a.db)
    filings = collect_window(client, end, a.lookback_days)
    if a.max_filings:
        filings = filings[: a.max_filings]
    print(f"[scan] {len(filings)} Form 4 filings in the last {a.lookback_days} days", file=sys.stderr, flush=True)
    tally: dict[str, int] = {}
    for i, f in enumerate(filings, 1):
        r = store_filing(conn, client, f)
        tally[r] = tally.get(r, 0) + 1
        if i % 250 == 0:
            print(f"[scan] {i}/{len(filings)} {tally}", file=sys.stderr, flush=True)
    clusters = find_clusters(conn, end, a.window_days, a.min_insiders, a.min_aggregate_usd)
    out = {
        "tool": "sec-form4-clusters", "version": __version__,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "scan_date": end.isoformat(),
        "params": {"lookback_days": a.lookback_days, "window_days": a.window_days,
                   "min_insiders": a.min_insiders, "min_aggregate_usd": a.min_aggregate_usd,
                   "max_filings": a.max_filings},
        "filings_in_index": len(filings), "filings_by_result": tally,
        "clusters_found": len(clusters), "clusters": clusters,
        "source": "SEC EDGAR daily index + Form 4 XML (public data)",
        "hosted_version": PAID["apify"],
    }
    text = json.dumps(out, indent=2, ensure_ascii=False)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"[done] {len(clusters)} clusters -> {a.out}", file=sys.stderr)
    else:
        print(text)
    errs = tally.get("http_error", 0)
    if filings and errs == len(filings):
        print("[FAILED] every filing fetch failed (network or EDGAR block)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
