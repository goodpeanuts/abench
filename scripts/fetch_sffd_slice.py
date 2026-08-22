#!/usr/bin/env python3
"""Fetch and validate a reproducible slice of the SFFD SODA dataset.

The script intentionally uses only the Python standard library.  It writes the
raw CSV and the portal metadata to a caller supplied directory (normally a
temporary directory, not the repository), then writes a small manifest with
hashes and validation results.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence


DATASET_ID = "nuek-vuh3"
DEFAULT_DATA_URL = f"https://data.sfgov.org/resource/{DATASET_ID}.csv"
DEFAULT_METADATA_URL = f"https://data.sfgov.org/api/views/{DATASET_ID}"

# The CSV export currently contains these 36 business fields.  The portal
# metadata may expose additional computed region columns; those are optional.
EXPECTED_FIELDS = (
    "call_number",
    "unit_id",
    "incident_number",
    "call_type",
    "call_date",
    "watch_date",
    "received_dttm",
    "entry_dttm",
    "dispatch_dttm",
    "response_dttm",
    "on_scene_dttm",
    "transport_dttm",
    "hospital_dttm",
    "call_final_disposition",
    "available_dttm",
    "address",
    "city",
    "zipcode_of_incident",
    "battalion",
    "station_area",
    "box",
    "original_priority",
    "priority",
    "final_priority",
    "als_unit",
    "call_type_group",
    "number_of_alarms",
    "unit_type",
    "unit_sequence_in_call_dispatch",
    "fire_prevention_district",
    "supervisor_district",
    "neighborhoods_analysis_boundaries",
    "rowid",
    "case_location",
    "data_as_of",
    "data_loaded_at",
)


def parse_datetime(value: str) -> datetime:
    """Validate an API datetime while retaining the user's wall-clock value."""

    candidate = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid ISO-8601 datetime: {value!r}"
        ) from exc


