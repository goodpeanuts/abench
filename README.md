# A Bench：SFFD Rolling Surge

一个面向 LLM Agent 的在线应急医疗调度 benchmark。当前任务 `SFFD Rolling Surge 01` 基于匿名化的 San Francisco Fire Department（SFFD）CAD 公开记录构建，让 Agent 在信息逐步到达、资源有限且未来不可见的条件下，持续完成派遣、改派、待命点调整和时间推进。

> 当前状态：**Harbor-compatible pilot**，任务版本 `v1.1.0`，Harbor schema `1.4`。仓库已经包含可复现的公开开发场景、独立 verifier、容器化运行环境和模型评测脚本；尚不宣称已经通过官方 Harbor runner 验证。

## Benchmark 测什么

它不是一组彼此独立的静态问答，而是一个持续两小时仿真时间的在线决策 episode：

- 1200 秒事件到达窗口，7200 秒完整仿真时域；
- 16 个到达事件，其中 12 个是可派遣的医疗事件；
- Agent 只能看到当前时刻已经公开的状态，无法访问隐藏场景、未来事件、随机种子或 verifier；
- Agent 只能通过 `emsctl` 执行 `observe`、`dispatch`、`reassign`、`reposition`、`advance` 和 `finish`；
- 每次状态变化都会更新 `state_version`，动作使用唯一 `action_id`，相同动作可安全重试；
- episode 结束后，由隔离的 verifier 重放动作日志并计算得分。

这个任务主要考察五类能力：在线规划、部分可观测条件下的资源分配、突发负载适应、覆盖与响应时间权衡，以及长时域工具调用的状态一致性。

## 数据与场景

场景来自 SFFD Calls for Service 数据集 `nuek-vuh3`。当前数据快照包含 7,408,845 行公开 CAD 记录，时间跨度为 2000-04-12 至 2026-08-21；每行表示某个 incident/unit 的一次响应记录，因此同一 incident 可以对应多行。

仓库提交的是从真实记录校准后生成的确定性反事实场景，而不是旧金山 EMS 系统的完整数字孪生。公开开发切片、数据血缘和校准统计见 [`data_manifest.json`](tasks/sffd-rolling-surge-01/data_manifest.json)。原始 CSV、私有变体和模型运行结果均不会提交到 Git。

## 评测架构

```text
LLM Agent
   │  only `emsctl`
   ▼
固定参数工具桥接层
   │  Unix socket / Podman Compose
   ▼
状态仿真器 ── append-only action log + hashes
   │
   ▼
独立 Verifier ── replay ── reward.json / details / trajectory
```

- 模型侧没有 shell、文件系统或网络工具；
- simulator 持有隐藏事件、未来状态、种子和权威状态；
- verifier 不接受 Agent 自报结果，而是从初始状态重放完整动作序列；
- 正式模型评测时，宿主 harness 可以访问模型 API，但任务容器保持无网络。

更完整的信任边界和数据流见[评测架构图](docs/evaluation-architecture.html)。

## 评分

总分由六部分组成：

| 维度 | 权重 | 关注点 |
| --- | ---: | --- |
| Response | 40% | 按事件优先级加权的 SLA 响应质量 |
| Coverage | 20% | 区域覆盖和资源可用性 |
| Safety | 15% | 高优先级事件、无效动作与运行安全 |
| Adaptation | 10% | 对滚动事件和负载变化的响应 |
| Efficiency | 10% | 资源使用、改派和空驶成本 |
| Integrity | 5% | 轨迹完整性、可重放性和终止条件 |

`overall` 是连续分数；`strict_pass` 是单独的全条件通过指标，两者不能互换。未提供有效服务的运行会受到 service gate 限制，因此不能只靠轨迹完整性获得高分。

当前公开场景的权威基线用于校准和回归检查：

| Policy | Overall |
| --- | ---: |
| No-op | 0.050000 |
| Random feasible | 0.198002 |
| Rolling horizon | 0.219143 |
| Nearest unit | 0.330338 |
| Priority nearest | 0.330338 |

这些数字不是模型排行榜，也不代表 benchmark 已经达到最终区分度目标。

## 快速开始

### 1. 运行本地测试

本地测试只需要 Bash 和 Python 3：

```bash
git clone <your-fork-or-repository-url> abench
cd abench/tasks/sffd-rolling-surge-01
bash tests/test.sh
```

测试会覆盖场景结构、仿真动作、版本冲突、幂等行为、事件推进、评分和 verifier 重放等核心路径。

### 2. 启动容器化开发环境

需要 Podman 和 `podman compose`：

```bash
cd abench
bash scripts/run-pi-harness.sh \
  --provider dashscope \
  --model qwen3.8-max \
  --bootstrap-only
```

`--bootstrap-only` 只构建并检查环境，不会调用模型 API。也可以使用 [`docs/pi-harness.md`](docs/pi-harness.md) 中的 Compose 命令直接检查 `emsctl observe`。

## 运行模型评测

