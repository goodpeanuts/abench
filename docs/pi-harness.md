# pi 作为 abench 的评测 Harness（宿主机桥接版）

本文档说明如何用 [pi coding agent](
https://www.npmjs.com/package/@earendil-works/pi-coding-agent) 作为本仓库
Harbor 任务（当前为 `tasks/sffd-rolling-surge-01`）的评测 harness。参考实现
是 [`scripts/run-pi-harness.sh`](../scripts/run-pi-harness.sh)；provider 目录、
setup 与冒烟脚本与 3bench 仓库保持同源。

## 1. 为什么 harness 在宿主机侧

任务契约把被评分的环境设计为离线：`task.toml` 中 agent 与 verifier 环境都是
`network_mode: no-network`，`main` 容器（agent 环境）里只有 `emsctl` 与公开
契约，没有 node。而 LLM harness 必须访问模型 API。两者的交界处本来就有正确的
信任边界——隐藏 scenario 只挂载在 `simulator` sidecar 里，agent 经只读的
unix socket（`/tmp/ems/emsctl.sock`）交互——因此本 harness 把 pi 放在**宿主
机**上运行（LLM 调用走宿主机网络），其 bash 工具通过一条逐字桥接命令驱动
容器内的 `emsctl`：

```
podman compose -f <task>/environment/docker-compose.yaml \
               -f <task>/environment/docker-compose.local.yaml \
               -p sffd-pi exec -T main emsctl <子命令>
```

容器侧的 no-network 性质因此完好：agent 拿不到 `main` 的 shell，也接触不到
simulator 的文件系统。

## 2. 用法

```bash
bash scripts/pi-setup.sh                # 安装 config/pi/models.json 到 ~/.pi/agent/（幂等、带备份）
bash scripts/pi-smoke.sh --no-call      # 静态检查：二进制、目录、四个模型、密钥存在性
bash scripts/pi-smoke.sh --provider dashscope --model qwen3.8-max   # 真实往返

# 完整评测跑（模型四选一：claude-opus-5 / gpt-5-sol / kimi-k3 / qwen3.8-max）
bash scripts/run-pi-harness.sh --provider dashscope --model qwen3.8-max --thinking low

# 只验证基础设施（podman machine、镜像构建、socket 健康、一次桥接 observe）
bash scripts/run-pi-harness.sh --provider dashscope --model qwen3.8-max --bootstrap-only
```

常用选项：`--thinking off|minimal|low|medium|high|xhigh|max`（默认 `low`）、
`--timeout <秒>`（默认 840，= Harbor `agent.timeout_sec` 900 减余量）、
`--task <目录>`、`--keep`（保留容器与卷）、`--no-verify`（跳过评分）、
`--skip-smoke`。密钥以环境变量注入当前 shell（`WODEX_API_KEY` /
`MOONSHOT_API_KEY` / `DASHSCOPE_API_KEY`），不写入任何文件。

## 3. 一次运行发生什么

1. **预检**：pi/node/python3/podman 就位；`pi-smoke --no-call`；按名检查所选
   provider 的密钥变量；podman machine 未运行则自动启动。
2. **清场拉起**：`down -v`（`simulator-state` 卷残留会让 simulator 以
   `load_existing=True` 恢复旧回合，必须清掉）→ `up -d --wait`（等
   simulator healthcheck 的 socket）→ 一次桥接 `emsctl observe` 冒烟。
3. **组装 prompt**：`instruction.md` 全文 + 桥接段（逐字命令、JSON 语义、
   时间预算、禁区），同一内容写入 workspace 的 `BRIDGE.md`。
4. **跑 pi**：cwd 为每次运行独立的 `results/pi-runs/<时间戳>-<模型>/workspace`
   （`AGENTS.md` 随之拷入并被 pi 自动加载）；`pi -p --approve --no-session
   --mode json --provider … --model … --thinking … --tools bash,read,grep,find,
   ls,write,edit`；手动 watchdog 在预算超时后 TERM→KILL。
5. **判定**：解析事件流——pi 的 CLI 在最后一轮 API 错误时也以 0 退出，因此
   按 `agent_end` 与最后一条 assistant 消息的 `stopReason` 判定
   （`ok` / `error_stop` / `no_agent_end`）。判定失败**不阻断评分**。
6. **收集与评分**：`compose cp` 取出 sidecar 的 `/tmp/ems-state`（其中
   `actions.jsonl` 是评分权威动作日志），在宿主机跑 `tests/harbor_verify.sh`。
   三个输出路径必须显式覆盖——脚本默认写 `/logs/verifier/*`，宿主机下
   `mkdir` 会直接失败：
   `HARBOR_REWARD_PATH / HARBOR_DETAILS_PATH / HARBOR_REPLAY_PATH`。
7. **落盘**：`run-manifest.json` 记录模型/档位/时长/超时/判定/reward
   （八键：response、coverage、safety、adaptation、efficiency、integrity、
   overall、strict_pass）/action_count；EXIT trap 自动 `down -v` 清理。

## 4. 与 3bench（VeriForge 版）的差异

| | 3bench `pi_agent.sh` | abench `run-pi-harness.sh` |
| --- | --- | --- |
| 契约 | `VERIFORGE_*` 环境变量 | 无外部契约，自包含 |
| pi 配置 | 每 roll 生成 `PI_CODING_AGENT_DIR` + models.json | 复用全局 `~/.pi/agent/models.json`（由 `pi-smoke` 保证） |
| 工具白名单 | 仅文件工具（无 bash） | **必开 bash**（任务需要 `emsctl` 命令执行） |
| 判定失败 | fail closed（退出非零） | 记录 verdict 但照常评分（回合状态真实存在） |
| 运行位置 | runner 子进程 | 宿主机 + 容器桥接 |

## 5. 边界与已知事项（如实）

- **防作弊是荣誉制**：pi 在宿主机有 bash，理论上可经绝对路径读到隐藏的
  `scenario.json`。workspace 隔离 cwd、`AGENTS.md` 与 prompt 禁区只能防误
  操作，不能防恶意模型。本 harness 定位是**本地评测工具**，不是官方榜单。
- pi 偶发的静默启动卡死（同命令偶发数分钟零输出）由 watchdog 兜底；wodex
  网关偶发 403 重试即恢复；moonshot Key 无 `[1m]` 变体权限，目录使用无后缀
  `kimi-k3`（256K）。
- 本地 compose 下 `main` 使用默认网络（Harbor 侧为 no-network）——桥接模式
  下 agent 拿不到容器 shell，无泄漏路径，属 parity 偏差，已记录。

## 6. 未来项

- harbor CLI 就位后的官方容器内 agent 定义（TOML）。
- per-run `PI_CODING_AGENT_DIR` 隔离（对齐 3bench 的做法）。
- `pi-events.jsonl` → ATIF 轨迹导出（`tests/atif.py` 已具备解析侧）。
- 多 roll 聚合脚本（循环调 `run-pi-harness.sh`，汇总 manifest 的 overall）。
