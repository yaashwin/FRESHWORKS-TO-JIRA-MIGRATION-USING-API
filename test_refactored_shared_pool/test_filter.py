"""
fetch_issue_counts.py
─────────────────────
Fetches the issue count per issue type for every project in PROJECT_KEYS.

  • Skips issue types marked as "drop" in data_mapping.xlsx (Work Types sheet).
  • Runs up to PARALLEL_WORKERS projects concurrently (default: 5).
  • Retries FOREVER on transient errors (429, 443, 5xx, timeouts, network blips).
  • Only skips on hard permission errors (403 / 401).
  • Prints per-project totals + a final grand-total summary table at the end.
  • Saves results to data/issue_counts_by_project.json.
"""

import os
import json
import time
import threading
import requests
import openpyxl
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
import sys
from shared_http import get_shared_session

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
load_dotenv()

BASE_URL   = os.getenv("BASE_URL")
AUTH_TOKEN = os.getenv("AUTH_TOKEN")

PROJECT_KEYS = [
    "AB1", "FV", "JVS", "FS", "CXBU", "FBOTS", "FRESHCALL", "FC", "FD", "FM",
    "FSALES", "MCX", "CB", "CEROAD", "CENTRALSRE", "INFRA", "CL3", "CE2", "TOPS",
    "DT1", "DE4", "DM", "CHOUSE", "DE3", "DSC", "DR", "DC", "DO", "DX", "FCC",
    "HAYS", "AML", "SSD", "UD", "WE", "INC", "MAR", "MSI", "NEOROAD", "BILLING",
    "CH", "CSTORE", "MP", "ED", "EMAIL", "ESTR", "FL", "FSERV", "FRESHID",
    "FRESHPIPE", "EV", "IRIS", "KA", "LEGO", "RTS", "SIPC", "SEARCH", "SIP",
    "FSURVEY", "MCR", "WH", "WA", "DESIGN", "DZNE", "DS", "SECURITY", "DEVREL", "EE",
]

PARALLEL_WORKERS = int(os.getenv("PROJECT_PARALLEL_WORKERS", "2"))  # controlled project parallelism
RATE_LIMIT_DELAY = 0.3        # seconds between successful API calls (per thread)
PER_PAGE         = 150
OUTPUT_FILE      = "data/issue_counts_by_project.json"

EXCEL_FILE       = "data_mapping.xlsx"
SHEET_NAME       = "Work Types"
COL_PROJECT_KEY  = "FR Project Key"
COL_WORK_TYPE    = "Freshrelease Work Item Type"
COL_INDICATOR    = "Indicator"

# Non-retriable HTTP status codes (permission / auth / not-found)
HARD_FAIL_CODES = {400, 401, 403, 404}

HEADERS = {
    "Authorization": f"Token {AUTH_TOKEN}",
    "Content-Type":  "application/json",
}

# Shared connection pool: reuses the ONE process-wide session so this
# script pools connections together with every other extractor
# instead of opening its own separate pool to the same host.
SESSION = get_shared_session()

# Thread-safe print lock
_print_lock = threading.Lock()

def tprint(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs)


# ─────────────────────────────────────────────
# EXCEL — read dropped types for a project
# ─────────────────────────────────────────────
def get_dropped_labels(project_key: str) -> set:
    if not os.path.exists(EXCEL_FILE):
        return set()

    wb = openpyxl.load_workbook(EXCEL_FILE, read_only=True, data_only=True)
    if SHEET_NAME not in wb.sheetnames:
        wb.close()
        return set()

    ws   = wb[SHEET_NAME]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    if not rows:
        return set()

    headers = [str(h).strip() if h else "" for h in rows[0]]
    try:
        idx_pk   = headers.index(COL_PROJECT_KEY)
        idx_type = headers.index(COL_WORK_TYPE)
        idx_ind  = headers.index(COL_INDICATOR)
    except ValueError:
        return set()

    dropped = set()
    for row in rows[1:]:
        if all(c is None for c in row):
            continue
        pk  = str(row[idx_pk]).strip()   if row[idx_pk]   else ""
        wt  = str(row[idx_type]).strip() if row[idx_type]  else ""
        ind = str(row[idx_ind]).strip()  if row[idx_ind]   else ""
        if pk.lower() == project_key.lower() and ind.lower() == "drop" and wt:
            dropped.add(wt.lower())

    return dropped


