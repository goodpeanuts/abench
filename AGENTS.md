# abench — Agent 边界守则（pi harness 运行时自动加载）

你正在作为 benchmark 中被评测的 Agent 运行。规则：

1. 只通过任务提示（或工作区内的 `BRIDGE.md`）给出的**逐字**桥接命令
   （`podman compose … exec -T main emsctl …`）与任务环境交互；不要发明
   其他进入容器的方式。
2. 仓库中 `tasks/` 目录下的所有内容——尤其是 `scenario.json`、`tests/`、
   `environment/simulator/`、`solution/`——属于评测方私有：不要读取、列出
   或修改它们，也不要推断隐藏的未来事件。
3. 不要创建或修改任何仓库文件；临时文件只写在当前工作目录（workspace）内。
4. 不要使用任务要求之外的网络、不要发送消息、不要产生任何外部副作用。
5. 结束之前，确认任务回合已经 `finish` 或推进到 horizon。
