#!/usr/bin/env python3
"""Build evaluator-private SFFD variants from distinct real event windows.

Hidden variants must not reuse the public development scenario's future event
stream. This generator scans a wider frozen SFFD response slice, selects
well-separated real 20-minute windows with an evaluator-keyed HMAC ranking,
and rebuilds a complete scenario for each selected window. A separate
HMAC-derived dynamics seed fixes counterfactual samples within each variant.

The raw CSV, evaluator key, generated scenarios, and private manifest stay
outside the public repository. The same frozen bytes, key, and arguments
produce byte-identical variants.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from build_sffd_scenario import (
    DEFAULT_ARRIVAL_WINDOW,
    DEFAULT_HORIZON,
    group_incidents,
    load_rows,
    make_scenario,
    parse_dt,
)


GENERATOR_VERSION = "sffd-hidden-variant-generator/0.2.0"
MANIFEST_SCHEMA = "sffd-hidden-real-window-variants.v1"
DEFAULT_KEY_ENV = "SFFD_VARIANT_KEY"


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def derive_seed(key: bytes, source_hash: str, t0: datetime, index: int) -> int:
    message = (
        f"{GENERATOR_VERSION}|seed|{source_hash}|{t0.isoformat()}|{index}"
    ).encode("utf-8")
    digest = hmac.new(key, message, hashlib.sha256).digest()
    return 1 + (int.from_bytes(digest[:8], "big") % (2**31 - 2))


def opaque_scenario_id(key: bytes, source_hash: str, t0: datetime) -> str:
    message = f"{GENERATOR_VERSION}|id|{source_hash}|{t0.isoformat()}".encode("utf-8")
    tag = hmac.new(key, message, hashlib.sha256).hexdigest()[:16]
    return f"sffd-hidden-{tag}"


def scan_candidates(
    rows: Sequence[dict[str, str]],
    *,
    start: datetime,
    end: datetime,
    step: timedelta,
    arrival_window_seconds: int,
    min_dispatchable: int,
    max_dispatchable: int,
    min_priority_one: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    current = start
    while current < end:
        incidents, _ = group_incidents(rows, current, arrival_window_seconds)
        dispatchable = [item for item in incidents if item.get("dispatchable")]
        priority_one = sum(str(item.get("priority")) == "1" for item in dispatchable)
        if (
            min_dispatchable <= len(dispatchable) <= max_dispatchable
            and priority_one >= min_priority_one
        ):
            source_ids = sorted(str(item.get("source_incident_number", "")) for item in incidents)
            candidates.append(
                {
                    "t0": current,
                    "incident_count": len(incidents),
                    "dispatchable_count": len(dispatchable),
                    "priority_one_count": priority_one,
                    "event_stream_fingerprint": hashlib.sha256(
                        "|".join(source_ids).encode("utf-8")
                    ).hexdigest(),
                }
            )
        current += step
    return candidates


def rank_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    key: bytes,
    source_hash: str,
) -> list[dict[str, Any]]:
    def rank(item: Mapping[str, Any]) -> bytes:
        t0 = item.get("t0")
        if not isinstance(t0, datetime):
            raise ValueError("candidate t0 must be a datetime")
        message = f"{GENERATOR_VERSION}|window|{source_hash}|{t0.isoformat()}".encode("utf-8")
        return hmac.new(key, message, hashlib.sha256).digest()

    return [dict(item) for item in sorted(candidates, key=rank)]


def is_well_separated(t0: datetime, selected: Sequence[datetime], gap: timedelta) -> bool:
    return all(abs(t0 - existing) >= gap for existing in selected)


def scenario_args(
    *,
    csv_path: Path,
    t0: datetime,
    seed: int,
    scenario_id: str,
    arrival_window_seconds: int,
    horizon_seconds: int,
    roster_days: int,
    roster_size: int,
    min_calibration_observations: int,
) -> argparse.Namespace:
    return argparse.Namespace(
        csv=csv_path,
        output=Path("scenario.json"),
        t0=t0.isoformat(timespec="seconds"),
        t0_dt=t0,
        arrival_window_seconds=arrival_window_seconds,
        horizon_seconds=horizon_seconds,
        expected_dispatchable_incidents=0,
        roster_days=roster_days,
        roster_size=roster_size,
        min_calibration_observations=min_calibration_observations,
        seed=seed,
        generated_at=None,
        generated_at_dt=None,
        allow_short_roster_history=False,
        scenario_id=scenario_id,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path, help="wider frozen SFFD CSV")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--candidate-start", default=None, help="inclusive ISO local timestamp")
    parser.add_argument("--candidate-end", default=None, help="exclusive ISO local timestamp")
    parser.add_argument("--step-minutes", type=int, default=10)
    parser.add_argument("--minimum-gap-hours", type=float, default=12.0)
    parser.add_argument("--arrival-window-seconds", type=int, default=DEFAULT_ARRIVAL_WINDOW)
    parser.add_argument("--horizon-seconds", type=int, default=DEFAULT_HORIZON)
    parser.add_argument("--min-dispatchable", type=int, default=10)
    parser.add_argument("--max-dispatchable", type=int, default=16)
    parser.add_argument("--min-priority-one", type=int, default=1)
    parser.add_argument("--roster-days", type=int, default=7)
    parser.add_argument("--roster-size", type=int, default=12)
    parser.add_argument("--min-calibration-observations", type=int, default=5)
    parser.add_argument("--key-file", type=Path, default=None)
    parser.add_argument("--key-env", default=DEFAULT_KEY_ENV)
    parser.add_argument("--manifest-name", default="hidden_variants_manifest.json")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.count < 1:
        parser.error("count must be positive")
    if args.step_minutes < 1 or args.minimum_gap_hours <= 0:
        parser.error("step-minutes and minimum-gap-hours must be positive")
    if args.min_dispatchable < 1 or args.max_dispatchable < args.min_dispatchable:
        parser.error("invalid dispatchable incident range")
    if args.roster_days < 1 or args.roster_size < 1:
        parser.error("roster-days and roster-size must be positive")
    return args


def read_key(args: argparse.Namespace) -> bytes:
    if args.key_file is not None:
        key = args.key_file.read_bytes().strip()
        source = str(args.key_file)
    else:
        key = os.environ.get(args.key_env, "").encode("utf-8")
        source = f"environment variable {args.key_env}"
    if len(key) < 16:
        raise ValueError(f"{source} must supply at least 16 bytes")
    return key


def write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        key = read_key(args)
        rows, _ = load_rows(args.csv)
        received = [parse_dt(row.get("received_dttm")) for row in rows]
        received = [value for value in received if value is not None]
        if not received:
            raise ValueError("source CSV contains no received_dttm values")
        default_start = min(received) + timedelta(days=args.roster_days)
        default_end = max(received) - timedelta(seconds=args.arrival_window_seconds)
        start = parse_dt(args.candidate_start) if args.candidate_start else default_start
        end = parse_dt(args.candidate_end) if args.candidate_end else default_end
        if start is None or end is None or start >= end:
            raise ValueError("candidate-start must be earlier than candidate-end")
        if start < default_start:
            raise ValueError("candidate-start lacks the required complete roster history")

        source_hash = sha256_file(args.csv)
        candidates = scan_candidates(
            rows,
            start=start,
            end=end,
            step=timedelta(minutes=args.step_minutes),
            arrival_window_seconds=args.arrival_window_seconds,
            min_dispatchable=args.min_dispatchable,
            max_dispatchable=args.max_dispatchable,
            min_priority_one=args.min_priority_one,
        )
        ranked = rank_candidates(candidates, key=key, source_hash=source_hash)
        selected_times: list[datetime] = []
        variants: list[dict[str, Any]] = []
        entries: list[dict[str, Any]] = []
        gap = timedelta(hours=args.minimum_gap_hours)
        for candidate in ranked:
            t0 = candidate["t0"]
            if not is_well_separated(t0, selected_times, gap):
                continue
            index = len(variants) + 1
            seed = derive_seed(key, source_hash, t0, index)
            scenario_id = opaque_scenario_id(key, source_hash, t0)
            try:
                scenario = make_scenario(
                    scenario_args(
                        csv_path=args.csv,
                        t0=t0,
                        seed=seed,
                        scenario_id=scenario_id,
                        arrival_window_seconds=args.arrival_window_seconds,
                        horizon_seconds=args.horizon_seconds,
                        roster_days=args.roster_days,
                        roster_size=args.roster_size,
                        min_calibration_observations=args.min_calibration_observations,
                    )
                )
            except ValueError:
                continue
            metadata = scenario.setdefault("metadata", {})
            metadata["hidden_variant"] = {
                "generator_version": GENERATOR_VERSION,
                "index": index,
                "selection": "evaluator-keyed real historical window",
                "source_csv_sha256": source_hash,
            }
            payload = canonical_json_bytes(scenario)
            file_name = f"{scenario_id}.json"
            variants.append(scenario)
            selected_times.append(t0)
            entries.append(
                {
                    "index": index,
                    "scenario_id": scenario_id,
                    "file": file_name,
                    "scenario_sha256": hashlib.sha256(payload).hexdigest(),
                    "seed": seed,
                    "t0": t0.isoformat(timespec="seconds"),
                    "incident_count": len(scenario.get("incidents", [])),
                    "dispatchable_count": scenario["metadata"]["dispatchable_incident_count"],
                    "event_stream_fingerprint": candidate["event_stream_fingerprint"],
                }
            )
            if len(variants) == args.count:
                break
        if len(variants) != args.count:
            raise ValueError(
                f"only {len(variants)} eligible well-separated real windows were buildable; "
                f"requested {args.count} from {len(candidates)} preliminary candidates"
            )

        manifest = {
            "schema_version": MANIFEST_SCHEMA,
            "generator_version": GENERATOR_VERSION,
            "source_csv_sha256": source_hash,
            "source_csv_rows": len(rows),
            "count": len(variants),
            "key_id": hashlib.sha256(key).hexdigest()[:16],
            "agent_visible": False,
            "selection": {
                "candidate_start": start.isoformat(timespec="seconds"),
                "candidate_end": end.isoformat(timespec="seconds"),
                "step_minutes": args.step_minutes,
                "minimum_gap_hours": args.minimum_gap_hours,
                "arrival_window_seconds": args.arrival_window_seconds,
                "min_dispatchable": args.min_dispatchable,
                "max_dispatchable": args.max_dispatchable,
                "min_priority_one": args.min_priority_one,
                "preliminary_candidate_count": len(candidates),
            },
            "variants": entries,
        }
        output_paths = [args.output_dir / item["file"] for item in entries]
        output_paths.append(args.output_dir / args.manifest_name)
        if not args.force:
            existing = [str(path) for path in output_paths if path.exists()]
            if existing:
                raise FileExistsError(
                    "refusing to overwrite generated output; pass --force: "
                    + ", ".join(existing)
                )
        for scenario, entry in zip(variants, entries):
            write_atomic(args.output_dir / entry["file"], canonical_json_bytes(scenario))
        write_atomic(args.output_dir / args.manifest_name, canonical_json_bytes(manifest))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"build_sffd_variants: error: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "output_dir": str(args.output_dir),
                "count": manifest["count"],
                "manifest": str(args.output_dir / args.manifest_name),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
