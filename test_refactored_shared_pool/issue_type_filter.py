"""
Shared helper module: issue_type_filter.py

Reads the Excel mapping file (Work Types sheet), identifies issue types
marked as "drop", fetches all issue types from the API (with pagination),
and returns only the allowed issue_type_ids for extraction.

Used by: pipeline_orchestrator.py, extract_issues_list.py
"""

import os
import time
import requests
from dotenv import load_dotenv
import sys
from shared_http import get_shared_session

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

load_dotenv()

BASE_URL    = os.getenv("BASE_URL")
AUTH_TOKEN  = os.getenv("AUTH_TOKEN")
PROJECT_KEY = os.getenv("project_key")

DATE_FROM  = "1970-01-01"
DATE_TO    = "2025-02-01"
# DATE_FROM  = "2025-02-01"
# DATE_TO    = "2026-08-12"

SORT_BY    = "display_id"
SORT_TYPE  = "desc"
PER_PAGE   = 150

HEADERS = {
    "Authorization": f"Token {AUTH_TOKEN}",
    "Content-Type":  "application/json",
}

# Shared connection pool: reuses the ONE process-wide session so this
# module pools connections together with every other extractor
# instead of opening its own separate pool to the same host.
SESSION = get_shared_session()

# Excel file is in the same directory as the scripts
EXCEL_FILE = "data_mapping.xlsx"
SHEET_NAME = "Work Types"

# Column names in the Excel sheet
COL_FR_PROJECT_KEY = "FR Project Key"
COL_WORK_ITEM_TYPE = "Freshrelease Work Item Type"
COL_INDICATOR      = "Indicator"

RATE_LIMIT_DELAY     = 0.2
ISSUE_TYPES_PER_PAGE = 100  # page size for issue types API


# =============================================================
# STEP 1 — Read Excel and collect excluded type labels
# =============================================================
def get_excluded_type_labels(project_key: str) -> list[str]:
    """
    Read the Work Types sheet from the Excel file.
    Return a list of Freshrelease Work Item Type labels
    where Indicator == 'drop' AND FR Project Key == project_key.
    Matching is case-insensitive for the Indicator column.
    """
    try:
        import openpyxl
    except ImportError:
        raise ImportError(
            "openpyxl is required to read Excel files. "
            "Install it with: pip install openpyxl"
        )

    if not os.path.exists(EXCEL_FILE):
        raise FileNotFoundError(
            f"Excel file not found: '{EXCEL_FILE}'\n"
            f"Expected in the same directory as the scripts."
        )

    wb = openpyxl.load_workbook(EXCEL_FILE, read_only=True, data_only=True)

    if SHEET_NAME not in wb.sheetnames:
        raise ValueError(
            f"Sheet '{SHEET_NAME}' not found in '{EXCEL_FILE}'.\n"
            f"Available sheets: {wb.sheetnames}"
        )

    ws = wb[SHEET_NAME]
    rows = list(ws.iter_rows(values_only=True))

    if not rows:
        raise ValueError(f"Sheet '{SHEET_NAME}' is empty.")

    # Parse header row (first row)
    headers = [str(h).strip() if h is not None else "" for h in rows[0]]

    try:
        idx_project_key = headers.index(COL_FR_PROJECT_KEY)
        idx_work_type   = headers.index(COL_WORK_ITEM_TYPE)
        idx_indicator   = headers.index(COL_INDICATOR)
    except ValueError as e:
        raise ValueError(
            f"Required column not found in '{SHEET_NAME}' sheet: {e}\n"
            f"Found columns: {headers}"
        )

    excluded = []
    for row in rows[1:]:
        # Skip completely empty rows
        if all(cell is None for cell in row):
            continue

        row_project_key = str(row[idx_project_key]).strip() if row[idx_project_key] else ""
        work_type       = str(row[idx_work_type]).strip()   if row[idx_work_type]   else ""
        indicator       = str(row[idx_indicator]).strip()   if row[idx_indicator]   else ""

        # Filter by project key (case-insensitive) and indicator == "drop"
        if (row_project_key.lower() == project_key.lower()
                and indicator.lower() == "drop"
                and work_type):
            excluded.append(work_type)

    wb.close()

    print(f"  [filter] Project '{project_key}' — excluded work types from Excel: {excluded}")
    return excluded


