import requests
import subprocess
import os
import json
import time
import sys
from issue_type_filter import DATE_FROM, DATE_TO, SORT_BY, SORT_TYPE
import run_all_issue_extractions
from shared_thread_pool import shutdown_executor
from shared_http import get_shared_session
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
from datetime import datetime
from dotenv import load_dotenv
from threading import Thread, Lock

# =============================================================
# CONFIGURATION
# =============================================================
load_dotenv()
BASE_URL    = os.getenv("BASE_URL")
AUTH_TOKEN  = os.getenv("AUTH_TOKEN")
PROJECT_KEY = os.getenv("project_key")

DATA_ROOT        = f"{PROJECT_KEY}_data" if PROJECT_KEY else "data"
RAW_DATA_DIR     = f"{DATA_ROOT}/raw"
RATE_LIMIT_DELAY = 0.3
ISSUES_PER_PAGE  = 150
MAX_PAGES        = 10000  # per issue type per page batch
EXTRACTION_MAX_WORKERS = os.getenv("EXTRACTION_MAX_WORKERS", "3")
MAX_PARALLEL_EXTRACTION_SCRIPTS = os.getenv("MAX_PARALLEL_EXTRACTION_SCRIPTS", "1")

HEADERS = {
    "Authorization": f"Token {AUTH_TOKEN}",
    "Content-Type":  "application/json"
}

# Connection pooling: use the SAME process-wide shared session as the
# extractors (see shared_http.py) instead of a separate one just for
# fetch_page(), so the issues-list fetch pools connections together
# with every extractor's API calls rather than opening its own pool.
SESSION = get_shared_session()
SESSION.headers.update(HEADERS)

print_lock = Lock()

# =============================================================
# LOGGING
# =============================================================
def log(message, prefix=""):
    with print_lock:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}]{' ' + prefix if prefix else ''} {message}")

# =============================================================
# FETCH ONE PAGE FOR A SPECIFIC ISSUE TYPE
# =============================================================
def fetch_page(page_num, type_id, type_label):
    """
    Fetch one page of issues filtered by issue_type_id.
    Saves a per-type per-page file and returns (issues, filepath, total_pages).
    Filename includes type_id to keep pages from different types separate.
    """
    url    = f"{BASE_URL}/{PROJECT_KEY}/issues"
    params = {
        "issue_type_id": type_id,
        "page":          page_num,
        "per_page":      ISSUES_PER_PAGE,
        "query_hash[0][condition]": "created_at",
        "query_hash[0][operator]":  "is_in_the_range",
        "query_hash[0][value]":     f"{DATE_FROM},{DATE_TO}",
        "sort":                   SORT_BY,
        "sort_type":              SORT_TYPE,
    }

    for attempt in range(3):
        try:
            response = SESSION.get(url, headers=HEADERS, params=params, timeout=30)
            response.raise_for_status()
            time.sleep(RATE_LIMIT_DELAY)
            data = response.json()

            issues = data.get("issues", [])
            if not issues:
                return [], None, 0

            # Save per-type per-page file
            filename = f"{PROJECT_KEY}_issues_list_type_{type_id}_page_{page_num}.json"
            filepath = os.path.join(RAW_DATA_DIR, "issues", filename)
            os.makedirs(os.path.dirname(filepath), exist_ok=True)

            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "issues":      issues,
                        "total_count": len(issues),
                        "page":        page_num,
                        "type_id":     type_id,
                        "type_label":  type_label
                    },
                    f, indent=4, ensure_ascii=False
                )

            # Save per-type per-page issue IDs
            issue_ids    = [issue["id"] for issue in issues]
            ids_filename = f"{PROJECT_KEY}_issue_ids_type_{type_id}_page_{page_num}.json"
            ids_filepath = os.path.join(RAW_DATA_DIR, "issues", ids_filename)
            with open(ids_filepath, "w", encoding="utf-8") as f:
                json.dump(
                    {"issue_ids": issue_ids, "count": len(issue_ids), "page": page_num},
                    f, indent=4, ensure_ascii=False
                )

            meta        = data.get("meta", {})
            total_pages = meta.get("total_pages", page_num)

            log(
                f"Type '{type_label}' page {page_num} fetched: "
                f"{len(issues)} issues → {filepath}",
                prefix="[FETCH]"
            )

            return issues, filepath, total_pages

        except requests.exceptions.RequestException as e:
            if hasattr(e, "response") and e.response is not None and e.response.status_code == 429:
                time.sleep(30)
                continue

            if attempt == 2:
                log(
                    f"Type '{type_label}' page {page_num} FAILED after 3 attempts: {e}",
                    prefix="[FETCH]"
                )
                return None, None, None
            time.sleep(1 * (attempt + 1))