### 前置条件

- Python 3
- Node.js
- Podman 与 `podman compose`
- `pi` CLI
- 所选 provider 的 API key

先完成本地配置和无模型调用 smoke test：

```bash
bash scripts/pi-setup.sh
bash scripts/pi-smoke.sh --source-config --no-call
```

API key 使用以下环境变量之一：

```text
WODEX_API_KEY
MOONSHOT_API_KEY
DASHSCOPE_API_KEY
DEEPSEEK_API_KEY
```

以 Qwen 为例运行一次完整评测：

```bash
bash scripts/run-pi-harness.sh \
  --provider dashscope \
  --model qwen3.8-max \
  --thinking max
```

正式结果要求使用模型配置规定的最高 thinking 档位：Claude、Kimi、Qwen、DeepSeek 使用 `max`，GPT-5.6 Sol 使用 `xhigh`。`--allow-any-thinking` 仅用于诊断，其结果不会进入正式聚合。

运行产物默认保存在 `results/pi-runs/`，包括模型响应、动作轨迹、verifier 输出、reward、运行元数据和重放证明；该目录已被 Git 忽略。

### 多次运行与统计聚合

同一模型建议至少保留 3 次 `verdict=ok`、非诊断运行：

```bash
bash scripts/run-pi-batch.sh \
  --n 3 \
  --out results/pi-runs/_batch/qwen-max.json \
  -- \
  --provider dashscope \
  --model qwen3.8-max
```

聚合指定模型的结果：

```bash
python3 scripts/aggregate_pi_runs.py \
  --scan results/pi-runs \
  --model qwen3.8-max \
  --thinking max \
  --metric overall \
  --output results/pi-runs/_batch/qwen-max-summary.json
```

正式比较时应保持相同的 `data_version`、模型配置和 thinking 档位，并报告样本数、标准差和 95% 置信区间。

### 查看分数构成

```bash
python3 scripts/build_score_explorer.py
```

然后在浏览器中打开 [`docs/score-explorer.html`](docs/score-explorer.html)，交互式检查各评分维度、service gate 和动作轨迹对总分的影响。

## 重建公开场景

公开数据获取需要网络。建议始终将原始切片放在仓库之外：

```bash
python3 scripts/fetch_sffd_slice.py \
  --start 2025-03-11T00:00:00 \
  --end 2025-03-12T00:00:00 \
  --output-dir /tmp/abench-sffd

python3 scripts/build_sffd_scenario.py \
  --csv /tmp/abench-sffd/raw-response-slice.csv \
  --output /tmp/abench-sffd/scenario.json
```

私有变体由 `scripts/build_sffd_variants.py` 生成，应与密钥、原始 CSV 一起保存在受控环境中，不能提交到公开仓库或暴露给被评测 Agent。

## 仓库结构

```text
.
├── AGENTS.md                         # Agent 的任务运行约束
├── config/pi/                        # provider、模型与上下文配置
├── docs/                             # 架构、harness、答辩和分数探索文档
├── scripts/                          # 数据构建、运行、聚合与报告脚本
├── tasks/sffd-rolling-surge-01/
│   ├── instruction.md                # 发给 Agent 的任务说明
│   ├── scenario.json                 # 公开开发场景
│   ├── data_manifest.json            # 数据血缘、校准和基线
│   ├── environment/                  # simulator、emsctl 与 Compose 环境
│   ├── solution/                     # verifier 与评分实现
│   └── tests/                        # 本地回归测试
└── sffd-rolling-surge-report.html    # 项目完整报告
```

## 已知边界

- 当前是 real-CAD-grounded 的确定性反事实 pilot，不建模完整的实时路网、医院容量、患者结局或警务系统；
- 公开开发场景适合复现和调试，正式比较还需要受控的隐藏变体和多次独立运行；
- `dispatch_delay` 已进入数据校准与保存流程，但尚未作为 v1 中独立的动态阶段启用；
- 当前权威基线尚未满足预设的全部区分度验收目标；
- 官方 Harbor runner 的端到端验证仍是待完成项。

更详细的限制、威胁模型和答辩口径见 [`docs/sffd-benchmark-defense-background.md`](docs/sffd-benchmark-defense-background.md)。

## 延伸文档

- [完整项目报告](sffd-rolling-surge-report.html)
- [Pi harness 使用说明](docs/pi-harness.md)
- [评测架构](docs/evaluation-architecture.html)
- [滚动突发场景演示](docs/sffd-rolling-surge-demo.md)
- [Benchmark 答辩背景](docs/sffd-benchmark-defense-background.md)
- [代码清单](docs/code-manifest.md)
- [轨迹清单](docs/trajectory-manifest.md)

## 数据许可

SFFD Calls for Service 数据由 City and County of San Francisco 发布，数据许可为 [Public Domain Dedication and License（PDDL）](https://opendatacommons.org/licenses/pddl/)。仓库目前没有声明独立的代码许可证；复用代码前请先确认项目维护者给出的许可范围。
