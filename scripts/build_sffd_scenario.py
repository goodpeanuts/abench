#!/usr/bin/env python3
"""Build a privacy-preserving, deterministic SFFD scenario manifest.

The source CSV is a response log (one row per unit/call), not a simulator.
This script therefore keeps observed event timing and coarse geography, while
making the roster and calibration provenance explicit.  It never copies an
address or coordinate into the generated scenario.
"""

from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
import math
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


SCHEMA_VERSION = "sffd-ems-scenario.v1"
DEFAULT_T0 = "2025-03-11T10:00:00"
DEFAULT_SEED = 20250311
DEFAULT_ARRIVAL_WINDOW = 20 * 60
DEFAULT_HORIZON = 120 * 60
EMS_TYPES = ("MEDIC", "BLS", "PRIVATE")
UNIT_QUOTAS = {"MEDIC": 8, "BLS": 2, "PRIVATE": 2}
REQUIRED_FIELDS = {
    "call_number",
    "unit_id",
    "incident_number",
    "call_type",
    "received_dttm",
    "dispatch_dttm",
    "response_dttm",
    "on_scene_dttm",
    "available_dttm",
    "call_final_disposition",
    "battalion",
    "station_area",
    "priority",
    "original_priority",
    "final_priority",
    "als_unit",
    "unit_type",
    "rowid",
}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path, help="frozen SFFD CSV")
    parser.add_argument("--output", required=True, type=Path, help="scenario.json")
    parser.add_argument("--t0", default=DEFAULT_T0)
    parser.add_argument("--arrival-window-seconds", type=int, default=DEFAULT_ARRIVAL_WINDOW)
    parser.add_argument("--horizon-seconds", type=int, default=DEFAULT_HORIZON)
    parser.add_argument(
        "--expected-dispatchable-incidents",
        type=int,
        default=12,
        help="require an exact candidate count; use 0 to accept any positive count",
    )
    parser.add_argument("--roster-days", type=int, default=7)
    parser.add_argument("--roster-size", type=int, default=12)
    parser.add_argument(
        "--min-calibration-observations",
        type=int,
        default=5,
        help="minimum aggregate observations required for each calibrated phase",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--generated-at",
        default=None,
        help=(
            "provenance timestamp to store in scenario metadata; when omitted "
            "the deterministic maximum data_as_of value is used"
        ),
    )
    parser.add_argument(
        "--allow-short-roster-history",
        action="store_true",
        help="development-only: permit a source slice shorter than the roster window",
    )
    parser.add_argument(
        "--scenario-id",
        default="sffd-rolling-surge-01",
    )
    args = parser.parse_args(argv)
    try:
        args.t0_dt = parse_dt(args.t0)
    except ValueError as exc:
        parser.error(str(exc))
    if args.generated_at is not None:
        try:
            args.generated_at_dt = parse_dt(args.generated_at)
        except ValueError as exc:
            parser.error(str(exc))
    else:
        args.generated_at_dt = None
    if args.arrival_window_seconds <= 0 or args.horizon_seconds <= 0:
        parser.error("arrival and horizon durations must be positive")
    if args.horizon_seconds < args.arrival_window_seconds:
        parser.error("horizon must be at least the arrival window")
    if args.roster_days <= 0 or args.roster_size <= 0:
        parser.error("roster-days and roster-size must be positive")
    if args.expected_dispatchable_incidents < 0:
        parser.error("expected-dispatchable-incidents cannot be negative")
    if args.min_calibration_observations <= 0:
        parser.error("min-calibration-observations must be positive")
    return args


