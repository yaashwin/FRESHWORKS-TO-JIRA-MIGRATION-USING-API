"""
Standalone script to extract issues list only — with issue type filtering.
This MUST be run BEFORE any issue-level extraction scripts.
Run this independently: python extract_issues_list.py

Filtering:
  - Reads data_mapping.xlsx (Work Types sheet)
  - Excludes issue types where Indicator == "drop"
  - Fetches only allowed issue types via:
      /{project_key}/issues?issue_type_id={id}
  - Combines all into the same output structure (filenames unchanged)
"""

import requests
import json
import os
import time
from datetime import datetime
from dotenv import load_dotenv
import sys
from issue_type_filter import DATE_FROM, DATE_TO, SORT_BY, SORT_TYPE
from shared_http import get_shared_session
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
# =============================================================
# CONFIGURATION
# =============================================================
load_dotenv()
BASE_URL    = os.getenv("BASE_URL")
AUTH_TOKEN  = os.getenv("AUTH_TOKEN")
PROJECT_KEY = os.getenv("project_key")

RAW_DATA_DIR     = f"{PROJECT_KEY}_data/raw" if PROJECT_KEY else "data/raw"
RATE_LIMIT_DELAY = 0.2
ISSUES_PER_PAGE  = 150
MAX_PAGES        = 100000  # per issue type

HEADERS = {
    "Authorization": f"Token {AUTH_TOKEN}",
    "Content-Type":  "application/json"
}

# Shared connection pool: reuses the ONE process-wide session so this
# extractor pools connections together with every other extractor
# instead of opening its own separate pool to the same host.
SESSION = get_shared_session()

# =============================================================
# STATS
# =============================================================
stats = {
    "script":           "extract_issues_list",
    "started_at":       None,
    "completed_at":     None,
    "total_api_calls":  0,
    "failed_api_calls": 0,
    "total_issues":     0,
    "pages_processed":  0,
    "issue_types_extracted": [],
    "issue_types_skipped":   [],
    "errors":           []
}

# =============================================================
# HELPER FUNCTIONS
# =============================================================
# def api_get(endpoint, params=None, retries=3):
#     url = f"{BASE_URL}/{endpoint.lstrip('/')}"
#     stats["total_api_calls"] += 1

#     for attempt in range(retries):
#         try:
#             response = requests.get(url, headers=HEADERS, params=params, timeout=30)
#             response.raise_for_status()
#             time.sleep(RATE_LIMIT_DELAY)
#             return response.json()

#         except requests.exceptions.RequestException as e:
#             if hasattr(e, "response") and e.response is not None and e.response.status_code == 429:
#                 time.sleep(30)
#                 continue

#             if hasattr(e, "response") and e.response is not None:
#                 response = e.response

#                 print("\n" + "=" * 80)
#                 print(f"[FAILED API] {response.request.method} {response.url}")
#                 print(f"[STATUS] {response.status_code}")

#                 print("\n[RESPONSE HEADERS]")
#                 for k, v in response.headers.items():
#                     print(f"{k}: {v}")

#                 print("=" * 80 + "\n")

#             if attempt == retries - 1:
#                 error_msg = f"{endpoint}: {str(e)}"
#                 print(f" {error_msg}")
#                 stats["failed_api_calls"] += 1
#                 stats["errors"].append({
#                     "endpoint": endpoint,
#                     "error": error_msg
#                 })
#                 return None

