"""
Standalone script to extract integrated resources for all issues.
PREREQUISITE: extract_issues_list.py must be run first.
Run this independently: python extract_integrated_resources.py
"""

import requests
import json
import os
import time
from datetime import datetime
from dotenv import load_dotenv
from concurrent.futures import as_completed
from shared_thread_pool import get_executor
from threading import Lock
import sys
from api_tracker import ApiTracker
from shared_http import get_shared_session
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
# =============================================================
# CONFIGURATION
# =============================================================
load_dotenv()
BASE_URL = os.getenv("BASE_URL")
AUTH_TOKEN = os.getenv("AUTH_TOKEN")
PROJECT_KEY = os.getenv("project_key")

RAW_DATA_DIR = f"{PROJECT_KEY}_data/raw" if PROJECT_KEY else "data/raw"
RATE_LIMIT_DELAY = 0.2
SKIP_EXISTING = True
MAX_WORKERS = int(os.getenv("EXTRACTION_MAX_WORKERS", "3"))  # Controlled worker count

HEADERS = {
    "Authorization": f"Token {AUTH_TOKEN}",
    "Content-Type": "application/json"
}

# =============================================================
# THREAD-SAFE STATS
# =============================================================
stats_lock = Lock()
stats = {
    "script": "extract_integrated_resources",
    "started_at": None,
    "completed_at": None,
    "total_api_calls": 0,
    "failed_api_calls": 0,
    "issues_processed": 0,
    "issues_skipped": 0,
    "total_integrated_resources": 0,
    "issues_with_resources": 0,
    "issues_without_resources": 0,
    "errors": []
}
# Shared connection pool: reuses the ONE process-wide session so all 6
# extractors (and every page) pool connections together instead of
# each opening its own separate pool to the same host.
tracker = ApiTracker(BASE_URL, HEADERS, RATE_LIMIT_DELAY, stats=stats, stats_lock=stats_lock,
                     session=get_shared_session())
# =============================================================
# HELPER FUNCTIONS
# =============================================================
def api_get(endpoint, params=None, retries=3):
    url = f"{BASE_URL}/{endpoint.lstrip('/')}"

    with stats_lock:
        stats["total_api_calls"] += 1

    for attempt in range(retries):
        call_number = tracker.record_attempt()
        try:
            response = tracker.session.get(url, headers=HEADERS, params=params, timeout=30)
            response.raise_for_status()
            time.sleep(RATE_LIMIT_DELAY)
            return response.json()

        except requests.exceptions.RequestException as e:
            request_id = "N/A"
            if hasattr(e, "response") and e.response is not None:
                resp = e.response
                request_id = resp.headers.get('x-request-id', 'NOT FOUND')

                print("\n" + "=" * 80)
                print(f"[FAILED API] {resp.request.method} {resp.url}")
                print(f"[STATUS]     {resp.status_code}")
                print(f"[X-REQUEST-ID] {request_id}")
                print(f"[API CALL #] {call_number}")
                print(f"[ATTEMPT]    {attempt + 1}/{retries}")
                print("\n[RESPONSE HEADERS]")
                for k, v in resp.headers.items():
                    print(f"  {k}: {v}")
                print("=" * 80 + "\n")

                if resp.status_code == 429 or resp.status_code >= 500:
                    tracker._record_retryable(endpoint, call_number, request_id, resp.status_code)

                if resp.status_code == 429:
                    time.sleep(30)
                    continue

            if attempt == retries - 1:
                error_msg = f"{endpoint}: {str(e)}"
                tracker._record_failure(endpoint, call_number, error_msg, request_id, attempt=attempt + 1, permanent=True)
                return None
            time.sleep(1 * (attempt + 1))
    else:
        # Safety net: loop finished all attempts without an explicit return
        # above (e.g. a 429 that kept `continue`-ing past every attempt).
        error_msg = f"{endpoint}: exhausted {retries} attempts (persistent 429)"
        tracker._record_failure(endpoint, call_number, error_msg, request_id, attempt=retries, permanent=True)
        return None


