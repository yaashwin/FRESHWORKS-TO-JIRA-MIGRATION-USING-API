
import os
import json
import time
import threading
import requests
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
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

EXCEL_FILE      = "data_mapping.xlsx"
SHEET_NAME      = "Work Types"
COL_PROJECT_KEY = "FR Project Key"
COL_WORK_TYPE   = "Freshrelease Work Item Type"
COL_INDICATOR   = "Indicator"

RATE_LIMIT_DELAY = 0.3
PER_PAGE         = 150          # used for both issue_types and issues pages
BATCH_SIZE       = int(os.getenv("PROJECT_BATCH_SIZE", "2"))  # controlled project parallelism

# Hard-fail HTTP codes — no retry, skip immediately
HARD_FAIL_CODES = {400, 401, 403, 404}

OUTPUT_EXCEL = "data/issues_detail.xlsx"
OUTPUT_JSON  = "data/issue_counts_by_project.json"

CSV_COLUMNS = [
    "project_key",
    "issue_key",
    "issue_type_id",
    "issue_type_label",
    "created_at",
    "resolved",
    "resolved_at",
    "updated_at",
]

HEADERS = {
    "Authorization": f"Token {AUTH_TOKEN}",
    "Content-Type":  "application/json",
}

# Shared connection pool: reuses the ONE process-wide session so this
# extractor pools connections together with every other extractor
# instead of opening its own separate pool to the same host.
SESSION = get_shared_session()

print_lock = threading.Lock()


# ─────────────────────────────────────────────
# THREAD-SAFE PRINT
# ─────────────────────────────────────────────
def tprint(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs)


# ─────────────────────────────────────────────
# EXCEL — read dropped types for a project
# ─────────────────────────────────────────────
def get_dropped_labels(project_key: str) -> set:
    if not os.path.exists(EXCEL_FILE):
        tprint(f"  [mapping] '{EXCEL_FILE}' not found — no types will be skipped.")
        return set()

    wb = openpyxl.load_workbook(EXCEL_FILE, read_only=True, data_only=True)

    if SHEET_NAME not in wb.sheetnames:
        tprint(f"  [mapping] Sheet '{SHEET_NAME}' not found — no types will be skipped.")
        wb.close()
        return set()

    ws   = wb[SHEET_NAME]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    if not rows:
        return set()

    hdrs = [str(h).strip() if h else "" for h in rows[0]]

    try:
        idx_pk   = hdrs.index(COL_PROJECT_KEY)
        idx_type = hdrs.index(COL_WORK_TYPE)
        idx_ind  = hdrs.index(COL_INDICATOR)
    except ValueError as e:
        tprint(f"  [mapping] Column not found: {e} — no types will be skipped.")
        return set()

    dropped = set()
    for row in rows[1:]:
        if all(c is None for c in row):
            continue
        pk  = str(row[idx_pk]).strip()   if row[idx_pk]   else ""
        wt  = str(row[idx_type]).strip() if row[idx_type] else ""
        ind = str(row[idx_ind]).strip()  if row[idx_ind]  else ""
        if pk.lower() == project_key.lower() and ind.lower() == "drop" and wt:
            dropped.add(wt.lower())

    return dropped


# ─────────────────────────────────────────────
# API HELPER — retry forever on transient errors
# Hard-fail immediately on 4xx client errors
# ─────────────────────────────────────────────
def api_get(endpoint: str, params: dict = None, project_key: str = ""):
    """
    GET with infinite retry on transient failures.

    Backoff schedule:
      400/401/403/404  → hard-fail immediately (no retry)
      429              → wait 30s flat (server-side rate limit)
      SSL / 443        → exponential: 2s, 4s, 8s … capped at 120s
      5xx / timeout /
      connection error → same exponential backoff
    """
    url     = f"{BASE_URL}/{endpoint.lstrip('/')}"
    attempt = 0

    while True:
        attempt += 1
        try:
            resp = SESSION.get(url, headers=HEADERS, params=params, timeout=30)

            # ── Hard client errors — never retry ─────────────────
            if resp.status_code in HARD_FAIL_CODES:
                tprint(f"  [{project_key}] [SKIP {resp.status_code}] {url} "
                       f"— bad request / auth / permission / not-found.")
                return None

            # ── Rate-limited ──────────────────────────────────────
            if resp.status_code == 429:
                tprint(f"  [{project_key}] [429] Rate-limited — "
                       f"waiting 30s (attempt {attempt})…")
                time.sleep(30)
                continue

            # ── Other HTTP errors (5xx etc.) ──────────────────────
            if not resp.ok:
                wait = min(2 ** attempt, 120)
                tprint(f"  [{project_key}] [HTTP {resp.status_code}] {url} "
                       f"— retrying in {wait}s (attempt {attempt})…")
                time.sleep(wait)
                continue

            # ── Success ───────────────────────────────────────────
            time.sleep(RATE_LIMIT_DELAY)
            return resp.json()

        except requests.exceptions.SSLError as e:
            wait = min(2 ** attempt, 120)
            tprint(f"  [{project_key}] [SSL/443] retrying in {wait}s "
                   f"(attempt {attempt}): {e}")
            time.sleep(wait)

        except requests.exceptions.ConnectionError as e:
            wait = min(2 ** attempt, 120)
            tprint(f"  [{project_key}] [CONNECTION ERROR] retrying in {wait}s "
                   f"(attempt {attempt}): {e}")
            time.sleep(wait)

        except requests.exceptions.Timeout as e:
            wait = min(2 ** attempt, 120)
            tprint(f"  [{project_key}] [TIMEOUT] retrying in {wait}s "
                   f"(attempt {attempt}): {e}")
            time.sleep(wait)

        except requests.exceptions.RequestException as e:
            wait = min(2 ** attempt, 120)
            tprint(f"  [{project_key}] [REQUEST ERROR] retrying in {wait}s "
                   f"(attempt {attempt}): {e}")
            time.sleep(wait)


