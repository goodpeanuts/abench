#!/usr/bin/env python3
"""Aggregate pi harness run manifests and compare configurations.

The script deliberately uses only the Python standard library so that the
statistics can run in the same environments as the harness itself.
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import math
import os
import pathlib
import statistics
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Sequence


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_SCAN_DIR = REPO_ROOT / "results" / "pi-runs"
DEFAULT_DATA_VERSION = "1.0.0"
REFERENCE_TRACK_VERSION = "1.1.0"
REFERENCE_KEYS = (
    "response_linear",
    "coverage",
    "safety_ungated",
    "adaptation_ungated",
    "efficiency_ungated",
    "integrity",
)
VERSION_MAP: dict[str, str] = {}

# Keep this in sync with the manually annotated pollution exclusions in
# build_score_explorer.py. New manifests can instead set polluted=true.
KNOWN_POLLUTED_PREFIXES = ("20260822T174859",)

# Two-sided 95% Student t critical values. The benchmark convention uses the
# normal approximation at df >= 30; fractional Welch df use the conservative
# value for floor(df).
T95 = (
    None,
    12.706,
    4.303,
    3.182,
    2.776,
    2.571,
    2.447,
    2.365,
    2.306,
    2.262,
    2.228,
    2.201,
    2.179,
    2.160,
    2.145,
    2.131,
    2.120,
    2.110,
    2.101,
    2.093,
    2.086,
    2.080,
    2.074,
    2.069,
    2.064,
    2.060,
    2.056,
    2.052,
    2.048,
    2.045,
)


@dataclass(frozen=True, order=True)
class GroupKey:
    model: str
    thinking: str
    data_version: str
    diagnostic: bool

    @property
    def mode(self) -> str:
        return "diagnostic" if self.diagnostic else "evaluation"

    @property
    def identifier(self) -> str:
        return f"{self.model}|{self.thinking}|{self.data_version}|{self.mode}"

    def as_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "thinking": self.thinking,
            "data_version": self.data_version,
            "diagnostic": self.diagnostic,
        }


@dataclass(frozen=True)
class RunSample:
    run_dir: pathlib.Path
    key: GroupKey
    value: float


def utc_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def generated_at() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def t95_critical(df: float) -> float:
    if not math.isfinite(df) or df <= 0:
        raise ValueError("degrees of freedom must be positive and finite")
    if df >= 30:
        return 1.960
    return float(T95[max(1, math.floor(df))])


def numeric(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def flag(value: object) -> bool:
    """Read manifest booleans defensively without treating ``"false"`` as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return False


def diagnostic_of(manifest: dict, run_name: str) -> bool:
    declared = manifest.get("diagnostic")
    if declared is not None:
        return flag(declared)
    return run_name.endswith("-diagnostic")


def data_version_of(
    manifest: dict,
    name: str = "",
    details: dict | None = None,
    reward: dict | None = None,
) -> str:
    """Infer the score epoch, keeping compatibility with old manifests."""
    for prefix, version in VERSION_MAP.items():
        if name.startswith(prefix):
            return version
    declared = manifest.get("task_version") or manifest.get("data_version")
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    verifier_version = manifest.get("verifier_version")
    if verifier_version is None and isinstance(details, dict):
        verifier_version = details.get("verifier_version")
    if verifier_version == "sffd-harbor-verifier-0.2":
        return REFERENCE_TRACK_VERSION
    if isinstance(reward, dict) and all(
        numeric(reward.get(key)) is not None for key in REFERENCE_KEYS
    ):
        return REFERENCE_TRACK_VERSION
    return DEFAULT_DATA_VERSION