# =============================================================
# TRIGGER run_all_issue_extractions.py FOR A PAGE
# =============================================================
# =============================================================
# RUN EXTRACTIONS FOR A PAGE (in-process — no subprocess, no isolation)
# =============================================================
def run_extractions_for_page(page_num, issues_filepath, type_id, type_label):
    """
    Runs run_all_issue_extractions.main() directly, in this process,
    for one page.

    This used to spawn `python run_all_issue_extractions.py` as its own
    subprocess (per page), which is what forced each page — and each of
    its 6 extractors — onto separate, independent thread pools. Calling
    main() in-process instead means every API call, for every extractor,
    for every page, for every issue type, goes through the exact same
    ThreadPoolExecutor (shared_thread_pool.get_executor()).

    Trade-off: page context (EXTRACTION_PAGE_NUM, EXTRACTION_ISSUES_FILE,
    etc.) is passed via process environment variables, which are global
    to this process. That's safe for one page at a time, but unsafe for
    two pages running concurrently (one page's env vars could leak into
    another's extraction run mid-flight). So pages are now processed one
    at a time — the per-page process isolation is gone, and so is
    per-page *concurrency*; what you get in exchange is all API-call
    concurrency going through one bounded, shared pool sized by
    EXTRACTION_MAX_WORKERS, instead of it being scattered across N
    independent pools across N processes.
    """
    os.environ["EXTRACTION_PAGE_NUM"]    = str(page_num)
    os.environ["EXTRACTION_ISSUES_FILE"] = issues_filepath
    os.environ["EXTRACTION_TYPE_ID"]     = str(type_id)
    os.environ["EXTRACTION_TYPE_LABEL"]  = type_label
    os.environ["EXTRACTION_MAX_WORKERS"] = EXTRACTION_MAX_WORKERS
    os.environ["MAX_PARALLEL_EXTRACTION_SCRIPTS"] = MAX_PARALLEL_EXTRACTION_SCRIPTS

    log(
        f"Running extraction — type '{type_label}' page {page_num}",
        prefix="[PIPELINE]"
    )

    try:
        run_all_issue_extractions.main()
        log(f"Type '{type_label}' page {page_num} extractions completed", prefix="[PIPELINE]")
    except Exception as e:
        log(f"Type '{type_label}' page {page_num} extractions failed: {e}", prefix="[PIPELINE]")