#             time.sleep(1 * (attempt + 1))
def api_get(endpoint, params=None, retries=3):
    url = f"{BASE_URL}/{endpoint.lstrip('/')}"
    stats["total_api_calls"] += 1

    for attempt in range(retries):
        try:
            response = SESSION.get(url, headers=HEADERS, params=params, timeout=30)
            response.raise_for_status()
            time.sleep(RATE_LIMIT_DELAY)
            return response.json()

        except requests.exceptions.RequestException as e:
            if hasattr(e, "response") and e.response is not None and e.response.status_code == 429:
                time.sleep(30)
                continue

            if hasattr(e, "response") and e.response is not None:
                response = e.response

                print("\n" + "=" * 80)
                print(f"[FAILED API] {response.request.method} {response.url}")
                print(f"[STATUS] {response.status_code}")
                
                # Highlight the x-request-id
                request_id = response.headers.get('x-request-id', 'NOT FOUND')
                print(f"\n[X-REQUEST-ID] {request_id}")

                print("\n[RESPONSE HEADERS]")
                for k, v in response.headers.items():
                    print(f"{k}: {v}")

                print("=" * 80 + "\n")

            if attempt == retries - 1:
                error_msg = f"{endpoint}: {str(e)}"
                print(f" {error_msg}")
                stats["failed_api_calls"] += 1
                stats["errors"].append({
                    "endpoint": endpoint,
                    "error": error_msg,
                    "x-request-id": response.headers.get('x-request-id', 'N/A') if hasattr(e, "response") and e.response is not None else 'N/A'
                })
                return None

            time.sleep(1 * (attempt + 1))


def save_json(data, *path_parts):
    filepath = os.path.join(RAW_DATA_DIR, *path_parts)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

    return filepath


# =============================================================
# FETCH ISSUES FOR ONE ISSUE TYPE (paginated)
# =============================================================
def fetch_issues_for_type(project_key, type_id, type_label):
    """
    Fetch all issues for a single issue_type_id with pagination.
    Returns list of issues.
    """
    issues_for_type = []
    page = 1
    pages_processed = 0

    print(f"\n  Fetching '{type_label}' (id={type_id})...")

    while page <= MAX_PAGES:
        print(f"    Page {page}...", end=" ", flush=True)

        data = api_get(
            f"{project_key}/issues",
            params={
                "issue_type_id": type_id,
                "page":          page,
                "per_page":      ISSUES_PER_PAGE,
                "query_hash[0][condition]": "created_at",
                "query_hash[0][operator]":  "is_in_the_range",
                "query_hash[0][value]":     f"{DATE_FROM},{DATE_TO}",
                "sort":                     SORT_BY,
                "sort_type":                SORT_TYPE,
            }
        )

        if not data:
            print("Failed")
            break

        issues = data.get("issues", [])
        if not issues:
            print("No issues")
            break

        issues_for_type.extend(issues)
        print(f"{len(issues)} issues")

        meta        = data.get("meta", {})
        total_pages = meta.get("total_pages", page)
        total_count = meta.get("total_count", len(issues_for_type))
        pages_processed = page

        print(f"    Progress: {len(issues_for_type)}/{total_count} issues for '{type_label}'")

        if page >= total_pages or len(issues) < ISSUES_PER_PAGE:
            break

        page += 1

    stats["pages_processed"] += pages_processed
    print(f"  '{type_label}' done — {len(issues_for_type)} issues extracted.")
    return issues_for_type


