"""
Standalone script to extract sprints only.
Run this independently: python extract_sprints.py
"""

import requests
import json
import os
import time
from datetime import datetime
from dotenv import load_dotenv
import sys
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
PER_PAGE = int(os.getenv("SPRINTS_PER_PAGE", "100"))

HEADERS = {
    "Authorization": f"Token {AUTH_TOKEN}",
    "Content-Type": "application/json"
}

# Shared connection pool: reuses the ONE process-wide session so this
# extractor pools connections together with every other extractor
# instead of opening its own separate pool to the same host.
SESSION = get_shared_session()

# =============================================================
# STATS
# =============================================================
stats = {
    "script": "extract_sprints",
    "started_at": None,
    "completed_at": None,
    "total_api_calls": 0,
    "failed_api_calls": 0,
    "sprints_extracted": 0,
    "errors": []
}

# =============================================================
# HELPER FUNCTIONS
# =============================================================
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
                    "error": error_msg
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
# EXTRACTION
# =============================================================
def extract_sprints(project_key):
    """Extract ALL sprints for the project, paginated."""
    print("=" * 60)
    print("   EXTRACTING SPRINTS")
    print("=" * 60)
    print(f"  Project: {project_key}")
    print(f"  Output:  {RAW_DATA_DIR}/sprints/")
    print("=" * 60)

    all_sprints = []
    page = 1

    while True:
        data = api_get(f"{project_key}/sprints", params={"page": page, "per_page": PER_PAGE})
        if not data:
            break

        sprints = data.get("sprints", [])
        all_sprints.extend(sprints)

        meta        = data.get("meta", {})
        total_pages = meta.get("total_pages")
        total       = meta.get("total", "?")

        print(f"  page {page}" + (f"/{total_pages}" if total_pages else "")
              + f" — {len(sprints)} fetched (running: {len(all_sprints)}/{total})")

        if total_pages is not None and page >= int(total_pages):
            break
        if len(sprints) < PER_PAGE:
            break
        if not sprints:
            break
        page += 1

    if all_sprints:
        # Save in the SAME shape flatten.py's flatten_sprints() expects:
        # {"sprints": [...]}  — so downstream stays unchanged.
        filepath = save_json({"sprints": all_sprints}, "sprints", f"{project_key}_sprints.json")

        stats["sprints_extracted"] = len(all_sprints)

        print(f"\n Extracted {len(all_sprints)} sprints total ({page} page(s))")
        print(f" Saved to: {filepath}")

        print(f"\n Sprint Details:")
        print("-" * 60)
        for i, sprint in enumerate(all_sprints, 1):
            sprint_name = sprint.get("name", "Unnamed")
            sprint_id = sprint.get("id", "N/A")
            status = sprint.get("status", "N/A")
            start_date = sprint.get("start_date", "N/A")
            end_date = sprint.get("end_date", "N/A")

            print(f"{i}. {sprint_name} (ID: {sprint_id})")
            print(f"   Status: {status}")
            print(f"   Duration: {start_date}  {end_date}")
            print()

        return all_sprints
    else:
        print(f" Failed to extract sprints")
        return []


# =============================================================
# MAIN
# =============================================================
def main():
    stats["started_at"] = datetime.now().isoformat()
    
    if not PROJECT_KEY:
        print(" ERROR: project_key not found in .env file")
        return
    
    sprints = extract_sprints(PROJECT_KEY)
    
    stats["completed_at"] = datetime.now().isoformat()
    
    # Save stats
    stats_file = save_json(stats, "logs", "sprints_extraction.json")
    
    print("=" * 60)
    print("   SUMMARY")
    print("=" * 60)
    print(f"  Sprints Extracted: {stats['sprints_extracted']}")
    print(f"  Total API Calls: {stats['total_api_calls']}")
    print(f"  Failed API Calls: {stats['failed_api_calls']}")
    print(f"  Errors: {len(stats['errors'])}")
    print(f"  Stats saved to: {stats_file}")
    print("=" * 60)
    
    if sprints:
        print("\n Sprints extraction completed successfully!\n")
    else:
        print("\n  No sprints found or extraction failed!\n")


if __name__ == "__main__":
    main()