def is_polluted(manifest: dict, run_name: str) -> bool:
    if run_name.startswith(KNOWN_POLLUTED_PREFIXES):
        return True
    if any(flag(manifest.get(key)) for key in ("polluted", "contaminated")):
        return True
    lowered_name = run_name.lower()
    if "pollut" in lowered_name or "contaminat" in lowered_name:
        return True
    note_parts = [manifest.get("note"), manifest.get("notes")]
    text = " ".join(part for part in note_parts if isinstance(part, str)).lower()
    return "pollut" in text or "contaminat" in text or "\u6c61\u67d3" in text


def flatten_run_dirs(values: Sequence[Sequence[str]] | None) -> list[pathlib.Path]:
    if not values:
        return []
    return [pathlib.Path(item) for group in values for item in group]


def manifest_candidates(
    run_dirs: Sequence[pathlib.Path], scan_dir: pathlib.Path | None
) -> list[pathlib.Path]:
    candidates: list[pathlib.Path] = []
    if run_dirs:
        for path in run_dirs:
            candidates.append(path if path.name == "run-manifest.json" else path / "run-manifest.json")
    else:
        root = scan_dir or DEFAULT_SCAN_DIR
        if root.is_dir():
            candidates.extend(root.rglob("run-manifest.json"))
        elif root.exists():
            raise ValueError(f"scan path is not a directory: {root}")

    unique: dict[str, pathlib.Path] = {}
    for path in candidates:
        resolved = path.resolve(strict=False)
        unique[str(resolved)] = resolved
    return [unique[key] for key in sorted(unique)]


def exclusion(path: pathlib.Path, *reasons: str) -> dict[str, object]:
    return {"run_dir": str(path.parent), "reasons": list(reasons)}


def collect_samples(
    candidates: Sequence[pathlib.Path],
    *,
    metric: str = "overall",
    model: str | None = None,
    thinking: str | None = None,
    include_diagnostic: bool = False,
    require_verdict_ok: bool = True,
) -> tuple[list[RunSample], list[dict[str, object]], int]:
    samples: list[RunSample] = []
    excluded: list[dict[str, object]] = []
    filtered_out = 0

    for manifest_path in candidates:
        run_dir = manifest_path.parent
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            excluded.append(exclusion(manifest_path, "missing-manifest"))
            continue
        except (OSError, json.JSONDecodeError) as exc:
            excluded.append(exclusion(manifest_path, f"invalid-manifest:{type(exc).__name__}"))
            continue
        if not isinstance(manifest, dict):
            excluded.append(exclusion(manifest_path, "invalid-manifest:not-an-object"))
            continue

        run_model = manifest.get("model")
        run_thinking = manifest.get("thinking")
        if model is not None and run_model != model:
            filtered_out += 1
            continue
        if thinking is not None and run_thinking != thinking:
            filtered_out += 1
            continue

        diagnostic = diagnostic_of(manifest, run_dir.name)
        reasons: list[str] = []
        if diagnostic and not include_diagnostic:
            reasons.append("diagnostic-excluded")
        if require_verdict_ok and manifest.get("verdict") != "ok":
            reasons.append("verdict-not-ok")
        if flag(manifest.get("timed_out")):
            reasons.append("timed-out")
        if is_polluted(manifest, run_dir.name):
            reasons.append("polluted")

        details: dict = {}
        details_path = run_dir / "verifier" / "details.json"
        try:
            parsed_details = json.loads(details_path.read_text(encoding="utf-8"))
            if isinstance(parsed_details, dict):
                details = parsed_details
        except (OSError, json.JSONDecodeError):
            pass
        artifact_reward: dict = {}
        reward_path = run_dir / "verifier" / "reward.json"
        try:
            parsed_reward = json.loads(reward_path.read_text(encoding="utf-8"))
            if isinstance(parsed_reward, dict):
                artifact_reward = parsed_reward
        except (OSError, json.JSONDecodeError):
            pass

        reward = manifest.get("reward")
        # New manifests nest reward keys; accepting top-level keys as a
        # fallback keeps the aggregator useful for small hand-written fixtures
        # and early harness prototypes. If the manifest is partial, fill only
        # missing keys from the verifier artifact and retain manifest values.
        if isinstance(reward, dict):
            reward = {**artifact_reward, **reward}
        elif artifact_reward:
            reward = artifact_reward
        else:
            reward = manifest
        metric_value = numeric(reward.get(metric)) if isinstance(reward, dict) else None
        if metric_value is None:
            reasons.append("metric-missing-or-nonnumeric")
        if reasons:
            excluded.append(exclusion(manifest_path, *reasons))
            continue

        key = GroupKey(
            model=str(run_model) if run_model is not None else "?",
            thinking=str(run_thinking) if run_thinking is not None else "?",
            data_version=data_version_of(manifest, run_dir.name, details, reward),
            diagnostic=diagnostic,
        )
        samples.append(RunSample(run_dir=run_dir, key=key, value=metric_value))

    return samples, excluded, filtered_out


