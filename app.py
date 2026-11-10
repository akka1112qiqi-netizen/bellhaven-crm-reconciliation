
import json
import re
import sqlite3
import hashlib
from pathlib import Path

import requests
import streamlit as st

BASE_URL = "https://analyst-assessment-production.up.railway.app/api/v1"
PARENT_ID = "0015QAPLGS3FVYEEEM"

DATA = Path("data")
DATA.mkdir(exist_ok=True)
DB_PATH = DATA / "review_decisions.db"

st.set_page_config(page_title="Bellhaven CRM Review", layout="wide")


# =====================================================
# DATABASE AND REVIEW HISTORY
# =====================================================

def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS decisions (
            proposal_id TEXT PRIMARY KEY,
            decision TEXT NOT NULL,
            details TEXT,
            decided_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS operations (
            proposal_id TEXT PRIMARY KEY,
            operation_type TEXT NOT NULL,
            state TEXT NOT NULL,
            new_account_id TEXT,
            message TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn


def decision_for(pid):
    with db() as conn:
        return conn.execute(
            "SELECT decision, details FROM decisions "
            "WHERE proposal_id = ?",
            (pid,)
        ).fetchone()


def save_decision(pid, decision, details):
    with db() as conn:
        conn.execute(
            "INSERT INTO decisions "
            "(proposal_id, decision, details) "
            "VALUES (?, ?, ?)",
            (pid, decision, details)
        )
        conn.commit()


def operation_for(pid):
    with db() as conn:
        return conn.execute(
            "SELECT state, new_account_id, message "
            "FROM operations WHERE proposal_id = ?",
            (pid,)
        ).fetchone()


def record_operation(pid, kind, state, new_id=None, message=""):
    with db() as conn:
        conn.execute("""
            INSERT INTO operations
            (proposal_id, operation_type, state,
             new_account_id, message)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(proposal_id) DO UPDATE SET
                state = excluded.state,
                new_account_id = excluded.new_account_id,
                message = excluded.message,
                updated_at = CURRENT_TIMESTAMP
        """, (pid, kind, state, new_id, message))
        conn.commit()


# =====================================================
# DATA HELPERS
# =====================================================

def load_json(filename):
    with open(DATA / filename, encoding="utf-8") as f:
        return json.load(f)


def proposal_id(proposal):
    if proposal.get("proposal_id"):
        return proposal["proposal_id"]

    raw = json.dumps(
        proposal, sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def norm(value):
    return re.sub(
        r"[^a-z0-9]", "", str(value or "").lower()
    )


ABBREVIATIONS = {
    "rd": "road",
    "st": "street",
    "ave": "avenue",
    "blvd": "boulevard",
    "ln": "lane",
    "dr": "drive",
    "ct": "court",
    "pk": "pike",
    "pkwy": "parkway",
    "nw": "northwest",
    "ne": "northeast",
    "sw": "southwest",
    "se": "southeast",
    "n": "north",
    "s": "south",
    "e": "east",
    "w": "west",
}


def street_norm(value):
    words = re.findall(
        r"[a-z0-9]+", str(value or "").lower()
    )
    return "".join(
        ABBREVIATIONS.get(word, word)
        for word in words
    )


def zip5(value):
    return re.sub(
        r"\D", "", str(value or "")
    )[:5]


def money(value):
    return float(value or 0)


def same_address(account, location):
    return all([
        street_norm(account.get("billing_street"))
        == street_norm(location.get("address")),
        norm(account.get("billing_city"))
        == norm(location.get("city")),
        norm(account.get("billing_state"))
        == norm(location.get("state")),
        zip5(account.get("billing_zip"))
        == zip5(location.get("zip")),
    ])


# =====================================================
# CRM API
# =====================================================

def api(method, path, token, payload=None, params=None):
    response = requests.request(
        method,
        BASE_URL + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json=payload,
        params=params,
        timeout=30,
    )

    response.raise_for_status()

    if not response.content:
        return {}

    return response.json()


def unwrap(result):
    if isinstance(result, dict):
        return result.get("data", result)
    return result


def get_account(account_id, token):
    result = unwrap(
        api("GET", f"/accounts/{account_id}", token)
    )
    if not isinstance(result, dict):
        raise ValueError("Invalid CRM account response")
    return result


def get_all_accounts(token):
    accounts = []
    page = 1

    while page <= 100:
        result = api(
            "GET", "/accounts", token,
            params={"page": page, "page_size": 50}
        )
        batch = result.get("data", [])

        if not isinstance(batch, list):
            raise ValueError("Invalid CRM list response")

        accounts.extend(batch)
        total = result.get("total")

        if total is not None and len(accounts) >= int(total):
            break

        if len(batch) < 50:
            break

        page += 1

    return accounts


def patch_account(account_id, changes, token):
    if not changes:
        return {}
    return api(
        "PATCH",
        f"/accounts/{account_id}",
        token,
        payload=changes,
    )


def build_account(location):
    offerings = location.get("care_offerings", [])
    care_type = (
        ", ".join(offerings)
        if isinstance(offerings, list)
        else str(offerings or "")
    )

    return {
        "name": location["name"],
        "parent_id": PARENT_ID,
        "billing_street": location["address"],
        "billing_city": location["city"],
        "billing_state": location["state"],
        "billing_zip": location["zip"],
        "care_type": care_type,
        "status": "Active",
    }


# =====================================================
# ORDINARY CRM FIXES
# =====================================================

def approve_fix(proposal, token):
    account_id = proposal["account_id"]
    current = get_account(account_id, token)

    if current.get("account_id") != account_id:
        raise ValueError("Account identity mismatch")

    changes = dict(proposal.get("proposed_changes") or {})

    if not changes:
        raise ValueError("No proposed changes")

    allowed = {
        "name", "parent_id", "billing_street",
        "billing_city", "billing_state",
        "billing_zip", "care_type", "status"
    }

    if not set(changes).issubset(allowed):
        raise ValueError("Unexpected CRM fields in proposal")

    expected = proposal.get("expected_current_values") or {}

    for field, old_value in expected.items():
        if field in changes and current.get(field) != old_value:
            if current.get(field) != changes[field]:
                raise ValueError(
                    f"{field} changed since review. Refresh CRM."
                )

    if "parent_id" in changes:
        changing_parent = (
            current.get("parent_id") != changes["parent_id"]
        )
        if changing_parent and (
            money(current.get("lifetime_revenue")) > 0
            and money(current.get("outstanding_ar")) > 0
        ):
            raise ValueError(
                "CHOW required. Direct parent change blocked."
            )

    if current.get("duplicate_of_account"):
        raise ValueError(
            "This account is marked as duplicate."
        )

    updates = {
        k: v for k, v in changes.items()
        if current.get(k) != v
    }

    if updates:
        patch_account(account_id, updates, token)

    verified = get_account(account_id, token)

    for field, value in changes.items():
        if verified.get(field) != value:
            raise RuntimeError(
                f"CRM verification failed: {field}"
            )

    return f"Updated and verified account {account_id}"


# =====================================================
# ACCOUNT CREATION WITH JOURNAL
# =====================================================

def create_with_journal(proposal, token):
    pid = proposal_id(proposal)
    kind = proposal["type"]
    location = proposal["location"]

    previous = operation_for(pid)

    if previous:
        state, new_id, message = previous

        if new_id:
            account = get_account(new_id, token)

            if (
                account.get("parent_id") == PARENT_ID
                and same_address(account, location)
            ):
                return new_id

            raise ValueError(
                "Previous created account does not match."
            )

        raise ValueError(
            "Previous creation attempt exists. Inspect CRM "
            f"before retrying. State: {state}. {message}"
        )

    accounts = get_all_accounts(token)

    existing = [
        a for a in accounts
        if a.get("parent_id") == PARENT_ID
        and a.get("status") == "Active"
        and same_address(a, location)
    ]

    if existing:
        raise ValueError(
            "Account already exists at this address: "
            + ", ".join(a["account_id"] for a in existing)
        )

    payload = build_account(location)

    record_operation(
        pid, kind, "POST_STARTED",
        message="Account creation initiated"
    )

    try:
        result = unwrap(
            api("POST", "/accounts", token, payload=payload)
        )
    except Exception as error:
        record_operation(
            pid, kind, "NEEDS_INSPECTION",
            message=str(error)[:500]
        )
        raise RuntimeError(
            "POST result uncertain. Inspect live CRM "
            "before retrying."
        ) from error

    new_id = None

    if isinstance(result, dict):
        new_id = result.get(
            "account_id", result.get("id")
        )

    if not new_id:
        accounts = get_all_accounts(token)
        matches = [
            a for a in accounts
            if a.get("created_by_candidate")
            and a.get("parent_id") == PARENT_ID
            and same_address(a, location)
        ]

        if len(matches) != 1:
            record_operation(
                pid, kind, "NEEDS_INSPECTION",
                message="Could not uniquely identify POST result"
            )
            raise RuntimeError(
                "Cannot uniquely identify created account. "
                "Do not repeat POST."
            )

        new_id = matches[0]["account_id"]

    record_operation(
        pid, kind, "CREATED", new_id=new_id,
        message="New account identified"
    )

    created = get_account(new_id, token)

    if not (
        created.get("parent_id") == PARENT_ID
        and same_address(created, location)
    ):
        raise RuntimeError(
            "Created account verification failed."
        )

    return new_id


# =====================================================
# CHOW SOP
# =====================================================

def approve_chow(proposal, token):
    pid = proposal_id(proposal)
    old_id = proposal["account_id"]
    location = proposal["location"]

    old = get_account(old_id, token)
    original_parent = proposal.get("current_parent_id")

    if old.get("account_id") != old_id:
        raise ValueError("Old account identity mismatch")

    if old.get("parent_id") != original_parent:
        raise ValueError(
            "Old parent changed since proposal generation."
        )

    if old.get("parent_id") == PARENT_ID:
        raise ValueError(
            "Old account already belongs to Bellhaven."
        )

    if not (
        money(old.get("lifetime_revenue")) > 0
        and money(old.get("outstanding_ar")) > 0
    ):
        raise ValueError(
            "CHOW financial conditions are not met."
        )

    existing_link = old.get("chow_current_account")

    if existing_link:
        linked = get_account(existing_link, token)

        if (
            linked.get("parent_id") == PARENT_ID
            and same_address(linked, location)
        ):
            record_operation(
                pid, "CHOW_REQUIRED",
                "COMPLETED", new_id=existing_link,
                message="Existing CHOW link verified"
            )
            return (
                f"CHOW already linked: {old_id} "
                f"-> {existing_link}"
            )

        raise ValueError("Conflicting existing CHOW link.")

    new_id = create_with_journal(proposal, token)

    # Re-read old account before the only permitted PATCH.
    old = get_account(old_id, token)

    if old.get("parent_id") != original_parent:
        raise RuntimeError(
            "Old parent changed. CHOW halted."
        )

    if old.get("chow_current_account"):
        if old["chow_current_account"] != new_id:
            raise RuntimeError(
                "Conflicting CHOW link detected."
            )
    else:
        # Preserve all old fields except the CHOW pointer.
        patch_account(
            old_id,
            {"chow_current_account": new_id},
            token,
        )

    verified_old = get_account(old_id, token)
    verified_new = get_account(new_id, token)

    if verified_old.get("parent_id") != original_parent:
        raise RuntimeError(
            "CHOW altered historical parent."
        )

    if verified_old.get("chow_current_account") != new_id:
        raise RuntimeError(
            "CHOW pointer verification failed."
        )

    if verified_new.get("parent_id") != PARENT_ID:
        raise RuntimeError(
            "New CHOW account has incorrect parent."
        )

    record_operation(
        pid, "CHOW_REQUIRED", "COMPLETED",
        new_id=new_id, message="CHOW verified"
    )

    return f"CHOW completed: {old_id} -> {new_id}"


# =====================================================
# MISSING CRM ACCOUNTS
# =====================================================

def approve_missing(proposal, token):
    location = proposal["location"]
    accounts = get_all_accounts(token)

    possible_existing = [
        a for a in accounts
        if same_address(a, location)
    ]

    if possible_existing:
        raise ValueError(
            "CRM already contains account(s) at "
            "this address: "
            + ", ".join(
                a["account_id"] for a in possible_existing
            )
        )

    new_id = create_with_journal(proposal, token)

    record_operation(
        proposal_id(proposal),
        "MISSING_ACCOUNT_REVIEW",
        "COMPLETED", new_id=new_id,
        message="Missing account created"
    )

    return f"New account created: {new_id}"


# =====================================================
# DUPLICATE RESOLUTION
# =====================================================

def approve_duplicate(proposal, survivor_id, token):
    pid = proposal_id(proposal)
    ids = proposal.get("duplicate_account_ids") or []
    location = proposal.get("location") or {}

    if len(ids) < 2 or len(ids) != len(set(ids)):
        raise ValueError("Invalid duplicate candidate group")

    if survivor_id not in ids:
        raise ValueError(
            "Survivor must be a reviewed candidate."
        )

    # Recheck all accounts against the live CRM.
    accounts = {
        aid: get_account(aid, token)
        for aid in ids
    }

    for aid, account in accounts.items():
        if account.get("account_id") != aid:
            raise ValueError(
                f"Account identity mismatch: {aid}"
            )

        if not same_address(account, location):
            raise ValueError(
                f"Address differs. Investigate account {aid}"
            )

        if (
            money(account.get("lifetime_revenue")) > 0
            or money(account.get("outstanding_ar")) > 0
        ):
            raise ValueError(
                f"Financial history present on {aid}. "
                "Automatic duplicate resolution blocked."
            )

        if account.get("chow_current_account"):
            raise ValueError(
                f"CHOW link exists on {aid}."
            )

        existing_link = account.get(
            "duplicate_of_account"
        )

        if existing_link and existing_link != survivor_id:
            raise ValueError(
                f"Conflicting duplicate pointer on {aid}"
            )

    survivor = accounts[survivor_id]

    if survivor.get("duplicate_of_account"):
        raise ValueError(
            "Selected survivor is already a duplicate."
        )

    record_operation(
        pid, "DUPLICATE_REVIEW", "PATCH_STARTED",
        message=f"Survivor: {survivor_id}"
    )

    try:
        survivor_changes = {}

        if survivor.get("name") != location["name"]:
            survivor_changes["name"] = location["name"]

        if survivor.get("parent_id") != PARENT_ID:
            survivor_changes["parent_id"] = PARENT_ID

        if survivor.get("status") != "Active":
            survivor_changes["status"] = "Active"

        if survivor_changes:
            patch_account(
                survivor_id, survivor_changes, token
            )

        for aid in ids:
            if aid == survivor_id:
                continue

            losing = accounts[aid]
            changes = {}

            if losing.get("duplicate_of_account") != survivor_id:
                changes["duplicate_of_account"] = survivor_id

            if losing.get("status") != "Inactive":
                changes["status"] = "Inactive"

            if changes:
                patch_account(aid, changes, token)

        final = {
            aid: get_account(aid, token)
            for aid in ids
        }

        confirmed_survivor = final[survivor_id]

        if not (
            confirmed_survivor.get("name")
            == location["name"]
            and confirmed_survivor.get("parent_id")
            == PARENT_ID
            and confirmed_survivor.get("status")
            == "Active"
        ):
            raise RuntimeError(
                "Survivor verification failed."
            )

        for aid in ids:
            if aid == survivor_id:
                continue

            account = final[aid]

            if not (
                account.get("duplicate_of_account")
                == survivor_id
                and account.get("status") == "Inactive"
            ):
                raise RuntimeError(
                    f"Duplicate verification failed: {aid}"
                )

    except Exception as error:
        record_operation(
            pid, "DUPLICATE_REVIEW",
            "NEEDS_INSPECTION",
            message=str(error)[:400]
        )
        raise

    record_operation(
        pid, "DUPLICATE_REVIEW", "COMPLETED",
        message=(
            "Survivor: " + survivor_id
            + "; duplicates: "
            + ", ".join(
                aid for aid in ids
                if aid != survivor_id
            )
        )
    )

    return (
        "Duplicate resolution completed and verified. "
        f"Survivor: {survivor_id}"
    )


# =====================================================
# STREAMLIT REVIEW APP
# =====================================================

st.title("Bellhaven CRM Review")
st.caption(
    "Website-to-CRM reconciliation with human approval"
)

try:
    proposals = load_json("proposals.json")
    snapshot = load_json("crm_accounts.json")
    locations = load_json("bellhaven_locations.json")
except Exception as error:
    st.error(f"Data loading failed: {error}")
    st.stop()

account_lookup = {
    account["account_id"]: account
    for account in snapshot
}

with st.sidebar:
    st.header("CRM Connection")

    token = st.text_input(
        "Personal API Token",
        type="password"
    ).strip()

    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    if st.button("Test Connection"):
        try:
            api("GET", "/me", token)
            st.success("CRM connected!")
        except Exception as error:
            st.error(str(error))

    st.divider()

    selected_type = st.selectbox(
        "Finding Type",
        ["All"] + sorted({
            p["type"] for p in proposals
        })
    )

    selected_status = st.selectbox(
        "Review Status",
        ["Pending", "Approved", "Rejected", "All"]
    )

st.subheader("Matching Results")

c1, c2, c3, c4 = st.columns(4)

c1.metric("Website Locations", len(locations))
c2.metric("CRM Snapshot", len(snapshot))
c3.metric("Total Findings", len(proposals))
c4.metric(
    "Needs Fix",
    sum(
        p["type"] == "NEEDS_FIX"
        for p in proposals
    )
)

st.caption(
    "Snapshot is historical. Approval operations "
    "read live CRM accounts."
)

st.subheader("Review Queue")

for proposal in proposals:
    if not proposal.get("requires_approval"):
        continue

    kind = proposal["type"]
    pid = proposal_id(proposal)

    decision = decision_for(pid)
    status = decision[0] if decision else "Pending"

    if selected_type not in ("All", kind):
        continue

    if selected_status not in ("All", status):
        continue

    location = proposal.get("location") or {}

    name = (
        location.get("name")
        or proposal.get("account_name")
        or "Unknown Facility"
    )

    with st.expander(
        f"{kind} | {name} | {status}"
    ):
        st.write(
            "**Reason:**",
            proposal.get("reason", "")
        )

        source_url = location.get("source_url")

        if source_url:
            st.markdown(
                f"[Official Website Evidence]({source_url})"
            )

        if location:
            st.write("### Website Information")
            st.json(location)

        account_id = proposal.get("account_id")

        if account_id:
            st.write("### CRM Snapshot")
            st.json(
                account_lookup.get(account_id, {})
            )

        if proposal.get("candidates"):
            st.write("### Matching Candidates")
            st.dataframe(proposal["candidates"])

        st.write("### Proposed Changes")
        st.json(
            proposal.get("proposed_changes") or {}
        )

        if kind == "CHOW_REQUIRED":
            st.warning(
                "CHOW SOP: Create a new Bellhaven account. "
                "Preserve the old parent, revenue, and AR. "
                "Set only old.chow_current_account."
            )
            st.write(
                "**Lifetime revenue:**",
                proposal.get("lifetime_revenue")
            )
            st.write(
                "**Outstanding AR:**",
                proposal.get("outstanding_ar")
            )

        survivor_id = None

        if kind == "DUPLICATE_REVIEW":
            duplicate_ids = proposal.get(
                "duplicate_account_ids"
            ) or []

            st.warning(
                "Select ONE survivor. Other accounts "
                "will be marked Inactive and linked "
                "through duplicate_of_account. "
                "No records are deleted."
            )

            st.write("### Duplicate Account Details")

            for aid in duplicate_ids:
                account = account_lookup.get(aid, {})
                st.json({
                    "account_id": aid,
                    "name": account.get("name"),
                    "parent": account.get("parent_name"),
                    "address": account.get("billing_street"),
                    "city": account.get("billing_city"),
                    "state": account.get("billing_state"),
                    "zip": account.get("billing_zip"),
                    "status": account.get("status"),
                    "lifetime_revenue": account.get(
                        "lifetime_revenue"
                    ),
                    "outstanding_ar": account.get(
                        "outstanding_ar"
                    ),
                })

            survivor_id = st.selectbox(
                "Surviving Account",
                [""] + duplicate_ids,
                format_func=lambda aid: (
                    "Select a survivor..."
                    if not aid else (
                        aid + " | "
                        + account_lookup.get(aid, {}).get(
                            "name", ""
                        )
                    )
                ),
                key=f"survivor_{pid}"
            )

        operation = operation_for(pid)

        if operation:
            state, new_id, message = operation
            st.write("**Operation State:**", state)

            if new_id:
                st.write("**New Account ID:**", new_id)

            if message:
                st.caption(message)

            if state == "NEEDS_INSPECTION":
                st.error(
                    "Previous operation requires manual "
                    "inspection before another attempt."
                )

        if decision:
            st.success(
                f"Already reviewed: {decision[0]}"
            )
            st.write(decision[1])
            continue

        reviewer_note = st.text_input(
            "Reviewer Notes",
            key=f"note_{pid}"
        )

        confirmed = st.checkbox(
            "I verified the website evidence and CRM data.",
            key=f"confirm_{pid}"
        )

        supported = kind in {
            "NEEDS_FIX",
            "CHOW_REQUIRED",
            "MISSING_ACCOUNT_REVIEW",
            "DUPLICATE_REVIEW",
        }

        blocked = (
            operation is not None
            and operation[0] == "NEEDS_INSPECTION"
        )

        approve_col, reject_col = st.columns(2)

        with approve_col:
            if st.button(
                "Approve & Apply",
                key=f"approve_{pid}",
                disabled=not (
                    supported
                    and bool(token)
                    and confirmed
                    and not blocked
                    and (
                        kind != "DUPLICATE_REVIEW"
                        or bool(survivor_id)
                    )
                )
            ):
                try:
                    if kind == "NEEDS_FIX":
                        result = approve_fix(
                            proposal, token
                        )

                    elif kind == "CHOW_REQUIRED":
                        result = approve_chow(
                            proposal, token
                        )

                    elif kind == "MISSING_ACCOUNT_REVIEW":
                        result = approve_missing(
                            proposal, token
                        )

                    elif kind == "DUPLICATE_REVIEW":
                        result = approve_duplicate(
                            proposal, survivor_id, token
                        )

                    else:
                        raise ValueError(
                            "Unsupported proposal type."
                        )

                    save_decision(
                        pid,
                        "Approved",
                        (
                            reviewer_note + " | " + result
                        ).strip(" |")
                    )

                    st.success(result)
                    st.rerun()

                except Exception as error:
                    st.error(str(error))
                    st.warning(
                        "Inspect the live CRM before retrying."
                    )

        with reject_col:
            if st.button(
                "Reject",
                key=f"reject_{pid}",
                disabled=not confirmed
            ):
                save_decision(
                    pid,
                    "Rejected",
                    reviewer_note
                    or "Rejected after manual review"
                )
                st.rerun()


# =====================================================
# AUDIT HISTORY
# =====================================================

st.divider()
st.subheader("Review History")

with db() as conn:
    history = conn.execute(
        "SELECT proposal_id, decision, details, decided_at "
        "FROM decisions ORDER BY decided_at DESC"
    ).fetchall()

if history:
    st.dataframe([
        {
            "Proposal ID": row[0],
            "Decision": row[1],
            "Details": row[2],
            "Date": row[3],
        }
        for row in history
    ])
else:
    st.info("No review decisions recorded yet.")
