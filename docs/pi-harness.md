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
机**上运行（LLM 调用走宿主机网络），并通过**工具级硬封锁**收敛模型的能力：
`scripts/pi-emsctl-tool.mjs` 注册唯一工具 `emsctl`，启动时 `--tools emsctl`
禁用全部内置工具（无 bash、无 read/write/edit/grep/find/ls）。模型唯一能做
的事就是调用该工具；工具内部以固定 argv（不经过 shell）执行：

```
podman compose -f <task>/environment/docker-compose.yaml \
               -f <task>/environment/docker-compose.local.yaml \
               -p sffd-pi exec -T main emsctl <子命令>
```

子命令白名单：observe / dispatch / reassign / reposition / advance /
finish；`--scenario/--state-dir/--json/--fresh` 四个直连模式全局旗子被拒绝。

## 2. 用法

```bash
bash scripts/pi-setup.sh                # 可选：安装到 PI_CODING_AGENT_DIR 或 ~/.pi/agent（带备份）
bash scripts/pi-smoke.sh --source-config --no-call  # 隔离检查五模型、1M/900K 与最高档
bash scripts/pi-smoke.sh --source-config --provider deepseek --model deepseek-v4-pro

# 完整评测跑（模型五选一；省略 --thinking 时自动取最高档）
bash scripts/run-pi-harness.sh --provider dashscope --model qwen3.8-max --thinking max

# 只验证基础设施（podman machine、镜像构建、socket 健康、一次桥接 observe）
bash scripts/run-pi-harness.sh --provider dashscope --model qwen3.8-max --bootstrap-only
```

常用选项：`--thinking <档>`（默认评测仅接受所选模型的最高档——
claude-opus-5 / kimi-k3 / qwen3.8-max / deepseek-v4-pro 为 `max`，
gpt-5.6-sol 为 `xhigh`；更低档位默认被拒绝）。仅诊断扫描可显式同时传入
`--allow-any-thinking`，正常评测不得使用该逃生口。
诊断逃生口会自动启用 `--no-verify`，run 目录与 manifest 都标记为 diagnostic，
因此低档结果不会混入正式评分。
`--timeout <秒>`（默认 840，= Harbor `agent.timeout_sec` 900 减余量）、
`--task <目录>`、`--keep`（保留容器与卷）、`--no-verify`（跳过评分）、
`--skip-smoke`（只跳过真实 agent 烟测，配置预检始终执行）。密钥以环境变量注入
当前 shell（`WODEX_API_KEY` / `MOONSHOT_API_KEY` / `DASHSCOPE_API_KEY` /
`DEEPSEEK_API_KEY`），不写入任何文件。

## 3. 一次运行发生什么

1. **预检**：把仓库 `config/pi/models.json` 与 `settings.json` 复制到临时
   `PI_CODING_AGENT_DIR`；检查五模型均为 1M、最高档与 100K reserve（900K
   触发压缩）；按名检查所选 provider 的密钥变量；podman machine 未运行则启动。
2. **清场拉起**：`down -v`（`simulator-state` 卷残留会让 simulator 以
   `load_existing=True` 恢复旧回合，必须清掉）→ `up -d --wait`（等
   simulator healthcheck 的 socket）→ 一次桥接 `emsctl observe` 冒烟。
3. **agent 烟测**：一次最小 LLM 调用验证扩展可加载、唯一工具已注册，并要求
   JSONL 合法、assistant 正常停止、精确回复 `OK` 且存在 `agent_end`；默认
   300 秒 watchdog 可由 `PI_HARNESS_SMOKE_TIMEOUT_SEC` 调整。
4. **组装 prompt**：`instruction.md` 全文 + 工具说明段（args 数组用法、
   JSON 语义、时间预算），同一内容写入 workspace 的 `BRIDGE.md`。
5. **跑 pi**：cwd 为每次运行独立的
   `results/pi-runs/<时间戳>-<provider>-<模型>-<思考档>-<运行类型>/workspace`
   （`AGENTS.md` 随之拷入并被 pi 自动加载）；`pi -p --approve --no-session
   --mode json --provider … --model … --thinking … --tools emsctl
   --extension scripts/pi-emsctl-tool.mjs --no-extensions`；手动 watchdog 在
   预算超时后 TERM→KILL。
5. **判定**：解析事件流——pi 的 CLI 在最后一轮 API 错误时也以 0 退出，因此
   按合法 JSONL、`agent_end` 与最后一条 assistant 消息的 `stopReason` 判定
   （`ok` / `error_stop` / `no_agent_end` / `incomplete_assistant` /
   `malformed_event` / `nonterminal_stop` / `timeout` / `pi_exit_error`）。正式运行
   会先完成评分和 manifest，再以非零退出码报告失败。
