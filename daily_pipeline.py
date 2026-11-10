
"""Daily Bellhaven reconciliation pipeline.

1. Scrape the official website.
2. Fetch current CRM data using a secret API token.
3. Generate matching proposals.

READ ONLY: This script never POSTs or PATCHes CRM accounts.
All CRM modifications require human approval in app.py.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import requests

BASE_URL = (
    "https://analyst-assessment-production.up.railway.app/api/v1"
)

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def run_script(filename):
    print(f"\nRunning {filename}", flush=True)

    subprocess.run(
        [sys.executable, str(ROOT / filename)],
        cwd=ROOT,
        check=True,
    )


def fetch_crm_headless():
    token = os.environ.get("CRM_API_TOKEN", "").strip()

    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    if not token:
        raise RuntimeError(
            "CRM_API_TOKEN is missing. "
            "Configure it as a GitHub Actions secret "
            "or local environment variable."
        )

    DATA.mkdir(parents=True, exist_ok=True)

    accounts = []
    page = 1

    with requests.Session() as session:
        session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })

        while page <= 100:
            response = session.get(
                BASE_URL + "/accounts",
                params={
                    "page": page,
                    "page_size": 50,
                },
                timeout=45,
            )

            response.raise_for_status()
            result = response.json()

            batch = result.get("data", [])

            if not isinstance(batch, list):
                raise ValueError(
                    "Unexpected CRM API response."
                )

            accounts.extend(batch)

            print(
                f"CRM page {page}: {len(batch)} accounts",
                flush=True,
            )

            total = result.get("total")

            if total is not None:
                if len(accounts) >= int(total):
                    break

            if not batch or len(batch) < 50:
                break

            page += 1

        else:
            raise RuntimeError(
                "Pagination limit exceeded."
            )

    if not accounts:
        raise RuntimeError(
            "CRM returned no accounts. "
            "Existing snapshot was not replaced."
        )

    ids = [a.get("account_id") for a in accounts]

    if len(ids) != len(set(ids)):
        raise RuntimeError(
            "Duplicate account IDs in API pagination."
        )

    output = DATA / "crm_accounts.json"
    temporary = DATA / "crm_accounts.tmp"

    temporary.write_text(
        json.dumps(
            accounts,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    temporary.replace(output)

    print(
        f"CRM download complete: {len(accounts)} accounts",
        flush=True,
    )


def main():
    print("BELLHAVEN DAILY RECONCILIATION")
    print("Mode: READ ONLY")

    # Check credentials before starting any work.
    if not os.environ.get("CRM_API_TOKEN", "").strip():
        raise RuntimeError(
            "Missing CRM_API_TOKEN."
        )

    # 1. Refresh website records.
    run_script("scraper.py")

    # 2. Refresh CRM account snapshot.
    fetch_crm_headless()

    # 3. Generate review proposals.
    run_script("matching.py")

    output = DATA / "proposals.json"

    if not output.exists():
        raise RuntimeError(
            "Matching did not produce proposals.json."
        )

    proposals = json.loads(
        output.read_text(encoding="utf-8")
    )

    pending = sum(
        bool(p.get("requires_approval"))
        for p in proposals
    )

    print("\nDAILY PIPELINE COMPLETE")
    print("Total findings:", len(proposals))
    print("Reviewable findings:", pending)
    print("CRM writes: NONE")
    print("Human approval required: YES")


if __name__ == "__main__":
    main()
