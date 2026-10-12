
"""
Standalone script to extract issue links for all issues.
PREREQUISITE: extract_issues_list.py must be run first.
Run this independently: python extract_issue_links.py
"""

import requests
import json
import os
import time
import re
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
    "script": "extract_issue_links",
    "started_at": None,
    "completed_at": None,
    "total_api_calls": 0,
    "failed_api_calls": 0,
    "issues_processed": 0,
    "issues_skipped": 0,
    "total_links": 0,
    "documents_downloaded": 0,
    "documents_failed": 0,
    "errors": []
}
# Shared connection pool: reuses the ONE process-wide session so all 6
# extractors (and every page) pool connections together instead of
# each opening its own separate pool to the same host.
tracker = ApiTracker(BASE_URL, HEADERS, RATE_LIMIT_DELAY, stats=stats, stats_lock=stats_lock,
                     session=get_shared_session())
# Thread-safe collectors
collectors_lock = Lock()
downloaded_doc_ids = set()

failed_issues = []
failed_issues_lock = Lock()

# =============================================================
# HELPER FUNCTIONS
# =============================================================
def api_get(endpoint, params=None, retries=5):
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
            error_str = str(e)
            request_id = "N/A"

            # ── HTTP error response (4xx, 5xx) ────────────────────────
            if hasattr(e, "response") and e.response is not None:
                response = e.response
                request_id = response.headers.get('x-request-id', 'NOT FOUND')

                print("\n" + "=" * 80)
                print(f"[FAILED API] {response.request.method} {response.url}")
                print(f"[STATUS]     {response.status_code}")
                print(f"[X-REQUEST-ID] {request_id}")
                print(f"[API CALL #] {call_number}")
                print(f"[ATTEMPT]    {attempt + 1}/{retries}")
                print("\n[RESPONSE HEADERS]")
                for k, v in response.headers.items():
                    print(f"  {k}: {v}")
                print("=" * 80 + "\n")

                if response.status_code == 429 or response.status_code >= 500:
                    tracker._record_retryable(endpoint, call_number, request_id, response.status_code)

                if response.status_code == 429:
                    time.sleep(30)
                    continue

                if attempt == retries - 1:
                    error_msg = f"{endpoint}: {error_str}"
                    tracker._record_failure(endpoint, call_number, error_msg, request_id, attempt=attempt + 1, permanent=True)
                    return None

            # ── DNS / connection failure ──────────────────────────────
            elif any(x in error_str for x in ["Name resolution", "NewConnectionError", "Failed to establish"]):
                wait = 20 * (attempt + 1)
                print("\n" + "=" * 80)
                print(f"[FAILED API] GET {url}")
                print(f"[STATUS]     DNS/Connection failure")
                print(f"[API CALL #] {call_number}")
                print(f"[ATTEMPT]    {attempt + 1}/{retries} — waiting {wait}s before retry")
                print(f"[ERROR]      {error_str}")
                print("=" * 80 + "\n")

                if attempt == retries - 1:
                    error_msg = f"{endpoint}: {error_str}"
                    tracker._record_failure(endpoint, call_number, error_msg, request_id, attempt=attempt + 1, permanent=True)
                    return None

                time.sleep(wait)
                continue
    else:
        error_msg = f"{endpoint}: exhausted {retries} attempts (persistent 429)"
        tracker._record_failure(endpoint, call_number, error_msg, request_id, attempt=retries, permanent=True)
        return None

