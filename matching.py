
"""Bellhaven website/CRM reconciliation.

Read-only matching and proposal generation.
Run after fetch_crm.py and scraper.py.
CRM changes require approval in app.py.
"""

import hashlib
import json
import re
import shutil
import sqlite3
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

DATA = Path("data")
PARENT = "0015QAPLGS3FVYEEEM"

ABBR = {
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


def load(name):
    with (DATA / name).open(encoding="utf-8") as f:
        return json.load(f)


def norm(value):
    return re.sub(
        r"[^a-z0-9]", "", str(value or "").lower()
    )


def street_norm(value):
    words = re.findall(
        r"[a-z0-9]+", str(value or "").lower()
    )
    return "".join(
        ABBR.get(word, word) for word in words
    )


def zip5(value):
    return re.sub(
        r"\D", "", str(value or "")
    )[:5]


def money(value):
    return float(value or 0)


def sim(a, b):
    return SequenceMatcher(
        None, norm(a), norm(b)
    ).ratio()


def site_key(location):
    return (
        location.get("source_url")
        or norm(location["name"]) + "|" + zip5(location["zip"])
    )


def geo_equal(account, location):
    return (
        norm(account.get("billing_city"))
        == norm(location["city"])
        and norm(account.get("billing_state"))
        == norm(location["state"])
    )


def full_address_equal(account, location):
    return (
        geo_equal(account, location)
        and street_norm(account.get("billing_street"))
        == street_norm(location["address"])
        and zip5(account.get("billing_zip"))
        == zip5(location["zip"])
    )


def candidate(account, location):
    name = sim(account.get("name"), location["name"])

    street = SequenceMatcher(
        None,
        street_norm(account.get("billing_street")),
        street_norm(location["address"]),
    ).ratio()

    city = sim(
        account.get("billing_city"),
        location["city"],
    )

    state = (
        norm(account.get("billing_state"))
        == norm(location["state"])
    )

    zipcode = (
        zip5(account.get("billing_zip"))
        == zip5(location["zip"])
    )

    score = (
        0.35 * name
        + 0.35 * street
        + 0.12 * city
        + 0.08 * state
        + 0.10 * zipcode
    )

    return {
        "account_id": account["account_id"],
        "name": account.get("name"),
        "parent_id": account.get("parent_id"),
        "score": round(score, 4),
        "name_similarity": round(name, 4),
        "street_similarity": round(street, 4),
        "zip_match": bool(zipcode),
    }


def proposal_key(proposal):
    value = json.dumps(
        {
            "type": proposal["type"],
            "location_url": (
                proposal.get("location") or {}
            ).get("source_url"),
            "account_id": proposal.get("account_id"),
            "candidate_ids": sorted(
                item["account_id"]
                for item in proposal.get("candidates", [])
            ),
            "proposed_changes": proposal.get(
                "proposed_changes", {}
            ),
        },
        sort_keys=True,
    )

    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()[:24]


def decided_ids():
    database = DATA / "review_decisions.db"

    if not database.exists():
        return set()

    with sqlite3.connect(database) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT proposal_id FROM decisions"
            )
        }


