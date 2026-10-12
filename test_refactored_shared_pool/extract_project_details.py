"""
Standalone script to extract project details only.
Run this independently: python extract_project_details.py
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
    "script": "extract_project_details",
    "started_at": None,
    "completed_at": None,
    "total_api_calls": 0,
    "failed_api_calls": 0,
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
def extract_project_details(project_key):
    """Extract project details"""
    print("=" * 60)
    print("   EXTRACTING PROJECT DETAILS")
    print("=" * 60)
    print(f"  Project: {project_key}")
    print(f"  Output:  {RAW_DATA_DIR}/projects/")
    print("=" * 60)
    
    data = api_get(f"projects/{project_key}")

    if data:
        filepath = save_json(data, "projects", f"{project_key}_details.json")
        print(f" Saved project details to: {filepath}")
        
        # Save users if present
        if "users" in data:
            users = data.get("users", [])
            if users:
                save_json(users, "reference", "users_from_project.json")
                print(f"    Saved {len(users)} users")
        
        # Save issue types if present
        if "issue_types" in data:
            issue_types = data.get("issue_types", [])
            if issue_types:
                save_json(issue_types, "reference", "issue_types_from_project.json")
                print(f"    Saved {len(issue_types)} issue types")
        
        # Save priorities if present
        if "priorities" in data:
            priorities = data.get("priorities", [])
            if priorities:
                save_json(priorities, "reference", "priorities_from_project.json")
                print(f"    Saved {len(priorities)} priorities")
        
        # Save statuses if present
        if "statuses" in data:
            statuses = data.get("statuses", [])
            if statuses:
                save_json(statuses, "reference", "statuses_from_project.json")
                print(f"    Saved {len(statuses)} statuses")
        
        return data
    else:
        print(f" Failed to extract project details")
        return None


# =============================================================
# MAIN
# =============================================================
def main():
    stats["started_at"] = datetime.now().isoformat()
    
    if not PROJECT_KEY:
        print(" ERROR: project_key not found in .env file")
        return
    
    result = extract_project_details(PROJECT_KEY)
    
    stats["completed_at"] = datetime.now().isoformat()
    
    # Save stats
    stats_file = save_json(stats, "logs", "project_details_extraction.json")
    
    print("\n" + "=" * 60)
    print("   SUMMARY")
    print("=" * 60)
    print(f"  Total API Calls: {stats['total_api_calls']}")
    print(f"  Failed API Calls: {stats['failed_api_calls']}")
    print(f"  Errors: {len(stats['errors'])}")
    print(f"  Stats saved to: {stats_file}")
    print("=" * 60)
    
    if result:
        print("\n Project details extraction completed successfully!\n")
    else:
        print("\n Project details extraction failed!\n")


if __name__ == "__main__":
    main()