# =============================================================
# SPRINTS & RELEASES (independent job — unchanged)
# =============================================================
def run_sprints():
    def run():
        log("Starting sprint_and_release.py (independent job)", prefix="[SPRINTS]")

        process = subprocess.Popen(
            [sys.executable, "sprint_and_release.py"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
            env=os.environ.copy()
        )

        for line in process.stdout:
            line = line.rstrip()
            if line:
                log(line, prefix="[sprints]")

        return_code = process.wait()
        status = "completed" if return_code == 0 else f"failed (code {return_code})"
        log(f"sprint_and_release.py {status}", prefix="[SPRINTS]")

    t = Thread(target=run, daemon=True)
    t.start()
    return t


# =============================================================
# MAIN
# =============================================================
def main():
    print("\n" + "=" * 80)
    print("  PIPELINE ORCHESTRATOR (FILTERED BY ISSUE TYPE)")
    print("=" * 80)
    print(f"  Project      : {PROJECT_KEY}")
    print(f"  Issues/page  : {ISSUES_PER_PAGE}")
    print(f"  Max pages    : {MAX_PAGES} per type")
    print(f"  Scripts/page : {MAX_PARALLEL_EXTRACTION_SCRIPTS}")
    print(f"  Shared pool workers (total, across all pages/scripts): {EXTRACTION_MAX_WORKERS}")
    print("=" * 80 + "\n")

    if not PROJECT_KEY:
        print("ERROR: project_key not found in .env file")
        return

    # --- Load allowed issue types from filter ---
    try:
        from issue_type_filter import get_allowed_issue_type_ids
        allowed_types = get_allowed_issue_type_ids(PROJECT_KEY)
    except Exception as e:
        print(f"ERROR: Could not load issue type filter: {e}")
        return

    log(f"Allowed issue types: {[t['label'] for t in allowed_types]}", prefix="[FILTER]")

    all_issues   = []

    # --- Start sprints job independently (unchanged — separate script/job) ---
    sprints_thread = run_sprints()

    # --- Loop over each allowed issue type ---
    for type_info in allowed_types:
        type_id    = type_info["id"]
        type_label = type_info["label"]

        log(f"Processing type '{type_label}' (id={type_id})...", prefix="[FETCH]")

        page = 1

        while page <= MAX_PAGES:
            result = fetch_page(page, type_id, type_label)

            if not result or result[0] is None:
                log(
                    f"Fetch failed for type '{type_label}' page {page}. Skipping.",
                    prefix="[FETCH]"
                )
                break

            issues, filepath, total_pages = result

            if not issues:
                log(
                    f"No issues for type '{type_label}' page {page}. Done.",
                    prefix="[FETCH]"
                )
                break

            all_issues.extend(issues)

            # Run this page's extraction now, in-process, before fetching the
            # next page. Pages used to fan out onto background subprocesses
            # (each with its own thread pool); now they share one pool, so
            # they run one at a time — see run_extractions_for_page() for why.
            run_extractions_for_page(page, filepath, type_id, type_label)

            if page >= total_pages or len(issues) < ISSUES_PER_PAGE:
                log(
                    f"Last page for type '{type_label}' "
                    f"({page} of {total_pages}).",
                    prefix="[FETCH]"
                )
                break

            page += 1

    log("All types fetched and extracted.", prefix="[PIPELINE]")

    # Wait for sprints thread
    sprints_thread.join()
    log("All extractions complete.", prefix="[PIPELINE]")

    # Every page/type is done — safe to tear down the one shared pool now.
    shutdown_executor(wait=True)

    # ── WRITE COMBINED STATS ──────────────────────────────────────
    details_dir = os.path.join(RAW_DATA_DIR, "issues", PROJECT_KEY)
    extracted_count = 0
    if os.path.isdir(details_dir):
        extracted_count = len([f for f in os.listdir(details_dir) if f.endswith(".json")])

    failed_log = os.path.join(RAW_DATA_DIR, "logs", "failed_issues.json")
    failed_count = 0
    if os.path.exists(failed_log):
        with open(failed_log, "r") as f:
            failed_count = json.load(f).get("count", 0)

    summary = {
        "completed_at":               datetime.now().isoformat(),
        "total_issues_in_list":       len(all_issues),
        "total_detail_files_on_disk": extracted_count,
        "total_failed_after_retries": failed_count,
        "types_processed":            [t["label"] for t in allowed_types],
    }

    summary_path = os.path.join(RAW_DATA_DIR, "logs", "pipeline_combined_stats.json")
    os.makedirs(os.path.dirname(summary_path), exist_ok=True)
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=4)

    log(f"Combined stats: {summary_path}", prefix="[STATS]")
    log(f"Issues in list : {len(all_issues)}", prefix="[STATS]")
    log(f"Files on disk  : {extracted_count}", prefix="[STATS]")
    log(f"Still failed   : {failed_count}", prefix="[STATS]")

    # --- Save combined issues file (same filename as before — unchanged) ---
    if all_issues:
        combined_path = os.path.join(
            RAW_DATA_DIR, "issues", f"{PROJECT_KEY}_issues_list.json"
        )
        os.makedirs(os.path.dirname(combined_path), exist_ok=True)
        with open(combined_path, "w", encoding="utf-8") as f:
            json.dump(
                {"issues": all_issues, "total_count": len(all_issues)},
                f, indent=4, ensure_ascii=False
            )
        log(f"Combined issues file saved: {combined_path}", prefix="[FETCH]")
        log(f"Total issues across all types: {len(all_issues)}", prefix="[FETCH]")

    print("\n" + "=" * 80)
    print(f"  DONE. {len(all_issues)} issues processed.")
    print(f"  Types: {[t['label'] for t in allowed_types]}")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user.\n")
        sys.exit(1)