def summarize_values(values: Sequence[float]) -> dict[str, float | int | None]:
    if not values:
        raise ValueError("cannot summarize an empty sample")
    n = len(values)
    mean = statistics.fmean(values)
    std = statistics.stdev(values) if n >= 2 else None
    ci95 = t95_critical(n - 1) * std / math.sqrt(n) if std is not None else None
    return {
        "n": n,
        "mean": mean,
        "std": std,
        "min": min(values),
        "max": max(values),
        "ci95": ci95,
        "ci95_low": mean - ci95 if ci95 is not None else None,
        "ci95_high": mean + ci95 if ci95 is not None else None,
    }


def summarize_groups(samples: Iterable[RunSample]) -> list[dict[str, object]]:
    grouped: dict[GroupKey, list[RunSample]] = defaultdict(list)
    for sample in samples:
        grouped[sample.key].append(sample)

    summaries: list[dict[str, object]] = []
    for key in sorted(grouped):
        runs = sorted(grouped[key], key=lambda item: str(item.run_dir))
        stats = summarize_values([item.value for item in runs])
        summaries.append(
            {
                "id": key.identifier,
                "key": key.as_dict(),
                **stats,
                "runs": [
                    {"run_dir": str(item.run_dir), "value": item.value} for item in runs
                ],
            }
        )
    # Keep the JSON deterministic, while putting higher-mean groups first
    # inside each version/mode partition so the first row is the natural rank.
    summaries.sort(
        key=lambda item: (
            item["key"]["data_version"],
            item["key"]["diagnostic"],
            -float(item["mean"]),
            item["id"],
        )
    )
    for partition in {
        (item["key"]["data_version"], item["key"]["diagnostic"])
        for item in summaries
    }:
        members = [
            item
            for item in summaries
            if (item["key"]["data_version"], item["key"]["diagnostic"]) == partition
        ]
        if not members:
            continue
        top = members[0]
        for item in members:
            item["vs_top"] = (
                "top" if item is top else welch_comparison(item, top)["status"]
            )
            item["vs_top_group"] = top["id"]
    return summaries