# =============================================================
# EXTRACTION (filtered by allowed issue types)
# =============================================================
def extract_issues_list(project_key):
    """
    Extract issues for all allowed issue types and combine into
    the same output structure as before (filenames unchanged).
    """
    # --- Import filter helper ---
    try:
        from issue_type_filter import get_allowed_issue_type_ids
    except ImportError:
        print("  ERROR: issue_type_filter.py not found in the same directory.")
        return []

    print("=" * 60)
    print("   EXTRACTING ISSUES LIST (FILTERED BY ISSUE TYPE)")
    print("=" * 60)
    print(f"  Project      : {project_key}")
    print(f"  Output       : {RAW_DATA_DIR}/issues/")
    print(f"  Issues/page  : {ISSUES_PER_PAGE}")
    print(f"  Max pages    : {MAX_PAGES} per type")
    print("=" * 60)

    # --- Get allowed issue types ---
    print("\n  Loading issue type filter...")
    try:
        allowed_types = get_allowed_issue_type_ids(project_key)
    except Exception as e:
        print(f"  ERROR loading filter: {e}")
        return []

    print(f"\n  Will extract {len(allowed_types)} issue type(s):")
    for t in allowed_types:
        print(f"    id={t['id']}  label='{t['label']}'")

    # --- Fetch issues per type and combine ---
    all_issues = []

    print("\n  Fetching issues by type...")

    for type_info in allowed_types:
        type_id    = type_info["id"]
        type_label = type_info["label"]

        issues = fetch_issues_for_type(project_key, type_id, type_label)

        if issues:
            all_issues.extend(issues)
            stats["issue_types_extracted"].append({
                "id":    type_id,
                "label": type_label,
                "count": len(issues)
            })
        else:
            stats["issue_types_skipped"].append({
                "id":    type_id,
                "label": type_label
            })

    # --- Save combined output (same filenames as before) ---
    if all_issues:
        # Main issues list file — same filename, same structure
        filepath = save_json(
            {"issues": all_issues, "total_count": len(all_issues)},
            "issues",
            f"{project_key}_issues_list.json"
        )

        stats["total_issues"] = len(all_issues)

        print(f"\n  Total issues extracted : {len(all_issues)}")
        print(f"  Saved to               : {filepath}")

        # Issue IDs file — same filename as before
        issue_ids      = [issue["id"] for issue in all_issues]
        ids_filepath   = save_json(
            {"issue_ids": issue_ids, "count": len(issue_ids)},
            "issues",
            f"{project_key}_issue_ids.json"
        )
        print(f"  Issue IDs saved to     : {ids_filepath}")

        # Print sample
        print(f"\n  Sample Issues (first 5):")
        print("-" * 60)
        for i, issue in enumerate(all_issues[:5], 1):
            issue_id  = issue.get("id",    "N/A")
            issue_key = issue.get("key",   "N/A")
            title     = issue.get("title", "Untitled")
            status    = (
                issue.get("status", {}).get("name", "N/A")
                if isinstance(issue.get("status"), dict)
                else "N/A"
            )
            itype     = (
                issue.get("issue_type", {}).get("label", "N/A")
                if isinstance(issue.get("issue_type"), dict)
                else "N/A"
            )
            print(f"  {i}. [{issue_key}] {title}")
            print(f"     ID: {issue_id} | Status: {status} | Type: {itype}")

        if len(all_issues) > 5:
            print(f"\n     ... and {len(all_issues) - 5} more issues")

        return all_issues

    else:
        print(f"\n  No issues found for any allowed type.")
        return []


# =============================================================
# MAIN
# =============================================================
def main():
    stats["started_at"] = datetime.now().isoformat()

    if not PROJECT_KEY:
        print("  ERROR: project_key not found in .env file")
        return

    issues = extract_issues_list(PROJECT_KEY)

    stats["completed_at"] = datetime.now().isoformat()

    # Save stats
    stats_file = save_json(stats, "logs", "issues_list_extraction.json")

    print("\n" + "=" * 60)
    print("   SUMMARY")
    print("=" * 60)
    print(f"  Total Issues     : {stats['total_issues']}")
    print(f"  Pages Processed  : {stats['pages_processed']}")
    print(f"  Types Extracted  : {[t['label'] for t in stats['issue_types_extracted']]}")
    print(f"  Types Skipped    : {[t['label'] for t in stats['issue_types_skipped']]}")
    for t in stats["issue_types_extracted"]:
        print(f"    {t['label']}: {t['count']} issues")
    print(f"  Total API Calls  : {stats['total_api_calls']}")
    print(f"  Failed API Calls : {stats['failed_api_calls']}")
    print(f"  Errors           : {len(stats['errors'])}")
    print(f"  Stats saved to   : {stats_file}")
    print("=" * 60)

    if issues:
        print("\n  Issues list extraction completed successfully!")
        print(f"  IMPORTANT: This file is required for all issue-level extractions")
        print(f"  File: data/raw/issues/{PROJECT_KEY}_issues_list.json\n")
    else:
        print("\n  Issues list extraction failed or no issues found!\n")


if __name__ == "__main__":
    main()