# =============================================================
# STEP 2 — Fetch ALL issue types from API (with pagination)
# =============================================================
def fetch_issue_types(project_key: str) -> list[dict]:
    """
    Call /{project_key}/issue_types with pagination and return
    the complete list of issue type objects across ALL pages.
    Each object has: id, label, name, level, ...
    """
    all_issue_types = []
    page = 1

    print(f"  [filter] Fetching issue types from API (paginated)...")

    while True:
        url    = f"{BASE_URL}/{project_key}/issue_types"
        params = {"page": page, "per_page": ISSUE_TYPES_PER_PAGE}

        for attempt in range(3):
            try:
                response = SESSION.get(url, headers=HEADERS, params=params, timeout=30)
                response.raise_for_status()
                time.sleep(RATE_LIMIT_DELAY)
                data = response.json()

                issue_types = data.get("issue_types", [])
                all_issue_types.extend(issue_types)

                meta        = data.get("meta", {})
                total_pages = meta.get("total_pages", 1)
                total       = meta.get("total", len(all_issue_types))

                print(f"  [filter] Page {page}/{total_pages} — "
                      f"{len(issue_types)} types fetched "
                      f"(running total: {len(all_issue_types)}/{total})")

                # Stop if last page or fewer results than page size
                if page >= total_pages or len(issue_types) < ISSUE_TYPES_PER_PAGE:
                    print(f"  [filter] All issue types fetched: {len(all_issue_types)} total.")
                    return all_issue_types

                page += 1
                break  # break retry loop, continue to next page

            except requests.exceptions.RequestException as e:
                # Handle rate limiting
                if hasattr(e, "response") and e.response is not None and e.response.status_code == 429:
                    print(f"  [filter] Rate limited on page {page}, waiting 30s...")
                    time.sleep(30)
                    continue

                if attempt == 2:
                    raise RuntimeError(
                        f"Failed to fetch issue types page {page} "
                        f"after 3 attempts: {e}"
                    )
                time.sleep(1 * (attempt + 1))

    return all_issue_types


# =============================================================
# STEP 3 — Build allowed issue_type_ids
# =============================================================
def get_allowed_issue_type_ids(project_key: str) -> list[dict]:
    """
    Main entry point.

    Returns a list of dicts for ALLOWED issue types:
        [{"id": 12, "label": "User Story"}, {"id": 14, "label": "Task"}, ...]

    Excluded types (Indicator == "drop" in Excel) are removed.
    If ALL types are excluded, raises an error to prevent empty extraction.
    If the Excel file is missing or has no rows for this project,
    falls back to extracting ALL issue types (safe default).
    """
    print(f"\n  [filter] Building allowed issue type list for project: {project_key}")

    # --- Get excluded labels from Excel ---
    try:
        excluded_labels = get_excluded_type_labels(project_key)
    except (FileNotFoundError, ValueError) as e:
        print(f"  [filter] WARNING: {e}")
        print(f"  [filter] Falling back to extracting ALL issue types.")
        excluded_labels = []

    # Normalise for case-insensitive comparison
    excluded_lower = {label.lower() for label in excluded_labels}

    # --- Fetch ALL issue types from API (paginated) ---
    all_types = fetch_issue_types(project_key)

    if not all_types:
        raise RuntimeError(
            f"No issue types returned from API for project '{project_key}'."
        )

    # --- Collect only allowed types (excluded ones are simply never used) ---
    allowed = []

    for it in all_types:
        # Match using the "label" field (e.g. "Task", "Bug", "User Story")
        label   = str(it.get("label", "")).strip()
        type_id = it.get("id")

        if label.lower() not in excluded_lower:
            allowed.append({"id": type_id, "label": label})

    print(f"  [filter] Allowed  : {[t['label'] for t in allowed]}")
    print(f"  [filter] Excluded : {excluded_labels}")

    if not allowed:
        raise RuntimeError(
            "All issue types are marked as 'drop' in the Excel file. "
            "Nothing would be extracted. Please review the mapping."
        )

    print("\n data", allowed, "\n data")
    return allowed


# =============================================================
# Quick standalone test
# =============================================================
if __name__ == "__main__":
    allowed = get_allowed_issue_type_ids(PROJECT_KEY)
    print("\nAllowed issue types:")
    for t in allowed:
        print(f"  id={t['id']}  label={t['label']}")