def parse_dt(value: str | None) -> datetime | None:
    if value is None or not str(value).strip():
        return None
    candidate = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(candidate)
    # The SFFD export is a local-looking timestamp.  Strip an accidental
    # offset only after parsing so comparisons remain deterministic.
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def iso_z(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt is not None else None


def seconds_since(t0: datetime, value: str | None) -> int | None:
    parsed = parse_dt(value)
    if parsed is None:
        return None
    return int(round((parsed - t0).total_seconds()))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("source CSV has no header")
        fields = list(reader.fieldnames)
        missing = sorted(REQUIRED_FIELDS - set(fields))
        if missing:
            raise ValueError("source CSV is missing fields: " + ", ".join(missing))
        rows: list[dict[str, str]] = []
        seen_rowids: set[str] = set()
        for line_number, row in enumerate(reader, start=2):
            if None in row:
                raise ValueError(f"source CSV row {line_number} has too many fields")
            if any(value is None for value in row.values()):
                # Empty cells are represented by empty strings by DictReader;
                # None indicates a malformed short row.
                raise ValueError(f"source CSV row {line_number} is malformed")
            rowid = row.get("rowid", "").strip()
            if not rowid:
                raise ValueError(f"source CSV row {line_number} has empty rowid")
            if rowid in seen_rowids:
                raise ValueError(f"duplicate rowid in source CSV: {rowid}")
            seen_rowids.add(rowid)
            # Validate every non-empty timestamp that we use later.
            for field in (
                "received_dttm",
                "dispatch_dttm",
                "response_dttm",
                "on_scene_dttm",
                "available_dttm",
            ):
                if row.get(field, "").strip():
                    parse_dt(row[field])
            rows.append(row)
    if not rows:
        raise ValueError("source CSV contains no rows")
    return rows, fields


def data_quality(rows: Sequence[dict[str, str]], fields: Sequence[str]) -> dict[str, Any]:
    """Return deterministic schema/order/dedup/missingness evidence."""

    received = [parse_dt(row.get("received_dttm")) for row in rows if row.get("received_dttm")]
    incident_counts: collections.Counter[str] = collections.Counter(
        row.get("incident_number", "").strip() for row in rows if row.get("incident_number", "").strip()
    )
    missing_rates = {
        field: round(sum(not (row.get(field) or "").strip() for row in rows) / max(1, len(rows)), 6)
        for field in fields
    }
    return {
        "schema_fields": len(fields),
        "row_count": len(rows),
        "rowid_unique": len({row.get("rowid", "").strip() for row in rows}) == len(rows),
        "received_time_monotonic": received == sorted(received),
        "unique_incident_numbers": len(incident_counts),
        "multi_row_incident_numbers": sum(count > 1 for count in incident_counts.values()),
        "missing_rates": missing_rates,
    }


def stable_rank(value: str, seed: int) -> str:
    return hashlib.sha256(f"{seed}:{value}".encode("utf-8")).hexdigest()


def zone_for(row: dict[str, str]) -> tuple[str, str]:
    battalion = (row.get("battalion") or "").strip()
    if battalion:
        return f"B:{battalion}", "battalion"
    station = (row.get("station_area") or "").strip()
    if station:
        return f"S:{station}", "station_area"
    return "UNKNOWN", "unknown"


def normalise_priority(row: dict[str, str]) -> tuple[int | None, str]:
    raw = (row.get("priority") or row.get("original_priority") or "").strip().upper()
    # Numeric 2025 values are the primary source.  Letter codes are retained
    # for older rows and are mapped only for calibration, never hidden as raw.
    mapping = {"1": 1, "2": 2, "3": 3, "A": 1, "E": 1, "I": 1, "B": 2, "C": 2, "T": 2}
    return mapping.get(raw), raw


def bool_value(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"true", "1", "yes", "y", "t"}


def is_ems(row: dict[str, str]) -> bool:
    return (row.get("unit_type") or "").strip().upper() in EMS_TYPES


def disposition_invalid(value: str | None) -> bool:
    value = (value or "").strip().lower()
    return value in {"cancelled", "unable to locate"}


def percentile(values: Sequence[float], q: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * q
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def summary(values: Sequence[float]) -> dict[str, int | float]:
    if not values:
        raise ValueError("cannot summarize an empty sample")
    return {
        "count": len(values),
        "min_seconds": int(round(min(values))),
        "p25_seconds": int(round(percentile(values, 0.25))),
        "median_seconds": int(round(statistics.median(values))),
        "p75_seconds": int(round(percentile(values, 0.75))),
        "max_seconds": int(round(max(values))),
    }


def mode_value(values: Iterable[str], default: str = "UNKNOWN") -> str:
    values = [v for v in values if v and v != "UNKNOWN"]
    if not values:
        return default
    counts = collections.Counter(values)
    return sorted(counts, key=lambda value: (-counts[value], value))[0]


def infer_status(
    history: Sequence[dict[str, str]], t0: datetime
) -> tuple[str, int | None, dict[str, Any]]:
    intervals: list[tuple[datetime, datetime, dict[str, str]]] = []
    for row in history:
        dispatch = parse_dt(row.get("dispatch_dttm"))
        available = parse_dt(row.get("available_dttm"))
        if dispatch is None or available is None or available < dispatch:
            continue
        intervals.append((dispatch, available, row))
    crossing = [item for item in intervals if item[0] <= t0 < item[1]]
    if crossing:
        latest = max(crossing, key=lambda item: item[1])
        return (
            "BUSY",
            int(round((latest[1] - t0).total_seconds())),
            {"basis_rowid": latest[2].get("rowid"), "available_dttm": iso_z(latest[1])},
        )
    completed = [item for item in intervals if item[1] <= t0]
    if completed:
        latest = max(completed, key=lambda item: item[1])
        return "FREE", None, {"basis_rowid": latest[2].get("rowid"), "available_dttm": iso_z(latest[1])}
    return "UNKNOWN", None, {}


def choose_roster(
    rows: Sequence[dict[str, str]],
    t0: datetime,
    roster_days: int,
    roster_size: int,
    seed: int,
    allow_short: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    history_start = t0 - timedelta(days=roster_days)
    source_min = min(parse_dt(row["received_dttm"]) for row in rows if row.get("received_dttm"))
    history_complete = source_min <= history_start
    if not history_complete and not allow_short:
        raise ValueError(
            "source CSV does not cover the full roster history: "
            f"needed <= {iso_z(history_start)}, earliest row is {iso_z(source_min)}; "
            "refetch a wider slice or pass --allow-short-roster-history for development"
        )
    by_unit: dict[str, list[dict[str, str]]] = collections.defaultdict(list)
    for row in rows:
        received = parse_dt(row.get("received_dttm"))
        if received is None or not (history_start <= received < t0) or not is_ems(row):
            continue
        by_unit[row["unit_id"].strip()].append(row)

    candidates: list[dict[str, Any]] = []
    for unit_id, history in by_unit.items():
        status, busy_until, status_basis = infer_status(history, t0)
        if status == "UNKNOWN":
            continue
        unit_type = mode_value((r.get("unit_type", "").strip().upper() for r in history), "MEDIC")
        als_votes = sum(bool_value(r.get("als_unit")) for r in history)
        als = als_votes * 2 >= len(history)
        zones = [zone_for(r)[0] for r in history]
        candidates.append(
            {
                "unit_id": unit_id,
                "unit_type": unit_type,
                "als": als,
                "initial_zone": mode_value(zones),
                "initial_status": status,
                **({"busy_until_s": busy_until} if busy_until is not None else {}),
                "_rank": stable_rank(unit_id, seed),
                "_history_rows": len(history),
                "_status_basis": status_basis,
            }
        )
    selected: list[dict[str, Any]] = []
    used: set[str] = set()
    for unit_type, quota in UNIT_QUOTAS.items():
        for candidate in sorted(
            (item for item in candidates if item["unit_type"] == unit_type),
            key=lambda item: item["_rank"],
        )[:quota]:
            if len(selected) >= roster_size:
                break
            selected.append(candidate)
            used.add(candidate["unit_id"])
    if len(selected) < roster_size:
        for candidate in sorted(candidates, key=lambda item: item["_rank"]):
            if candidate["unit_id"] in used:
                continue
            selected.append(candidate)
            used.add(candidate["unit_id"])
            if len(selected) >= roster_size:
                break
    if len(selected) < roster_size:
        raise ValueError(
            f"only {len(selected)} eligible EMS units were inferred; need {roster_size}"
        )
    public_units: list[dict[str, Any]] = []
    for item in sorted(selected, key=lambda value: value["unit_id"]):
        public = {
            "unit_id": item["unit_id"],
            "unit_type": item["unit_type"],
            "als": item["als"],
            "initial_zone": item["initial_zone"],
            "initial_status": item["initial_status"],
        }
        if "busy_until_s" in item:
            public["busy_until_s"] = item["busy_until_s"]
        public_units.append(public)
    roster_meta = {
        "history_start": iso_z(history_start),
        "history_end_exclusive": iso_z(t0),
        "source_earliest_received": iso_z(source_min),
        "history_complete": history_complete,
        "short_history_allowed": bool(allow_short),
        "selection": "sha256(seed:unit_id), quotas MEDIC=8 BLS=2 PRIVATE=2 then deterministic fill",
        "requested_size": roster_size,
        "selected_size": len(public_units),
        "selected_unit_ids": [item["unit_id"] for item in public_units],
        "status_basis_hidden": {
            item["unit_id"]: item["_status_basis"] for item in selected if item["_status_basis"]
        },
    }
    return public_units, roster_meta


def group_incidents(
    rows: Sequence[dict[str, str]], t0: datetime, arrival_window: int
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, str]]]]:
    by_incident: dict[str, list[dict[str, str]]] = collections.defaultdict(list)
    for row in rows:
        incident_id = row.get("incident_number", "").strip()
        if incident_id:
            by_incident[incident_id].append(row)
    groups: list[tuple[datetime, str, list[dict[str, str]]]] = []
    end = t0 + timedelta(seconds=arrival_window)
    for incident_id, incident_rows in by_incident.items():
        arrivals = [parse_dt(r.get("received_dttm")) for r in incident_rows]
        arrivals = [value for value in arrivals if value is not None]
        if not arrivals:
            continue
        arrival = min(arrivals)
        if t0 <= arrival < end:
            groups.append((arrival, incident_id, incident_rows))
    groups.sort(key=lambda item: (item[0], item[1]))
    incidents: list[dict[str, Any]] = []
    for index, (arrival, incident_id, incident_rows) in enumerate(groups, start=1):
        representative = min(
            incident_rows,
            key=lambda row: (parse_dt(row.get("received_dttm")) or arrival, row.get("rowid", "")),
        )
        priority, raw_priority = normalise_priority(representative)
        zone, zone_level = zone_for(representative)
        medical = representative.get("call_type", "").strip().lower() == "medical incident"
        ems_rows = [row for row in incident_rows if is_ems(row)]
        invalid = any(disposition_invalid(row.get("call_final_disposition")) for row in incident_rows)
        complete_ems = [
            row
            for row in ems_rows
            if row.get("dispatch_dttm")
            and row.get("on_scene_dttm")
            and row.get("available_dttm")
            and not disposition_invalid(row.get("call_final_disposition"))
        ]
        dispatchable = bool(medical and ems_rows and complete_ems and not invalid)
        # Requires-ALS is a benchmark policy derived from priority, not inferred
        # from the unit that historical dispatch happened to choose.
        requires_als = bool(dispatchable and priority == 1)
        item: dict[str, Any] = {
            "incident_id": f"I{index:03d}",
            "source_incident_number": incident_id,
            "arrival_s": int(round((arrival - t0).total_seconds())),
            "call_type": representative.get("call_type", "").strip(),
            "priority": priority,
            "priority_raw": raw_priority,
            "requires_als": requires_als,
            "zone": zone,
            "zone_level": zone_level,
            "dispatchable": dispatchable,
            "source_rowids": sorted(row.get("rowid", "") for row in incident_rows),
            "response_row_count": len(incident_rows),
        }
        if not dispatchable:
            reasons = []
            if not medical:
                reasons.append("not_medical_incident")
            if not ems_rows:
                reasons.append("no_ems_unit_response")
            if invalid:
                reasons.append("cancelled_or_unable_to_locate")
            if not complete_ems:
                reasons.append("no_complete_ems_response_chain")
            item["non_dispatchable_reasons"] = sorted(set(reasons))
        incidents.append(item)
    return incidents, by_incident


