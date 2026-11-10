
# Bellhaven CRM Reconciliation

Sales Operations Analyst Take-Home Assessment

## Overview

This project reconciles Bellhaven Senior Living's official community directory against CRM account records.

It identifies missing accounts, incorrect account information, duplicate records, potential ownership changes (CHOW), and accounts that may no longer belong to Bellhaven.

The solution combines automated data collection and matching with a human-in-the-loop review application. No CRM changes are made without explicit reviewer approval.

## Technology

- Python
- Requests and BeautifulSoup
- Streamlit
- SQLite
- REST API
- GitHub Actions

## Project Files

| File | Purpose |
|------|---------|
| `scraper.py` | Scrapes community information from the official website |
| `fetch_crm.py` | Retrieves CRM account records through the API |
| `matching.py` | Reconciles website locations with CRM accounts |
| `app.py` | Interactive approval and rejection interface |
| `daily_pipeline.py` | Runs the read-only reconciliation pipeline |
| `.github/workflows/daily.yml` | Schedules daily reconciliation |
| `requirements.txt` | Python dependencies |

## Data Collection

The scraper extracts:

- Facility name
- Street address
- City
- State
- ZIP code
- Care offerings
- Official source URL

The initial website contained 35 communities.

CRM data is retrieved using the authenticated REST API, including account ownership, financial history, status, and duplicate/CHOW references.

## Matching Logic

The reconciliation process uses multiple signals:

1. Normalized physical addresses
2. Facility names and similarity scores
3. City, state, and ZIP
4. Parent account relationships
5. Existing duplicate and CHOW links

Common address abbreviations are normalized, including:

- Rd / Road
- St / Street
- Ave / Avenue
- Ln / Lane
- NW / Northwest
- W / West
- Pk / Pike

This prevents formatting differences from triggering unnecessary corrections.

### Finding Types

**CONFIDENT_MATCH**

The CRM account matches the website and requires no correction.

**NEEDS_FIX**

The account is identified, but specific fields require updating.

**MISSING_ACCOUNT_REVIEW**

A website location has no sufficiently verified CRM account and may require creating one.

**DUPLICATE_REVIEW**

Multiple CRM accounts appear to represent the same physical facility. A reviewer selects a survivor.

**CHOW_REQUIRED**

An account needs a parent change while having both lifetime revenue and outstanding accounts receivable.

**CRM_ONLY_REVIEW**

A CRM account currently belongs to Bellhaven but has no confirmed website match. These findings require investigation; website absence alone does not justify deactivation.

**MANUAL_REVIEW**

The matching evidence is insufficient for an automatic classification.

## Human-in-the-Loop Review

Run the application with:

```bash
streamlit run app.py
```

The reviewer can:

- Inspect official website evidence
- Review CRM account snapshots and candidate matches
- Examine proposed field changes
- Enter reviewer notes
- Approve or reject findings
- Select a surviving account for duplicate resolution
- Verify approved updates through the CRM API

Review decisions are stored in a local SQLite database.

CRM writes occur only after explicit approval.

## CHOW Business Rules

For a parent account change:

- If lifetime revenue > 0 AND outstanding AR > 0, the existing account must not be reparented.
- Instead, create a new account under the correct parent.
- Update only the historical account's `chow_current_account` field to reference the new account.
- Preserve the old parent, revenue, AR, and remaining historical fields.

If either revenue or outstanding AR is zero, direct reparenting may be performed after approval.

The application uses an operation journal to reduce the risk of duplicate account creation after uncertain API responses.

## Duplicate Resolution

When duplicate accounts are confirmed:

1. The reviewer selects a surviving account.
2. The survivor remains Active.
3. Other accounts are set to Inactive.
4. Each duplicate's `duplicate_of_account` points to the survivor.
5. Historical accounts are preserved rather than deleted.

The application checks current CRM financial values and existing relationships before applying these changes.

## Daily Reconciliation

The read-only pipeline can be run using:

```bash
python daily_pipeline.py
```

The GitHub Actions workflow is configured to run once daily.

The workflow:

1. Scrapes the official website.
2. Retrieves the latest CRM data.
3. Executes matching logic.
4. Saves review findings as workflow artifacts.

The automatic pipeline does not approve proposals or modify CRM accounts.

### Authentication

Set the `CRM_API_TOKEN` environment variable before running the pipeline.

For GitHub Actions, configure it as a repository secret named `CRM_API_TOKEN`.

Never commit API credentials to source control.

## Idempotence

Matching incorporates current CRM state, existing CHOW references, duplicate links, and persisted review decisions.

The goal is to avoid repeating previously resolved actions while allowing newly identified issues to enter the review queue.

Repeated matching runs against an unchanged snapshot have produced consistent findings.

### Persistence Limitation

The Streamlit review application currently uses a local SQLite decision database.

GitHub Actions runs in an isolated environment and does not automatically synchronize this local database or its approval history.

Therefore, scheduled workflow artifacts are independent daily reconciliation reports, not a continuously synchronized review queue.

Production deployment would require persistent shared storage for reviewer decisions and proposal state.

## Data Safety

- Credentials are not embedded in source code.
- Local CRM snapshots and review databases are excluded from Git.
- Scheduled matching is read-only.
- CHOW history is preserved.
- Duplicate accounts are never deleted.
- Ambiguous CRM-only accounts are not automatically deactivated.

## Demonstration

The recorded walkthrough demonstrates the review interface, supporting evidence, a human approval and rejection, and verification of an approved change in the live CRM.

## Assessment Results

The project identified and resolved ordinary CRM inconsistencies, missing accounts, duplicate records, and ownership-transition cases.

Three remaining CRM-only accounts require additional business evidence before any ownership or status modification can be justified.
