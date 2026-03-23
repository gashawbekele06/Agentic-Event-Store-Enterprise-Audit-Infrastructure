"""
Seed the Week 5 event store from starter project data files.

Reads:
  data/applicant_profiles.json  — 80 company profiles (risk, compliance flags)
  data/seed_events.jsonl        — pre-generated loan application events

Writes events into PostgreSQL via the event store.

Usage:
    uv run python scripts/seed_from_starter.py
    uv run python scripts/seed_from_starter.py --limit 10   # first 10 applications only
    uv run python scripts/seed_from_starter.py --dry-run    # print without writing
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
PROFILES_FILE = ROOT / "data" / "applicant_profiles.json"
EVENTS_FILE   = ROOT / "data" / "seed_events.jsonl"

sys.path.insert(0, str(ROOT))

import asyncpg


# ── Load reference data ───────────────────────────────────────────────────────

def load_profiles() -> dict[str, dict]:
    """Return {company_id: profile_dict} from applicant_profiles.json"""
    data = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
    return {p["company_id"]: p for p in data}


def load_seed_events() -> list[dict]:
    """Return all events from seed_events.jsonl as raw dicts."""
    events = []
    for line in EVENTS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def group_by_stream(events: list[dict]) -> dict[str, list[dict]]:
    """Group events by stream_id, preserving order."""
    streams: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        streams[e["stream_id"]].append(e)
    return dict(streams)


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to a timezone-aware datetime, or return None."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    # Handle date-only strings like "2025-08-27"
    if len(value) == 10:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ── Direct DB writer (handles unknown event types from starter) ───────────────

async def write_stream(
    conn: asyncpg.Connection,
    stream_id: str,
    events: list[dict],
    dry_run: bool,
) -> int:
    """
    Write a list of raw event dicts to one stream.
    Uses direct SQL instead of the ORM so that starter event types
    (DocumentUploaded, PackageCreated, etc.) are accepted without
    needing Pydantic models for each.

    Returns number of events written.
    """
    aggregate_type = stream_id.split("-")[0]

    if dry_run:
        print(f"  [DRY] stream={stream_id:<30s}  {len(events)} events")
        return len(events)

    async with conn.transaction():
        # Upsert stream row
        await conn.execute(
            """
            INSERT INTO event_streams (stream_id, aggregate_type, current_version)
            VALUES ($1, $2, 0)
            ON CONFLICT (stream_id) DO NOTHING
            """,
            stream_id,
            aggregate_type,
        )

        # Check current version (skip if already seeded)
        current_v = await conn.fetchval(
            "SELECT current_version FROM event_streams WHERE stream_id = $1",
            stream_id,
        )
        if current_v and current_v > 0:
            print(f"  [SKIP] {stream_id} already has {current_v} events")
            return 0

        corr_id = str(uuid.uuid4())
        written = 0
        for i, ev in enumerate(events, start=1):
            await conn.execute(
                """
                INSERT INTO events (
                    event_id, stream_id, event_type, event_version,
                    payload, recorded_at, stream_position, metadata
                ) VALUES ($1,$2,$3,$4,$5,
                    COALESCE($6::timestamptz, now()),
                    $7, $8::jsonb)
                ON CONFLICT ON CONSTRAINT uq_stream_position DO NOTHING
                """,
                str(uuid.uuid4()),
                stream_id,
                ev["event_type"],
                ev.get("event_version", 1),
                json.dumps(ev["payload"]),
                _parse_dt(ev.get("recorded_at")),
                i,
                json.dumps({"correlation_id": corr_id, "causation_id": None}),
            )
            written += 1

        # Update stream version
        await conn.execute(
            "UPDATE event_streams SET current_version = $1 WHERE stream_id = $2",
            len(events),
            stream_id,
        )

    return written


# ── Compliance flag events ────────────────────────────────────────────────────

def build_compliance_flag_events(
    application_id: str,
    applicant_id: str,
    profile: dict,
) -> list[dict]:
    """
    For each active compliance flag in the applicant profile,
    emit a ComplianceRuleFailed event into the loan stream.
    This bridges the profile data into the audit event store.
    """
    flag_events = []
    for flag in profile.get("compliance_flags", []):
        if not flag.get("is_active", False):
            continue
        flag_events.append({
            "stream_id": f"loan-{application_id}",
            "event_type": "ComplianceRuleFailed",
            "event_version": 1,
            "payload": {
                "application_id": application_id,
                "rule_id": flag["flag_type"],
                "rule_name": flag["flag_type"].replace("_", " ").title(),
                "regulation": "BSA/AML",
                "severity": flag["severity"],
                "failure_reason": flag.get("note", ""),
                "evaluated_by": "compliance-screening-agent",
                "evaluation_timestamp": flag.get("added_date"),
                "source": "applicant_profile_flag",
                "applicant_id": applicant_id,
            },
            "recorded_at": flag.get("added_date"),
        })
    return flag_events


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(limit: int | None, dry_run: bool) -> None:
    dsn = os.getenv(
        "DATABASE_URL",
        "postgresql://postgres:13621@localhost:5433/apex_ledger",
    )

    profiles = load_profiles()
    raw_events = load_seed_events()
    streams = group_by_stream(raw_events)

    # Find all loan application stream IDs (APEX-XXXX)
    loan_streams = sorted(
        k for k in streams if k.startswith("loan-APEX-")
    )
    if limit:
        loan_streams = loan_streams[:limit]

    print(f"Profiles loaded : {len(profiles)}")
    print(f"Total streams   : {len(streams)}")
    print(f"Loan streams    : {len(loan_streams)} (loading {len(loan_streams)})")
    print(f"Dry run         : {dry_run}\n")

    if dry_run:
        for sid in loan_streams:
            evs = streams[sid]
            print(f"  loan stream   : {sid}  ({len(evs)} events)")
            app_id   = sid.replace("loan-", "")
            docpkg   = f"docpkg-{app_id}"
            if docpkg in streams:
                print(f"  docpkg stream : {docpkg} ({len(streams[docpkg])} events)")
            # Show compliance flags for this applicant
            first = evs[0]["payload"]
            comp_id = first.get("applicant_id", "")
            prof = profiles.get(comp_id, {})
            active = [f for f in prof.get("compliance_flags", []) if f.get("is_active")]
            if active:
                print(f"  *** compliance flags: {[f['flag_type'] for f in active]}")
        return

    conn = await asyncpg.connect(dsn)
    total_events = 0
    total_streams = 0

    try:
        for sid in loan_streams:
            loan_evs = streams[sid]
            app_id   = sid.replace("loan-", "")
            docpkg   = f"docpkg-{app_id}"

            # Find applicant_id from the ApplicationSubmitted event
            comp_id = ""
            for ev in loan_evs:
                if ev["event_type"] == "ApplicationSubmitted":
                    comp_id = ev["payload"].get("applicant_id", "")
                    break

            profile = profiles.get(comp_id, {})

            # Append compliance flag events to the loan stream
            flag_evs = build_compliance_flag_events(app_id, comp_id, profile)
            all_loan_evs = loan_evs + flag_evs

            # Write loan stream
            n = await write_stream(conn, sid, all_loan_evs, dry_run=False)
            total_events += n
            if n > 0:
                total_streams += 1
                name = profile.get("name", comp_id)
                risk = profile.get("risk_segment", "?")
                traj = profile.get("trajectory", "?")
                flags_note = f"  [{len(flag_evs)} compliance flags]" if flag_evs else ""
                print(f"  [OK] {sid:<20s}  {comp_id}  {name[:35]:<35s}  {risk:<6s}  {traj:<12s}{flags_note}")

            # Write docpkg stream
            if docpkg in streams:
                n2 = await write_stream(conn, docpkg, streams[docpkg], dry_run=False)
                total_events += n2

    finally:
        await conn.close()

    print(f"\nDone. {total_streams} streams, {total_events} events written to store.")
    print("\nVerify with:")
    print("  uv run python scripts/query_seeded_data.py")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed event store from starter data")
    parser.add_argument("--limit", type=int, default=None, help="Only load first N applications")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be loaded, don't write")
    args = parser.parse_args()
    asyncio.run(main(limit=args.limit, dry_run=args.dry_run))