def api_download(endpoint, save_path, retries=3):
    url = f"{BASE_URL}/{endpoint.lstrip('/')}"
    
    with stats_lock:
        stats["total_api_calls"] += 1

    download_headers = {
        "Authorization": f"Token {AUTH_TOKEN}"
    }

    for attempt in range(retries):
        try:
            response = tracker.session.get(
                url,
                headers=download_headers,
                timeout=60,
                stream=True
            )
            response.raise_for_status()

            # Determine filename
            original_filename = None
            cd = response.headers.get("Content-Disposition", "")
            if "filename=" in cd:
                match = re.search(r'filename[*]?=["\']?([^"\';\r\n]+)', cd)
                if match:
                    original_filename = match.group(1).strip()

            import mimetypes
            content_type = response.headers.get("Content-Type", "")
            guessed_ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""

            if original_filename:
                doc_id = os.path.splitext(os.path.basename(save_path))[0]
                final_name = f"{doc_id}_{original_filename}"
                final_path = os.path.join(os.path.dirname(save_path), final_name)
            elif guessed_ext:
                final_path = save_path + guessed_ext
            else:
                final_path = save_path + ".bin"

            os.makedirs(os.path.dirname(final_path), exist_ok=True)
            with open(final_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            time.sleep(RATE_LIMIT_DELAY)
            return final_path

        except requests.exceptions.RequestException as e:
            if hasattr(e, "response") and e.response is not None and e.response.status_code == 429:
                time.sleep(30)
                continue
            if attempt == retries - 1:
                error_msg = f"DOWNLOAD {endpoint}: {str(e)}"
                with stats_lock:
                    stats["failed_api_calls"] += 1
                    stats["errors"].append({"endpoint": endpoint, "error": error_msg})
                return None
            time.sleep(1 * (attempt + 1))


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
# DOCUMENT EXTRACTION
# =============================================================
def extract_document_ids_from_html(html_content):
    if not html_content or not isinstance(html_content, str):
        return set()

    doc_ids = set()

    src_pattern = re.findall(
        r'(?:src|href)\s*=\s*["\']/?documents/(\d+)["\']',
        html_content,
        re.IGNORECASE
    )
    doc_ids.update(src_pattern)

    data_pattern = re.findall(
        r'data-inline-document-id\s*=\s*["\'](\d+)["\']',
        html_content,
        re.IGNORECASE
    )
    doc_ids.update(data_pattern)

    return doc_ids


def scan_and_collect_doc_ids(obj):
    doc_ids = set()

    if isinstance(obj, dict):
        for value in obj.values():
            doc_ids.update(scan_and_collect_doc_ids(value))

    elif isinstance(obj, list):
        for item in obj:
            doc_ids.update(scan_and_collect_doc_ids(item))

    elif isinstance(obj, str) and ("/documents/" in obj or "data-inline-document-id" in obj):
        doc_ids.update(extract_document_ids_from_html(obj))

    return doc_ids


def download_document(project_key, doc_id):
    """Thread-safe document download"""
    with collectors_lock:
        if doc_id in downloaded_doc_ids:
            return
        
    doc_dir = os.path.join(RAW_DATA_DIR, "documents", project_key)
    if SKIP_EXISTING and os.path.isdir(doc_dir):
        try:
            existing = [f for f in os.listdir(doc_dir) if f.startswith(f"{doc_id}_") or f.startswith(f"{doc_id}.")]
            if existing:
                with collectors_lock:
                    downloaded_doc_ids.add(doc_id)
                return
        except:
            pass

    endpoints_to_try = [
        f"documents/{doc_id}",
        f"{project_key}/documents/{doc_id}",
        f"documents/{doc_id}/download",
        f"{project_key}/documents/{doc_id}/download",
    ]

    base_save_path = os.path.join(RAW_DATA_DIR, "documents", project_key, str(doc_id))

    for endpoint in endpoints_to_try:
        result = api_download(endpoint, base_save_path)
        if result:
            with collectors_lock:
                downloaded_doc_ids.add(doc_id)
            with stats_lock:
                stats["documents_downloaded"] += 1
            return

    with stats_lock:
        stats["documents_failed"] += 1


def download_documents_from_data(project_key, data):
    if not data:
        return

    doc_ids = scan_and_collect_doc_ids(data)
    if doc_ids:
        for doc_id in sorted(doc_ids, key=int):
            download_document(project_key, doc_id)


# =============================================================
# EXTRACTION
# =============================================================
def extract_issue_links(project_key, issue_id):
    """Extract issue links"""

    # Skip if file already exists
    if SKIP_EXISTING and file_exists("links", project_key, f"links_{issue_id}.json"):
        filepath = os.path.join(RAW_DATA_DIR, "links", project_key, f"links_{issue_id}.json")

        # Try to reuse existing file for downstream processing
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            download_documents_from_data(project_key, data)

        except Exception:
            pass  # ignore corrupted file

        return True, "skipped", 0

    # API call
    data = api_get(f"{project_key}/issues/{issue_id}/links")

    if not data:
        # No response from API
        return False, "failed", 0

    links = data.get("issue_links", [])

    # If links exist → save + process
    if links:
        save_json(data, "links", project_key, f"links_{issue_id}.json")
        download_documents_from_data(project_key, data)
        return True, "processed", len(links)

    # If no links → optionally skip saving empty file
    return True, "processed", 0


def process_single_issue(project_key, issue_id, index, total):
    """Process a single issue - runs in parallel"""
    try:
        success, status, link_count = extract_issue_links(project_key, issue_id)
        
        with stats_lock:
            if status == "processed":
                stats["issues_processed"] += 1
                stats["total_links"] += link_count
                if link_count > 0:
                    print(f"[{index}/{total}]  Issue {issue_id} - {link_count} link(s)")
                else:
                    print(f"[{index}/{total}]  Issue {issue_id} - no links")
            elif status == "skipped":
                stats["issues_skipped"] += 1
                print(f"[{index}/{total}]   Issue {issue_id} - skipped (exists)")
            else:
                print(f"[{index}/{total}]  Issue {issue_id} - failed")
                with failed_issues_lock:
                    failed_issues.append(issue_id)
        
        return success, issue_id
    
    except Exception as e:
        print(f"[{index}/{total}]  Issue {issue_id} - error: {str(e)}")
        with stats_lock:
            stats["errors"].append({
                "issue_id": issue_id,
                "error": str(e)
            })
        with failed_issues_lock:
            failed_issues.append(issue_id)
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
    
    print(f" Reading issues from: {filepath}")
    return data.get("issues", [])


# =============================================================
# MAIN
# =============================================================
def main():
    stats["started_at"] = datetime.now().isoformat()
    
    print("=" * 60)
    print("   EXTRACTING ISSUE LINKS (PARALLEL)")
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
    
    executor = get_executor()
    futures = {
        executor.submit(
            process_single_issue,
            PROJECT_KEY,
            issue["id"],
            i + 1,
            len(issues)
        ): issue["id"]
        for i, issue in enumerate(issues)
    }
    
    for future in as_completed(futures):
        issue_id = futures[future]
        try:
            future.result()
        except Exception as e:
            print(f" Unexpected error for issue {issue_id}: {str(e)}")

    # ── RETRY LOOP FOR FAILED ISSUES ──────────────────────────────
    MAX_RETRY_ROUNDS = 3

    for retry_round in range(1, MAX_RETRY_ROUNDS + 1):
        if not failed_issues:
            break

        # FIX (Bug 1): Copy and clear inside the lock to avoid race condition
        with failed_issues_lock:
            retry_targets = failed_issues.copy()
            failed_issues.clear()

        print(f"\n{'='*60}")
        print(f"  RETRY ROUND {retry_round} — {len(retry_targets)} failed issues")
        print(f"  Waiting 30s before retry to let DNS recover...")
        print(f"{'='*60}\n")
        time.sleep(30)

        executor = get_executor()
        futures = {
            executor.submit(
                process_single_issue,
                PROJECT_KEY,
                issue_id,
                i + 1,
                len(retry_targets)
            ): issue_id
            for i, issue_id in enumerate(retry_targets)
        }

        for future in as_completed(futures):
            issue_id = futures[future]
            try:
                future.result()
            except Exception as e:
                print(f"  Retry error for issue {issue_id}: {str(e)}")

    # Save any remaining failed issue IDs to a file for manual inspection
    if failed_issues:
        failed_path = save_json(
            {"failed_issue_ids": failed_issues, "count": len(failed_issues)},
            "logs", "failed_issue_links.json"
        )
        print(f"\n  {len(failed_issues)} issues still failed after {MAX_RETRY_ROUNDS} retries")
        print(f"  Saved to: {failed_path}")

    stats["completed_at"] = datetime.now().isoformat()
    
    # Save stats
    stats_file = save_json(stats, "logs", "issue_links_extraction.json")
    
    print("\n" + "=" * 60)
    print("   SUMMARY")
    print("=" * 60)
    print(f"  Total Issues:        {len(issues)}")
    print(f"  Processed:           {stats['issues_processed']}")
    print(f"  Skipped:             {stats['issues_skipped']}")
    # FIX (Bug 3): Use stats counters instead of the local `failed` variable
    print(f"  Failed:              {stats['failed_api_calls']}")
    print(f"  Total Links:         {stats['total_links']}")
    print(f"  Documents Downloaded:{stats['documents_downloaded']}")
    print(f"  Documents Failed:    {stats['documents_failed']}")
    print(f"  Total API Calls:     {stats['total_api_calls']}")
    print(f"  Failed API Calls:    {stats['failed_api_calls']}")
    print(f"  Errors:              {len(stats['errors'])}")
    print(f"  Stats saved to:      {stats_file}")
    print("=" * 60)
    tracker.print_summary()
    tracker.save_timing_log(
        os.path.join(RAW_DATA_DIR, "logs", f"{stats['script']}_timing.json"),
        source_name=stats["script"]
    )
    print("\n Issue links extraction completed!\n")


if __name__ == "__main__":
    main()