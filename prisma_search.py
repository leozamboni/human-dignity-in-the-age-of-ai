#!/usr/bin/env python3
"""
Reproducible OpenAlex PRISMA search for the MH-IS / iSys paper.

Human Dignity in the Age of AI: Digital Ethics, Fairness, and Responsible
Software Development through the Lens of Magnifica Humanitas

This script documents and (optionally) re-runs the OpenAlex query that
produced dados/prisma_summary.json. It supports:

  1. Counting identified works (meta.count from OpenAlex).
  2. Retrieving an operational screening sample (default: 2000).
  3. Writing prisma_summary.json with the canonical pipeline counts
     used in the article (title → abstract → included).

OpenAlex polite pool: set MAILTO (recommended by OpenAlex).
Default mailto used in this project: leonardonunes169@gmail.com

Usage examples
--------------
  # Recompute identification count and refresh summary JSON
  python dados/prisma_search.py --write-summary

  # Also download a screening sample CSV (may take several minutes)
  python dados/prisma_search.py --write-summary --fetch-sample --sample-size 2000

  # Dry-run: print query URL and expected summary only
  python dados/prisma_search.py --dry-run

Notes
-----
- Screening decisions (title/abstract/inclusion) were performed by the
  authors; this script does not re-label papers automatically. When
  --write-summary is used without a full re-screen, the documented
  screening counts from the published protocol are preserved so that
  prisma_summary.json remains consistent with the article.
- Live OpenAlex totals may drift slightly over time as the index updates;
  the article reports the snapshot recorded in prisma_summary.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Protocol constants (aligned with revisao/prisma.md and the iSys article)
# ---------------------------------------------------------------------------

MAILTO = "leonardonunes169@gmail.com"

SEARCH_STRING = (
    '("human dignity" OR "digital ethics" OR "responsible AI" OR '
    '"AI ethics" OR fairness) AND ("information systems" OR '
    '"software development" OR "AI governance" OR "algorithmic fairness")'
)

DATE_FROM = "2021-01-01"
DATE_TO = "2026-12-31"
DATABASE = "OpenAlex"

# Canonical PRISMA counts reported in the article / monografia
CANONICAL = {
    "identified": 196943,
    "retrieved_for_screening": 2000,
    "after_title_screen": 822,
    "after_abstract_screen": 107,
    "included_full_text_review": 23,
    "theme_coverage": {
        "fairness": 6,
        "ethics_dev": 14,
        "sociotech_is": 11,
        "responsible_ai_gov": 8,
        "trust_xai": 6,
        "human_dignity": 2,
    },
}

OPENALEX_WORKS = "https://api.openalex.org/works"
PER_PAGE = 200  # OpenAlex maximum per request
USER_AGENT = f"MH-IS-PRISMA/1.0 (mailto:{MAILTO})"

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUMMARY = ROOT / "dados" / "prisma_summary.json"
DEFAULT_SAMPLE = ROOT / "dados" / "prisma_identified_sample.csv"


def build_filter() -> str:
    """OpenAlex filter: publication window + English abstracts preferred via search."""
    return f"from_publication_date:{DATE_FROM},to_publication_date:{DATE_TO}"


def build_works_url(
    *,
    cursor: str | None = None,
    per_page: int = PER_PAGE,
    select: str | None = None,
) -> str:
    params: dict[str, str] = {
        "search": SEARCH_STRING,
        "filter": build_filter(),
        "per_page": str(per_page),
        "mailto": MAILTO,
    }
    if cursor:
        params["cursor"] = cursor
    else:
        params["cursor"] = "*"
    if select:
        params["select"] = select
    return f"{OPENALEX_WORKS}?{urllib.parse.urlencode(params)}"


def openalex_get(url: str, retries: int = 3, pause: float = 1.0) -> dict:
    """GET JSON from OpenAlex with polite User-Agent and simple retry."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — network/API robustness
            last_err = exc
            time.sleep(pause * attempt)
    raise RuntimeError(f"OpenAlex request failed after {retries} tries: {last_err}")


def fetch_identified_count() -> int:
    """Return meta.count for the protocol query (identification stage)."""
    url = build_works_url(per_page=1, select="id")
    data = openalex_get(url)
    return int(data["meta"]["count"])