def main():
    locations = load("bellhaven_locations.json")
    accounts = load("crm_accounts.json")

    if not any(
        account["account_id"] == PARENT
        for account in accounts
    ):
        raise ValueError(
            "Bellhaven parent missing from CRM snapshot"
        )

    children = [
        account
        for account in accounts
        if (
            account["account_id"] != PARENT
            and account.get("status") != "Inactive"
            and not account.get("duplicate_of_account")
            and not account.get("chow_current_account")
        )
    ]

    proposals = []
    matched = set()
    claimed = defaultdict(set)

    for location in locations:
        ranked = sorted(
            (
                candidate(account, location)
                for account in children
            ),
            key=lambda item: item["score"],
            reverse=True,
        )

        exact = [
            account
            for account in children
            if full_address_equal(account, location)
        ]

        exact.sort(
            key=lambda account: (
                norm(account.get("name"))
                != norm(location["name"]),
                account["account_id"],
            )
        )

        same_city_name = [
            account
            for account in children
            if (
                geo_equal(account, location)
                and norm(account.get("name"))
                == norm(location["name"])
            )
        ]

        postal = [
            account
            for account in same_city_name
            if re.search(
                r"\bp\s*o\s*box\b",
                str(account.get("billing_street") or ""),
                re.I,
            )
        ]

        proposal = {
            "location": location,
            "requires_approval": True,
            "candidates": ranked[:5],
            "proposed_changes": {},
        }

        if len(exact) > 1:
            ids = [
                account["account_id"]
                for account in exact
            ]

            matched.update(ids)

            for account_id in ids:
                claimed[account_id].add(
                    site_key(location)
                )

            proposal.update(
                type="DUPLICATE_REVIEW",
                reason=(
                    "Multiple active CRM records have the "
                    "same physical location. Select a "
                    "surviving account and mark other "
                    "records as duplicates."
                ),
                duplicate_account_ids=ids,
                account_id=ids[0],
                target_parent_id=PARENT,
                proposed_changes={
                    "survivor": {
                        "name": location["name"],
                        "parent_id": PARENT,
                    },
                    "duplicates": (
                        "Set duplicate_of_account to "
                        "survivor and status to Inactive"
                    ),
                },
            )

        elif len(exact) == 1:
            account = exact[0]
            account_id = account["account_id"]

            matched.add(account_id)
            claimed[account_id].add(
                site_key(location)
            )

            changes = {}

            if norm(account.get("name")) != norm(
                location["name"]
            ):
                changes["name"] = location["name"]

            if norm(account.get("billing_city")) != norm(
                location["city"]
            ):
                changes["billing_city"] = location["city"]

            if norm(account.get("billing_state")) != norm(
                location["state"]
            ):
                changes["billing_state"] = location["state"]

            if zip5(account.get("billing_zip")) != zip5(
                location["zip"]
            ):
                changes["billing_zip"] = location["zip"]

            parent_diff = (
                account.get("parent_id") != PARENT
            )

            chow_required = (
                parent_diff
                and money(account.get("lifetime_revenue")) > 0
                and money(account.get("outstanding_ar")) > 0
            )

            if chow_required:
                changes = {}

                if account.get("chow_current_account"):
                    kind = "CHOW_ALREADY_LINKED_REVIEW"
                else:
                    kind = "CHOW_REQUIRED"

            else:
                if parent_diff:
                    changes["parent_id"] = PARENT

                kind = (
                    "NEEDS_FIX"
                    if changes
                    else "CONFIDENT_MATCH"
                )

            proposal.update(
                type=kind,
                account_id=account_id,
                account_name=account.get("name"),
                current_parent_id=account.get("parent_id"),
                target_parent_id=PARENT,
                lifetime_revenue=money(
                    account.get("lifetime_revenue")
                ),
                outstanding_ar=money(
                    account.get("outstanding_ar")
                ),
                reason=(
                    "Exact normalized physical address "
                    "and ZIP match"
                ),
                proposed_changes=changes,
                requires_approval=(
                    kind != "CONFIDENT_MATCH"
                ),
            )

        elif postal:
            account = postal[0]
            account_id = account["account_id"]

            matched.add(account_id)
            claimed[account_id].add(
                site_key(location)
            )

            proposal.update(
                type="CONFIDENT_MATCH",
                account_id=account_id,
                account_name=account.get("name"),
                reason=(
                    "Same facility name and geography. "
                    "CRM has a PO Box billing address, "
                    "which must not be overwritten."
                ),
                requires_approval=False,
            )

        else:
            zip_errors = [
                account
                for account in children
                if (
                    geo_equal(account, location)
                    and norm(account.get("name"))
                    == norm(location["name"])
                    and street_norm(
                        account.get("billing_street")
                    )
                    == street_norm(location["address"])
                )
            ]

            if len(zip_errors) == 1:
                account = zip_errors[0]
                account_id = account["account_id"]

                matched.add(account_id)
                claimed[account_id].add(
                    site_key(location)
                )

                changes = {
                    "billing_zip": location["zip"]
                }

                if account.get("parent_id") != PARENT:
                    if (
                        money(
                            account.get("lifetime_revenue")
                        ) > 0
                        and money(
                            account.get("outstanding_ar")
                        ) > 0
                    ):
                        kind = "CHOW_REQUIRED"
                        changes = {}
                    else:
                        kind = "NEEDS_FIX"
                        changes["parent_id"] = PARENT
                else:
                    kind = "NEEDS_FIX"

                proposal.update(
                    type=kind,
                    account_id=account_id,
                    account_name=account.get("name"),
                    current_parent_id=account.get("parent_id"),
                    reason=(
                        "Verified name/address match; "
                        "ZIP differs"
                    ),
                    proposed_changes=changes,
                    lifetime_revenue=money(
                        account.get("lifetime_revenue")
                    ),
                    outstanding_ar=money(
                        account.get("outstanding_ar")
                    ),
                )

            else:
                near = [
                    account
                    for account in children
                    if (
                        geo_equal(account, location)
                        and (
                            norm(account.get("name"))
                            == norm(location["name"])
                            or (
                                zip5(account.get("billing_zip"))
                                == zip5(location["zip"])
                                and SequenceMatcher(
                                    None,
                                    street_norm(
                                        account.get("billing_street")
                                    ),
                                    street_norm(
                                        location["address"]
                                    ),
                                ).ratio() >= 0.75
                            )
                        )
                    )
                ]

                if len(near) == 1:
                    account = near[0]
                    account_id = account["account_id"]

                    matched.add(account_id)
                    claimed[account_id].add(
                        site_key(location)
                    )

                    proposal.update(
                        type="MANUAL_REVIEW",
                        account_id=account_id,
                        account_name=account.get("name"),
                        reason=(
                            "Possible same facility but "
                            "address/name differs; verify."
                        ),
                    )

                else:
                    proposal.update(
                        type=(
                            "MISSING_ACCOUNT_REVIEW"
                            if not near
                            else "MANUAL_REVIEW"
                        ),
                        reason=(
                            "No unique verified account "
                            "at website address"
                        ),
                    )

                    for account in near:
                        matched.add(
                            account["account_id"]
                        )

        proposals.append(proposal)

    # CRM accounts with no confirmed website match.
    for account in accounts:
        account_id = account["account_id"]

        if (
            account.get("parent_id") == PARENT
            and account_id not in matched
            and account_id != PARENT
            and not account.get("duplicate_of_account")
            and account.get("status") != "Inactive"
            and not account.get("chow_current_account")
        ):
            proposals.append(
                {
                    "type": "CRM_ONLY_REVIEW",
                    "account_id": account_id,
                    "account_name": account.get("name"),
                    "current_parent_id": PARENT,
                    "lifetime_revenue": money(
                        account.get("lifetime_revenue")
                    ),
                    "outstanding_ar": money(
                        account.get("outstanding_ar")
                    ),
                    "reason": (
                        "No confirmed website match. "
                        "Absence alone is insufficient "
                        "evidence to inactivate or reparent."
                    ),
                    "proposed_changes": {},
                    "requires_approval": True,
                }
            )

    for proposal in proposals:
        proposal["proposal_id"] = proposal_key(
            proposal
        )


    # IDEMPOTENCE:
    # Store durable signatures of decided proposals.
    # A decision suppresses the SAME issue, not every
    # future issue at the same website location.

    previous = DATA / "proposals.json"
    done = decided_ids()

    def issue_signature(proposal):
        location = proposal.get("location") or {}

        identity = {
            "type": proposal.get("type"),
            "location": (
                site_key(location) if location else None
            ),
            "account_id": proposal.get("account_id"),
            "duplicate_ids": sorted(
                proposal.get("duplicate_account_ids") or []
            ),
            "changes": (
                proposal.get("proposed_changes") or {}
            ),
        }

        encoded = json.dumps(
            identity,
            sort_keys=True,
            ensure_ascii=False,
        )

        return hashlib.sha256(
            encoded.encode("utf-8")
        ).hexdigest()

    registry = DATA / "review_decisions.db"
    resolved = set()

    with sqlite3.connect(registry) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS resolved_signatures (
                signature TEXT PRIMARY KEY,
                original_proposal_id TEXT NOT NULL,
                recorded_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Recover decisions from prior proposal snapshots.
        # Historical decisions remain in the existing DB.
        for source in sorted(DATA.glob("proposals*.json")):
            try:
                with source.open(
                    encoding="utf-8"
                ) as file:
                    historical = json.load(file)
            except (OSError, ValueError):
                continue

            if not isinstance(historical, list):
                continue

            for old in historical:
                if not isinstance(old, dict):
                    continue

                old_id = old.get("proposal_id")

                if old_id not in done:
                    continue

                signature = issue_signature(old)

                conn.execute(
                    """
                    INSERT OR IGNORE INTO resolved_signatures
                    (signature, original_proposal_id)
                    VALUES (?, ?)
                    """,
                    (signature, old_id),
                )

        conn.commit()

        resolved = {
            row[0]
            for row in conn.execute(
                "SELECT signature FROM resolved_signatures"
            )
        }

    before_count = len(proposals)

    proposals = [
        proposal
        for proposal in proposals
        if not (
            proposal.get("requires_approval")
            and (
                proposal["proposal_id"] in done
                or issue_signature(proposal) in resolved
            )
        )
    ]

    suppressed = before_count - len(proposals)

    print(
        "Previously decided identical proposals "
        "suppressed:", suppressed
    )
    print(
        "Historical decision signatures:",
        len(resolved)
    )

    DATA.mkdir(exist_ok=True)

    if previous.exists():
        shutil.copy2(
            previous,
            DATA / "proposals_before_rematch.json",
        )

    with previous.open(
        "w", encoding="utf-8"
    ) as f:
        json.dump(
            proposals,
            f,
            indent=2,
            ensure_ascii=False,
        )

    print("\nMATCHING COMPLETE (read-only)")
    print("Website locations:", len(locations))
    print("CRM accounts:", len(accounts))
    print(
        "Findings:",
        dict(Counter(
            p["type"] for p in proposals
        )),
    )
    print("CRM writes: NONE")
    print(
        "REMINDER: Keep Tiffin CHOW pending for demo."
    )


if __name__ == "__main__":
    main()
