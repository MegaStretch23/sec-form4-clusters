# sec-form4-clusters

Find **SEC Form 4 insider-buying clusters** from free EDGAR data: several different officers, directors or 10% owners making open-market purchases of the same stock inside a short window.

- Python 3.9+, **standard library only** (no `pip install`), SQLite cache, JSON output.
- Reads the EDGAR daily index, downloads each Form 4 XML, keeps open-market purchases (transaction code `P`) and groups them by ticker.
- Flags purchases tagged as a Rule 10b5-1 plan (form-level flag or footnote), shows each insider's role and dollars, links every filing.
- Not investment advice. A cluster is a filter result on public filings, not a recommendation.

## Quickstart

```bash
git clone https://github.com/MegaStretch23/sec-form4-clusters.git
cd sec-form4-clusters

# EDGAR requires a User-Agent with a contact: "Your Name you@example.com"
python -m sec_form4_clusters --user-agent "Your Name you@example.com" --lookback-days 3 --max-filings 25
```

That prints JSON to stdout (EDGAR posts a day's index after the day ends, so today is empty; a 25-filing demo almost never contains a cluster, so expect `"clusters_found": 0`). A full week:

```bash
python -m sec_form4_clusters --user-agent "Your Name you@example.com" --lookback-days 7 --out clusters.json
```

The first full 7-day run parsed 1,859 filings in about 17 minutes (two requests per filing, paced at ~7.5/s, under EDGAR's 10/s limit). The SQLite cache (`.form4_cache.db`) means a repeat run only fetches new filings.

Options: `--window-days 14` (cluster window), `--min-insiders 3`, `--min-aggregate-usd 500000`, `--db PATH`, `--end-date YYYY-MM-DD`. Env `SEC_USER_AGENT` works instead of `--user-agent`. Tests: `python -m unittest discover -s tests` (offline).
Exit code 1 means every filing fetch failed; the tool never prints "no clusters" when it could not read the data.

## Real sample output

Saved run from 2026-11-13 (7-day lookback, 14-day cluster window, 3+ insiders, $500,000+): 1,859 Form 4 filings fetched and parsed, 4 clusters found. Full file: [`examples/sample-run-2026-11-13.json`](examples/sample-run-2026-11-13.json).

| ticker | issuer | insiders | total bought | buy dates |
|---|---|---|---|---|
| FUN | Six Flags Entertainment Corporation/NEW | 3 | $15,066,320 | 2026-11-06 to 2026-11-10 |
| OBIO | Orchestra BioMed Holdings, Inc. | 5 | $6,230,000 | 2026-11-09 |
| MP | MP Materials Corp. / DE | 3 | $3,353,606 | 2026-11-10 to 2026-11-11 |
| MTDR | Matador Resources Co | 5 | $541,690 | 2026-11-05 to 2026-11-10 |

First cluster, trimmed:

```json
{
  "ticker": "FUN",
  "insider_count": 3,
  "total_dollar": 15066320.0,
  "first_buy": "2026-11-06",
  "last_buy": "2026-11-10",
  "plan_pct": 0.0,
  "insiders": [
    {"name": "JAFFER REHAN", "role": "Director", "dollar": 14929000.0, "any_10b5_1": false},
    {"name": "HADDRILL RICHARD M", "role": "Executive Chair / Director", "dollar": 90720.0, "any_10b5_1": false},
    {"name": "Bennett Christopher Lawrence", "role": "Chief Legal Officer", "dollar": 46600.0, "any_10b5_1": false}
  ]
}
```

## Limits (read these)

- Only the first reporting owner of a joint filing is attributed.
- Form 4/A amendments are skipped; derivative rows (option exercises, warrants) are ignored on purpose.
- Open-market code `P` only. Grants, tax withholding and sales are not counted as buys.
- Data is whatever EDGAR published; the tool does not check a filing against the company's other disclosures.

## Don't want to run it yourself?

The same idea runs hosted on Apify as a pay-per-event Actor: **[SEC Form 4 Scraper: Insider Buying Cluster Scanner](https://apify.com/johnveds15/sec-form4-insider-clusters)**. Prices read from the Apify API on 2026-11-13: $0.0004 per Form 4 filing parsed and $0.02 per insider cluster detected (no start fee). Check the Actor page for the current prices.

## License

MIT. SEC EDGAR data is public.