# ─────────────────────────────────────────────
# API HELPER — retry forever on transient errors
# ─────────────────────────────────────────────
def api_get(endpoint: str, params: dict = None):
    """
    GET request with infinite retry on transient failures.
    Only gives up on hard permission/auth errors (401, 403, 404).

    Backoff schedule:
      429              → wait 30 s (rate-limited by server)
      5xx / timeout    → exponential backoff: 2s, 4s, 8s … capped at 120s
      network error    → same exponential backoff
    """
    url     = f"{BASE_URL}/{endpoint.lstrip('/')}"
    attempt = 0

    while True:
        attempt += 1
        try:
            resp = SESSION.get(url, headers=HEADERS, params=params, timeout=30)

            # ── Hard fail — permission / not found ───────────────
            if resp.status_code in HARD_FAIL_CODES:
                tprint(f"  [SKIP {resp.status_code}] {url} — skipping (bad request / auth / permission / not-found).")
                return None

            # ── Rate limit ────────────────────────────────────────
            if resp.status_code == 429:
                wait = 30
                tprint(f"  [429] Rate-limited on {url} — waiting {wait}s (attempt {attempt})…")
                time.sleep(wait)
                continue

            # ── Other HTTP errors (5xx etc.) ──────────────────────
            if not resp.ok:
                wait = min(2 ** attempt, 120)
                tprint(f"  [HTTP {resp.status_code}] {url} — retrying in {wait}s (attempt {attempt})…")
                time.sleep(wait)
                continue

            # ── Success ───────────────────────────────────────────
            time.sleep(RATE_LIMIT_DELAY)
            return resp.json()

        except requests.exceptions.SSLError as e:
            # Covers 443 / TLS handshake failures
            wait = min(2 ** attempt, 120)
            tprint(f"  [SSL/443 ERROR] {url} — retrying in {wait}s (attempt {attempt}): {e}")
            time.sleep(wait)

        except requests.exceptions.ConnectionError as e:
            wait = min(2 ** attempt, 120)
            tprint(f"  [CONNECTION ERROR] {url} — retrying in {wait}s (attempt {attempt}): {e}")
            time.sleep(wait)

        except requests.exceptions.Timeout as e:
            wait = min(2 ** attempt, 120)
            tprint(f"  [TIMEOUT] {url} — retrying in {wait}s (attempt {attempt}): {e}")
            time.sleep(wait)

        except requests.exceptions.RequestException as e:
            wait = min(2 ** attempt, 120)
            tprint(f"  [REQUEST ERROR] {url} — retrying in {wait}s (attempt {attempt}): {e}")
            time.sleep(wait)


# ─────────────────────────────────────────────
# FETCH issue types for a project
# ─────────────────────────────────────────────
def fetch_issue_types(project_key: str) -> list:
    """
    Fetches ALL issue types for a project, paginating until complete.
    Passes per_page=150 so most projects finish in a single call.
    """
    all_types = []
    page      = 1

    while True:
        data = api_get(
            f"{project_key}/issue_types",
            params={"page": page, "per_page": PER_PAGE},
        )
        if not data:
            break

        batch = data.get("issue_types", [])
        all_types.extend(batch)

        meta        = data.get("meta", {})
        total_pages = meta.get("total_pages")

        if total_pages is not None and page >= int(total_pages):
            break
        if len(batch) < PER_PAGE:
            break
        if not batch:
            break

        page += 1

    return all_types