# ─────────────────────────────────────────────
# FETCH ALL ISSUE TYPES — fully paginated
# per_page=150 so most projects finish in 1 call
# ─────────────────────────────────────────────
def fetch_issue_types(project_key: str) -> list:
    """
    Fetches ALL issue types for a project with full pagination.
    With per_page=150 most projects complete in a single API call.
    """
    all_types = []
    page      = 1

    while True:
        data = api_get(
            f"{project_key}/issue_types",
            params={"page": page, "per_page": PER_PAGE},
            project_key=project_key,
        )

        if not data:
            # Hard-fail (4xx) — return whatever collected so far
            break

        batch = data.get("issue_types", [])
        all_types.extend(batch)

        meta        = data.get("meta", {})
        total_pages = meta.get("total_pages")
        total       = meta.get("total", "?")

        tprint(f"  [{project_key}] issue_types page {page}: "
               f"{len(batch)} fetched  (running: {len(all_types)}/{total})")

        # Stop conditions
        if total_pages is not None and page >= int(total_pages):
            break
        if len(batch) < PER_PAGE:
            break
        if not batch:
            break

        page += 1

    tprint(f"  [{project_key}] Total issue types fetched: {len(all_types)}")
    return all_types


# ─────────────────────────────────────────────
# FETCH ALL ISSUES FOR ONE TYPE  (generator)
# ─────────────────────────────────────────────
def fetch_issues_for_type(project_key: str, type_id: int, type_label: str):
    page  = 0
    count = 0

    while True:
        page += 1
        data = api_get(
            f"{project_key}/issues",
            params={"issue_type_id": type_id, "page": page, "per_page": PER_PAGE},
            project_key=project_key,
        )

        issues = data.get("issues", []) if data else []
        if not issues:
            break

        for issue in issues:
            yield issue

        count += len(issues)
        meta        = data.get("meta", {}) if data else {}
        total_pages = meta.get("total_pages")

        tprint(
            f"  [{project_key}] {type_label}  page {page}"
            + (f"/{total_pages}" if total_pages else "")
            + f"  →  {count} so far"
        )

        if total_pages is not None and page >= int(total_pages):
            break
        if len(issues) < PER_PAGE:
            break


# ─────────────────────────────────────────────
# PROCESS ONE PROJECT
# ─────────────────────────────────────────────
def process_project(project_key: str) -> dict:
    tprint(f"\n{'='*50}\n  START: {project_key}\n{'='*50}")

    dropped      = get_dropped_labels(project_key)
    drop_display = sorted(dropped) if dropped else "(none)"
    tprint(f"  [{project_key}] Drop list: {drop_display}")

    issue_types = fetch_issue_types(project_key)
    if not issue_types:
        tprint(f"  [{project_key}] ⚠ No issue types returned.")
        return {"rows": [], "counts": {}}

    rows           = []
    project_counts = {}
    grand_total    = 0

    for it in issue_types:
        type_id    = it.get("id")
        type_label = it.get("label") or it.get("name") or f"type_{type_id}"

        if type_label.lower() in dropped:
            tprint(f"  [{project_key}] ✗ [drop]  {type_label}")
            continue

        tprint(f"  [{project_key}] ✔ [keep]  [{type_id}] {type_label} — fetching…")

        type_count = 0
        for issue in fetch_issues_for_type(project_key, type_id, type_label):
            rows.append({
                "project_key":      project_key,
                "issue_key":        issue.get("key", ""),
                "issue_type_id":    type_id,
                "issue_type_label": type_label,
                "created_at":       issue.get("created_at", ""),
                "resolved":         issue.get("resolved", ""),
                "resolved_at":      issue.get("resolved_at", ""),
                "updated_at":       issue.get("updated_at", ""),
            })
            type_count += 1

        project_counts[type_label] = type_count
        grand_total += type_count
        tprint(f"  [{project_key}]   {type_label:<27} : {type_count:>6}")

    tprint(f"  [{project_key}] {'─'*38}")
    tprint(f"  [{project_key}]   {'TOTAL':<27} : {grand_total:>6}")

    project_counts["_TOTAL"] = grand_total
    return {"rows": rows, "counts": project_counts}


