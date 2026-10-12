"""
============================================================
version_sprints_update.py
============================================================
Fixes the missing-pagination bug in extract_sprints.py / extract_releases.py
(only ~30 records were pulled per project instead of the full set), WITHOUT
touching issues/comments/links/attachments, which are already loaded.

WHY THIS IS SAFE TO RUN AFTER ISSUES ARE ALREADY LOADED
---------------------------------------------------------
- load_versions() / load_sprints() (bulk_load_to_jira.py) already skip any
  source_id already present in id_mapping — re-running them will NOT
  recreate the ~30 that already exist, only create what's missing.
- assign_versions_to_issues() / assign_issues_to_sprints() use Jira's
  "add" semantics (fixVersions add, sprint move-issues) which are
  idempotent — re-running them for ALL issues is safe, and this is also
  what actually fixes issues that were SILENTLY skipped before (their
  sprint/version didn't exist yet, so the assign step just `continue`d
  with no error logged).

DOES THIS NEED TRANSFORM + PREPARE_PAYLOAD, OR CAN THE RAW DATA BE POSTED
DIRECTLY?
---------------------------------------------------------
It needs both. The raw extracted JSON isn't in Jira's POST shape:
  - transform_sprints()/transform_releases() map project keys and
    status/state codes, and add the source_sprint_id/source_release_id
    columns load_versions()/load_sprints() key off of.
  - prepare_sprints()/prepare_versions() turn that into the exact
    {"source_release_id": ..., "jira_project_key": ..., "payload": {...}}
    shape load_versions()/load_sprints() expect.
This script runs both stages automatically — you don't need to do
anything by hand beyond running this one script.

PIPELINE (sprints/versions only):
  1. extract_releases.py / extract_sprints.py   — paginated (subprocess)
  2. flatten_sprints() + flatten_releases()       (flatten.py)
  3. transform_sprints() + transform_releases()   (transform.py)
  4. prepare_sprints() + prepare_versions()       (prepare_payload.py)
  5. load_versions() + load_sprints()             (bulk_load_to_jira.py, resume-safe)
  6. assign_versions_to_issues() + assign_issues_to_sprints()
                                                   (bulk_load_to_jira.py, idempotent)

Run:  python version_sprints_update.py
============================================================
"""

import os
import sys
import subprocess
from dotenv import load_dotenv

load_dotenv()

PROJECT_KEY = os.getenv("project_key")


def run_step(description, fn, *args, **kwargs):
    print(f"\n{'=' * 70}")
    print(f"  {description}")
    print(f"{'=' * 70}")
    return fn(*args, **kwargs)


def check_raw_data_path_alignment():
    """
    extract_releases.py / extract_sprints.py write to:
        {project_key}_data/raw/releases/  and  .../sprints/
    (relative to whatever directory THEY are run from).

    flatten.py's flatten_releases()/flatten_sprints() read from
    flatten.RAW_DATA_DIR, which in this codebase is hardcoded as:
        test_refactored_shared_pool/{project_key}_data/raw

    These two only line up if version_sprints_update.py (and the two
    extract_*.py scripts) are run from ONE level above the
    test_refactored_shared_pool/ folder that flatten.py lives in. If
    your setup runs everything from inside test_refactored_shared_pool/
    itself, this WILL silently produce an empty sprints.csv/releases.csv
    (flatten_releases()/flatten_sprints() just print "no directory found"
    and return an empty DataFrame — not a loud error), so we check and
    fail loudly here instead.
    """
    import flatten  # local import so this check runs before anything heavy

    extraction_target = os.path.abspath(
        os.path.join("test_refactored_shared_pool", f"{PROJECT_KEY}_data", "raw")
    )
    flatten_expects    = os.path.abspath(flatten.RAW_DATA_DIR)

    print(f"  Extraction will write raw data to : {extraction_target}")
    print(f"  flatten.py will read raw data from: {flatten_expects}")

    if extraction_target != flatten_expects:
        print(
            "\n  ✖ PATH MISMATCH — extraction and flatten.py are pointed at "
            "different directories.\n"
            "    Run this script from the directory ONE LEVEL ABOVE "
            "test_refactored_shared_pool/\n"
            "    (i.e. the parent of the folder flatten.py lives in), so both "
            "paths resolve to the\n"
            "    same place. Aborting rather than silently producing an "
            "empty sprints/versions run."
        )
        return False

    print("  ✔ Paths align — safe to proceed.\n")
    return True


def main():
    if not PROJECT_KEY:
        print(" ERROR: project_key not set in .env")
        return 1

    print("=" * 70)
    print("  VERSION / SPRINT PAGINATION FIX + RE-SYNC")
    print("=" * 70)
    print(f"  Project: {PROJECT_KEY}")
    print("  This does NOT touch issues, comments, links, or attachments.")
    print("=" * 70)

    if not check_raw_data_path_alignment():
        return 1

   # ── Step 1: Re-extract ALL sprints + releases (paginated) ──────────
    print(f"\n{'=' * 70}")
    print("  STEP 1: Re-extracting sprints + releases (paginated)")
    print(f"{'=' * 70}")

    EXTRACTOR_DIR = "test_refactored_shared_pool"

    for script in ("extract_releases.py", "extract_sprints.py"):
        script_path = os.path.join(EXTRACTOR_DIR, script)
        if not os.path.exists(script_path):
            print(f" ERROR: {script} not found in {EXTRACTOR_DIR}/.")
            return 1
        print(f"\n--- running {script} (in {EXTRACTOR_DIR}/) ---")
        result = subprocess.run([sys.executable, script], cwd=EXTRACTOR_DIR)
        if result.returncode != 0:
            print(f" ERROR: {script} failed (exit code {result.returncode}) — stopping.")
            return 1

    # ── Step 2-4: flatten -> transform -> prepare_payload (sprints/versions only) ──
    import flatten
    import transform
    import prepare_payload

    run_step("STEP 2: Flattening releases", flatten.flatten_releases)
    run_step("STEP 2b: Flattening sprints", flatten.flatten_sprints)

    run_step("STEP 3: Transforming releases", transform.transform_releases)
    run_step("STEP 3b: Transforming sprints", transform.transform_sprints)

    run_step("STEP 4: Preparing version payloads", prepare_payload.prepare_versions)
    run_step("STEP 4b: Preparing sprint payloads", prepare_payload.prepare_sprints, prepare_payload.BOARD_ID)

    # ── Step 5-6: load + re-link via bulk_load_to_jira (already resume-safe) ──
    # Importing this module loads the existing id_mapping.json (RESUME_MODE
    # is hardcoded True there) — so load_versions()/load_sprints() will
    # correctly recognize and skip the ~30 already created.
    import bulk_load_to_jira as bl

    run_step("STEP 5: Loading versions to Jira (skips already-created)", bl.load_versions)
    run_step("STEP 5b: Loading sprints to Jira (skips already-created)", bl.load_sprints)

    run_step(
        "STEP 6: Re-assigning fixVersions to issues (idempotent — safe to re-run for all issues)",
        bl.assign_versions_to_issues,
    )
    run_step(
        "STEP 6b: Re-assigning issues to sprints (idempotent — safe to re-run for all issues)",
        bl.assign_issues_to_sprints,
    )

    bl.export_versions_to_excel()
    bl.export_sprints_to_excel()

    print(f"\n{'=' * 70}")
    print("  DONE — versions and sprints are now fully synced.")
    print(f"  See {bl.MAPPING_DIR}/versions_mapping.xlsx and sprints_mapping.xlsx")
    print(f"{'=' * 70}")
    return 0


if __name__ == "__main__":
    exit(main())