# ─────────────────────────────────────────────
# FETCH count for one issue type
# ─────────────────────────────────────────────
def fetch_count_for_type(project_key: str, type_id: int) -> int:
    count = 0
    page  = 1

    while True:
        data = api_get(
            f"{project_key}/issues",
            params={"issue_type_id": type_id, "page": page, "per_page": PER_PAGE},
        )

        if data is None:
            # api_get only returns None on hard-fail (403/401/404)
            return -1

        issues = data.get("issues", [])
        meta   = data.get("meta", {})

        # Fast path — API gives total directly
        if page == 1 and "total_count" in meta:
            return int(meta["total_count"])

        count += len(issues)

        total_pages = meta.get("total_pages")
        if total_pages is not None and page >= int(total_pages):
            break
        if len(issues) < PER_PAGE:
            break
        if not issues:
            break

        page += 1

    return count


# ─────────────────────────────────────────────
# PROCESS one project (runs in its own thread)
# ─────────────────────────────────────────────
def process_project(project_key: str) -> tuple:
    """Returns (project_key, counts_dict, total)."""

    tprint(f"\n  ▶ Starting: {project_key}")

    dropped     = get_dropped_labels(project_key)
    issue_types = fetch_issue_types(project_key)

    if not issue_types:
        tprint(f"  ⚠  [{project_key}] No issue types found — check key / permissions.")
        return project_key, {}, 0

    project_counts = {}
    total          = 0

    for it in issue_types:
        type_id    = it.get("id")
        type_label = it.get("label") or it.get("name") or f"type_{type_id}"

        if type_label.lower() in dropped:
            tprint(f"  [{project_key}] ✗ skipped  {type_label}")
            continue

        count = fetch_count_for_type(project_key, type_id)
        project_counts[type_label] = count

        if count >= 0:
            total += count
            tprint(f"  [{project_key}]  {type_label:<27} : {count:>6}")
        else:
            tprint(f"  [{project_key}]  {type_label:<27} :    PERM-ERR (skipped)")

    project_counts["_TOTAL"] = total
    tprint(f"  ✔ Done: {project_key}  →  TOTAL = {total}")
    return project_key, project_counts, total


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    if not BASE_URL or not AUTH_TOKEN:
        print("ERROR: BASE_URL or AUTH_TOKEN not set in .env")
        return

    print("\n" + "=" * 60)
    print(f"  ISSUE COUNTS — {len(PROJECT_KEYS)} projects  |  {PARALLEL_WORKERS} in parallel")
    print("=" * 60)

    all_results   = {}
    project_order = []   # preserve insertion order for summary

    # ── Run projects in parallel ──────────────────────────────────
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
        futures = {
            executor.submit(process_project, key): key
            for key in PROJECT_KEYS
        }
        for future in as_completed(futures):
            key    = futures[future]
            try:
                proj_key, counts, total = future.result()
                all_results[proj_key]   = counts
                project_order.append(proj_key)
            except Exception as exc:
                tprint(f"\n  [FATAL] {key} raised an unexpected exception: {exc}")
                all_results[key] = {"_TOTAL": 0}
                project_order.append(key)

    # ── Save JSON ─────────────────────────────────────────────────
    os.makedirs(os.path.dirname(OUTPUT_FILE) or ".", exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=4, ensure_ascii=False)

    # ── Final summary table ───────────────────────────────────────
    grand_total = 0

    print("\n")
    print("=" * 60)
    print("  FINAL SUMMARY — TOTAL ISSUES PER PROJECT")
    print("=" * 60)
    print(f"  {'Project Key':<20}  {'Total Issues':>12}")
    print(f"  {'─'*20}  {'─'*12}")

    for key in sorted(all_results.keys()):
        counts  = all_results[key]
        ptotal  = counts.get("_TOTAL", 0)
        grand_total += ptotal
        flag = "  ⚠ " if ptotal == 0 else "    "
        print(f"{flag}{key:<20}  {ptotal:>12,}")

    print(f"  {'─'*20}  {'─'*12}")
    print(f"  {'GRAND TOTAL':<20}  {grand_total:>12,}")
    print("=" * 60)
    print(f"\n  Results saved → {OUTPUT_FILE}")
    print(f"  Projects processed : {len(all_results)}")
    print(f"  Grand total issues : {grand_total:,}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()