def welch_comparison(
    first: dict[str, object], second: dict[str, object]
) -> dict[str, object]:
    n1 = int(first["n"])
    n2 = int(second["n"])
    mean1 = float(first["mean"])
    mean2 = float(second["mean"])
    delta = mean1 - mean2
    result: dict[str, object] = {
        "group_a": first["id"],
        "group_b": second["id"],
        "delta_mean": delta,
        "standard_error": None,
        "welch_t": None,
        "degrees_of_freedom": None,
        "critical_value": None,
        "margin": None,
        "status": "insufficient-sample",
        "winner": None,
    }
    if n1 < 3 or n2 < 3:
        return result

    std1 = float(first["std"])
    std2 = float(second["std"])
    variance_term1 = std1 * std1 / n1
    variance_term2 = std2 * std2 / n2
    standard_error = math.sqrt(variance_term1 + variance_term2)
    result["standard_error"] = standard_error

    if standard_error == 0:
        significant = not math.isclose(delta, 0.0, rel_tol=0.0, abs_tol=0.0)
        result["margin"] = 0.0
        result["status"] = "significant" if significant else "not-significant"
    else:
        denominator = 0.0
        if variance_term1:
            denominator += variance_term1 * variance_term1 / (n1 - 1)
        if variance_term2:
            denominator += variance_term2 * variance_term2 / (n2 - 1)
        degrees_of_freedom = (
            (variance_term1 + variance_term2) ** 2 / denominator
            if denominator
            else math.inf
        )
        critical = t95_critical(degrees_of_freedom)
        margin = critical * standard_error
        result.update(
            {
                "welch_t": delta / standard_error,
                "degrees_of_freedom": degrees_of_freedom,
                "critical_value": critical,
                "margin": margin,
                "status": (
                    "not-significant" if abs(delta) <= margin else "significant"
                ),
            }
        )

    if result["status"] == "significant":
        result["winner"] = first["id"] if delta > 0 else second["id"]
    return result