# ─────────────────────────────────────────────
# WRITE EXCEL — one sheet per project key
# ─────────────────────────────────────────────
HEADER_FILL = PatternFill("solid", fgColor="2563EB")
HEADER_FONT = Font(bold=True, color="FFFFFF")

def write_excel(results: dict):
    os.makedirs(os.path.dirname(OUTPUT_EXCEL) or ".", exist_ok=True)
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    for project_key, data in results.items():
        rows = data.get("rows", [])

        sheet_name = project_key[:31]
        ws = wb.create_sheet(title=sheet_name)

        ws.append(CSV_COLUMNS)
        for col_idx, _ in enumerate(CSV_COLUMNS, start=1):
            cell = ws.cell(row=1, column=col_idx)
            cell.font      = HEADER_FONT
            cell.fill      = HEADER_FILL
            cell.alignment = Alignment(horizontal="center")

        for row in rows:
            ws.append([row.get(col, "") for col in CSV_COLUMNS])

        for col in ws.columns:
            max_len = max((len(str(cell.value or "")) for cell in col), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 50)

        tprint(f"  [Excel] Sheet '{sheet_name}' — {len(rows)} rows written.")

    wb.save(OUTPUT_EXCEL)
    tprint(f"\n  [Excel] Saved → {OUTPUT_EXCEL}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    if not BASE_URL or not AUTH_TOKEN:
        print("ERROR: BASE_URL or AUTH_TOKEN not set in .env")
        return

    print("\n" + "=" * 60)
    print("  ISSUE DETAIL FETCH — ALL PROJECTS (DYNAMIC POOL)")
    print(f"  Max parallel workers : {BATCH_SIZE}")
    print(f"  Total projects       : {len(PROJECT_KEYS)}")
    print("=" * 60)

    all_results = {}
    completed   = 0

    with ThreadPoolExecutor(max_workers=BATCH_SIZE) as executor:
        future_to_key = {
            executor.submit(process_project, pk): pk
            for pk in PROJECT_KEYS
        }

        for future in as_completed(future_to_key):
            pk = future_to_key[future]
            completed += 1
            try:
                all_results[pk] = future.result()
                tprint(
                    f"\n  ✔ [{pk}] done  "
                    f"({completed}/{len(PROJECT_KEYS)} complete, "
                    f"{len(PROJECT_KEYS) - completed} remaining)"
                )
            except Exception as exc:
                tprint(f"  [{pk}] Unexpected exception: {exc}")
                all_results[pk] = {"rows": [], "counts": {}}

    # ── Write Excel ───────────────────────────────────────────────
    write_excel(all_results)

    # ── Save JSON count summary ───────────────────────────────────
    count_summary = {pk: v["counts"] for pk, v in all_results.items()}
    os.makedirs(os.path.dirname(OUTPUT_JSON) or ".", exist_ok=True)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(count_summary, f, indent=4, ensure_ascii=False)

    # ── Final summary table ───────────────────────────────────────
    print("\n" + "=" * 60)
    print("  FINAL SUMMARY — TOTAL ISSUES PER PROJECT")
    print("=" * 60)
    print(f"  {'Project Key':<20}  {'Total Issues':>12}")
    print(f"  {'─'*20}  {'─'*12}")

    grand = 0
    for pk in sorted(all_results.keys()):
        total = all_results[pk]["counts"].get("_TOTAL", 0)
        grand += total
        flag = "  ⚠ " if total == 0 else "    "
        print(f"{flag}{pk:<20}  {total:>12,}")

    print(f"  {'─'*20}  {'─'*12}")
    print(f"  {'GRAND TOTAL':<20}  {grand:>12,}")
    print("=" * 60)
    print(f"\n  Excel  →  {OUTPUT_EXCEL}")
    print(f"  JSON   →  {OUTPUT_JSON}\n")


if __name__ == "__main__":
    main()