6. **收集与评分**：`compose cp` 取出 sidecar 的 `/tmp/ems-state`（其中
   `actions.jsonl` 是评分权威动作日志），在宿主机跑 `tests/harbor_verify.sh`。
   三个输出路径必须显式覆盖——脚本默认写 `/logs/verifier/*`，宿主机下
   `mkdir` 会直接失败：
   `HARBOR_REWARD_PATH / HARBOR_DETAILS_PATH / HARBOR_REPLAY_PATH`。
7. **落盘**：`run-manifest.json` 记录模型/档位/diagnostic/时长/超时/判定/reward
   （八键：response、coverage、safety、adaptation、efficiency、integrity、
   overall、strict_pass）/action_count；EXIT trap 自动 `down -v` 清理。

## 4. 与 3bench（VeriForge 版）的差异

| | 3bench `pi_agent.sh` | abench `run-pi-harness.sh` |
| --- | --- | --- |
| 契约 | `VERIFORGE_*` 环境变量 | 无外部契约，自包含 |
| pi 配置 | 每 roll 生成 `PI_CODING_AGENT_DIR` + models.json | 每次运行隔离复制仓库 models/settings，不依赖全局 `~/.pi` |
| 工具白名单 | 仅文件工具（无 bash） | **唯一自定义工具 emsctl**（无 bash、无文件工具、无网络） |
| 判定失败 | fail closed（退出非零） | 先记录 verdict/评分，再退出非零（回合状态仍保留） |
| 运行位置 | runner 子进程 | 宿主机 + 容器桥接 |

## 5. 边界与已知事项（如实）

- **工具级硬封锁（v2 起）**：模型没有 bash、没有文件读写、没有网络工具，
  唯一工具 emsctl 在 harness 进程内以固定 argv 执行白名单子命令。三条外泄
  路径的封堵情况——①宿主机文件读取（`cat scenario.json`）：无任何文件/
  shell 工具，不可达；②容器路径（`exec simulator cat …`）：模型接触不到
  podman，工具的桥接前缀固定为 `main` 服务；③网络外发：无网络工具，LLM
  调用由 harness 进程自身完成。依据：历史两次成功跑的工具使用统计显示模型
  从未读取任何仓库既有文件（文件活动仅限 workspace 内自建辅助脚本），因此
  收敛为单工具不损失任务能力。
- **残余边界**：封锁的执行者是 harness 自己的扩展代码——扩展校验逻辑的
  bug 即漏洞（模型无法绕过工具层，但工具层本身的正确性由本仓库保障）；
  对外声称完全隔离的评测仍应采用容器内 harness 架构（见「未来项」）。
- pi 偶发的静默启动卡死（同命令偶发数分钟零输出）由 watchdog 兜底。五模型
  客户端均声明 1M、900K 触发压缩；Kimi 保持已验证可用的无后缀 `kimi-k3`。
  验收使用短调用，不发送接近 1M tokens 的高成本请求。
- 2026-08-23 隔离 source-config 实测：`claude-opus-5:max`、
  `gpt-5.6-sol:xhigh`、`kimi-k3:max`、`qwen3.8-max:max`、
  `deepseek-v4-pro:max` 均退出码为 0、产生正常 `agent_end` 且回复 `OK`。
- **wodex 网关对 pi 客户端的大请求 403**（"Your request was blocked"）：小
  prompt 的 pi 调用可通过，携带完整任务 prompt（约 5–10KB）的 pi 调用稳定被
  拦；用 curl 复刻完全相同的内容/参数/UA/编码（含 content blocks、
  cache_control、stream、thinking、max_tokens=32768）全部 200。指向网关按
  TLS 指纹 + 请求体大小的组合规则拦截 Node 客户端，需 wodex 侧解决；期间
  wodex 两个模型（claude-opus-5 / gpt-5.6-sol）无法跑完整评测。
- 本地 compose 下 `main` 使用默认网络（Harbor 侧为 no-network）——封锁模式
  下模型无法触达任何网络，该差异仅影响 harness 自身，已记录。

## 6. 未来项

- harbor CLI 就位后的官方容器内 agent 定义（TOML）。
- `pi-events.jsonl` → ATIF 轨迹导出（`tests/atif.py` 已具备解析侧）。
- 多 roll 聚合脚本（循环调 `run-pi-harness.sh`，汇总 manifest 的 overall）。
