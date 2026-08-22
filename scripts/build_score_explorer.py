#!/usr/bin/env python3
"""build_score_explorer.py — 从 results/pi-runs 生成 docs/score-explorer.html

收集每次运行的 run-manifest.json、verifier/reward.json（六维分项）、
verifier/details.json（原始证据指标）与 finish 时点，内嵌为 JSON，生成一个
权重可调、实时重算 overall 的单文件 HTML（无外部依赖）。

用法：python3 scripts/build_score_explorer.py
"""

from __future__ import annotations

import json
import pathlib
import sys

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

# 数据版本断代映射（目录名前缀 -> task.toml 版本）。未列出的跑次使用其
# 任务目录 task.toml 的当前版本。契约/数据改版后，在下面为改版时刻之后
# 的新跑次登记新版本；历史跑次必须显式登记，否则会被错误归入新版本。
VERSION_MAP: dict[str, str] = {
    # 示例："20260901T000000": "1.1.0",
}

DIMENSION_KEYS = ["response", "coverage", "safety", "adaptation", "efficiency", "integrity"]


def task_version(task_rel: str) -> str:
    """读取任务目录 task.toml 里声明的版本号。"""
    import re

    toml_path = REPO / task_rel / "task.toml"
    if not toml_path.is_file():
        return "?"
    match = re.search(r'^version\s*=\s*"([^"]+)"', toml_path.read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1) if match else "?"


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

        name = run_dir.name
        verdict = manifest.get("verdict", "unknown")
        invalid = verdict == "error_stop"
        data_version = next(
            (version for prefix, version in VERSION_MAP.items() if name.startswith(prefix)),
            task_version(str(manifest.get("task", "tasks/sffd-rolling-surge-01"))),
        )
        rows.append({
            "dir": name,
            "stamp": name[:15],
            "model": manifest.get("model", "?"),
            "provider": manifest.get("provider", "?"),
            "thinking": manifest.get("thinking", "?"),
            "harness": manifest.get("harness", "?"),
            "data_version": data_version,
            "diagnostic": name.endswith("-diagnostic"),
            "verdict": verdict,
            "timed_out": bool(manifest.get("timed_out")),
            "actions": details.get("action_count"),
            "covered": evidence.get("covered_weight"),
            "completed": evidence.get("completed_weight"),
            "total_weight": evidence.get("total_priority_weight"),
            "high_missed": evidence.get("high_priority_missed"),
            "late": evidence.get("late_count"),
            "invalid_actions": evidence.get("scored_invalid_attempts"),
            "hard_failure": bool(evidence.get("hard_failure")),
            "finish_at": finish_at,
            "saw_finish": saw_finish,
            "dims": {key: reward.get(key) for key in DIMENSION_KEYS},
            "overall_official": reward.get("overall"),
            "strict_pass": reward.get("strict_pass"),
            "note": next((note for prefix, note in NOTES.items() if name.startswith(prefix)), ""),
            "invalid": invalid,
        })
    return rows


TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SFFD 分数探索器 — 历史跑次 × 可调权重</title>
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
  .wrap { max-width: 1180px; margin: 0 auto; padding: 28px 20px 60px; }
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
  .pill.think-xhigh   { border-color: #ea580c; color: #ea580c; }
  .pill.think-max     { border-color: #dc2626; color: #dc2626; background: rgba(220,38,38,.08); }
  tr.row-kimi   { background: rgba(147, 51, 234, .07); }
  tr.row-qwen   { background: rgba(6, 182, 212, .07); }
  tr.row-claude { background: rgba(217, 119, 6, .08); }
  tr.row-gpt    { background: rgba(22, 163, 74, .07); }
  tr.row-deepseek { background: rgba(219, 39, 119, .07); }
  tr.row-other  { background: rgba(100, 116, 139, .06); }
  select { background: var(--card); color: var(--ink); border: 1px solid var(--line); border-radius: 6px; padding: 3px 6px; font-size: 12px; }
  .rank { font-weight: 700; color: var(--muted); }
  .rank-1 { color: var(--good); }
  .note-cell { color: var(--warn); font-size: 11px; max-width: 220px; white-space: normal; }
  .legend { color: var(--muted); font-size: 12px; margin-top: 8px; }
  .delta-up { color: var(--good); } .delta-down { color: var(--bad); }
  h2 { font-size: 15px; margin: 0 0 10px; }
  code { background: var(--accent-soft); border-radius: 4px; padding: 0 4px; font-size: 12px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>SFFD 分数探索器</h1>
  <p class="sub">历史跑次原始指标内嵌；拖动权重滑杆实时重算 overall（权重自动归一化）。
  官方 overall = 0.40·response + 0.20·coverage + 0.15·safety + 0.10·adaptation + 0.10·efficiency + 0.05·integrity，
  其中 safety/adaptation/efficiency 已乘 service gate（= response × coverage）。</p>

  <div class="card">
    <div class="controls" id="controls"></div>
    <div class="btnrow">
      <button id="reset">恢复官方权重</button>
      <button class="ghost" id="equal">等权重</button>
      <label class="chk"><input type="checkbox" id="gateOff"> 假设分析：去掉 service gate（用 response×coverage 反解未门控三维）</label>
      <label class="chk"><input type="checkbox" id="hideInvalid" checked> 表格中灰显无效/超时/污染跑</label>
      <label class="chk"><input type="checkbox" id="onlyValid" checked> 汇总只计有效跑（剔除 403 无效、超时负例、污染跑）</label>
      <label class="chk">思考档筛选：<select id="thinkFilter"></select></label>
      <label class="chk">数据版本：<select id="versionFilter"></select></label>
    </div>
  </div>

  <div class="card">
    <h2>分组汇总（当前权重下的动态 overall）</h2>
    <table id="summary"><thead><tr>
      <th>#</th><th class="txt">模型</th><th class="txt">思考档</th><th>样本</th><th>均值</th><th>最低</th><th>最高</th><th>官方均值</th>
    </tr></thead><tbody></tbody></table>
  </div>

  <div class="card">
    <h2>全部跑次（点表头排序；「动态」列为当前权重重算值）</h2>
    <div style="overflow-x:auto; max-height: 70vh; overflow-y:auto;">
    <table id="runs"><thead><tr>
      <th class="txt">时间</th><th class="txt">模型</th><th>档</th><th class="txt">模式</th><th class="txt">版本</th>
      <th>覆盖</th><th>完成</th><th>高漏</th><th>迟到</th><th>无效写</th><th>动作</th><th>finish@</th>
      <th>resp</th><th>cov</th><th>saf</th><th>adap</th><th>effi</th><th>integ</th>
      <th>官方</th><th>动态</th><th>Δ</th><th class="txt">备注</th>
    </tr></thead><tbody></tbody></table>
    </div>
    <p class="legend">覆盖/完成 = 权重比（分母为可派事故总权重 8.25）；finish@ = finish 时的模拟时间 / 7200；
    resp/cov/…/integ 为六维原值；Δ = 动态 − 官方。hard_failure 的跑在任何权重下动态值恒为 0。
    行底色区分模型（紫=kimi、青=qwen、橙=claude、绿=gpt、粉=deepseek、灰=其他）；思考档标签：灰=low、蓝=medium、紫=high、橙=xhigh、红=max。
    汇总表按当前权重下的动态均值实时降序排名，调整权重即重排；可用下拉按思考档或数据版本筛选（同时作用于汇总与明细）。</p>
  </div>
</div>

<script>
const DATA = __DATA__;
const DIMS = [
  ["response",    "response 响应质量", "Σw·q²/Σw，q=clamp(1−delay/SLA)"],
  ["coverage",    "coverage 及时覆盖", "Σw·q（仅计已完成）/Σw"],
  ["safety",      "safety 操作有效性", "(1−无效尝试率)×gate；灾难→0"],
  ["adaptation",  "adaptation 动态适应", "重规划覆盖度×gate"],
  ["efficiency",  "efficiency 运营经济", "(1−超额churn−无效/2)×gate"],
  ["integrity",   "integrity 回放完整性", "重放通过=1，作弊→overall归零"],
];
const OFFICIAL = {response:.40, coverage:.20, safety:.15, adaptation:.10, efficiency:.10, integrity:.05};
let weights = {...OFFICIAL};
let sortKey = null, sortAsc = false;

const fmt = (v, d=3) => (v===null||v===undefined) ? "—" : Number(v).toFixed(d);

function effectiveDims(row) {
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
  const total = Object.values(weights).reduce((a,b)=>a+b, 0);
  if (total <= 0) return 0;
  let s = 0;
  for (const [k] of DIMS) s += (weights[k]/total) * (dims[k]||0);
  return Math.max(0, Math.min(1, s));
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
  return only && (row.invalid || row.timed_out || row.note.includes("污染"));
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
  if (m.includes("kimi")) return "kimi";
  if (m.includes("qwen")) return "qwen";
  if (m.includes("claude")) return "claude";
  if (m.includes("gpt")) return "gpt";
  if (m.includes("deepseek")) return "deepseek";
  return "other";
}
function thinkPill(t) {
  return `<span class="pill think-${t}">${t}</span>`;
}
let thinkFilter = "all", versionFilter = "all";
function rowVisible(row) {
  if (thinkFilter !== "all" && row.thinking !== thinkFilter) return false;
  if (versionFilter !== "all" && (row.data_version || "?") !== versionFilter) return false;
  return true;
}
function initFilters() {
  const order = ["off", "minimal", "low", "medium", "high", "xhigh", "max"];
  const thinks = [...new Set(DATA.map(r => r.thinking))]
    .sort((a, b) => (order.indexOf(a) ?? 99) - (order.indexOf(b) ?? 99));
  const tSel = document.getElementById("thinkFilter");
  tSel.innerHTML = `<option value="all">全部档位</option>` + thinks.map(t => `<option value="${t}">${t}</option>`).join("");
  tSel.addEventListener("change", () => { thinkFilter = tSel.value; render(); });
  const versions = [...new Set(DATA.map(r => r.data_version || "?"))].sort();
  const vSel = document.getElementById("versionFilter");
  vSel.innerHTML = `<option value="all">全部版本</option>` + versions.map(v => `<option value="${v}">${v}</option>`).join("");
  vSel.addEventListener("change", () => { versionFilter = vSel.value; render(); });
}
function renderSummary() {
  const groups = {};
  for (const row of DATA) {
    if (excludedFromSummary(row) || !rowVisible(row)) continue;
    const key = row.model + "|" + row.thinking;
    (groups[key] ||= []).push(row);
  }
  const tbody = document.querySelector("#summary tbody");
  const mean = a => a.reduce((x,y)=>x+y,0)/a.length;
  const entries = Object.entries(groups).map(([key, rows]) => {
    const dyn = rows.map(dynamicOverall);
    return { model: key.split("|")[0], thinking: key.split("|")[1],
             n: rows.length, dyn, meanDyn: mean(dyn),
             minDyn: Math.min(...dyn), maxDyn: Math.max(...dyn),
             meanOff: mean(rows.map(r => r.overall_official||0)) };
  });
  // 按当前权重下的动态均值实时降序排名（权重一变即重排）
  entries.sort((a,b) => b.meanDyn - a.meanDyn);
  const maxMean = Math.max(...entries.map(e => e.meanDyn), 1e-9);
  tbody.innerHTML = entries.map((e, i) => `<tr class="row-${modelClass(e.model)}">
      <td class="rank ${i===0?"rank-1":""}">${i+1}</td>
      <td class="txt"><span class="pill ${modelClass(e.model)}">${e.model}</span></td>
      <td class="txt">${thinkPill(e.thinking)}</td><td>${e.n}</td>
      <td><b>${fmt(e.meanDyn)}</b><span class="bar" style="width:${(e.meanDyn/maxMean*80).toFixed(0)}px"></span></td>
      <td>${fmt(e.minDyn)}</td><td>${fmt(e.maxDyn)}</td>
      <td>${fmt(e.meanOff)}</td></tr>`).join("")
    || `<tr><td colspan="8" class="txt">（无有效样本）</td></tr>`;
}
function renderRuns() {
  const tbody = document.querySelector("#runs tbody");
  let rows = [...DATA].filter(rowVisible);
  if (sortKey) rows.sort((a,b) => {
    const va = a[sortKey], vb = b[sortKey];
    const c = (va===null||va===undefined? -1 : va===vb? 0 : va<vb? -1:1);
    return sortAsc ? c : -c;
  });
  tbody.innerHTML = rows.map(row => {
    const dyn = dynamicOverall(row);
    const off = row.overall_official || 0;
    const delta = dyn - off;
    const finish = row.saw_finish ? (row.finish_at + "/7200") : (row.actions ? "未finish" : "—");
    const modelPill = `<span class="pill ${modelClass(row.model)}">${row.model}</span>`;
    const mode = row.harness.replace("pi-host-bridge/","") + (row.diagnostic ? "·诊断" : "");
    return `<tr class="${classify(row)}${(document.getElementById("hideInvalid").checked && classify(row)) ? " invalid-row":""} row-${modelClass(row.model)}">
      <td class="txt">${row.stamp}</td><td class="txt">${modelPill}</td><td>${thinkPill(row.thinking)}</td><td class="txt">${mode}</td><td class="txt">${row.data_version || "?"}</td>
      <td>${fmt(row.covered,2)}/${fmt(row.total_weight,2)}</td>
      <td>${fmt(row.completed,2)}/${fmt(row.total_weight,2)}</td>
      <td>${row.high_missed ?? "—"}</td><td>${row.late ?? "—"}</td><td>${row.invalid_actions ?? "—"}</td>
      <td>${row.actions ?? "—"}</td><td>${finish}</td>
      <td>${fmt(row.dims.response)}</td><td>${fmt(row.dims.coverage)}</td><td>${fmt(row.dims.safety)}</td>
      <td>${fmt(row.dims.adaptation)}</td><td>${fmt(row.dims.efficiency)}</td><td>${fmt(row.dims.integrity)}</td>
      <td>${fmt(off)}</td>
      <td><b>${fmt(dyn)}</b><span class="bar" style="width:${(dyn*90).toFixed(0)}px"></span></td>
      <td class="${delta>=0?"delta-up":"delta-down"}">${delta>=0?"+":""}${fmt(delta)}</td>
      <td class="txt note-cell">${row.note || (row.verdict!=="ok" ? row.verdict : "")}</td></tr>`;
  }).join("");
}
function render() { updateShares(); renderSummary(); renderRuns(); }

document.getElementById("reset").addEventListener("click", () => { weights = {...OFFICIAL}; renderControls(); render(); });
document.getElementById("equal").addEventListener("click", () => { weights = Object.fromEntries(DIMS.map(([k])=>[k,1/6])); renderControls(); render(); });
document.getElementById("gateOff").addEventListener("change", render);
document.getElementById("hideInvalid").addEventListener("change", render);
document.getElementById("onlyValid").addEventListener("change", render);
document.querySelectorAll("#runs th").forEach((th, i) => {
  const keys = ["stamp","model","thinking","harness","data_version","covered","completed","high_missed","late","invalid_actions","actions","finish_at",
                null,null,null,null,null,null,"overall_official",null,null,null];
  const key = keys[i];
  if (!key) return;
  th.addEventListener("click", () => {
    if (sortKey === key) sortAsc = !sortAsc; else { sortKey = key; sortAsc = false; }
    render();
  });
});

initFilters();
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
    print(f"已生成 {OUT}（{len(rows)} 个跑次）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