def compare_groups(groups: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    comparisons: list[dict[str, object]] = []
    for first, second in itertools.combinations(groups, 2):
        key1 = first["key"]
        key2 = second["key"]
        if not isinstance(key1, dict) or not isinstance(key2, dict):
            continue
        # Cross-version and diagnostic-vs-evaluation claims are invalid by
        # construction, so no comparison row is emitted for those pairs.
        if key1["data_version"] != key2["data_version"]:
            continue
        if key1["diagnostic"] != key2["diagnostic"]:
            continue
        comparisons.append(welch_comparison(first, second))
    return comparisons


def fmt(value: object, digits: int = 6) -> str:
    if value is None:
        return "--"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def md_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, object]) -> str:
    groups = report["groups"]
    comparisons = report["comparisons"]
    lines = [
        f"# Pi run aggregate ({report['metric']})",
        "",
        "| model | thinking | data version | mode | n | mean +/- 95% CI | std | min | max | vs top |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    if isinstance(groups, list) and groups:
        for group in groups:
            key = group["key"]
            ci = group["ci95"]
            mean_ci = fmt(group["mean"]) + (f" +/- {fmt(ci)}" if ci is not None else "")
            lines.append(
                "| {model} | {thinking} | {version} | {mode} | {n} | {mean_ci} | "
                "{std} | {minimum} | {maximum} | {vs_top} |".format(
                    model=md_cell(key["model"]),
                    thinking=md_cell(key["thinking"]),
                    version=md_cell(key["data_version"]),
                    mode="diagnostic" if key["diagnostic"] else "evaluation",
                    n=group["n"],
                    mean_ci=mean_ci,
                    std=fmt(group["std"]),
                    minimum=fmt(group["min"]),
                    maximum=fmt(group["max"]),
                    vs_top=group.get("vs_top", "--"),
                )
            )
    else:
        lines.append("| -- | -- | -- | -- | 0 | -- | -- | -- | -- | -- |")

    lines.extend(
        [
            "",
            "## Welch comparisons",
            "",
            "| group A | group B | delta mean | df | margin | result | winner |",
            "|---|---|---:|---:|---:|---|---|",
        ]
    )
    if isinstance(comparisons, list) and comparisons:
        for item in comparisons:
            lines.append(
                "| {a} | {b} | {delta} | {df} | {margin} | {status} | {winner} |".format(
                    a=md_cell(item["group_a"]),
                    b=md_cell(item["group_b"]),
                    delta=fmt(item["delta_mean"]),
                    df=fmt(item["degrees_of_freedom"], 3),
                    margin=fmt(item["margin"]),
                    status=item["status"],
                    winner=md_cell(item["winner"] or "--"),
                )
            )
    else:
        lines.append("| -- | -- | -- | -- | -- | no-comparable-groups | -- |")

    counts = report["counts"]
    lines.extend(
        [
            "",
            "Discovered: {discovered}; selected: {selected}; included: {included}; "
            "excluded: {excluded}; filtered out: {filtered}.".format(
                discovered=counts["discovered"],
                selected=counts["selected"],
                included=counts["included"],
                excluded=counts["excluded"],
                filtered=counts["filtered_out"],
            ),
        ]
    )
    return "\n".join(lines)


def write_json(path: pathlib.Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def build_report(
    *,
    candidates: Sequence[pathlib.Path],
    samples: Sequence[RunSample],
    exclusions: Sequence[dict[str, object]],
    filtered_out: int,
    metric: str = "overall",
    model: str | None = None,
    thinking: str | None = None,
    include_diagnostic: bool = False,
    require_verdict_ok: bool = True,
    source: dict[str, object] | None = None,
) -> dict[str, object]:
    groups = summarize_groups(samples)
    comparisons = compare_groups(groups)
    return {
        "schema_version": "abench-pi-aggregate/v1",
        "generated_at": generated_at(),
        "metric": metric,
        "source": source or {},
        "filters": {
            "model": model,
            "thinking": thinking,
            "include_diagnostic": include_diagnostic,
            "required_verdict": "ok" if require_verdict_ok else None,
            "exclude_timed_out": True,
            "exclude_polluted": True,
        },
        "statistics": {
            "std": "sample standard deviation (ddof=1)",
            "ci95": "two-sided Student t margin; df>=30 uses 1.960",
            "comparison": "two-sided Welch threshold; abs(delta_mean) <= margin is not-significant",
            "minimum_comparison_n_per_group": 3,
        },
        "counts": {
            "discovered": len(candidates),
            "selected": len(candidates) - filtered_out,
            "included": len(samples),
            "excluded": len(exclusions),
            "filtered_out": filtered_out,
        },
        "groups": groups,
        "comparisons": comparisons,
        "excluded_runs": list(exclusions),
    }


def write_synthetic_manifest(
    root: pathlib.Path,
    name: str,
    *,
    model: str,
    thinking: str,
    value: float | None,
    version: str = "1.1.0",
    diagnostic: bool = False,
    verdict: str = "ok",
    timed_out: bool = False,
    polluted: bool = False,
) -> pathlib.Path:
    run_dir = root / name
    run_dir.mkdir()
    reward = {} if value is None else {"overall": value}
    manifest = {
        "model": model,
        "thinking": thinking,
        "task_version": version,
        "diagnostic": diagnostic,
        "verdict": verdict,
        "timed_out": timed_out,
        "polluted": polluted,
        "reward": reward,
    }
    path = run_dir / "run-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="aggregate-pi-runs-test-") as temporary:
        root = pathlib.Path(temporary)
        paths: list[pathlib.Path] = []

        for index, value in enumerate((1.0, 2.0, 3.0), 1):
            paths.append(
                write_synthetic_manifest(
                    root, f"alpha-{index}", model="alpha", thinking="max", value=value
                )
            )
        for index, value in enumerate((1.1, 2.1, 3.1), 1):
            paths.append(
                write_synthetic_manifest(
                    root, f"beta-{index}", model="beta", thinking="max", value=value
                )
            )
        for index, value in enumerate((10.0, 10.1, 10.2), 1):
            paths.append(
                write_synthetic_manifest(
                    root, f"high-{index}", model="high", thinking="max", value=value
                )
            )
        for index, value in enumerate((4.0, 4.2), 1):
            paths.append(
                write_synthetic_manifest(
                    root, f"small-{index}", model="small", thinking="max", value=value
                )
            )
        paths.extend(
            [
                write_synthetic_manifest(
                    root, "diagnostic", model="diag", thinking="low", value=0.5, diagnostic=True
                ),
                write_synthetic_manifest(
                    root, "bad-verdict", model="bad", thinking="max", value=0.5, verdict="error_stop"
                ),
                write_synthetic_manifest(
                    root, "timeout", model="bad", thinking="max", value=0.5, timed_out=True
                ),
                write_synthetic_manifest(
                    root, "polluted", model="bad", thinking="max", value=0.5, polluted=True
                ),
                write_synthetic_manifest(
                    root, "missing-metric", model="bad", thinking="max", value=None
                ),
                write_synthetic_manifest(
                    root, "new-version", model="alpha", thinking="max", value=9.0, version="2.0.0"
                ),
            ]
        )

        samples, exclusions, filtered = collect_samples(
            paths,
            metric="overall",
            model=None,
            thinking=None,
            include_diagnostic=False,
            require_verdict_ok=True,
        )
        assert filtered == 0
        assert len(exclusions) == 5
        groups = summarize_groups(samples)
        alpha = next(item for item in groups if item["id"] == "alpha|max|1.1.0|evaluation")
        assert alpha["n"] == 3
        high_summary = next(item for item in groups if item["id"] == "high|max|1.1.0|evaluation")
        assert high_summary["vs_top"] == "top"
        assert math.isclose(float(alpha["mean"]), 2.0)
        assert math.isclose(float(alpha["std"]), 1.0)
        assert math.isclose(float(alpha["ci95"]), 4.303 / math.sqrt(3))

        comparisons = compare_groups(groups)
        alpha_beta = next(
            item
            for item in comparisons
            if {item["group_a"], item["group_b"]}
            == {"alpha|max|1.1.0|evaluation", "beta|max|1.1.0|evaluation"}
        )
        assert alpha_beta["status"] == "not-significant"
        alpha_high = next(
            item
            for item in comparisons
            if {item["group_a"], item["group_b"]}
            == {"alpha|max|1.1.0|evaluation", "high|max|1.1.0|evaluation"}
        )
        assert alpha_high["status"] == "significant"
        alpha_small = next(
            item
            for item in comparisons
            if {item["group_a"], item["group_b"]}
            == {"alpha|max|1.1.0|evaluation", "small|max|1.1.0|evaluation"}
        )
        assert alpha_small["status"] == "insufficient-sample"
        assert not any("2.0.0" in str(item) and "1.1.0" in str(item) for item in comparisons)

        with_diagnostic, _, _ = collect_samples(
            paths,
            metric="overall",
            model=None,
            thinking=None,
            include_diagnostic=True,
            require_verdict_ok=True,
        )
        assert any(item.key.diagnostic for item in with_diagnostic)
        alpha_only, _, alpha_filtered = collect_samples(
            paths,
            metric="overall",
            model="alpha",
            thinking="max",
            include_diagnostic=False,
            require_verdict_ok=True,
        )
        assert alpha_filtered > 0
        assert all(item.key.model == "alpha" for item in alpha_only)
        non_ok, _, _ = collect_samples(
            paths,
            metric="overall",
            model="bad",
            thinking="max",
            include_diagnostic=False,
            require_verdict_ok=False,
        )
        # The timeout/pollution/missing-metric fixtures remain excluded, while
        # the non-ok verdict-only fixture is admitted by this opt-out.
        assert len([item for item in non_ok if item.key.model == "bad"]) == 1
        bad_verdict = write_synthetic_manifest(
            root, "bad-only-verdict", model="bad", thinking="max", value=0.7, verdict="error_stop"
        )
        non_ok, _, _ = collect_samples(
            [bad_verdict],
            metric="overall",
            model=None,
            thinking=None,
            include_diagnostic=False,
            require_verdict_ok=False,
        )
        assert len(non_ok) == 1
        # Early 1.1.0 manifests did not yet carry task_version; the verifier
        # artifact must still keep them out of the 1.0.0 epoch.
        artifact_dir = root / "artifact-new" / "verifier"
        artifact_dir.mkdir(parents=True)
        artifact_manifest = artifact_dir.parent / "run-manifest.json"
        artifact_manifest.write_text(
            json.dumps({
                "model": "artifact",
                "thinking": "max",
                "verdict": "ok",
                "reward": {"overall": 0.4},
            }),
            encoding="utf-8",
        )
        artifact_reward = {key: 0.1 for key in REFERENCE_KEYS}
        artifact_reward["overall"] = 0.4
        (artifact_dir / "reward.json").write_text(json.dumps(artifact_reward), encoding="utf-8")
        (artifact_dir / "details.json").write_text(
            json.dumps({"verifier_version": "sffd-harbor-verifier-0.2"}), encoding="utf-8"
        )
        artifact_samples, _, _ = collect_samples([artifact_manifest])
        assert artifact_samples[0].key.data_version == "1.1.0"
        assert t95_critical(30) == 1.960

    print("aggregate_pi_runs.py: self-test passed")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate valid pi run manifests and perform Welch comparisons."
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--run-dir",
        action="append",
        nargs="+",
        metavar="DIR",
        help="run directory (or manifest path); may be repeated",
    )
    source.add_argument(
        "--scan",
        type=pathlib.Path,
        metavar="DIR",
        help=f"recursively scan a directory (default: {DEFAULT_SCAN_DIR})",
    )
    parser.add_argument("--model", help="include only this exact manifest model")
    parser.add_argument("--thinking", help="include only this exact thinking level")
    parser.add_argument(
        "--metric",
        default="overall",
        help="numeric reward key to aggregate (default: overall)",
    )
    parser.add_argument(
        "--include-diagnostic",
        action="store_true",
        help="include diagnostic runs, in separate groups",
    )
    parser.add_argument(
        "--no-require-verdict-ok",
        action="store_true",
        help="allow non-ok verdicts (timeouts and polluted runs remain excluded)",
    )
    parser.add_argument(
        "--output",
        type=pathlib.Path,
        metavar="FILE",
        help="JSON output path (default: results/pi-runs/_batch/aggregate-<stamp>.json)",
    )
    parser.add_argument(
        "--self-test", action="store_true", help="run synthetic-data assertions and exit"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    if not args.metric.strip():
        print("aggregate_pi_runs.py: --metric must not be empty", file=sys.stderr)
        return 2

    run_dirs = flatten_run_dirs(args.run_dir)
    try:
        candidates = manifest_candidates(run_dirs, args.scan)
    except ValueError as exc:
        print(f"aggregate_pi_runs.py: {exc}", file=sys.stderr)
        return 2

    samples, exclusions, filtered_out = collect_samples(
        candidates,
        metric=args.metric,
        model=args.model,
        thinking=args.thinking,
        include_diagnostic=args.include_diagnostic,
        require_verdict_ok=not args.no_require_verdict_ok,
    )
    source = (
        {"kind": "run-dir", "paths": [str(path.resolve(strict=False)) for path in run_dirs]}
        if run_dirs
        else {"kind": "scan", "path": str((args.scan or DEFAULT_SCAN_DIR).resolve(strict=False))}
    )
    report = build_report(
        candidates=candidates,
        samples=samples,
        exclusions=exclusions,
        filtered_out=filtered_out,
        metric=args.metric,
        model=args.model,
        thinking=args.thinking,
        include_diagnostic=args.include_diagnostic,
        require_verdict_ok=not args.no_require_verdict_ok,
        source=source,
    )
    output = args.output or (
        DEFAULT_SCAN_DIR / "_batch" / f"aggregate-{utc_stamp()}.json"
    )
    if output.exists() and output.is_dir():
        output = output / f"aggregate-{utc_stamp()}.json"
    try:
        write_json(output, report)
    except OSError as exc:
        print(f"aggregate_pi_runs.py: cannot write {output}: {exc}", file=sys.stderr)
        return 1

    print(render_markdown(report))
    print(f"\nJSON: {output}")
    if not samples:
        print("aggregate_pi_runs.py: no eligible runs", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
