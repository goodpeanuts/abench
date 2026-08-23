#!/usr/bin/env python3
"""build_score_explorer.py — 从 results/pi-runs 生成 docs/score-explorer.html

收集每次运行的 run-manifest.json、verifier/reward.json（六维分项 + 参考轨）、
verifier/details.json（原始证据指标）、finish 时点与 token 用量，内嵌为 JSON，
生成一个双计分口径（官方平方+门控 / 参考一次方未门控）、权重可调、实时重算
overall 的单文件 HTML（无外部依赖）。

用法：python3 scripts/build_score_explorer.py
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime

REPO = pathlib.Path(__file__).resolve().parent.parent
RUNS = REPO / "results" / "pi-runs"
OUT = REPO / "docs" / "score-explorer.html"

# 特定跑次的人工标注（按目录名前缀匹配）
NOTES = {
    "20260822T174859": "污染跑：prompt heredoc bug 误嵌 finish 结果",
    "20260822T181604": "watchdog 负例（--timeout 30）",
    "20260822T190840": "wodex 网关 403，未到模型",
    "20260822T190841": "wodex 网关 403，未到模型",
}

# 断代默认：升级 harness（task_version 写入 manifest）之前的旧跑一律归入
# 该版本。manifest 带 task_version 的新跑以其声明为准；VERSION_MAP 仅用于
# 对个别跑次做手工覆盖（目录名前缀 -> 版本）。
LEGACY_DEFAULT_VERSION = "1.0.0"
# Verifier 0.2 introduced the reference-track fields at the same epoch as the
# task 1.1.0 contract. A few runs were produced after that change but before
# the harness began writing task_version into run-manifest.json, so the
# verifier/details and reward artifacts are also accepted as version evidence.
REFERENCE_TRACK_VERSION = "1.1.0"
VERSION_MAP: dict[str, str] = {
    # 示例："20260901T000000": "1.1.0",
}

DIMENSION_KEYS = ["response", "coverage", "safety", "adaptation", "efficiency", "integrity"]
# 参考轨六维键名与官方轨对齐（response 一次方、三维未门控），复用同一套
# 权重滑杆；旧版 verifier 产出的 reward.json 没有这些键 -> dims_ref = None。
REFERENCE_KEYS = ["response_linear", "coverage", "safety_ungated",
                  "adaptation_ungated", "efficiency_ungated", "integrity"]


def data_version_of(
    manifest: dict,
    name: str,
    details: dict | None = None,
    reward: dict | None = None,
) -> str:
    """Return the score/data epoch for one run.

    Directory-prefix overrides are deliberately checked first: VERSION_MAP is
    the escape hatch for correcting an individual historical run even when a
    manifest contains a stale declared version. Older manifests may lack both
    task_version and verifier_version; the reference-track keys provide a
    stable artifact-level marker for the 1.1.0 epoch in that case.
    """
    for prefix, version in VERSION_MAP.items():
        if name.startswith(prefix):
            return version
    declared = manifest.get("task_version")
    if isinstance(declared, str) and declared:
        return declared
    verifier_version = (details or {}).get("verifier_version")
    if verifier_version == "sffd-harbor-verifier-0.2":
        return REFERENCE_TRACK_VERSION
    if isinstance(reward, dict) and all(
        isinstance(reward.get(key), (int, float)) for key in REFERENCE_KEYS
    ):
        return REFERENCE_TRACK_VERSION
    return LEGACY_DEFAULT_VERSION


def usage_from_events(events_path: pathlib.Path) -> dict | None:
    """从 pi-events.jsonl 聚合 token 用量（口径与 run-pi-harness.sh 一致）。

    只按 turn_end.message.usage 聚合——每回合恰好一条；message_start/
    message_update/message_end 会对同一次调用重复携带 usage，流式 partial
    还会出现多次，混入会严重重复计数。旧版 manifest 无 usage 字段，这里
    为旧跑回填。
    """
    usage = {
        "turns": 0, "tool_calls": 0,
        "tokens_output_total": 0, "tokens_reasoning_total": 0,
        "tokens_input_total": 0, "context_final": None,
    }
    try:
        handle = open(events_path, encoding="utf-8")
    except OSError:
        return None
    with handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "tool_execution_start":
                usage["tool_calls"] += 1
                continue
            if kind != "turn_end":
                continue
            usage["turns"] += 1
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            u = message.get("usage") if isinstance(message.get("usage"), dict) else None
            if u is None:
                continue
            usage["tokens_output_total"] += int(u.get("output") or 0)
            usage["tokens_reasoning_total"] += int(u.get("reasoning") or 0)
            input_side = int(u.get("input") or 0) + int(u.get("cacheRead") or 0) + int(u.get("cacheWrite") or 0)
            usage["tokens_input_total"] += input_side
            usage["context_final"] = input_side
    return usage


def action_metrics(actions_path: pathlib.Path) -> dict:
    """Return cheap process counters from the authoritative sidecar log."""
    counts = {
        "advance_count": 0,
        "write_count": 0,
        "finish_count": 0,
    }
    try:
        handle = open(actions_path, encoding="utf-8")
    except OSError:
        return counts
    with handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            command = record.get("command")
            if not isinstance(command, dict):
                continue
            name = command.get("command")
            if name == "advance":
                counts["advance_count"] += 1
            elif name == "finish":
                counts["finish_count"] += 1
            elif name in {"dispatch", "assign", "reassign", "transfer", "reposition"}:
                counts["write_count"] += 1
    return counts


def wall_clock_seconds(manifest: dict) -> float | None:
    """Compute elapsed wall time from the UTC manifest timestamps."""
    started = manifest.get("started_at")
    finished = manifest.get("finished_at")
    if not isinstance(started, str) or not isinstance(finished, str):
        return None
    try:
        start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
        finish_dt = datetime.fromisoformat(finished.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (finish_dt - start_dt).total_seconds())


def collect() -> list[dict]:
    rows = []
    for run_dir in sorted(RUNS.iterdir()) if RUNS.is_dir() else []:
        manifest_path = run_dir / "run-manifest.json"
        reward_path = run_dir / "verifier" / "reward.json"
        details_path = run_dir / "verifier" / "details.json"
        if not (manifest_path.is_file() and reward_path.is_file() and details_path.is_file()):
            continue
        manifest = json.loads(manifest_path.read_text())
        reward = json.loads(reward_path.read_text())
        details = json.loads(details_path.read_text())
        evidence = details.get("evidence", {})

        finish_at = None
        saw_finish = False
        actions_path = run_dir / "state" / "actions.jsonl"
        if actions_path.is_file():
            for line in actions_path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    action = json.loads(line)
                except json.JSONDecodeError:
                    continue
                command = action.get("command")
                if isinstance(command, dict) and command.get("command") == "finish":
                    saw_finish = True
                    finish_at = action.get("sim_time")

        # token 用量：manifest.usage（新跑）优先，旧跑从 pi-events.jsonl 回填
        usage = manifest.get("usage")
        if not isinstance(usage, dict) or not usage.get("turns"):
            usage = usage_from_events(run_dir / "pi-events.jsonl") or {}

        process = manifest.get("process")
        action_process = action_metrics(actions_path)
        if not isinstance(process, dict):
            process = action_process
        else:
            # A partially written manifest must not erase counters recoverable
            # from the authoritative sidecar log.
            process = {
                key: process.get(key) if isinstance(process.get(key), (int, float))
                else fallback
                for key, fallback in action_process.items()
            }

        ref_values = [reward.get(key) for key in REFERENCE_KEYS]
        dims_ref = None
        if all(isinstance(v, (int, float)) for v in ref_values):
            dims_ref = {
                "response": reward["response_linear"],
                "coverage": reward["coverage"],
                "safety": reward["safety_ungated"],
                "adaptation": reward["adaptation_ungated"],
                "efficiency": reward["efficiency_ungated"],
                "integrity": reward["integrity"],
            }

        name = run_dir.name
        verdict = manifest.get("verdict", "unknown")
        invalid = verdict != "ok"
        completed = evidence.get("completed_weight")
        total_weight = evidence.get("total_priority_weight")
        completion_rate = reward.get("completion_rate")
        if not isinstance(completion_rate, (int, float)):
            if isinstance(completed, (int, float)) and isinstance(total_weight, (int, float)) and total_weight:
                completion_rate = completed / total_weight
            else:
                completion_rate = None
        rows.append({
            "dir": name,
            "stamp": name[:15],
            "model": manifest.get("model", "?"),
            "provider": manifest.get("provider", "?"),
            "thinking": manifest.get("thinking", "?"),
            "harness": manifest.get("harness", "?"),
            "data_version": data_version_of(manifest, name, details, reward),
            "diagnostic": bool(manifest.get("diagnostic")) or name.endswith("-diagnostic"),
            "verdict": verdict,
            "timed_out": bool(manifest.get("timed_out")),
            "actions": details.get("action_count"),
            "covered": evidence.get("covered_weight"),
            "completed": completed,
            "total_weight": total_weight,
            "completion_rate": completion_rate,
            "high_missed": evidence.get("high_priority_missed"),
            "late": evidence.get("late_count"),
            "invalid_actions": evidence.get("scored_invalid_attempts"),
            "hard_failure": bool(evidence.get("hard_failure")),
            "finish_at": finish_at,
            "saw_finish": saw_finish,
            "dims": {key: reward.get(key) for key in DIMENSION_KEYS},
            "dims_ref": dims_ref,
            "overall_official": reward.get("overall"),
            "overall_reference": reward["overall_reference"] if isinstance(reward.get("overall_reference"), (int, float)) else None,
            "strict_pass": reward.get("strict_pass"),
            "usage": {
                "turns": usage.get("turns"),
                "tool_calls": usage.get("tool_calls"),
                "tokens_output_total": usage.get("tokens_output_total"),
                "tokens_input_total": usage.get("tokens_input_total"),
                "context_final": usage.get("context_final"),
            },
            "advance_count": process.get("advance_count"),
            "write_count": process.get("write_count"),
            "finish_count": process.get("finish_count"),
            "wall_clock_seconds": manifest.get("wall_clock_seconds")
                if isinstance(manifest.get("wall_clock_seconds"), (int, float))
                else wall_clock_seconds(manifest),
            "note": next((note for prefix, note in NOTES.items() if name.startswith(prefix)), ""),
            "invalid": invalid,
        })
    return rows


TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SFFD 分数探索器 — 历史跑次 × 双口径 × 可调权重</title>
<style>
  :root {
    --bg: #f6f7f9; --card: #ffffff; --ink: #1c2733; --muted: #66727f;
    --line: #dfe4ea; --accent: #2563eb; --accent-soft: #dbeafe;
    --good: #15803d; --bad: #b91c1c; --warn: #b45309;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #10151b; --card: #1a212b; --ink: #e6ebf1; --muted: #93a1af;
      --line: #2c3641; --accent: #60a5fa; --accent-soft: #1e3a5f;
      --good: #4ade80; --bad: #f87171; --warn: #fbbf24;
    }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink);
         font: 14px/1.55 ui-sans-serif, system-ui, "PingFang SC", "Microsoft YaHei", sans-serif; }
  .wrap { max-width: 1280px; margin: 0 auto; padding: 28px 20px 60px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: var(--muted); margin: 0 0 20px; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 16px 18px; margin-bottom: 18px; }
  .controls { display: grid; grid-template-columns: repeat(auto-fit, minmax(165px, 1fr)); gap: 14px; }
  .ctl label { display: flex; justify-content: space-between; font-weight: 600; font-size: 13px; margin-bottom: 4px; }
  .ctl .share { color: var(--muted); font-weight: 400; }
  .ctl input[type=range] { width: 100%; accent-color: var(--accent); }
  .ctl .formula { color: var(--muted); font-size: 12px; min-height: 30px; }
  .btnrow { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-top: 14px; }
  button { background: var(--accent); border: none; color: #fff; padding: 7px 14px; border-radius: 8px; cursor: pointer; font-size: 13px; }
  button.ghost { background: transparent; border: 1px solid var(--line); color: var(--ink); }
  label.chk { display: inline-flex; gap: 6px; align-items: center; font-size: 13px; color: var(--muted); cursor: pointer; }
  label.chk.disabled { opacity: .45; cursor: not-allowed; }
  table { border-collapse: collapse; width: 100%; font-size: 12.5px; }
  th, td { border-bottom: 1px solid var(--line); padding: 6px 8px; text-align: right; white-space: nowrap; }
  th { position: sticky; top: 0; background: var(--card); cursor: pointer; user-select: none; font-size: 12px; color: var(--muted); }
  th:first-child, td:first-child, th.txt, td.txt { text-align: left; }
  tr.invalid-row td { opacity: 0.45; }
  tr.timeout-row td { opacity: 0.6; }
  .bar { display: inline-block; height: 9px; border-radius: 4px; background: var(--accent); vertical-align: middle; margin-left: 6px; }
  .pill { display: inline-block; padding: 1px 7px; border-radius: 999px; font-size: 11px; border: 1px solid var(--line); color: var(--muted); }
  .pill.kimi { border-color: #7c3aed; color: #7c3aed; }
  .pill.qwen { border-color: #0891b2; color: #0891b2; }
  .pill.claude { border-color: #b45309; color: #b45309; }
  .pill.gpt { border-color: #15803d; color: #15803d; }
  .pill.deepseek { border-color: #db2777; color: #db2777; }
  .pill.other { border-color: #64748b; color: #64748b; }
  .pill.think-off     { border-color: #475569; color: #475569; }
  .pill.think-low     { border-color: #64748b; color: #64748b; }
  .pill.think-medium  { border-color: #2563eb; color: #2563eb; }
  .pill.think-high    { border-color: #7c3aed; color: #7c3aed; }
  .pill.think-xhigh,
  .pill.think-max     { border-color: #dc2626; color: #dc2626; background: rgba(220,38,38,.08); }
  tr.row-kimi   { background: rgba(147, 51, 234, .07); }
  tr.row-qwen   { background: rgba(6, 182, 212, .07); }
  tr.row-claude { background: rgba(217, 119, 6, .08); }
  tr.row-gpt    { background: rgba(22, 163, 74, .07); }
  tr.row-deepseek { background: rgba(219, 39, 119, .07); }
  tr.row-other  { background: rgba(100, 116, 139, .06); }
  select { background: var(--card); color: var(--ink); border: 1px solid var(--line); border-radius: 6px; padding: 3px 6px; font-size: 12px; }
  select:disabled { opacity: .45; }
  .rank { font-weight: 700; color: var(--muted); }
  .rank-1 { color: var(--good); }
  .note-cell { color: var(--warn); font-size: 11px; max-width: 220px; white-space: normal; }
  .legend { color: var(--muted); font-size: 12px; margin-top: 8px; }
  .modehint { color: var(--warn); font-size: 12px; }
  .delta-up { color: var(--good); } .delta-down { color: var(--bad); }
  h2 { font-size: 15px; margin: 0 0 10px; }
  code { background: var(--accent-soft); border-radius: 4px; padding: 0 4px; font-size: 12px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>SFFD 分数探索器</h1>
  <p class="sub">历史跑次原始指标内嵌；两种计分口径可切换，拖动权重滑杆实时重算（权重自动归一化）。<br>
  <b>官方口径（平方+门控）</b> = 0.40·response(q²) + 0.20·coverage + 0.15·safety×gate + 0.10·adaptation×gate + 0.10·efficiency×gate + 0.05·integrity，
  gate = response × coverage——这是唯一用于排名的官方分（排序尺，绝对值非掌握度百分比）。<br>
  <b>参考口径（一次方·未门控）</b> 由 verifier 0.2+ 直出（response_linear 与三维未门控底值），仅供直观解读；旧版本跑次无此数据，参考口径对其禁选。</p>

  <div class="card">
    <div class="controls" id="controls"></div>
    <div class="btnrow">
      <button id="reset">恢复官方权重</button>
      <button class="ghost" id="equal">等权重</button>
      <label class="chk">计分口径：<select id="modeSel">
        <option value="official">官方（平方+门控）</option>
        <option value="reference">参考（一次方·未门控）</option>
      </select></label>
      <label class="chk" id="gateOffWrap"><input type="checkbox" id="gateOff"> 假设分析：去掉 service gate（仅官方口径）</label>
      <label class="chk"><input type="checkbox" id="hideInvalid" checked> 表格中灰显无效/超时/污染跑</label>
      <label class="chk"><input type="checkbox" id="onlyValid" checked> 汇总只计正式有效跑（剔除诊断、403 无效、超时负例、污染跑）</label>
      <label class="chk">思考档筛选（最高档合并）：<select id="thinkFilter"></select></label>
      <label class="chk">数据版本：<select id="versionFilter"></select></label>
      <button class="ghost" id="exportCsv">导出 CSV（当前口径与权重）</button>
      <span class="modehint" id="modeHint"></span>
    </div>
  </div>

  <div class="card">
    <h2>分组汇总（当前口径与权重下的动态 overall，mean ± 95% CI）</h2>
    <div style="overflow-x:auto;">
    <table id="summary"><thead><tr>
      <th>#</th><th class="txt">模型</th><th class="txt">思考档</th><th>样本</th><th>均值 ± CI</th><th>std</th><th>最低</th><th>最高</th><th>官方均值</th><th>参考均值</th><th>平均动作</th><th>平均tok出</th><th>平均tok入</th>
    </tr></thead><tbody></tbody></table>
    </div>
  </div>

  <div class="card">
    <h2>全部跑次（点表头排序；「动态」列为当前口径与权重重算值）</h2>
    <div style="overflow-x:auto; max-height: 70vh; overflow-y:auto;">
    <table id="runs"><thead><tr>
      <th class="txt">时间</th><th class="txt">模型</th><th>档</th><th class="txt">模式</th><th class="txt">版本</th>
      <th>覆盖</th><th>完成</th><th>完成率</th><th>高漏</th><th>迟到</th><th>无效写</th><th>动作</th><th>推进</th><th>写命令</th><th>finish次数</th><th>回合</th><th>工具调用</th><th>tok出</th><th>tok入</th><th>ctx终</th><th>墙钟</th><th>finish@</th>
      <th>resp</th><th>cov</th><th>saf</th><th>adap</th><th>effi</th><th>integ</th>
      <th>官方</th><th>参考</th><th>动态</th><th>Δ</th><th class="txt">备注</th>
    </tr></thead><tbody></tbody></table>
    </div>
    <p class="legend">覆盖/完成 = 权重比（分母为可派事故总权重）；完成率 = 完成权重/总权重（verifier 0.2+ 直出）；
    finish@ = finish 时的模拟时间 / 7200；resp/cov/…/integ 为六维官方原值；参考 = overall_reference（一次方口径总分，旧版跑为 —）。
    Δ = 动态 −（当前口径的官方权重总分）。hard_failure 的跑在任何口径下动态值恒为 0。
    推进/写命令 = sidecar 动作日志计数；墙钟 = manifest.started_at 到 finished_at。tok出 = Σ各回合输出 tokens；tok入 = Σ各回合输入侧 tokens（input+cache 读写，按回合计费口径累加）；ctx终 = 最后一回合的上下文规模。过程指标不参与计分。
    行底色区分模型（紫=kimi、青=qwen、橙=claude、绿=gpt、粉=deepseek、灰=其他）；思考档标签：灰=low、蓝=medium、紫=high、红=最高档（max / xhigh）。
    汇总表按当前口径与权重下的动态均值实时降序排名；CI 为 t 分布 95% 置信区间（n&lt;2 显示 —）。参考口径下无参考数据的旧版本跑不参与汇总。
    排行只在同一数据版本内比较：版本下拉不提供「全部版本」，默认选中最新版本。</p>
  </div>
</div>

<script>
const DATA = __DATA__;
const DIMS = [
  ["response",    "response 响应质量", "官方: Σw·q²/Σw；参考: Σw·q/Σw"],
  ["coverage",    "coverage 及时覆盖", "Σw·q（仅计已完成）/Σw"],
  ["safety",      "safety 操作有效性", "(1−无效尝试率)×gate；灾难→0"],
  ["adaptation",  "adaptation 动态适应", "重规划覆盖度×gate"],
  ["efficiency",  "efficiency 运营经济", "(1−超额churn−无效/2)×gate"],
  ["integrity",   "integrity 回放完整性", "重放通过=1，作弊→归零"],
];
const OFFICIAL = {response:.40, coverage:.20, safety:.15, adaptation:.10, efficiency:.10, integrity:.05};
let weights = {...OFFICIAL};
let scoreMode = "official";   // "official" | "reference"
let sortKey = null, sortAsc = false;

// t 分布 95% 双侧临界值（df 1..29，df>=30 用正态 1.960）
const T95 = [12.706,4.303,3.182,2.776,2.571,2.447,2.365,2.306,2.262,2.228,
             2.201,2.179,2.160,2.145,2.131,2.120,2.110,2.101,2.093,2.086,
             2.080,2.074,2.069,2.064,2.060,2.056,2.052,2.048,2.045];
function t95(df) { return df >= 30 ? 1.960 : T95[Math.max(0, df-1)]; }

const fmt = (v, d=3) => (v===null||v===undefined) ? "—" : Number(v).toFixed(d);
const fmtInt = (v) => (v===null||v===undefined) ? "—" : Number(v).toLocaleString();
function fmtTokens(v) {
  if (v===null||v===undefined) return "—";
  if (v >= 1e6) return (v/1e6).toFixed(1) + "M";
  if (v >= 1e3) return (v/1e3).toFixed(1) + "K";
  return String(v);
}
function fmtSeconds(v) {
  if (v===null||v===undefined) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  if (n < 60) return n.toFixed(1) + "s";
  const minutes = Math.floor(n / 60);
  const seconds = n - minutes * 60;
  return `${minutes}m ${seconds.toFixed(1)}s`;
}

function effectiveDims(row) {
  if (scoreMode === "reference") {
    return row.dims_ref ? {...row.dims_ref} : null;
  }
  const dims = {...row.dims};
  if (document.getElementById("gateOff").checked) {
    const gate = (row.dims.response||0) * (row.dims.coverage||0);
    for (const k of ["safety","adaptation","efficiency"]) {
      dims[k] = gate > 1e-9 ? Math.min(1, (row.dims[k]||0)/gate) : (row.dims[k]||0);
    }
  }
  return dims;
}
function dynamicOverall(row) {
  if (row.hard_failure) return 0;
  const dims = effectiveDims(row);
  if (!dims) return null;
  const total = Object.values(weights).reduce((a,b)=>a+b, 0);
  if (total <= 0) return 0;
  let s = 0;
  for (const [k] of DIMS) s += (weights[k]/total) * (dims[k]||0);
  return Math.max(0, Math.min(1, s));
}
function baselineOverall(row) {
  // 当前口径在官方权重下的总分：官方=overall_official；参考=overall_reference
  return scoreMode === "reference" ? row.overall_reference : row.overall_official;
}
function effRow() {
  const total = Object.values(weights).reduce((a,b)=>a+b,0) || 1;
  return Object.fromEntries(DIMS.map(([k])=>[k, weights[k]/total]));
}
function classify(row) {
  if (row.invalid) return "invalid-row";
  if (row.timed_out) return "timeout-row";
  return "";
}
function excludedFromSummary(row) {
  const only = document.getElementById("onlyValid").checked;
  return only && (row.diagnostic || row.invalid || row.timed_out || row.verdict !== "ok" || row.note.includes("污染"));
}

function renderControls() {
  const box = document.getElementById("controls");
  const share = effRow();
  box.innerHTML = DIMS.map(([k, label, formula]) => `
    <div class="ctl">
      <label>${label}<span class="share" id="share-${k}"></span></label>
      <input type="range" min="0" max="0.6" step="0.01" value="${weights[k]}" data-dim="${k}">
      <div class="formula">${formula}</div>
    </div>`).join("");
  box.querySelectorAll("input[type=range]").forEach(el => {
    el.addEventListener("input", () => { weights[el.dataset.dim] = parseFloat(el.value); render(); });
  });
  updateShares();
}
function updateShares() {
  const share = effRow();
  for (const [k] of DIMS) {
    const el = document.getElementById("share-"+k);
    if (el) el.textContent = (share[k]*100).toFixed(1) + "%";
  }
}
function modelClass(m) {
  m = String(m || "?");
  if (m.includes("kimi")) return "kimi";
  if (m.includes("qwen")) return "qwen";
  if (m.includes("claude")) return "claude";
  if (m.includes("gpt")) return "gpt";
  if (m.includes("deepseek")) return "deepseek";
  return "other";
}
// xhigh is the GPT naming for the same top reasoning tier that other
// providers expose as max. Keep raw values in DATA, but use one UI bucket.
function thinkingGroup(value) {
  const raw = String(value ?? "?").trim().toLowerCase();
  const compact = raw.replace(/[-_]/g, "");
  return compact === "xhigh" || compact === "max" ? "max" : raw || "?";
}
function thinkingLabel(value) {
  return thinkingGroup(value) === "max" ? "最高档（max / xhigh）" : thinkingGroup(value);
}
function thinkPill(t) {
  const group = thinkingGroup(t);
  return `<span class="pill think-${group}">${thinkingLabel(t)}</span>`;
}
let thinkFilter = "all", versionFilter = "";
function rowVisible(row) {
  if (thinkFilter !== "all" && thinkingGroup(row.thinking) !== thinkFilter) return false;
  if ((row.data_version || "?") !== versionFilter) return false;
  return true;
}
function versionCompare(a, b) {
  const pa = String(a).split(".").map(x => parseInt(x) || 0);
  const pb = String(b).split(".").map(x => parseInt(x) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] || 0) - (pb[i] || 0);
    if (d) return d;
  }
  return 0;
}
function syncModeAvailability() {
  // 参考口径（一次方·未门控）只要当前版本含有一次方指标就可选；
  // 思考档筛选不能改变这个资格判断，否则同一版本会因子集而被错误解锁。
  const rows = DATA.filter(r => (r.data_version || "?") === versionFilter);
  const hasRef = rows.length > 0 && rows.some(r => r.dims_ref);
  const sel = document.getElementById("modeSel");
  sel.querySelector('option[value="reference"]').disabled = !hasRef;
  const hint = document.getElementById("modeHint");
  if (!hasRef) {
    if (scoreMode === "reference") { scoreMode = "official"; sel.value = "official"; }
    const withRef = rows.filter(r => r.dims_ref).length;
    hint.textContent = withRef > 0
      ? `当前版本仅 ${withRef}/${rows.length} 个跑次有一次方指标，参考口径不可选（子集无法排序）`
      : "当前版本为旧版数据（无一次方指标），参考口径不可选";
  } else {
    const withRef = rows.filter(r => r.dims_ref).length;
    hint.textContent = scoreMode === "reference"
      ? (withRef < rows.length
        ? `参考口径：一次方·未门控；${rows.length - withRef} 个缺字段跑次不参与汇总`
        : "参考口径：一次方·未门控，仅供解读，不作官方排名")
      : "";
  }
  const gateWrap = document.getElementById("gateOffWrap");
  const gate = document.getElementById("gateOff");
  gate.disabled = scoreMode === "reference";
  gateWrap.classList.toggle("disabled", scoreMode === "reference");
}
function initFilters() {
  const order = ["off", "minimal", "low", "medium", "high", "max"];
  const orderRank = value => {
    const index = order.indexOf(value);
    return index < 0 ? 99 : index;
  };
  const thinks = [...new Set(DATA.map(r => thinkingGroup(r.thinking)))]
    .sort((a, b) => orderRank(a) - orderRank(b));
  const tSel = document.getElementById("thinkFilter");
  tSel.innerHTML = `<option value="all">全部档位</option>` + thinks.map(t => `<option value="${t}">${thinkingLabel(t)}</option>`).join("");
  tSel.addEventListener("change", () => { thinkFilter = tSel.value; syncModeAvailability(); render(); });
  // 排行只在同一数据版本内比较：不提供「全部版本」，默认选中最新版本
  const versions = [...new Set(DATA.map(r => r.data_version || "?"))].sort(versionCompare);
  const vSel = document.getElementById("versionFilter");
  vSel.innerHTML = versions.map(v => `<option value="${v}">${v}</option>`).join("");
  versionFilter = versions[versions.length - 1];
  vSel.value = versionFilter;
  vSel.addEventListener("change", () => { versionFilter = vSel.value; syncModeAvailability(); render(); });
  document.getElementById("modeSel").addEventListener("change", (e) => {
    scoreMode = e.target.value; syncModeAvailability(); render();
  });
}
function renderSummary() {
  const groups = {};
  for (const row of DATA) {
    if (excludedFromSummary(row) || !rowVisible(row)) continue;
    // 参考口径下没有参考数据的跑无法参与排序，直接排除
    if (scoreMode === "reference" && !row.dims_ref) continue;
    const key = row.model + "|" + thinkingGroup(row.thinking);
    (groups[key] ||= []).push(row);
  }
  const tbody = document.querySelector("#summary tbody");
  const mean = a => a.length ? a.reduce((x,y)=>x+y,0)/a.length : null;
  const meanPresent = a => {
    const values = a.filter(v => v !== null && v !== undefined && Number.isFinite(Number(v))).map(Number);
    return mean(values);
  };
  const entries = Object.entries(groups).map(([key, rows]) => {
    const dyn = rows.map(dynamicOverall);
    const meanDyn = mean(dyn);
    const n = dyn.length;
    let std = null, ci = null;
    if (n >= 2) {
      std = Math.sqrt(dyn.reduce((acc,v)=>acc+(v-meanDyn)**2,0)/(n-1));
      ci = t95(n-1) * std / Math.sqrt(n);
    }
    const refs = rows.map(r => r.overall_reference).filter(v => v !== null && v !== undefined);
    const u = rows.map(r => r.usage || {});
    return { model: key.split("|")[0], thinking: key.split("|")[1],
             n, meanDyn, std, ci,
             minDyn: Math.min(...dyn), maxDyn: Math.max(...dyn),
             meanOff: meanPresent(rows.map(r => r.overall_official)),
             meanRef: refs.length ? mean(refs) : null,
             meanActions: meanPresent(rows.map(r => r.actions)),
             meanTokOut: meanPresent(u.map(x => x.tokens_output_total)),
             meanTokIn: meanPresent(u.map(x => x.tokens_input_total)) };
  });
  // 按当前口径与权重下的动态均值实时降序排名（权重一变即重排）
  entries.sort((a,b) => b.meanDyn - a.meanDyn);
  const maxMean = Math.max(...entries.map(e => e.meanDyn), 1e-9);
  tbody.innerHTML = entries.map((e, i) => `<tr class="row-${modelClass(e.model)}">
      <td class="rank ${i===0?"rank-1":""}">${i+1}</td>
      <td class="txt"><span class="pill ${modelClass(e.model)}">${e.model}</span></td>
      <td class="txt">${thinkPill(e.thinking)}</td><td>${e.n}</td>
      <td><b>${fmt(e.meanDyn)}</b> ${e.ci===null?"":"±"+fmt(e.ci)}<span class="bar" style="width:${(e.meanDyn/maxMean*80).toFixed(0)}px"></span></td>
      <td>${fmt(e.std)}</td>
      <td>${fmt(e.minDyn)}</td><td>${fmt(e.maxDyn)}</td>
      <td>${fmt(e.meanOff)}</td><td>${fmt(e.meanRef)}</td>
      <td>${fmt(e.meanActions,1)}</td><td>${fmtTokens(e.meanTokOut)}</td><td>${fmtTokens(e.meanTokIn)}</td></tr>`).join("")
    || `<tr><td colspan="13" class="txt">（无有效样本${scoreMode==="reference"?"——当前版本无参考轨数据":""}）</td></tr>`;
}
function sortValue(row, key) {
  if (!key) return undefined;
  if (key === "thinking") return thinkingGroup(row.thinking);
  let value = row;
  for (const part of key.split(".")) {
    if (value === null || value === undefined) return undefined;
    value = value[part];
  }
  return value;
}
function renderRuns() {
  const tbody = document.querySelector("#runs tbody");
  let rows = [...DATA].filter(rowVisible);
  if (sortKey) rows.sort((a,b) => {
    const va = sortValue(a, sortKey), vb = sortValue(b, sortKey);
    const c = (va===null||va===undefined? -1 : va===vb? 0 : va<vb? -1:1);
    return sortAsc ? c : -c;
  });
  tbody.innerHTML = rows.map(row => {
    const dyn = dynamicOverall(row);
    const off = row.overall_official;
    const base = baselineOverall(row);
    const delta = (dyn===null||base===null||base===undefined) ? null : dyn - base;
    const finish = row.saw_finish ? (row.finish_at + "/7200") : (row.actions ? "未finish" : "—");
    const modelPill = `<span class="pill ${modelClass(row.model)}">${row.model}</span>`;
    const mode = row.harness.replace("pi-host-bridge/","") + (row.diagnostic ? "·诊断" : "");
    const u = row.usage || {};
    const stateClass = document.getElementById("hideInvalid").checked ? classify(row) : "";
    return `<tr class="${stateClass} row-${modelClass(row.model)}">
      <td class="txt">${row.stamp}</td><td class="txt">${modelPill}</td><td>${thinkPill(row.thinking)}</td><td class="txt">${mode}</td><td class="txt">${row.data_version || "?"}</td>
      <td>${fmt(row.covered,2)}/${fmt(row.total_weight,2)}</td>
      <td>${fmt(row.completed,2)}/${fmt(row.total_weight,2)}</td>
      <td>${fmt(row.completion_rate)}</td>
      <td>${row.high_missed ?? "—"}</td><td>${row.late ?? "—"}</td><td>${row.invalid_actions ?? "—"}</td>
      <td>${row.actions ?? "—"}</td><td>${row.advance_count ?? "—"}</td><td>${row.write_count ?? "—"}</td><td>${row.finish_count ?? "—"}</td>
      <td>${u.turns ?? "—"}</td><td>${u.tool_calls ?? "—"}</td><td>${fmtTokens(u.tokens_output_total)}</td><td>${fmtTokens(u.tokens_input_total)}</td><td>${fmtTokens(u.context_final)}</td>
      <td>${fmtSeconds(row.wall_clock_seconds)}</td>
      <td>${finish}</td>
      <td>${fmt(row.dims.response)}</td><td>${fmt(row.dims.coverage)}</td><td>${fmt(row.dims.safety)}</td>
      <td>${fmt(row.dims.adaptation)}</td><td>${fmt(row.dims.efficiency)}</td><td>${fmt(row.dims.integrity)}</td>
      <td>${fmt(off)}</td>
      <td>${fmt(row.overall_reference)}</td>
      <td><b>${dyn===null?"—":fmt(dyn)}</b>${dyn===null?"":`<span class="bar" style="width:${(dyn*90).toFixed(0)}px"></span>`}</td>
      <td class="${delta===null?"":(delta>=0?"delta-up":"delta-down")}">${delta===null?"—":(delta>=0?"+":"")+fmt(delta)}</td>
      <td class="txt note-cell">${row.note || (row.verdict!=="ok" ? row.verdict : "")}</td></tr>`;
  }).join("");
}
function exportCsv() {
  const share = effRow();
  const header = ["score_mode","dir","stamp","model","thinking","thinking_display","version","diagnostic","verdict",
                  "overall_official","overall_reference","completion_rate",
                  ...DIMS.map(([k]) => "w_"+k),
                  ...DIMS.map(([k]) => "official_"+k),
                  ...DIMS.map(([k]) => (scoreMode==="reference" ? "ref_"+k : "hyp_"+k)),
                  "dynamic_overall","actions","advance_count","write_count","finish_count","turns","tool_calls",
                  "tokens_output_total","tokens_input_total","context_final","wall_clock_seconds","finish_at","note"];
  const lines = [header.join(",")];
  const q = (v) => {
    const s = (v===null||v===undefined) ? "" : String(v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g,'""') + '"' : s;
  };
  for (const row of DATA.filter(rowVisible)) {
    const dyn = dynamicOverall(row);
    const u = row.usage || {};
    const dimsEff = effectiveDims(row) || {};
    lines.push([
      scoreMode, row.dir, row.stamp, row.model, row.thinking, thinkingLabel(row.thinking), row.data_version, row.diagnostic, row.verdict,
      row.overall_official, row.overall_reference, row.completion_rate,
      ...DIMS.map(([k]) => share[k].toFixed(4)),
      ...DIMS.map(([k]) => row.dims[k]),
      ...DIMS.map(([k]) => dimsEff[k] ?? ""),
      dyn===null ? "" : dyn.toFixed(6),
      row.actions, row.advance_count, row.write_count, row.finish_count, u.turns, u.tool_calls,
      u.tokens_output_total, u.tokens_input_total, u.context_final, row.wall_clock_seconds,
      row.finish_at, row.note,
    ].map(q).join(","));
  }
  const blob = new Blob(["\ufeff" + lines.join("\n")], {type: "text/csv;charset=utf-8"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `score-explorer-${scoreMode}-v${versionFilter}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}
function render() { updateShares(); renderSummary(); renderRuns(); }

document.getElementById("reset").addEventListener("click", () => { weights = {...OFFICIAL}; renderControls(); render(); });
document.getElementById("equal").addEventListener("click", () => { weights = Object.fromEntries(DIMS.map(([k])=>[k,1/6])); renderControls(); render(); });
document.getElementById("gateOff").addEventListener("change", render);
document.getElementById("hideInvalid").addEventListener("change", render);
document.getElementById("onlyValid").addEventListener("change", render);
document.getElementById("exportCsv").addEventListener("click", exportCsv);
document.querySelectorAll("#runs th").forEach((th, i) => {
  const keys = ["stamp","model","thinking","harness","data_version","covered","completed","completion_rate",
                "high_missed","late","invalid_actions","actions","advance_count","write_count","finish_count",
                "usage.turns","usage.tool_calls","usage.tokens_output_total","usage.tokens_input_total",
                "usage.context_final","wall_clock_seconds","finish_at",
                "dims.response","dims.coverage","dims.safety","dims.adaptation","dims.efficiency","dims.integrity",
                "overall_official","overall_reference",null,null,null];
  const key = keys[i];
  if (!key) return;
  th.addEventListener("click", () => {
    if (sortKey === key) sortAsc = !sortAsc; else { sortKey = key; sortAsc = false; }
    render();
  });
});

initFilters();
syncModeAvailability();
renderControls();
render();
</script>
</body>
</html>
"""


def main() -> int:
    rows = collect()
    if not rows:
        print("没有可收集的跑次（需要 results/pi-runs/<run>/run-manifest.json + verifier 产物）", file=sys.stderr)
        return 1
    payload = json.dumps(rows, ensure_ascii=False)
    html = TEMPLATE.replace("__DATA__", payload)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    versions = sorted({row["data_version"] for row in rows})
    with_ref = sum(1 for row in rows if row["dims_ref"])
    print(f"已生成 {OUT}（{len(rows)} 个跑次；数据版本 {versions}；含参考轨 {with_ref} 个）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
