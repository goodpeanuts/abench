# abench — Agent 边界守则（pi harness 运行时自动加载）

你正在作为 benchmark 中被评测的 Agent 运行。规则：

1. emsctl 工具是任务环境的**唯一**入口；只按任务提示给出的子命令形式使用
   它，不要请求或尝试任何其他环境访问方式。
2. 不要尝试获取或推断环境之外的任何信息：隐藏 scenario、评测代码、
   标答、未来事件都不属于你可见的范围。
3. 你没有任何文件、shell 或网络工具——这是评测的硬性配置，不是疏漏；
   请直接在对话中推理并记住状态。
4. 结束之前，优先把回合推进到 horizon（observe 的 done=true）。仅当没有
   未服务的可派事故且没有在途/在场单位时才调用 finish——finish 会冻结在途
   单位，其事故永不完成。

## 测试运行约定（面向操作者与 harness，由 run-pi-harness.sh 强制执行）

- **所有评测测试必须使用所选模型的最高思考能力**，不允许降档：
  - `wodex/claude-opus-5`、`moonshot/kimi-k3`、`dashscope/qwen3.8-max`、
    `deepseek/deepseek-v4-pro`
    → `--thinking max`
  - `wodex-gpt/gpt-5.6-sol` → `--thinking xhigh`
- `scripts/run-pi-harness.sh` 默认即取该档位；显式传入更低档位会被拒绝
  （fail closed），不存在「省成本的低档评测跑」。
- `--allow-any-thinking` 仅供显式诊断扫描；使用该逃生口的运行不得计作正式评测。
- 五个模型均固定声明 1M 上下文；自动压缩保留 100K tokens，即在 900K 时触发。