def time_bucket(dt: datetime) -> str:
    return f"weekday_{dt.weekday()}_hour_{dt.hour:02d}"


def build_calibration(
    rows: Sequence[dict[str, str]],
    incidents: Sequence[dict[str, Any]],
    by_incident: dict[str, list[dict[str, str]]],
    t0: datetime,
    min_observations: int = 5,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    incident_lookup = {item["source_incident_number"]: item for item in incidents}
    # Infer a unit's previous known zone when available; otherwise use its mode
    # over the source slice.  This is explicitly derived, never an exact GPS.
    # Unit origin is inferred from data strictly before T0.  Future response
    # rows may still calibrate dynamics, but must not influence the initial
    # roster's location proxy.
    history_rows = [
        row for row in rows
        if (parse_dt(row.get("received_dttm")) or datetime.max) < t0
    ]
    unit_rows: dict[str, list[dict[str, str]]] = collections.defaultdict(list)
    for row in history_rows or rows:
        unit_rows[row.get("unit_id", "")].append(row)
    unit_mode_zone = {
        unit: mode_value([zone_for(row)[0] for row in history])
        for unit, history in unit_rows.items()
    }
    travel_values: dict[tuple[str, str, str, str], list[float]] = collections.defaultdict(list)
    service_values: dict[tuple[str, str], list[float]] = collections.defaultdict(list)
    release_values: dict[tuple[str, str], list[float]] = collections.defaultdict(list)
    dispatch_values: dict[str, list[float]] = collections.defaultdict(list)
    reference_responses: list[dict[str, Any]] = []
    background_events: list[dict[str, Any]] = []
    arrivals = {item["source_incident_number"]: item for item in incidents}

    for row in rows:
        incident_number = row.get("incident_number", "").strip()
        incident = arrivals.get(incident_number)
        dispatch = parse_dt(row.get("dispatch_dttm"))
        response = parse_dt(row.get("response_dttm"))
        on_scene = parse_dt(row.get("on_scene_dttm"))
        available = parse_dt(row.get("available_dttm"))
        received = parse_dt(row.get("received_dttm"))
        destination, _ = zone_for(row)
        unit_type = row.get("unit_type", "").strip().upper() or "UNKNOWN"
        if dispatch and response and response >= dispatch:
            dispatch_values[unit_type].append((response - dispatch).total_seconds())
        if response and on_scene and on_scene >= response:
            origin = unit_mode_zone.get(row.get("unit_id", ""), destination)
            bucket = time_bucket(dispatch or response)
            travel_values[(unit_type, origin, destination, bucket)].append(
                (on_scene - response).total_seconds()
            )
        if on_scene and available and available >= on_scene:
            priority, _ = normalise_priority(row)
            key = (unit_type, str(priority if priority is not None else "unknown"))
            transport = parse_dt(row.get("transport_dttm"))
            # SFFD does not expose a single explicit "service complete"
            # timestamp.  Prefer the observed on-scene -> transport interval
            # for service and transport -> available for clearance.  The
            # latter deliberately includes transport-to-hospital travel and
            # handoff time: first-version dynamics do not expose hospital as
            # a separate phase, but the unit must remain occupied throughout
            # that observed interval.
            # When those phases are absent, retain the complete observed
            # on-scene -> available interval as service and explicitly use a
            # zero release delay; this is a derived, auditable fallback rather
            # than an invented wall-clock constant.
            if transport is not None and transport >= on_scene:
                service_values[key].append((transport - on_scene).total_seconds())
                if available >= transport:
                    release_values[key].append((available - transport).total_seconds())
            else:
                service_values[key].append((available - on_scene).total_seconds())
                release_values[key].append(0.0)

        # Rows outside the candidate arrival window are calibration-only.  The
        # candidate rows below additionally become hidden reference or
        # exogenous background events.
        if incident is None:
            continue
        if incident["dispatchable"]:
            # EMS rows on a dispatchable incident are hidden historical
            # references only.  Fire/support rows are still exogenous work and
            # are handled by the background branch below.
            if is_ems(row):
                reference_responses.append(
                    {
                        "incident_id": incident["incident_id"],
                        "unit_id": row.get("unit_id", ""),
                        "unit_type": unit_type,
                        "dispatch_s": seconds_since(t0, row.get("dispatch_dttm")),
                        "response_s": seconds_since(t0, row.get("response_dttm")),
                        "on_scene_s": seconds_since(t0, row.get("on_scene_dttm")),
                        "release_s": seconds_since(t0, row.get("available_dttm")),
                        "destination_zone": destination,
                        "source_rowid": row.get("rowid", ""),
                        "disposition": row.get("call_final_disposition", ""),
                    }
                )
                continue

        # Non-dispatchable calls and non-EMS responses on otherwise dispatchable
        # incidents are exogenous background occupancy.  The simulator may use
        # these records, but the Agent-facing adapter must never expose their
        # future unit IDs or release times.
        if (not incident["dispatchable"]) or not is_ems(row):
            background_events.append(
                {
                    "kind": "external_response",
                    "incident_id": incident["incident_id"],
                    "unit_id": row.get("unit_id", ""),
                    "unit_type": unit_type,
                    "arrival_s": seconds_since(t0, row.get("received_dttm")),
                    "dispatch_s": seconds_since(t0, row.get("dispatch_dttm")),
                    "response_s": seconds_since(t0, row.get("response_dttm")),
                    "on_scene_s": seconds_since(t0, row.get("on_scene_dttm")),
                    "release_s": seconds_since(t0, row.get("available_dttm")),
                    "zone": destination,
                    "source_rowid": row.get("rowid", ""),
                    "reference_only": True,
                }
            )

    def serialize_groups(groups: dict[Any, list[float]], names: Sequence[str]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key in sorted(groups, key=lambda value: tuple(str(part) for part in value) if isinstance(value, tuple) else str(value)):
            key_parts = key if isinstance(key, tuple) else (key,)
            output["|".join(str(part) for part in key_parts)] = {
                "dimensions": {name: str(part) for name, part in zip(names, key_parts)},
                "sample": summary(groups[key]),
            }
        return output

    if not travel_values:
        raise ValueError("no calibrated response-to-on-scene travel observations found")
    if not service_values:
        raise ValueError("no calibrated on-scene-to-available service observations found")
    if not release_values:
        raise ValueError("no calibrated release-delay observations found")
    if not dispatch_values:
        raise ValueError("no calibrated dispatch-to-response observations found")
    aggregate_counts = {
        "travel": sum(len(v) for v in travel_values.values()),
        "service": sum(len(v) for v in service_values.values()),
        "release": sum(len(v) for v in release_values.values()),
        "dispatch": sum(len(v) for v in dispatch_values.values()),
    }
    insufficient = [name for name, count in aggregate_counts.items() if count < min_observations]
    if insufficient:
        raise ValueError(
            "calibration strata are too small (need at least "
            f"{min_observations} observations): "
            + ", ".join(f"{name}={aggregate_counts[name]}" for name in insufficient)
        )
    def distribution_map(
        groups: dict[tuple[Any, ...], list[float]],
        dimensions: Sequence[str],
    ) -> dict[str, Any]:
        """Build simulator-compatible maps plus robust audit summaries.

        The detailed summaries remain in the public calibration section.  The
        compact maps use p25..p75 as a deterministic empirical range so the
        simulator can resolve alternate actions without needing raw records.
        """

        by_exact: dict[tuple[Any, ...], list[float]] = collections.defaultdict(list)
        by_pair: dict[tuple[Any, ...], list[float]] = collections.defaultdict(list)
        by_type: dict[str, list[float]] = collections.defaultdict(list)
        for key, values in groups.items():
            by_exact[key].extend(values)
            by_pair[key[:-1] if dimensions and dimensions[-1] == "time_bucket" else key].extend(values)
            if key:
                by_type[str(key[0])].extend(values)

        def value_for(values: Sequence[float]) -> list[int]:
            stats = summary(values)
            # Lists are intentional: the simulator's lookup layer treats a
            # mapping as a nested dimension map.  A compact empirical sample
            # therefore avoids that ambiguity while preserving deterministic
            # seed-based choice among p25/median/p75.
            return [
                int(stats["p25_seconds"]),
                int(stats["median_seconds"]),
                int(max(stats["p75_seconds"], stats["p25_seconds"])),
            ]

        output: dict[str, Any] = {}
        # Preserve the requested time-bucket stratum, then add explicitly
        # aggregated pair/type fallbacks.  The simulator chooses the exact
        # bucket for the logical clock and only falls back to these summaries.
        for key, values in sorted(by_exact.items(), key=lambda item: tuple(str(x) for x in item[0])):
            output["|".join(str(x) for x in key)] = value_for(values)
        for key, values in sorted(by_pair.items(), key=lambda item: tuple(str(x) for x in item[0])):
            output.setdefault("|".join(str(x) for x in key), value_for(values))
        for unit_type, values in sorted(by_type.items()):
            output.setdefault(unit_type, value_for(values))
        all_values = [value for values in groups.values() for value in values]
        if all_values:
            output.setdefault("default", value_for(all_values))
        return output

    travel_sim_map = distribution_map(
        travel_values, ("unit_type", "origin_zone", "destination_zone", "time_bucket")
    )
    service_sim_map = distribution_map(service_values, ("unit_type", "priority"))
    release_sim_map = distribution_map(release_values, ("unit_type", "priority"))
    calibration = {
        "method": "empirical robust summaries from frozen response log; no unmarked constants",
        "phase_definitions": {
            "travel": "response_dttm to on_scene_dttm",
            "service_with_transport": "on_scene_dttm to transport_dttm",
            "release_with_transport": (
                "transport_dttm to available_dttm, including transport-to-hospital "
                "and handoff while the unit remains occupied"
            ),
            "service_without_transport": "on_scene_dttm to available_dttm",
            "release_without_transport": (
                "zero because the complete busy interval is already represented as service"
            ),
        },
        "travel_time_s": travel_sim_map,
        "service_duration_s": service_sim_map,
        "release_delay_s": release_sim_map,
        "travel_time_seconds": serialize_groups(
            travel_values, ("unit_type", "origin_zone", "destination_zone", "time_bucket")
        ),
        "service_time_seconds": serialize_groups(service_values, ("unit_type", "priority")),
        "release_time_seconds": serialize_groups(release_values, ("unit_type", "priority")),
        "dispatch_delay_seconds": serialize_groups(dispatch_values, ("unit_type",)),
        "sample_counts": {
            "travel_groups": len(travel_values),
            "travel_observations": sum(len(v) for v in travel_values.values()),
            "service_groups": len(service_values),
            "service_observations": sum(len(v) for v in service_values.values()),
            "release_groups": len(release_values),
            "release_observations": sum(len(v) for v in release_values.values()),
            "dispatch_delay_groups": len(dispatch_values),
            "dispatch_delay_observations": sum(len(v) for v in dispatch_values.values()),
        },
        "fallback_policy": "missing strata are a verifier/configuration error; simulator may use only explicitly aggregated empirical type/default ranges",
    }
    reference_responses.sort(key=lambda item: (item["incident_id"], item["source_rowid"]))
    background_events.sort(key=lambda item: (item["arrival_s"] if item["arrival_s"] is not None else 10**9, item["source_rowid"]))
    for index, item in enumerate(background_events, start=1):
        item["event_id"] = f"B{index:03d}"
    return calibration, reference_responses, background_events


def make_scenario(args: argparse.Namespace) -> dict[str, Any]:
    rows, fields = load_rows(args.csv)
    quality = data_quality(rows, fields)
    if not quality["rowid_unique"] or not quality["received_time_monotonic"]:
        raise ValueError("source CSV fails row identity/order validation")
    t0 = args.t0_dt
    incidents, by_incident = group_incidents(rows, t0, args.arrival_window_seconds)
    dispatchable = [item for item in incidents if item["dispatchable"]]
    expected_dispatchable = args.expected_dispatchable_incidents
    if not dispatchable:
        raise ValueError("candidate window produced no complete dispatchable medical incidents")
    if expected_dispatchable and len(dispatchable) != expected_dispatchable:
        raise ValueError(
            f"candidate window produced {len(dispatchable)} complete dispatchable medical incidents; "
            f"expected {expected_dispatchable}"
        )
    units, roster_meta = choose_roster(
        rows, t0, args.roster_days, args.roster_size, args.seed, args.allow_short_roster_history
    )
    calibration, reference_responses, background_events = build_calibration(
        rows, incidents, by_incident, t0, args.min_calibration_observations
    )
    dispatchable_arrivals = sorted(item["arrival_s"] for item in dispatchable)
    initial_available = sum(
        item.get("initial_status") in {"FREE", "AVAILABLE"} for item in units
    )
    initial_busy = len(units) - initial_available
    burst_window = 900
    peak_burst = max(
        (sum(start <= arrival < start + burst_window for arrival in dispatchable_arrivals)
         for start in dispatchable_arrivals),
        default=0,
    )
    resource_competition = bool(
        initial_busy > 0 or peak_burst > initial_available or len(dispatchable) > initial_available
    )
    if not resource_competition:
        raise ValueError(
            "candidate window does not create a resource-competition condition; "
            "choose a denser arrival window or smaller roster"
        )
    zones: dict[str, dict[str, Any]] = {}
    for incident in incidents:
        zones.setdefault(
            incident["zone"],
            {
                "zone_id": incident["zone"],
                "level": incident["zone_level"],
                "source": "observed_coarse_location",
            },
        )
    for unit in units:
        zones.setdefault(
            unit["initial_zone"],
            {"zone_id": unit["initial_zone"], "level": "derived_mode_zone", "source": "derived"},
        )
    data_sha256 = sha256_file(args.csv)
    source_data_as_of = sorted({row.get("data_as_of", "") for row in rows if row.get("data_as_of")})
    # Do not use wall-clock time here: a rebuild from the same frozen bytes
    # must produce the same scenario hash.  ``data_as_of`` is supplied by the
    # source portal and is therefore a stable provenance boundary.  An
    # explicit override is available for a release process that records a
    # different, audited timestamp.
    if args.generated_at_dt is not None:
        generated_at = iso_z(args.generated_at_dt)
    elif source_data_as_of:
        generated_at = max(source_data_as_of)
    else:
        generated_at = iso_z(t0)
    scenario = {
        "schema_version": SCHEMA_VERSION,
        "scenario_id": args.scenario_id,
        "seed": args.seed,
        "clock": {
            "t0": iso_z(t0),
            "timezone": "America/Los_Angeles (source wall-clock; simulator uses naive logical seconds)",
            "arrival_window_seconds": args.arrival_window_seconds,
            "horizon_seconds": args.horizon_seconds,
            "event_order": "(sim_time, event_priority, monotonic_sequence)",
        },
        "zones": [zones[key] for key in sorted(zones)],
        "units": units,
        "incidents": incidents,
        "background_events": background_events,
        "calibration": calibration,
        "hidden_reference_responses": reference_responses,
        "metadata": {
            "generated_at": generated_at,
            "generated_at_source": (
                "explicit --generated-at"
                if args.generated_at_dt is not None
                else "max(data_as_of) from frozen source CSV"
            ),
            "scenario_schema_version": SCHEMA_VERSION,
            "simulator_version": "sffd-ems-simulator/0.1.0",
            "seed": args.seed,
            "source_csv": args.csv.name,
            "source_csv_sha256": data_sha256,
            "source_rows": len(rows),
            "source_fields": fields,
            "source_data_as_of_values": source_data_as_of,
            "data_quality": quality,
            "provenance": {
                "observed": [
                    "incident arrival time, call type, raw priority, coarse battalion/station zone, source row IDs",
                    "historical response phase timestamps used only for calibration/reference replay",
                ],
                "derived": [
                    "normalized priority, dispatchable filter, ALS requirement policy, 7-day roster, initial busy/free status, coarse unit zone, empirical calibration summaries",
                ],
                "counterfactual": [
                    "agent actions and resulting transitions, hidden future event observations, deterministic event replay under alternate dispatches",
                ],
            },
            "privacy": {
                "exact_addresses_removed": True,
                "coordinates_removed": True,
                "zones": "battalion, with station_area fallback",
            },
            "dispatchable_incident_count": len(dispatchable),
            "all_arrival_window_incident_count": len(incidents),
            "resource_competition": {
                "initial_available_units": initial_available,
                "initial_busy_units": initial_busy,
                "peak_dispatchable_arrivals_15m": peak_burst,
                "dispatchable_incidents": len(dispatchable),
                "detected": resource_competition,
            },
            "excluded_incidents": [
                {
                    "incident_id": item["incident_id"],
                    "source_incident_number": item["source_incident_number"],
                    "reasons": item.get("non_dispatchable_reasons", []),
                }
                for item in incidents
                if not item["dispatchable"]
            ],
            "roster": roster_meta,
            "development_warning": (
                "Roster history is shorter than the requested seven days; this scenario was generated with the explicit development override."
                if not roster_meta["history_complete"]
                else None
            ),
        },
    }
    # Keep provenance machine-readable at the field level.  The short
    # observed/derived/counterfactual lists above are useful for humans, but a
    # verifier and a future scenario builder need an unambiguous marker for
    # every emitted field and for every source field that was intentionally
    # removed from the public scenario.
    precise_fields = {"address", "city", "zipcode_of_incident", "case_location"}
    # ``exposed`` describes what can reach the Agent-facing observation, not
    # merely which source columns contributed to a derived value.  In
    # particular, historical dispatch/response/release timestamps, final
    # priority, incident numbers, and row IDs remain verifier-only.
    exposed_source_fields = {
        "unit_id",
        "call_type",
        "received_dttm",
        "battalion",
        "station_area",
        "original_priority",
        "priority",
        "unit_type",
    }
    source_markers = {
        field: {"source": "observed", "exposed": field in exposed_source_fields}
        for field in fields
    }
    source_markers.update({field: {"source": "observed", "exposed": False}
                           for field in sorted(precise_fields) if field not in source_markers})
    scenario_markers = {
        "schema_version": {"source": "derived", "exposed": True},
        # The evaluator may swap in a private historical window.  Exposing
        # its identifier would let an Agent fingerprint that window across
        # retries, so it is verifier-only just like the dynamics seed.
        "scenario_id": {"source": "derived", "exposed": False},
        "seed": {"source": "derived", "exposed": False},
        "clock": {"source": "derived", "exposed": True},
        "zones": {"source": "derived", "exposed": True},
        "units": {"source": "derived", "exposed": True},
        "incidents.arrival_s": {"source": "observed", "exposed": True},
        "incidents.call_type": {"source": "observed", "exposed": True},
        "incidents.priority_raw": {"source": "observed", "exposed": True},
        "incidents.source_incident_number": {"source": "observed", "exposed": False},
        "incidents.source_rowids": {"source": "observed", "exposed": False},
        "incidents.priority": {"source": "derived", "exposed": True},
        "incidents.requires_als": {"source": "derived", "exposed": True},
        "incidents.zone": {"source": "derived", "exposed": True},
        "incidents.dispatchable": {"source": "derived", "exposed": True},
        "incidents.non_dispatchable_reasons": {"source": "derived", "exposed": True},
        "background_events": {"source": "observed", "exposed": False},
        "calibration": {"source": "derived", "exposed": False},
        "hidden_reference_responses": {"source": "observed", "exposed": False},
        "metadata": {"source": "derived", "exposed": False},
        "agent_actions": {"source": "counterfactual", "exposed": False},
        "state_transitions": {"source": "counterfactual", "exposed": False},
        "score": {"source": "counterfactual", "exposed": False},
    }
    scenario["metadata"]["field_provenance"] = {
        "source_fields": source_markers,
        "scenario_fields": scenario_markers,
        "marker_vocabulary": ["observed", "derived", "counterfactual"],
    }
    return scenario


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        scenario = make_scenario(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(scenario, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
    except (OSError, ValueError, csv.Error) as exc:
        print(f"build_sffd_scenario: error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "output": str(args.output),
                "scenario_id": scenario["scenario_id"],
                "source_rows": scenario["metadata"]["source_rows"],
                "incidents": len(scenario["incidents"]),
                "dispatchable_incidents": scenario["metadata"]["dispatchable_incident_count"],
                "units": len(scenario["units"]),
                "history_complete": scenario["metadata"]["roster"]["history_complete"],
                "source_sha256": scenario["metadata"]["source_csv_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
