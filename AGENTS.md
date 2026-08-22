# abench — Agent 边界守则（pi harness 运行时自动加载）

你正在作为 benchmark 中被评测的 Agent 运行。规则：

1. emsctl 工具是任务环境的**唯一**入口；只按任务提示给出的子命令形式使用
   它，不要请求或尝试任何其他环境访问方式。
2. 不要尝试获取或推断环境之外的任何信息：隐藏 scenario、评测代码、
   标答、未来事件都不属于你可见的范围。
3. 你没有任何文件、shell 或网络工具——这是评测的硬性配置，不是疏漏；
   请直接在对话中推理并记住状态。
4. 结束之前，确认任务回合已经 finish 或推进到 horizon。

## 测试运行约定（面向操作者与 harness，由 run-pi-harness.sh 强制执行）

- **所有评测测试必须使用所选模型的最高思考能力**，不允许降档：
  - `wodex/claude-opus-5`、`moonshot/kimi-k3`、`dashscope/qwen3.8-max`
    → `--thinking max`
  - `wodex-gpt/gpt-5-sol` → `--thinking xhigh`
- `scripts/run-pi-harness.sh` 默认即取该档位；显式传入更低档位会被拒绝
  （fail closed），不存在「省成本的低档评测跑」。