def api_datetime(value: str) -> str:
    """Return a stable SODA-compatible timestamp without changing its timezone."""

    parsed = parse_datetime(value)
    # SFFD's values are stored as local-looking ISO values.  Keep an explicit
    # offset if the caller supplied one; otherwise use the canonical .000 form.
    if parsed.tzinfo is not None:
        return parsed.isoformat(timespec="milliseconds")
    return parsed.isoformat(timespec="milliseconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_bytes(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/csv,application/json",
            "User-Agent": "abench-sffd-freezer/0.1",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def csv_shape(data: bytes) -> tuple[list[str], int]:
    """Return header and row count, rejecting malformed CSV rows."""

    text = data.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ValueError("CSV response is empty") from exc
    if not header or any(not name for name in header):
        raise ValueError("CSV header contains an empty field")
    if len(set(header)) != len(header):
        raise ValueError("CSV header contains duplicate fields")
    count = 0
    for line_number, row in enumerate(reader, start=2):
        if len(row) != len(header):
            raise ValueError(
                f"CSV row {line_number} has {len(row)} fields; expected {len(header)}"
            )
        count += 1
    return header, count


def canonical_csv(pages: Iterable[bytes]) -> tuple[bytes, list[str], int]:
    """Combine paginated SODA CSV responses into one deterministic CSV.

    SODA returns a header on every page.  Parsing and writing with a fixed LF
    terminator makes hashes independent of platform newline settings.
    """

    header: list[str] | None = None
    rows: list[list[str]] = []
    for page_number, data in enumerate(pages):
        page_header, page_count = csv_shape(data)
        if header is None:
            header = page_header
        elif page_header != header:
            raise ValueError(
                f"page {page_number} has a different CSV header than page 0"
            )
        reader = csv.reader(io.StringIO(data.decode("utf-8-sig"), newline=""))
        next(reader)
        page_rows = list(reader)
        if len(page_rows) != page_count:
            raise ValueError(f"page {page_number} row-count validation mismatch")
        rows.extend(page_rows)
    if header is None:
        raise ValueError("SODA returned no pages")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8"), header, len(rows)


def build_query(
    data_url: str,
    start: str,
    end: str,
    limit: int,
    offset: int,
) -> str:
    where = f"received_dttm >= '{api_datetime(start)}' AND received_dttm < '{api_datetime(end)}'"
    params = {
        "$where": where,
        "$order": "received_dttm, rowid",
        "$limit": str(limit),
        "$offset": str(offset),
    }
    return f"{data_url}?{urllib.parse.urlencode(params)}"


def fetch_csv_pages(
    data_url: str,
    start: str,
    end: str,
    page_size: int,
    timeout: float,
) -> Iterable[bytes]:
    offset = 0
    while True:
        url = build_query(data_url, start, end, page_size, offset)
        page = fetch_bytes(url, timeout)
        # An empty SODA response is still a valid CSV with no data rows.  Stop
        # after yielding it so the caller can report a useful zero-row result.
        yield page
        _, page_rows = csv_shape(page)
        if page_rows < page_size:
            break
        offset += page_rows


def validate_fields(header: Sequence[str], expected: Sequence[str]) -> None:
    missing = [field for field in expected if field not in header]
    if missing:
        raise ValueError(f"CSV is missing required fields: {', '.join(missing)}")


def validate_rows(rows: Sequence[Mapping[str, str]], header: Sequence[str]) -> dict[str, object]:
    """Validate row identity/order and return auditable data-quality stats.

    SFFD is intentionally a response-row dataset: repeated incident numbers
    are expected when several units answer one call.  We therefore reject
    duplicate *rowids*, while reporting the number of unique incidents and
    multi-row incident groups instead of incorrectly treating them as errors.
    """

    rowids = [str(row.get("rowid", "")).strip() for row in rows]
    if any(not value for value in rowids):
        raise ValueError("CSV rowid values contain an empty value")
    if len(set(rowids)) != len(rowids):
        raise ValueError("CSV rowid values are not unique")

    received_values: list[datetime] = []
    for index, row in enumerate(rows, start=2):
        value = str(row.get("received_dttm", "")).strip()
        if not value:
            continue
        try:
            received_values.append(parse_datetime(value))
        except argparse.ArgumentTypeError as exc:
            raise ValueError(f"invalid received_dttm at CSV row {index}: {value!r}") from exc
    if received_values != sorted(received_values):
        raise ValueError("received_dttm is not monotonic in the requested order")

    incident_counts: dict[str, int] = {}
    for row in rows:
        incident = str(row.get("incident_number", "")).strip()
        if incident:
            incident_counts[incident] = incident_counts.get(incident, 0) + 1
    missing_rates = {
        field: round(sum(not str(row.get(field, "")).strip() for row in rows) / max(1, len(rows)), 6)
        for field in header
    }
    return {
        "rowid_unique": True,
        "received_time_monotonic": True,
        "unique_incident_numbers": len(incident_counts),
        "multi_row_incident_numbers": sum(count > 1 for count in incident_counts.values()),
        "missing_rates": missing_rates,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="inclusive ISO datetime")
    parser.add_argument("--end", required=True, help="exclusive ISO datetime")
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="directory for raw CSV, metadata, and fetch_manifest.json",
    )
    parser.add_argument("--data-url", default=DEFAULT_DATA_URL)
    parser.add_argument("--metadata-url", default=DEFAULT_METADATA_URL)
    parser.add_argument("--csv-name", default="raw-response-slice.csv")
    parser.add_argument("--metadata-name", default="metadata.json")
    parser.add_argument("--manifest-name", default="fetch_manifest.json")
    parser.add_argument("--page-size", type=int, default=50000)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--allow-extra-fields",
        action="store_true",
        help="allow portal-added CSV columns while still requiring all business fields",
    )
    args = parser.parse_args(argv)
    start = parse_datetime(args.start)
    end = parse_datetime(args.end)
    if start >= end:
        parser.error("--start must be earlier than --end")
    if args.page_size <= 0:
        parser.error("--page-size must be positive")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        csv_bytes, header, row_count = canonical_csv(
            fetch_csv_pages(
                args.data_url,
                args.start,
                args.end,
                args.page_size,
                args.timeout,
            )
        )
        validate_fields(header, EXPECTED_FIELDS)
        extras = [field for field in header if field not in EXPECTED_FIELDS]
        if extras and not args.allow_extra_fields:
            raise ValueError(
                "CSV contains unexpected fields (use --allow-extra-fields): "
                + ", ".join(extras)
            )
        csv_path = args.output_dir / args.csv_name
        csv_path.write_bytes(csv_bytes)
        parsed_rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"), newline="")))
        quality = validate_rows(parsed_rows, header)
        received_values = sorted(
            row.get("received_dttm", "") for row in parsed_rows if row.get("received_dttm")
        )
        data_as_of_values = sorted(
            {row.get("data_as_of", "") for row in parsed_rows if row.get("data_as_of")}
        )

        metadata_bytes = fetch_bytes(args.metadata_url, args.timeout)
        # Validate JSON before writing the exact response bytes for hashing.
        metadata = json.loads(metadata_bytes.decode("utf-8"))
        if not isinstance(metadata, dict):
            raise ValueError("portal metadata is not a JSON object")
        metadata_path = args.output_dir / args.metadata_name
        metadata_path.write_bytes(metadata_bytes)

        manifest = {
            "schema_version": "sffd-fetch-manifest.v1",
            "fetched_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "dataset_id": DATASET_ID,
            "data_url": args.data_url,
            "metadata_url": args.metadata_url,
            "query": {
                "where": f"received_dttm >= '{api_datetime(args.start)}' AND received_dttm < '{api_datetime(args.end)}'",
                "order": "received_dttm, rowid",
                "page_size": args.page_size,
            },
            "requested_range": {
                "start": api_datetime(args.start),
                "end_exclusive": api_datetime(args.end),
            },
            "observed_received_range": {
                "min": received_values[0] if received_values else None,
                "max": received_values[-1] if received_values else None,
            },
            "observed_data_as_of_values": data_as_of_values,
            "csv": {
                "path": str(csv_path),
                "sha256": hashlib.sha256(csv_bytes).hexdigest(),
                "bytes": len(csv_bytes),
                "rows": row_count,
                "fields": len(header),
                "header": header,
                "schema_sha256": hashlib.sha256(
                    json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
            },
            "metadata": {
                "path": str(metadata_path),
                "sha256": hashlib.sha256(metadata_bytes).hexdigest(),
                "bytes": len(metadata_bytes),
            },
            "validation": {
                "required_fields_present": True,
                "unexpected_fields": extras,
                "row_widths_consistent": True,
                **quality,
            },
        }
        manifest_path = args.output_dir / args.manifest_name
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"fetch_sffd_slice: error: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "csv": str(csv_path),
                "metadata": str(metadata_path),
                "manifest": str(manifest_path),
                "rows": row_count,
                "fields": len(header),
                "csv_sha256": manifest["csv"]["sha256"],
                "metadata_sha256": manifest["metadata"]["sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
