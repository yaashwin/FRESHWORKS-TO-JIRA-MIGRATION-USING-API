"""
Run all 6 extraction scripts for a specific page.
Called by pipeline_orchestrator.py — do not run directly.
Reads EXTRACTION_PAGE_NUM and EXTRACTION_ISSUES_FILE from environment.

NOTE ON CONCURRENCY:
Each extractor used to be launched as its own subprocess, which meant each
one built its own independent ThreadPoolExecutor for its API calls — so a
single page could have up to 6 separate worker pools hammering the API at
once. These are now imported and run in-process (as threads, not
subprocesses) so every extractor's API calls flow through ONE shared
ThreadPoolExecutor (see shared_thread_pool.py). MAX_PARALLEL_EXTRACTION_SCRIPTS
still controls how many extractor "main" routines start at once, but actual
API-call concurrency is capped in one place by EXTRACTION_MAX_WORKERS.
"""

import os
import json
import time
import importlib
from datetime import datetime
from dotenv import load_dotenv
from threading import Thread, Lock
import sys

from shared_thread_pool import shutdown_executor

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

# =============================================================
# CONFIGURATION
# =============================================================
load_dotenv()
PROJECT_KEY = os.getenv("project_key")
RAW_DATA_DIR = f"{PROJECT_KEY}_data/raw" if PROJECT_KEY else "data/raw"
MAX_PARALLEL_EXTRACTION_SCRIPTS = int(os.getenv("MAX_PARALLEL_EXTRACTION_SCRIPTS", "1"))

EXTRACTION_SCRIPTS = [
    "extract_issue_details.py",
    "extract_comments.py",
    "extract_issue_links.py",
    "extract_dependencies.py",
    "extract_documents.py",
    "extract_integrated_resources.py"
]

output_lock = Lock()

# =============================================================
# HELPERS
# =============================================================
def run_script_in_process(script_name, script_stats, env):
    """
    Run a single extraction script's main() in-process (not as a subprocess).

    Running in-process is what lets all 6 extractors share ONE
    ThreadPoolExecutor (see shared_thread_pool.py) for their API calls,
    instead of each subprocess building its own independent pool.
    """
    script_stats['status'] = 'running'
    script_stats['start_time'] = datetime.now()
    page_num = env.get("EXTRACTION_PAGE_NUM", "?")
    script_short = script_name.replace("extract_", "").replace(".py", "")

    # Page/type context is passed via env vars; extractors read these with
    # os.getenv(...) at call time, so updating the process environment here
    # (this process only runs one page at a time) is equivalent to what the
    # old subprocess `env=` argument provided.
    os.environ.update(env)

    module_name = script_name[:-3] if script_name.endswith(".py") else script_name

    try:
        with output_lock:
            print(f"[page {page_num}][{script_short}] starting")

        module = importlib.import_module(module_name)
        module.main()

        script_stats['end_time'] = datetime.now()
        script_stats['return_code'] = 0
        script_stats['status'] = 'completed'

        with output_lock:
            print(f"[page {page_num}][{script_name}] completed successfully")

    except Exception as e:
        script_stats['status'] = 'error'
        script_stats['error'] = str(e)
        script_stats['return_code'] = 1
        script_stats['end_time'] = datetime.now()
        with output_lock:
            print(f"[page {page_num}][{script_name}] failed: {str(e)}")


def load_script_stats(script_name, page_num):
    """Load per-page log file if it exists."""
    script_log_name = script_name.replace("extract_", "").replace(".py", f"_extraction_page_{page_num}.json")
    log_path = os.path.join(RAW_DATA_DIR, "logs", script_log_name)
    if os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return None
    return None