def fetch_sample(n: int = 2000) -> list[dict]:
    """
    Retrieve up to n works for operational title/abstract screening.
    Uses cursor pagination. Fields kept lean for CSV export.
    """
    select = "id,doi,display_name,publication_year,cited_by_count,authorships,primary_location,open_access"
    works: list[dict] = []
    cursor = "*"
    while len(works) < n:
        url = build_works_url(cursor=cursor, per_page=min(PER_PAGE, n - len(works)), select=select)
        data = openalex_get(url)
        batch = data.get("results") or []
        if not batch:
            break
        works.extend(batch)
        cursor = data.get("meta", {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(0.2)  # polite pacing
        print(f"  retrieved {len(works)} / {n} ...", file=sys.stderr)
    return works[:n]


def work_to_row(w: dict) -> dict:
    authors = []
    for a in w.get("authorships") or []:
        name = (a.get("author") or {}).get("display_name")
        if name:
            authors.append(name)
    loc = w.get("primary_location") or {}
    source = ((loc.get("source") or {}).get("display_name")) or ""
    oa = w.get("open_access") or {}
    return {
        "openalex_id": w.get("id", ""),
        "doi": w.get("doi") or "",
        "title": w.get("display_name") or "",
        "year": w.get("publication_year") or "",
        "cited_by_count": w.get("cited_by_count") or 0,
        "authors": "; ".join(authors),
        "source": source,
        "is_oa": oa.get("is_oa", False),
        "oa_url": oa.get("oa_url") or "",
    }


def write_sample_csv(path: Path, rows: list[dict]) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "openalex_id",
        "doi",
        "title",
        "year",
        "cited_by_count",
        "authors",
        "source",
        "is_oa",
        "oa_url",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def build_summary(identified: int | None = None, retrieved: int | None = None) -> dict:
    """
    Build prisma_summary.json.

    Identification count may be refreshed from live OpenAlex. Screening
    and inclusion counts remain those of the documented protocol unless
    the authors re-screen and update CANONICAL manually.
    """
    summary = {
        "search_string": SEARCH_STRING,
        "database": DATABASE,
        "date_range": f"{DATE_FROM} to {DATE_TO}",
        "identified": identified if identified is not None else CANONICAL["identified"],
        "retrieved_for_screening": retrieved
        if retrieved is not None
        else CANONICAL["retrieved_for_screening"],
        "after_title_screen": CANONICAL["after_title_screen"],
        "after_abstract_screen": CANONICAL["after_abstract_screen"],
        "included_full_text_review": CANONICAL["included_full_text_review"],
        "theme_coverage": dict(CANONICAL["theme_coverage"]),
        "mailto": MAILTO,
        "notes": (
            "Title/abstract/inclusion counts reflect the author screening "
            "protocol documented in revisao/prisma.md. Live OpenAlex "
            "identified counts may vary slightly as the index updates."
        ),
    }
    return summary


def write_summary(path: Path, summary: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
        f.write("\n")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="OpenAlex PRISMA search for MH-IS / iSys paper")
    p.add_argument(
        "--mailto",
        default=MAILTO,
        help=f"Contact email for OpenAlex polite pool (default: {MAILTO})",
    )
    p.add_argument(
        "--write-summary",
        action="store_true",
        help="Write dados/prisma_summary.json (refresh identified count if online)",
    )
    p.add_argument(
        "--fetch-sample",
        action="store_true",
        help="Download operational screening sample CSV from OpenAlex",
    )
    p.add_argument(
        "--sample-size",
        type=int,
        default=2000,
        help="Number of works to retrieve for screening (default: 2000)",
    )
    p.add_argument(
        "--summary-path",
        type=Path,
        default=DEFAULT_SUMMARY,
        help="Output path for prisma_summary.json",
    )
    p.add_argument(
        "--sample-path",
        type=Path,
        default=DEFAULT_SAMPLE,
        help="Output path for screening sample CSV",
    )
    p.add_argument(
        "--offline",
        action="store_true",
        help="Do not call OpenAlex; rewrite summary from CANONICAL constants only",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print protocol and example URL without writing files",
    )
    return p.parse_args()


def main() -> int:
    global MAILTO, USER_AGENT  # allow CLI override for polite pool
    args = parse_args()
    MAILTO = args.mailto
    USER_AGENT = f"MH-IS-PRISMA/1.0 (mailto:{MAILTO})"

    print("=== MH-IS OpenAlex PRISMA search ===")
    print(f"Database : {DATABASE}")
    print(f"Dates    : {DATE_FROM} .. {DATE_TO}")
    print(f"Mailto   : {MAILTO}")
    print(f"Query    : {SEARCH_STRING}")
    print()
    print("Example works URL:")
    print(build_works_url(per_page=1, select="id"))
    print()

    if args.dry_run:
        print("Canonical summary:")
        print(json.dumps(build_summary(), indent=2, ensure_ascii=False))
        return 0

    identified = CANONICAL["identified"]
    retrieved = CANONICAL["retrieved_for_screening"]

    if not args.offline:
        print("Querying OpenAlex for identification count ...")
        try:
            identified = fetch_identified_count()
            print(f"  Live identified count: {identified}")
            if identified != CANONICAL["identified"]:
                print(
                    f"  Note: article snapshot used {CANONICAL['identified']}; "
                    "index drift is expected over time."
                )
        except Exception as exc:  # noqa: BLE001
            print(f"  WARNING: live count failed ({exc}); using canonical {identified}")
    else:
        print("Offline mode: using canonical identified count.")

    if args.fetch_sample:
        if args.offline:
            print("ERROR: --fetch-sample requires network access (omit --offline).")
            return 1
        print(f"Fetching screening sample (n={args.sample_size}) ...")
        works = fetch_sample(args.sample_size)
        rows = [work_to_row(w) for w in works]
        write_sample_csv(args.sample_path, rows)
        retrieved = len(rows)
        print(f"  Wrote {retrieved} rows → {args.sample_path}")

    if args.write_summary:
        # Preserve documented screening pipeline in the article snapshot
        # unless authors intentionally update CANONICAL after re-screening.
        summary = build_summary(
            identified=identified if not args.offline else CANONICAL["identified"],
            retrieved=retrieved if args.fetch_sample else CANONICAL["retrieved_for_screening"],
        )
        # For publication reproducibility, prefer documenting the article's
        # reported identification figure when rewriting without an explicit
        # live override flag. Here we store the live count when available
        # and keep screening numbers fixed.
        if not args.offline:
            # Keep article-facing identified number stable for PDF consistency;
            # record live count alongside for audit.
            summary["identified"] = CANONICAL["identified"]
            summary["identified_live_openalex"] = identified
        write_summary(args.summary_path, summary)
        print(f"Wrote summary → {args.summary_path}")
    else:
        print("Tip: pass --write-summary to refresh dados/prisma_summary.json")

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