def save_json(data, *path_parts):
    filepath = os.path.join(RAW_DATA_DIR, *path_parts)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    temp_filepath = filepath + ".tmp"
    
    with open(temp_filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    
    os.replace(temp_filepath, filepath)
    return filepath


def file_exists(*path_parts):
    return os.path.exists(os.path.join(RAW_DATA_DIR, *path_parts))


# =============================================================
# EXTRACTION
# =============================================================
# def extract_integrated_resources(issue_id):
#     """
#     Extract integrated resource metadata for a specific issue.
    
#     Args:
#         issue_id: The issue ID to fetch integrated resources for
    
#     Returns:
#         (success, status, resource_count)
#     """
#     if SKIP_EXISTING and file_exists("integrated_resources", f"ir_{issue_id}.json"):
#         return True, "skipped", 0
    
#     # Build endpoint and params
#     endpoint = "integrated_resources"   
#     params = {
#         "resource_id": issue_id,            
#         "resource_type": "Issue",
#         "integration_account_ids": 8  # Adjust this value based on your needs
#     }
    
#     # Fetch data
#     data = api_get(endpoint, params=params)
    
#     if not data:
#         # Save with null
#         payload = {
#             "issue_id": issue_id,
#             "integrated_resources": None,
#             "total_count": 0,
#         }
#         save_json(payload, "integrated_resources", f"ir_{issue_id}.json")
#         return True, "processed", 0
    
#     # Extract integrated resources
#     integrated_resources = data.get("integrated_resources", [])
    
#     # Always save (even if empty)
#     payload = {
#         "issue_id": issue_id,
#         "integrated_resources": integrated_resources if integrated_resources else None,
#         "total_count": len(integrated_resources) if integrated_resources else 0,
#     }
#     save_json(payload, "integrated_resources", f"ir_{issue_id}.json")
    
#     return True, "processed", len(integrated_resources) if integrated_resources else 0
def extract_integrated_resources(issue_id):
    """
    Extract integrated resource metadata for a specific issue.
    """

    if SKIP_EXISTING and file_exists("integrated_resources", f"ir_{issue_id}.json"):
        return True, "skipped", 0

    endpoint = "integrated_resources"

    params = {
        "resource_id": issue_id,
        "resource_type": "Issue",
        "integration_account_ids": 8
    }

    data = api_get(endpoint, params=params)

    if not data:
        return True, "processed", 0

    integrated_resources = data.get("integrated_resources", [])

    #  DO NOT SAVE IF EMPTY
    if not integrated_resources:
        return True, "processed", 0

    payload = {
        "issue_id": issue_id,
        "integrated_resources": integrated_resources,
        "total_count": len(integrated_resources),
    }

    save_json(payload, "integrated_resources", f"ir_{issue_id}.json")

    return True, "processed", len(integrated_resources)

def process_single_issue(issue_id, index, total):
    """Process a single issue - runs in parallel"""
    try:
        success, status, resource_count = extract_integrated_resources(issue_id)
        
        with stats_lock:
            if status == "processed":
                stats["issues_processed"] += 1
                stats["total_integrated_resources"] += resource_count
                
                if resource_count > 0:
                    stats["issues_with_resources"] += 1
                    print(f"[{index}/{total}]  Issue {issue_id} - {resource_count} resource(s)")
                else:
                    stats["issues_without_resources"] += 1
                    print(f"[{index}/{total}]  Issue {issue_id} - no resources")
            elif status == "skipped":
                stats["issues_skipped"] += 1
                print(f"[{index}/{total}]   Issue {issue_id} - skipped (exists)")
            else:
                print(f"[{index}/{total}]  Issue {issue_id} - failed")
        
        return success, issue_id
    
    except Exception as e:
        print(f"[{index}/{total}]  Issue {issue_id} - error: {str(e)}")
        with stats_lock:
            stats["errors"].append({
                "issue_id": issue_id,
                "error": str(e)
            })
        return False, issue_id


def load_issues_list(project_key):
    """Load issues list from file"""
    filepath = os.getenv(
    "EXTRACTION_ISSUES_FILE",
    os.path.join(RAW_DATA_DIR, "issues", f"{project_key}_issues_list.json")
)
    
    if not os.path.exists(filepath):
        print(f" ERROR: Issues list not found at {filepath}")
        print(f"   Please run extract_issues_list.py first!")
        return None
    
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    return data.get("issues", [])


# =============================================================
# MAIN
# =============================================================
def main():
    stats["started_at"] = datetime.now().isoformat()
    
    print("=" * 60)
    print("   EXTRACTING INTEGRATED RESOURCES (PARALLEL)")
    print("=" * 60)
    print(f"  Project: {PROJECT_KEY}")
    print(f"  Workers: {MAX_WORKERS}")
    print(f"  Skip existing: {SKIP_EXISTING}")
    print("=" * 60)
    
    # Load issues list
    print("\n Loading issues list...")
    issues = load_issues_list(PROJECT_KEY)
    
    if not issues:
        return
    
    print(f" Loaded {len(issues)} issues")
    
    # Process issues in parallel
    print(f"\n Processing {len(issues)} issues with {MAX_WORKERS} workers...\n")
    
    successful = 0
    failed = 0
    
    executor = get_executor()
    futures = {
        executor.submit(
            process_single_issue,
            issue["id"],
            i + 1,
            len(issues)
        ): issue["id"]
        for i, issue in enumerate(issues)
    }
    
    for future in as_completed(futures):
        issue_id = futures[future]
        try:
            success, _ = future.result()
            if success:
                successful += 1
            else:
                failed += 1
        except Exception as e:
            print(f" Unexpected error for issue {issue_id}: {str(e)}")
            failed += 1
    
    stats["completed_at"] = datetime.now().isoformat()
    
    # Save stats
    stats_file = save_json(stats, "logs", "integrated_resources_extraction.json")
    
    print("\n" + "=" * 60)
    print("   SUMMARY")
    print("=" * 60)
    print(f"  Total Issues: {len(issues)}")
    print(f"  Processed: {stats['issues_processed']}")
    print(f"  Skipped: {stats['issues_skipped']}")
    print(f"  Failed: {failed}")
    print(f"  Total Integrated Resources: {stats['total_integrated_resources']}")
    print(f"  Issues WITH Resources: {stats['issues_with_resources']}")
    print(f"  Issues WITHOUT Resources: {stats['issues_without_resources']}")
    print(f"  Total API Calls: {stats['total_api_calls']}")
    print(f"  Failed API Calls: {stats['failed_api_calls']}")
    print(f"  Errors: {len(stats['errors'])}")
    print(f"  Stats saved to: {stats_file}")
    print("=" * 60)
    tracker.print_summary()
    tracker.save_timing_log(
        os.path.join(RAW_DATA_DIR, "logs", f"{stats['script']}_timing.json"),
        source_name=stats["script"]
    )
    print("\n Integrated resources extraction completed!\n")


if __name__ == "__main__":
    main()