def print_summary(all_stats, total_time, page_num):
    """Print per-page summary."""
    print("\n" + "=" * 80)
    print(f"  SUMMARY - PAGE {page_num}")
    print("=" * 80)

    completed = sum(1 for s in all_stats.values() if s['status'] == 'completed')
    failed = sum(1 for s in all_stats.values() if s['status'] in ['failed', 'error'])

    print(f"\n  Total Scripts : {len(EXTRACTION_SCRIPTS)}")
    print(f"  Completed     : {completed}")
    print(f"  Failed        : {failed}")
    print(f"  Total Time    : {total_time:.2f} seconds")

    print("\n" + "-" * 80)
    print("  SCRIPT RESULTS:")
    print("-" * 80)

    for script_name, stats in all_stats.items():
        script_display = script_name.replace("extract_", "").replace(".py", "")
        status_symbol = "OK" if stats['status'] == 'completed' else "FAIL"

        duration = 0
        if stats.get('start_time') and stats.get('end_time'):
            duration = (stats['end_time'] - stats['start_time']).total_seconds()

        print(f"\n  [{status_symbol}] {script_display.upper()}")
        print(f"     Status  : {stats['status']}")
        print(f"     Duration: {duration:.2f}s")

        detailed = load_script_stats(script_name, page_num)
        if detailed:
            for key, label in [
                ('issues_processed',        'Issues Processed'),
                ('issues_skipped',          'Issues Skipped'),
                ('total_comments',          'Total Comments'),
                ('total_links',             'Total Links'),
                ('total_dependencies',      'Total Dependencies'),
                ('total_documents',         'Total Documents'),
                ('total_integrated_resources', 'Total Resources'),
                ('total_api_calls',         'API Calls'),
            ]:
                if key in detailed:
                    print(f"     {label}: {detailed[key]}")
            if detailed.get('errors'):
                print(f"     Errors  : {len(detailed['errors'])}")

    print("\n" + "=" * 80)
    if failed == 0:
        print(f"  PAGE {page_num} — ALL EXTRACTIONS COMPLETED SUCCESSFULLY!")
    else:
        print(f"  PAGE {page_num} — {failed} SCRIPT(S) FAILED")
    print("=" * 80 + "\n")


# =============================================================
# MAIN — called by pipeline_orchestrator.py (this process handles one page)
# =============================================================
def main():
    start_time = time.time()

    # Read page context from environment (set by pipeline_orchestrator.py)
    page_num = os.getenv("EXTRACTION_PAGE_NUM", "unknown")
    issues_file = os.getenv("EXTRACTION_ISSUES_FILE", "")

    print("\n" + "=" * 80)
    print(f"  PARALLEL EXTRACTION RUNNER — PAGE {page_num}")
    print("=" * 80)
    print(f"  Project      : {PROJECT_KEY}")
    print(f"  Issues file  : {issues_file}")
    print(f"  Scripts      : {len(EXTRACTION_SCRIPTS)}")
    print(f"  Script slots : {MAX_PARALLEL_EXTRACTION_SCRIPTS}")
    print(f"  Shared pool workers (total, across all scripts): {os.getenv('EXTRACTION_MAX_WORKERS', '3')}")
    print("=" * 80 + "\n")

    if not issues_file or not os.path.exists(issues_file):
        print(f"  ERROR: Issues file not found: {issues_file}")
        sys.exit(1)

    # Build env for child scripts — pass page context forward
    env = os.environ.copy()
    env["EXTRACTION_PAGE_NUM"] = str(page_num)
    env["EXTRACTION_ISSUES_FILE"] = issues_file

    # Initialize stats
    all_stats = {script: {'status': 'pending'} for script in EXTRACTION_SCRIPTS}

    print(
        f"  Starting {len(EXTRACTION_SCRIPTS)} scripts with "
        f"max {MAX_PARALLEL_EXTRACTION_SCRIPTS} script(s) at a time...\n"
    )

    # Launch extraction scripts with a controlled script-level cap.
    # Business logic inside each extractor is unchanged; only execution concurrency is controlled.
    for i in range(0, len(EXTRACTION_SCRIPTS), MAX_PARALLEL_EXTRACTION_SCRIPTS):
        batch = EXTRACTION_SCRIPTS[i:i + MAX_PARALLEL_EXTRACTION_SCRIPTS]
        threads = []

        for script_name in batch:
            thread = Thread(
                target=run_script_in_process,
                args=(script_name, all_stats[script_name], env)
            )
            thread.start()
            threads.append(thread)
            time.sleep(0.2)  # small stagger

        # Wait for the current batch before launching the next batch.
        for thread in threads:
            thread.join()

    total_time = time.time() - start_time
    print_summary(all_stats, total_time, page_num)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Interrupted by user.\n")
        sys.exit(1)
    finally:
        # Only tear the shared pool down when this script is run standalone.
        # When pipeline_orchestrator.py imports and calls main() itself (once
        # per page, in-process), IT owns the pool's lifetime — shutting it
        # down here would break every later page that reuses the same pool.
        shutdown_executor(wait=True)