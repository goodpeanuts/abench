# SFFD Rolling Surge 01：答辩背景与研究定位

> **用途**：项目答辩、研究背景说明和评测设计答疑
>
> **当前版本**：任务 1.1.0，场景 schema `sffd-ems-scenario.v1`，模拟器
> `sffd-ems-simulator/0.1.0`，评分器 `sffd-harbor-verifier-0.2`
>
> **证据快照**：2026-08-23。本文中的“当前结果”是 pilot 结果，不等同于
> 已完成的公开 leaderboard 或经过多窗口统计检验的最终结论。

## 0. 先记住三句话

1. **我们要测的不是模型能否写出一段合理的调度建议，而是它能否在看不见未来、资源有限、状态不断变化的环境中，持续做出可执行且可追溯的决策。**
2. **SFFD 数据提供真实的业务语义和时间分布，模拟器负责计算历史上没有发生过的反事实动作后果。** 历史数据本身不是“标准答案”，也不是完整的城市数字孪生。
3. **目前已经验证了工程闭环的可运行性，但还没有充分证明跨场景的模型区分度。** 正式结论还需要在隐藏窗口上进行基线、强规划器和模型的重复实验。

本文使用三个标签区分证据等级：

| 标签 | 含义 |
|---|---|
| `[事实]` | 来自官方数据页、冻结清单、代码或已保存运行产物，可直接复核 |
| `[设计]` | 我们为了形成可评测环境而作出的明确设计选择 |
| `[待验证]` | 已有合理机制或初步证据，但还不能作为最终科学结论 |

## 1. 研究背景：为什么需要这种 Benchmark

### 1.1 Agent 的能力边界已经从“回答问题”转向“执行决策”

传统语言模型评测通常给模型一个问题，然后比较最终文本答案。Agent 评测的
对象不同：模型需要读取环境状态、调用工具、处理工具返回、记住历史状态，
再决定下一步动作。一次任务的正确性不只取决于最后一句话，还取决于整个动作
轨迹是否满足约束，以及动作造成的实际环境后果。

一个连续调度回合可以抽象为：

```text
隐藏状态 s_t  ── redaction ──> Agent 观察 o_t
     │                              │
     │                              └── Agent 动作 a_t
     │                                      │
     └──── 环境动力学 s_(t+1) = f(s_t, a_t, 随机变量) <─┘
```

在本项目中，`o_t` 只包含当前已揭示的事故和车辆状态，`a_t` 可以是
`dispatch`、`reassign`、`reposition`、`advance` 或 `finish`。未来事故、车辆
真实释放时间和模拟器随机变量保留在环境内部。这样才能测到在线规划、资源
竞争、延迟管理和错误恢复，而不是测模型是否背过某条历史记录。

### 1.2 EMS 调度天然具有长程、在线和高约束特征

急救调度并不是一次性的“给每个事故配一辆最近车辆”。在真实系统中，调度员
同时面对：

- 事故陆续到达，而不是一次全部公布；
- 车辆在途、到场、服务、转运或交接，不能同时服务两个任务；
- MEDIC、BLS、PRIVATE 等资源能力不同，ALS 资源尤其稀缺；
- 位置和旅行时间影响到场延迟；
- 当前为低优先级事故服务，可能会消耗应对下一起高优先级事故的能力；
- 历史系统中的一条实际派遣记录并不等于当时的最优决策。

这些特征正好形成 Agent 的压力测试：模型必须维护状态、遵守协议、在不确定性
下规划，并把整个回合推进到完成。

### 1.3 为什么选择真实数据而不是完全手工编故事

完全合成的场景容易控制，却可能出现不真实的事件频率、时间间隔、资源类型或
服务时长。完全回放历史又不能测试反事实，因为 Agent 改派一辆没有在历史中
发生过的车辆后，数据不会告诉我们它会怎样。

因此本项目采用一个中间方案：

```text
真实 CAD 记录
    ↓ 观察业务字段和时间差
数据校验、事件聚合、隐私字段排除/空间粗化、来源标记
    ↓
经验分布校准的离散事件动力学
    ↓
隐藏未来、可复位、可重放的反事实环境
    ↓
独立 replay + verifier + 可解释评分
```

这种方法保留真实数据的业务约束，又允许我们严格控制随机性、隐私和评测
可复现性。

## 2. 现有研究与领域位置

### 2.1 代表性 Agent Benchmark

下表不是“谁好谁坏”的排名，而是说明已有工作覆盖了哪些能力，以及本项目
为什么仍然有一个交叉领域的研究空间。

| 工作 | 主要测量对象 | 环境和数据特点 | 已有贡献 | 与本项目的差异/未覆盖点 |
|---|---|---|---|---|
| **AgentBench**（Liu et al., ICLR 2024） | LLM 作为工具型 Agent 的推理、决策和指令遵循 | 8 类环境：操作系统、数据库、知识图谱、数字卡牌、侧向思维、家务、网页购物、网页浏览 | 代表性地将多个交互环境放进统一 Agent 评测框架，展示长期推理和决策失败模式 | 主要环境是文本/API、游戏或任务型交互；没有以真实 CAD 历史校准的连续 EMS 事件流、车辆互斥和空间旅行动力学 |
| **GAIA**（Mialon et al., ICLR 2024） | 通用助手的多步工具使用和综合推理 | 466 道问题，要求浏览、计算、文件或外部工具，通常按最终答案评分 | 覆盖面广，能测跨工具综合能力 | 问答式任务没有持续世界状态、外生事件到达和共享资源竞争；最终答案不能解释每次动作造成的业务后果 |
| **WebArena**（Zhou et al., ICLR 2024） | 长程网页操作和网站状态改变 | 自托管的电商、论坛、协作开发、内容管理等网站，约 812 个实例任务 | 真实感较强、可重置、任务有网站状态 verifier | 主要是浏览器和站点状态；用户数据和事件流是程序化的，没有移动资源、连续逻辑时钟和 EMS 能力约束 |
| **OSWorld**（Xie et al., NeurIPS 2024） | 多模态 Agent 在真实桌面/网页应用中的开放式操作 | 约 369 个任务，运行在真实 VM 和桌面应用中，用状态检查器评估 | 覆盖鼠标、键盘、视觉和多应用长轨迹 | VM 任务成本高，通常是单用户、相对静态的最终状态检查，不是持续外生事件与车队调度 |
| **τ-bench**（Yao et al., 2024；Sierra Research 项目） | Agent 与用户及有状态工具的交互可靠性 | 零售 115 题、航空 50 题，后端有订单、航班和业务 API，使用 `pass^k` 测可靠性 | 把工具调用、业务政策和最终数据库状态放在同一个评测里 | 后端数据是程序化业务世界，缺少真实历史事件流、空间旅行时间、资源占用和连续调度竞争 |
| **AppWorld**（Trivedi et al., ACL 2024） | 可执行 API 世界中的多步交互和 collateral damage | 9 个模拟应用、457 个 API、约 750 个任务，使用 state-based unit tests 检查目标状态和副作用 | 已经展示了“动作改变世界状态、verifier 检查状态”的强范式 | 用户、订单和应用是合成数字生活，以 API/数据库状态变更为主，未建模 SFFD 式持续外生事件、空间旅行和车队占用动力学 |
| **ToolSandbox**（Lu et al., 2024） | 有状态、对话式工具使用与中间状态可靠性 | 工具之间存在隐式状态依赖，带 user simulator，并可在轨迹中检查动态 milestones 和 collateral damage | 说明“状态化工具 + 用户模拟 + 动态检查”是可行的评测范式 | 主要是合成的数字工具世界，没有真实 EMS 事件到达、移动资源占用和旅行/释放动力学 |
| **CRMArena**（Liu et al., 2024） | 专业 CRM 工作流中的多步 Agent 能力 | 面向销售、客服等角色的互联业务对象和任务，强调现实业务约束 | 证明领域化、业务对象丰富的 Agent benchmark 能显著拉开模型差距 | CRM 状态和任务是合成的离散业务世界，不包含公共安全的连续时间、空间资源竞争和真实 CAD 校准 |
| **BrowserGym**（2024） | 浏览器 Agent 的统一实验接口 | Gym 风格的 observation/action/task/evaluator，整合 WebArena、WorkArena、AssistantBench 等套件 | 提供可复用的环境和实验基础设施 | 是环境框架而非单一真实数据集；主场景仍是 Web UI/DOM，不能替代 EMS 动力学 |
| **AssistantBench**（2024） | 现实、耗时的网页研究和信息整理 | 约 214 个自动评测任务，要求跨网页检索和多步整理 | 比普通网页问答更长、更贴近真实工作 | 主要按最终答案或证据评分，缺少连续时间、容量约束和动作反事实 |
| **InterCode**（2023） | 交互式编程 Agent | Bash、SQL、Python 等 Docker 沙箱，执行反馈作为下一次观察 | 标准化“执行→观察→再执行”的工具循环，隐藏测试可复核 | 代码沙箱的动力学通常确定且没有空间、时间、外生事件和多资源互斥 |
| **AgentDojo**（Debenedetti et al., 2024） | 动态工具环境中的 prompt injection 安全与防御 | 邮件、银行、旅行等工具任务和隐藏安全测试 | 说明动态状态、工具调用和隐藏测试可以组合评估 | 目标是安全攻击/防御，不是 EMS 时效、覆盖和资源调度；两者的“隐藏”语义不能混为一谈 |

这些工作已经解决了很多重要问题。本文不应声称“以前没有 Agent Benchmark”，
也不把下面的对比表当作穷尽式系统综述。更准确的、且可由本次核对支持的说法是：
**在我们核对的代表性 Benchmark 中，已有工作分别覆盖网页、桌面、代码、客服
工具或综合问答，但尚少有一个把真实公共安全运营数据、连续外生事件、移动资源
竞争、部分可观测反事实动力学、独立回放和 Harbor 任务封装同时结合起来的评测
任务。** 这是一项待继续检索和复核的研究定位判断，不是“全球不存在先例”的证明。

### 2.2 EMS 运筹和强化学习研究已经存在，但研究对象不同

EMS/ambulance dispatch 本身不是空白领域。运筹学研究已经讨论在线救护车派遣、
车辆再部署、需求预测和离线最优策略。例如 Jagtenberg、van den Berg 与 van der Mei
（在线发表 2016，印刷卷 2017）将“所有事故已知”的 offline optimal policy 作为在线策略的上界，
并比较最近车辆启发式与更强在线策略的迟到比例。Allen、Pearn 与 Monks
（2021）则提供了面向救护车位置问题的 OpenAI Gym-compatible 仿真框架。

这些研究的目标通常是比较调度算法或优化策略；它们不主要解决以下问题：

- 如何把 LLM Agent 限制在稳定的工具契约中；
- 如何隐藏未来并记录完整 tool trajectory；
- 如何在 Agent 停止后用独立 verifier 重放；
- 如何把日志、状态 hash、随机数 manifest 和评分结果绑定起来；
- 如何比较不同语言模型在同一真实数据驱动环境中的行为可靠性。

因此，本项目的创新位置是**评测任务和基础设施的结合**，不是声称发明了新的
EMS 优化算法。

### 2.3 交叉领域的具体空白

| 需求 | 常见已有方案 | 本项目要补齐的内容 |
|---|---|---|
| 真实世界依据 | 历史数据集或合成任务二选一 | 用公开 SFFD/DEM CAD 记录校准事件、资源和时间分布 |
| 反事实后果 | 直接回放历史动作，或手工固定常数 | 由离散事件模拟器根据 Agent 动作计算旅行、到场、服务和释放 |
| 在线不确定性 | 一次性公开全部题目 | 只揭示当前及过去事件，未来事件在 `advance` 后进入观察 |
| 资源竞争 | 单个 API 调用或独立任务 | 12 辆异构 EMS 车辆共享同一状态，车辆互斥占用 |
| 可追溯性 | 最终成功/失败标签 | append-only action/event log、state hash、RNG manifest、独立双次 replay |
| 可解释评分 | 二值任务成功 | response、coverage、safety、adaptation、efficiency、integrity 六维分数 |
| 防过拟合 | 固定公开测试集 | 评测器私有的 10 个真实历史窗口和窗口独立 seed |
| 任务封装 | 各项目自定义 runner | Harbor 1.4 风格 `task.toml`、独立 verifier、sidecar 和 artifact transfer |

## 3. 本项目的研究问题、贡献和非贡献

### 3.1 研究问题

1. **可行性问题**：SFFD 的 response-level CAD 记录是否足以构造一个有状态、
   可复位、可计算反事实的 EMS Agent 环境？
2. **测量问题**：在信息不完整、资源有限和事件持续到达时，模型能否保持状态
   一致、遵守资源能力约束，并优先减少高优先级事故的到场延迟？
3. **可靠性问题**：同一初态和同一 action log 是否能稳定重放到相同状态 hash，
   verifier 是否能定位篡改后的第一个 divergence？
4. **区分度问题**：简单规则、强规划器和语言模型之间的分差，是否稳定大于
   不同真实窗口带来的自然波动？

### 3.2 主要贡献（建议答辩时使用的保守表述）

**贡献一：真实数据到反事实环境的可审计转换链。**

我们没有把历史派车结果当作唯一正确答案，而是将可观察历史字段标为
`observed`，由时间差、区域和能力推导的内容标为 `derived`，Agent 动作造成的
状态转移和得分标为 `counterfactual`。这让“真实”与“模拟”之间的边界清楚可查。

**贡献二：面向连续决策的 EMS 状态机。**

事故会陆续出现，车辆会经历 `AVAILABLE → EN_ROUTE → ON_SCENE → AVAILABLE` 等
阶段，Agent 必须在每个观察点重新规划，而不是提交一张静态 dispatch 表。

**贡献三：隐未来和公平反事实。**

Agent 看不到未来事件、未来释放时间、校准表和随机 seed；不同策略在同一场景中
共享同一动力学和隐藏 seed。这样比较的是决策能力，而不是读取答案或碰巧抽到
更有利的环境。

**贡献四：Harbor-compatible 的隔离与回放闭环。**

Agent 只有 `emsctl` 工具，模拟器在 sidecar，verifier 在 Agent 停止后独立运行。
动作日志和状态 hash 允许第三方复核。当前应称为 Harbor-compatible pilot，
因为本机 Podman smoke 已通过，但官方 Harbor runner 尚未在本项目中完成验证。

**贡献五：连续、可解释而非单一二值的评分。**

分数同时体现时效、服务覆盖、操作安全、动态适应、效率和完整性，并保留
`late_count`、`completion_rate`、`high_priority_missed` 等诊断字段。

### 3.3 明确不声称的内容

- 不是旧金山消防/EMS 的 100% 数字孪生；单一 SFFD 数据集无法提供患者临床结局、
  完整医院容量、道路实时拥堵或所有警察调度信息。
- 不是完整的城市 911 警消联合调度数据集。`call_number` 与 Police 和 Fire 共用
  DEM 编号，但该数据集的响应行仍是 Fire/EMS 侧记录，没有完整 SFPD 车辆生命周期。
- 不是临床安全验证，也不应直接用于真实调度决策。
- 不是新的 EMS 优化算法；研究对象是 Agent 评测环境和测量方法。
- 当前尚不能声称已经证明 frontier Agent 的稳定区分度。

## 4. SFFD 数据集：完整内容与可信边界

### 4.1 官方来源

| 项目 | 内容 |
|---|---|
| 数据集 | Fire Department and Emergency Medical Services Dispatched Calls for Service |
| Socrata ID | `nuek-vuh3` |
| 发布方 | San Francisco Fire Department / Department of Emergency Management |
| 官方页面 | [DataSF 数据页](https://data.sfgov.org/Public-Safety/Fire-Department-and-Emergency-Medical-Services-Dis/nuek-vuh3) |
| API 元数据 | [Socrata metadata API](https://data.sfgov.org/api/views/nuek-vuh3) |
| CSV API | [Socrata CSV endpoint](https://data.sfgov.org/resource/nuek-vuh3.csv) |
| 许可证 | PDDL（Open Data Commons Public Domain Dedication and License） |
| 记录语义 | 一行通常表示某个 unit 对某个 incident 的一次 response；一个 incident/call 可以有多行 |
| 更新方式 | 官方页面说明为每日自动更新 |
| 冻结门户快照 | 7,408,845 条 response 行，36 个业务字段，覆盖 2000-04-12 至 2026-08-21 |

官方页面明确说明：数据来自 DEM 的 CAD 系统，包含 Fire units 对 911 calls 的
响应，也包括需要 EMS 人员的 Medical Incident；因为一个 call 往往派出多个
unit，所以不能把一行直接当作一个事故。

### 4.2 数据中出现的主要内容

在门户某次 metadata snapshot 中，常见 `call_type` 包括以下约数。由于官方数据
每日更新，这些分项统计不是本题 release CSV 的冻结统计；答辩时以 manifest 的
总量和 hash 为准：

| call type | response 行数（约） |
|---|---:|
| Medical Incident | 4,900,712 |
| Alarms | 866,365 |
| Structure Fire / Smoke in Building | 780,652 |
| Traffic Collision | 291,206 |
| Other | 128,242 |
| Citizen Assist / Service Call | 117,205 |

常见 `unit_type` 及 response 行数包括：

| unit type | response 行数（约） |
|---|---:|
| ENGINE | 2,675,973 |
| MEDIC | 2,260,623 |
| TRUCK | 775,178 |
| CHIEF | 546,093 |
| PRIVATE | 528,516 |
| RESCUE CAPTAIN | 256,208 |
| RESCUE SQUAD | 121,659 |
| SUPPORT | 112,597 |
| CP | 75,837 |
| BLS | 29,429 |
| AIRPORT | 19,531 |
| INVESTIGATION | 7,201 |

这些数量是 response rows，不是车辆数量，也不是独立 incident 数量。

### 4.3 36 个业务字段完整清单

官方数据页显示 36 个业务字段。Socrata metadata 还可能附带一个自动生成的
polygon computed field `:@computed_region_ajp5_b2md`，它不属于 CSV 的 36 个
业务字段，也没有进入本 Benchmark 场景。

| 分组 | API 字段 | 中文含义 | 原始数据中的主要用途 | 缺失情况/本题处理 |
|---|---|---|---|---|
| 标识 | `call_number` | DEM/911 呼叫编号，警消共用 | 关联同一 call 的多条 response | 校验和来源关联，不向 Agent 暴露 |
| 标识 | `unit_id` | 响应单元编号 | 追踪车辆/单元历史状态 | 生成 roster，当前使用公开单位标识/场景化 ID；若正式发布需要隐私映射，再做稳定映射 |
| 标识 | `incident_number` | DEM 火灾/事件编号 | 聚合同一 incident 的多辆车 | 内部聚合，Agent 使用场景 ID |
| 标识 | `rowid` | `Call Number-Unit ID` 拼接的唯一行号 | 行级更新和审计 | 内部校验，不暴露 |
| 分类 | `call_type` | 呼叫类型 | 区分 Medical、Fire、Alarm、Collision 等 | Medical 可派，其他可做背景事件 |
| 日期 | `call_date` | 接警日期 | 报表日期 | 主要用于过滤和审计 |
| 日期 | `watch_date` | 值班日，08:00 到次日 08:00 | 运营班次分析 | 不进入 Agent observation |
| 时间 | `received_dttm` | 911 接警进入 CAD 的时间 | 事件到达顺序、窗口选择 | 场景公开为逻辑 arrival time |
| 时间 | `entry_dttm` | 操作员录入初始信息的时间 | CAD 延迟分析 | 仅校准/审计 |
| 时间 | `dispatch_dttm` | 向该 unit 发出派遣指令的时间 | dispatch delay | 不公开历史实际派车 |
| 时间 | `response_dttm` | unit 确认出车、进入在途的时间 | dispatch 到 response、旅行阶段起点 | 校准；校准片缺失约 2.84% |
| 时间 | `on_scene_dttm` | unit 到达现场时间 | 旅行时间终点 | 校准；缺失约 21.79% |
| 时间 | `transport_dttm` | 救护车开始转运时间 | 现场服务阶段终点 | 校准；缺失约 77.50% |
| 时间 | `hospital_dttm` | 救护车到达医院时间 | 后续扩展的转运/医院阶段 | v1 不模拟；缺失约 77.96% |
| 处置 | `call_final_disposition` | 最终处置代码，如 Code 2 Transport、Fire、Cancelled | 解释历史结果和筛选取消记录 | 不作为 Agent 的 oracle 标签 |
| 时间 | `available_dttm` | unit 脱离该 call、重新可派时间 | 释放/占用结束 | 校准释放阶段；关键字段缺失率约 0 |
| 地理 | `address` | 与事件相关的路口或 call box 地址 | 地理关联 | 不进入场景，避免精确位置和隐私泄漏 |
| 地理 | `city` | 事件城市 | 地理过滤 | 不进入场景 |
| 地理 | `zipcode_of_incident` | 事件邮编 | 区域分析 | 不进入场景 |
| 地理 | `battalion` | 消防应急响应分区 | 粗粒度空间竞争 | 作为主要 zone，可见 |
| 地理 | `station_area` | 消防站首响区域 | battalion 缺失时的回退 | 可见回退字段 |
| 地理 | `box` | 细粒度消防 box（全市 2,400+） | 更细位置/派遣顺序 | 不暴露，避免过度精确 |
| 优先级 | `original_priority` | 初始呼叫优先级 | 研究信息更新前后的差异 | 仅在允许时作为观察；缺失约 1.95% |
| 优先级 | `priority` | 信息评估后的优先级 | 归一化 P1/P2/P3 和 SLA | 当前场景公开规范化 priority |
| 优先级 | `final_priority` | 最终优先级 | 历史事后字段 | 不进入初始观察，避免未来泄漏 |
| 能力 | `als_unit` | 是否含 ALS/paramedic | 车辆能力分析 | 由 roster 转成 `als` 能力 |
| 分类 | `call_type_group` | Fire、Alarm、Potentially Life Threatening、Non Life-threatening | 业务分组 | 辅助分析；缺失约 2.63% |
| 资源 | `number_of_alarms` | 火警级别和资源需求（1 到 5） | Fire 资源需求分析 | v1 不用于 Medical dispatch |
| 资源 | `unit_type` | ENGINE、MEDIC、BLS、TRUCK 等单元类型 | 能力和背景占用 | MEDIC/BLS/PRIVATE 可控，其余可作背景 |
| 顺序 | `unit_sequence_in_call_dispatch` | 本次 call 的派遣序号 | 历史 dispatch 顺序 | 仅审计，不给 Agent 历史答案 |
| 地理 | `fire_prevention_district` | 消防预防区 | 运营空间分析 | 不进入场景 |
| 地理 | `supervisor_district` | 监督区编号 | 运营空间分析 | 不进入场景 |
| 地理 | `neighborhoods_analysis_boundaries` | 旧金山社区边界 | 统计和可视化 | 不进入场景 |
| 来源 | `case_location` | 事件经纬点 | 地图和空间计算 | 不进入场景，精确坐标被排除 |
| 来源 | `data_as_of` | 源系统记录更新时间 | 冻结和数据新鲜度审计 | 内部 manifest |
| 来源 | `data_loaded_at` | 数据加载到门户的时间 | 传输/管道审计 | 内部 manifest |

### 4.4 缺失值和“历史是否处理正确”的回答

这个数据集不是带有“正确/错误处理”标签的监督学习数据集。它记录的是当时
系统发生了什么，不说明当时的派遣是否最优，也不提供患者最终临床结局。

在 release-grade 校准片的 7,183 行中，关键缺失率大致为：

| 字段 | 缺失率 | 解释 |
|---|---:|---|
| `response_dttm` | 2.84% | 有些记录没有完整出车确认 |
| `on_scene_dttm` | 21.7876% | 取消、无法定位或记录不完整时可能没有到场时间 |
| `transport_dttm` | 77.5024% | 并非每个 unit 都是救护车，也并非每次都转运 |
| `hospital_dttm` | 77.9619% | 同上，医院到达只适用于部分 ambulance response |
| `original_priority` | 1.949% | 早期字段可能缺失 |
| `call_type_group` | 2.6312% | 该派生分组不是所有历史行都有 |
| `available_dttm`、`priority`、`unit_type`、`battalion` | 约 0% | 可作为 v1 的稳定基础字段 |

因此，场景生成器不会把缺失时间硬填成一个未标记的常数，而是：

- 对可调度 incident 要求足够完整的 EMS response chain；
- 将取消、无法定位或没有完整链条的事件排除，并记录排除原因；
- 使用有明确样本数的经验分布；校准不足时直接报错；
- 将不完整的 Fire/支援响应保留为背景活动或审计参考，而不是伪装成可派任务。

## 5. 当前第一题如何由数据构建出来

### 5.1 三个冻结层级

原始 CSV 不提交仓库，公开开发者通过脚本从官方 API 获取，正式评测窗口由
评测器私有注入。当前冻结证据如下：

| 层级 | 时间范围/作用 | 行数 | 作用 |
|---|---|---:|---|
| 开发候选切片 | 2025-03-10 08:00 至 2025-03-11 13:00 | 1,356 | 早期结构验证和候选窗口筛选；不是最终 release 校准全集 |
| release 校准片 | 2025-03-04 09:00 至 2025-03-11 13:00 | 7,183 | 过去 7 天 roster、初始占用和经验动力学校准 |
| 评测器私有源片 | 2025-02-15 09:00 至 2025-03-12 13:00 | 25,375 | 生成 10 个不同真实 20 分钟窗口和隐藏变体 |

关键 release 校准片 SHA-256：
`7e6cd09f650a9f7fdb3c8f8cf1577ec066bb8280d7dbf7bd36137f3675283325`。
开发切片 SHA-256：
`0f1a990252574048f118da202438ebc51233d954c7a71067663b610343648f6b`。
字段 schema hash 为
`b5c88a74a5bbedbce283120b5921dbd5ceb4c81a626d2f6e62b291a03360d06d`。

评测器私有源片的 SHA-256 为
`8b837de4e9e4ab8109f53775e9c9be396fa1d6f27751a5b9fcb404a5f83a7558`；源数据本身
不提交仓库，Agent 也不可见。

完整来源、查询、抓取时间和校验信息见
[`data_manifest.json`](../tasks/sffd-rolling-surge-01/data_manifest.json)。

### 5.2 候选窗口和场景边界

当前开发题固定为：

| 项目 | 值 |
|---|---|
| 场景 ID | `sffd-rolling-surge-01` |
| `T0` | 2025-03-11 10:00:00（来源时区 America/Los_Angeles） |
| 事件揭示窗口 | 1,200 秒，即 20 分钟 |
| 模拟 horizon | 7,200 秒，即 2 小时逻辑时间 |
| 总到达事件 | 16 个 |
| 其中 Medical Incident | 15 个 |
| 其中可调度 Medical Incident | 12 个 |
| 背景/排除事件 | 4 个非 dispatchable incident（对应 11 条 background response rows），不进入 Agent 的 dispatch 目标 |
| 可调度优先级 | P1×1、P2×7、P3×4 |
| 事件空间 | 6 个 battalion 粗粒度 zone |
| 15 分钟峰值 | 10 个可调度事件到达 |

4 个排除事件不是随意删除，而是写入 manifest：

| 场景 ID | 排除原因 |
|---|---|
| I001 | 没有完整 EMS response chain，且没有 EMS unit response |
| I006 | cancelled 或 unable to locate，且没有完整 response chain |
| I009 | 没有完整 response chain |
| I015 | 没有完整 response chain、没有 EMS response，且不是 Medical Incident |

### 5.3 初始车队不是根据未来答案手工挑选

场景使用 T0 前完整 7 天历史，按预先声明的配额和稳定哈希选择 12 个 unit：

- 8 个 MEDIC，其中 7 个 ALS；
- 2 个 BLS；
- 2 个 PRIVATE；
- 开局 10 个 `FREE`，2 个 `BUSY`；
- 初始区域来自 T0 前历史的主要区域代理；
- roster 选择采用 `sha256(seed:unit_id)` 排名和固定能力配额。

这一步很重要：如果直接从未来窗口里挑“恰好响应得最好”的车辆，Agent 评测会
发生答案泄漏，也无法公平比较。当前 roster 为：

```text
AM114 AM122 AM207 AM215
M505 M552 M554 M555 M571 M584 M590 RA48
```

### 5.4 从 response 行推导阶段动力学

历史字段形成以下时间链：

```text
received → dispatch → response → on_scene → transport → hospital → available
```

第一版只实现四个阶段：

1. `dispatch`：Agent 决定把当前可用 unit 分给 incident；
2. `travel`：`response_dttm → on_scene_dttm` 的经验分布；
3. `service`：优先使用 `on_scene_dttm → transport_dttm`，没有转运时使用
   `on_scene_dttm → available_dttm`；
4. `release`：有转运时使用 `transport_dttm → available_dttm`，表示转运和交接
   期间车辆仍然被占用。

release 校准片包含：

- travel observations：5,617；
- service observations：5,595；
- release observations：5,595；
- dispatch-delay observations：6,978。

这里要区分“已统计”和“已进入 v1 动力学”：`dispatch_delay` 已被保存到校准和
来源清单，用于审计以及后续扩展；当前 v1 模拟器在 Agent 接受 `dispatch` 后直接
排入 `travel` 阶段，尚未把这项分布额外叠加到反事实到场时间。答辩时不能把它
说成已经影响当前分数的独立阶段。

模拟器按 `unit_type × origin_zone × destination_zone × time_bucket` 查找旅行
分布，按 `unit_type × priority` 查找 service/release 分布。同一隐藏窗口内的所有
策略共享该窗口的动力学 seed；不同 hidden variant 使用各自独立的 seed。这样在
同一窗口内比较策略时，不会因为环境随机性不同而产生伪差异。

### 5.5 离散事件队列和反事实状态转移

模拟器用逻辑时间处理事件，排序键为：

```text
(sim_time, event_priority, monotonic_sequence)
```

当 Agent 派 `M505 → I004` 时，环境会根据当前区域、能力、优先级和隐藏抽样值
安排未来的到场、服务和释放事件。如果 Agent 改派 `M590 → I004`，这些事件
会重新计算。这样才存在真正的反事实比较：历史记录只负责告诉我们分布长什么
样，环境负责回答“换一个动作会怎样”。

### 5.6 当前版本有意不模拟的内容

- 警察车辆和完整 SFPD 调度；
- 医院床位/急诊容量；
- 患者临床结局和治疗质量；
- 道路实时 API、天气和交通事故外生模型；
- 医院和转运的独立动作阶段（保留后续扩展接口）。

限制边界是为了让第一题能被准确验证，而不是假装已经重建了所有城市系统。

## 6. Agent 接口、Harbor 和 Harness

### 6.1 Agent 看到的唯一接口

Agent 通过唯一工具 `emsctl` 访问 simulator sidecar：

```text
emsctl observe
emsctl dispatch --incident ID --unit UNIT --state-version N --action-id ID
emsctl reassign --incident ID --from UNIT --to UNIT --state-version N --action-id ID
emsctl reposition --unit UNIT --zone ZONE --state-version N --action-id ID
emsctl advance --next-event
emsctl finish --reason TEXT
```

观察包含：

- 当前逻辑时间和 `state_version`；
- 已揭示 incident 的 ID、priority、zone、status；
- unit 的 ID、类型、ALS 能力、区域和占用状态；
- 新通知和已经发生的事件。

观察不包含：

- 未来 incident；
- 未来 release time；
- calibration table、随机 seed、oracle、verifier；
- source rowid、精确地址和坐标；
- 历史实际派车结果和 hidden reference responses。

写操作有三个关键协议约束：

- 过期 `state_version` 被拒绝；
- 重复 `action_id` 幂等，不重复改变状态；
- 忙碌车辆、未知对象和能力不匹配的派车被拒绝，且拒绝不改变状态。

回合控制也属于任务语义：Agent 通常应通过 `advance --next-event` 把逻辑时间推进
到 horizon 并取得 `done=true`；过早 `finish` 会冻结仍在途或在场的单位，相关事故
不会被凭空判定为完成。因此“停止调用工具”本身不是一种免费成功策略。

### 6.2 Harbor 是协议/运行封装，不是模型

Harbor 是用于启动任务环境、隔离 Agent 与 verifier、转移 artifacts 和生成
评测产物的框架。当前任务的 [`task.toml`](../tasks/sffd-rolling-surge-01/task.toml)
声明 Harbor 1.4 风格的任务 schema、no-network 环境和 separate verifier。

实际架构是：

```text
模型 API
   │ 由宿主机 pi harness 调用
   ▼
pi Agent（唯一注册 emsctl 工具）
   │ 固定 argv，不提供 bash/read/write/network
   ▼
main 容器 ── Unix socket ── simulator sidecar
                                  │
                                  ├─ 隐藏 scenario / seed / calibration
                                  └─ append-only actions/event/state logs
                                           │ Agent 停止后转移
                                           ▼
                                  独立 verifier 容器
                                           │
                                           ▼
                                  reward.json + replay manifest + ATIF
```

### 6.3 之前实际调用的模型和运行形式

需要把两个阶段分开说：

1. **早期环境校准**使用 `tests/baselines.py` 的五个手写规则策略，没有调用任何
   LLM。它们只能使用公开 `observe()`/`step()` 接口。
2. **后续 v1.1.0 诊断批次**才通过宿主机 `pi` harness 调用了五个模型：
   `claude-opus-5`、`gpt-5.6-sol`、`deepseek-v4-pro`、`kimi-k3`、
   `qwen3.8-max`。模型使用各自允许的最高思考档：Opus/Kimi/Qwen/DeepSeek
   为 `max`，GPT-5.6 Sol 为 `xhigh`。

模型不能执行 shell、读写文件或访问网络；它只能生成 `emsctl` 工具调用。任务
结束后，verifier 读取 sidecar 的权威 action log，独立重放并计算分数。完整运行
流程见 [`docs/pi-harness.md`](pi-harness.md)。

当前本地 Podman compose smoke 已验证 sidecar、独立 verifier、只读 artifact、
重放和 reward schema；但 manifest 明确记录 `official_harbor_runner_validated=false`。
所以答辩中应说“Harbor-compatible pilot”，不要说“已经通过官方 Harbor runner”。

## 7. 评分：它测量的到底是什么

### 7.1 官方总分

评分器使用：

```text
overall =
  0.40 × response
+ 0.20 × coverage
+ 0.15 × safety
+ 0.10 × adaptation
+ 0.10 × efficiency
+ 0.05 × integrity
```

每个事故的到场质量为：

```text
q = clip(1 - response_delay / SLA, 0, 1)
```

当前优先级权重和 SLA 为：

| 优先级 | 权重 | SLA |
|---|---:|---:|
| P1 | 1.00 | 480 秒 |
| P2 | 0.75 | 900 秒 |
| P3 | 0.50 | 1,200 秒 |

`response` 使用 priority-weighted `q²`。平方的作用是让接近 SLA 或超过 SLA 的
反复迟到更明显地扣分。`coverage` 只给实际到场并完成服务的 incident，并按
同一个 `q` 做质量加权，不是简单的“12 个完成了几个”。

当前实现还使用：

```text
service_gate = response × coverage
```

`safety`、`adaptation`、`efficiency` 会受 service gate 约束。这样模型不能靠
发送大量形式上合法但没有完成服务的操作来获得高运营分。

### 7.2 六个维度的通俗解释

| 维度 | 白话问题 | 当前含义 |
|---|---|---|
| `response` | 车到得够不够快？ | 优先级加权的 SLA 到场质量，平方惩罚迟到 |
| `coverage` | 事故真的被服务完成了吗？ | 到场且完成的 priority-weighted timely coverage |
| `safety` | 有没有违反资源和协议规则？ | 能力约束、车辆互斥、状态版本和非法动作 |
| `adaptation` | 新事故或资源变化后会不会重规划？ | 动态观察后的调整能力，当前单窗口测量仍有限 |
| `efficiency` | 有没有无谓重派、回位或 churn？ | 操作成本和无效动作，当前与 service gate 有耦合 |
| `integrity` | 证据能否被复核？ | action/event log、state hash、双次 replay 完整性 |

### 7.3 `strict_pass` 和 `overall` 不是一回事

`strict_pass` 是一个不参与加权的 0/1 全通门槛，要求全部事件完成、没有高优先级
漏派、没有 SLA 迟到、没有非法操作，并且日志能够稳定重放。一个模型可以有
`overall=0.25` 但 `strict_pass=0`；这表示它有部分业务能力，但没有满足全部
硬条件。

例如当前场景的 P1 事故 I004 在逻辑时间 178 秒到达，某些完整轨迹在 693 秒
才到场，延迟 515 秒，超过 P1 的 480 秒 SLA，因此 `q=0`。即使车辆后来真的
完成服务，该事故也不能获得及时 response/coverage 分数，也不能通过 strict pass。

### 7.4 必须区分 proxy 和 authoritative score

`tests/baselines.py` 里有一个只使用公开观察的运行时 proxy，便于策略调试；
它不是正式 leaderboard 分数。真正应引用的是 `authoritative_policies`，即
把同一 action log 交给独立 verifier replay 后得到的分数。

## 8. 当前初步结果与正确解读

### 8.1 五个规则基线：单个开发窗口

权威结果来自 `/private/tmp/abench-sffd-final-baselines.json` 的
`authoritative_policies`：

| 策略 | overall | response | coverage | 解释 |
|---|---:|---:|---:|---|
| no-op | 0.050000 | 0 | 0 | 不服务，只保留 0.05 的完整性残余 |
| random-feasible | 0.198002 | 0.182229 | 0.284747 | 能完成，但随机选择造成更明显迟到 |
| rolling-horizon | 0.219143 | 0.201967 | 0.326414 | 在线启发式略好于随机，但远未达到 0.90 |
| nearest-unit | 0.330338 | 0.336347 | 0.458889 | 本窗口的简单规则最好 |
| priority-nearest | 0.330338 | 0.336347 | 0.458889 | 与 nearest 动作完全相同 |

这组结果说明评分链路能够产生合理的基础排序，但不能证明 Benchmark 已经有
稳定的模型区分度。当前每次 dispatch 时可见队列中通常只有一个 incident，因而
`nearest-unit` 和 `priority-nearest` 没有真正发生优先级抢占冲突；
`safety`、`adaptation`、`efficiency` 在这些不使用 reassign/reposition 的策略上
也部分退化为同一个 service gate。

### 8.2 五模型冻结诊断批次

以下数字来自
[`v1.1.0 trajectories manifest`](sffd-rolling-surge-v1.1.0-trajectories-manifest.md)，
是同一场景、每个模型一条最高思考档运行，不是多窗口统计 leaderboard：

| 模型 | thinking | overall | 完成事故 | 逻辑时间 | 主要现象 |
|---|---|---:|---:|---:|---|
| Claude Opus 5 | max | 0.287464 | 12/12 | 7,200 | 完成闭环，但有迟到和少量 invalid；属于早期 carry-over，建议统一重跑 |
| GPT-5.6 Sol | xhigh | 0.252751 | 12/12 | 7,200 | 工具操作干净，但仍有 SLA 迟到 |
| DeepSeek V4 Pro | max | 0.208978 | 12/12 | 7,200 | 完成服务，但迟到和协议错误更多 |
| Kimi K3 | max | 0.197613 | 12/12 | 7,200 | 完成服务，等待资源造成较多迟到 |
| Qwen 3.8 Max | max | 0.076426 | 0/12 | 658 | 提前停止，`done=false`，不是 replay 或日志损坏 |

五条轨迹的 `integrity=1`、重复 hash 一致，但 `strict_pass` 全部为 0。正确
解释是：这批运行已经显示出工具循环完成度和响应时效上的行为差异，**但一条
窗口、每模型一次运行不足以支持模型排名或泛化结论**。正式模型比较至少应
使用同一版本、同一思考档、每模型多次运行，并报告均值、标准差和置信区间。

## 9. 当前有效性判断：工程闭环有效，测量结论仍需校准

### 9.1 已经有证据支持的部分

- 数据字段、rowid 唯一性、CSV 宽度和隐私排除规则经过测试；
- 冻结场景有明确 URL、查询、抓取时间、schema hash、CSV hash、seed 和版本；
- simulator 对事件排序、车辆互斥、状态版本和幂等 action 有测试；
- release manifest 中冻结的测试快照为 62/62；后续扩展的当前工作树
  `bash tasks/sffd-rolling-surge-01/tests/test.sh` 为 68/68。两者属于不同提交的
  测试计数，不能混写成同一版本；两者都包含重放、篡改 divergence 和隐藏状态回归；
- Podman smoke 验证了 main、sidecar、独立 verifier、只读 source artifacts、
  replay 和 reward schema；
- 10 个隐藏真实窗口已经生成，10 个 event-stream fingerprint 不同，最小间隔
  13.666667 小时，场景都可加载，重复 replay hash 一致。

### 9.2 还不能声称的部分

**第一，`rolling-horizon ≥ 0.90` 尚未被验证为合理门槛。**

当前 rolling baseline 只有 0.219143。一个辅助性的诊断估算显示，在“每个事故一
到达就立即派车、所有车辆永远可用、每个事故取最短校准旅行时间”的极度乐观假设
下，当前 SLA、平方 response 和 service gate 组合的 overall 约为 0.595。这个
数字不是正式 oracle、不是全局最优证明，也不应被当作最终 leaderboard 结果；它
只是提示 0.90 与当前窗口和评分公式可能不匹配。应先用可复现的强规划器和
evaluator-only oracle 上界重新校准阈值，不能把失败简单归因于模型能力不足。

**第二，当前单窗口没有充分测出优先级排序和动态适应。**

需要构造同时有多个 queued incident、ALS 稀缺、重派有价值、背景占用明显的窗口，
否则不同策略可能做出相同动作。

**第三，10 个隐藏变体尚未完成模型运行。**

生成变体只能证明数据管线和 replay 可用，不能证明 frontier Agent 在变体上有
稳定分差。必须真正运行基线、强规划器和模型，报告跨窗口分布。

**第四，当前动力学校准存在因果性审计风险。**

roster 初始状态使用 T0 前历史，但当前生成器的 travel/service/release 统计在
实现上遍历输入 `rows`，可能包含 T0 之后的行。对 Agent 本身没有直接泄漏，因为
校准表不在 observation 中，但如果论文声称“严格只用 T0 之前的数据校准”，
该说法目前不成立。正式发布前应改为仅使用 pre-T0 calibration，或在报告中明确
这是 evaluator-side lookahead 并给出样本数和敏感性分析。

**第五，官方 Harbor runner 尚未验证。**

当前是 Harbor-compatible task + Podman/pi host bridge。将任务发布为正式 Harbor
数据集前，还需要在目标 Harbor 版本中运行一次完整容器内流程，并固定协议版本。

## 10. 让 Benchmark 真正具备区分度的实验计划

### 10.1 先校准环境，再比较模型

对 10 个隐藏真实窗口执行同一实验矩阵：

```text
10 windows ×
  no-op
  random-feasible（多个 policy seed）
  nearest-unit
  priority-nearest
  rolling-horizon
  stronger public-state planner
  evaluator-only oracle upper bound
```

每个策略共享同一窗口的动力学 seed；random 策略只改变自己的 policy seed。报告：

- 每个窗口和每个策略的 raw score；
- 均值、标准差、95% 置信区间；
- 跨窗口排名稳定性；
- 强规划器与 oracle 的可达上限；
- 分差是否大于窗口方差。

### 10.2 用可达上限重新决定 `0.90`

建议规则：

- 如果 oracle 也低于 0.90，降低或改写 provisional threshold；
- 如果 oracle 接近 0.90，但强公开规划器明显低于它，说明有真实 Agent 能力空间；
- 如果简单规则和强规划器分差很小，增加冲突场景或重做评分维度；
- 如果某个维度在所有策略上几乎相同，不要继续把它当作已验证的区分维度。

阈值应由跨窗口可达分布预注册，而不是先写一个 0.90 再强行解释。

### 10.3 补足真正的连续决策难点

后续隐藏窗口应覆盖：

1. 多个事故同时 queued，必须选择服务顺序；
2. P1 与 P2/P3 冲突，观察优先级是否真的改变动作；
3. ALS 车辆不足，测试能力匹配和保留资源；
4. 车辆释放与新事件交错，测试 rolling replan；
5. reassign/reposition 有明显收益但也有 churn 代价；
6. 外生 ENGINE/TRUCK/CHIEF 占用造成背景资源压力；
7. 时间分布、空间分布和单位构成跨窗口变化。

### 10.4 完成正式模型实验的最低规范

- 同一 `data_version`、task version、prompt 和 thinking 档；
- 每个模型至少 3 条有效、非诊断运行；
- 记录 `n`、均值、标准差、双侧 95% CI；
- 失败运行保留原始 trajectory，不静默删除；
- 预注册官方 `overall`，reference 轨只用于解释；
- 任一组样本不足或置信区间包含零差异时，报告“证据不足”，不宣布赢家；
- 在修复 calibration lookahead 和官方 Harbor runner 验证后重新冻结版本。

## 11. 答辩常见问题与简短回答

### Q1：历史数据只有 response 记录，怎么知道换一种派车会怎样？

不知道历史上没有发生过的动作的真实结果，所以不能直接从表中读取反事实。
我们的做法是用历史记录估计旅行、服务和释放时间分布，再由程序化 simulator
在同一分布和隐藏 seed 下计算替代动作的后果。历史数据校准世界，模拟器计算
反事实。

### Q2：历史 dispatcher 的动作是不是标准答案？

不是。历史动作只是观察到的行为，可能受当时的信息、人员和策略影响。我们不
把它作为唯一标签，而是使用规则基线、强规划器和 evaluator-only oracle 形成
行为参照和可达上界。

### Q3：数据集是不是包含警察、消防、救护车全部信息？

不是完整的警消联合数据。它是 Fire Department/EMS response 数据，包含 ENGINE、
TRUCK、CHIEF、MEDIC、BLS、PRIVATE 等 Fire/EMS 相关单元。`call_number` 虽然是
Police 和 Fire 共用的 DEM 编号，但数据没有完整的 SFPD 车辆位置和调度生命周期。

### Q4：为什么不直接用精确经纬度和道路 API？

精确坐标会增加隐私和答案泄漏风险，实时道路 API 也会破坏离线、可重放和跨时间
一致性。第一版使用 battalion/station_area 粗粒度区域和历史经验旅行分布；后续
可以在不泄漏坐标的前提下加入经过冻结的路网版本。

### Q5：Harbor 是不是被评测的模型？

不是。Harbor 是任务和运行封装框架；模型通过 Harness 调用 `emsctl`，模拟器产生
状态转移，verifier 独立重放并输出 reward。之前的五个规则基线也不是语言模型。

### Q6：为什么完成 12/12 仍然只有 0.25 左右？

因为 `overall` 不只是完成率。到场迟到会降低平方 response quality，coverage
还要求及时到场并完成服务，三项运营分又受 service gate 约束。完成但迟到的
轨迹可以有部分分，但不能被误读为高质量调度。

### Q7：当前能不能说 Benchmark 已经有效地区分模型？

可以说接口、隔离、replay 和评分工程闭环已经通过 pilot 验证；不能说跨场景
模型区分度已经证明。需要完成 10 个隐藏窗口、强规划器/oracle、因果校准修复和
正式 Harbor runner 验证后，才能做更强的结论。

### Q8：你们的创新是提出了新的调度算法吗？

不是。EMS 调度算法已有大量运筹研究。我们的创新主张是把真实 CAD 数据、反事实
动态环境、隐藏未来、工具协议、独立 verifier、可重放日志和可解释评分组合成一
个面向 Agent 的评测任务，并明确验证边界。

## 12. 一页式答辩收束

可以用下面这段作为最后总结：

> SFFD Rolling Surge 01 是一个基于真实旧金山消防/EMS CAD 记录构建的连续决策
> Agent Benchmark。原始数据一行代表一个响应单元对一个事件的响应，包含接警、
> 派遣、出车、到场、转运、医院和恢复可用等时间字段，但不包含反事实结果或
> 最优性标签。我们先按事件聚合和隐私规则粗化空间，再用 T0 前历史构建初始
> 车队，并用经验时间分布校准离散事件模拟器。Agent 只能通过 `emsctl` 看到当前
> 状态，未来事件、seed 和 verifier 被隔离；它的每个动作都会改变后续资源可用
> 性和事件结果。任务结束后，独立 verifier 重放 action log，按响应时效、及时
> 覆盖、安全、适应、效率和完整性计算分数。
>
> 这项工作的价值不是声称已经重建了完整城市，也不是发明新的 EMS 调度算法，
> 而是提供一条真实数据到可重放 Agent 环境的完整、可审计转换链。当前 pilot 已
> 证明工程闭环可运行，并观察到不同模型在同一窗口上的行为差异；但我们仍把
> 跨窗口区分度视为待验证问题。下一步是在 10 个隐藏真实窗口上重复基线，加入
> 强规划器和 oracle 上界，修复校准因果性风险，重新确定评分阈值，然后再做正式
> 的模型比较。

## 13. 参考资料

### 数据和运行框架

1. San Francisco DataSF, *Fire Department and Emergency Medical Services Dispatched Calls for Service*, dataset `nuek-vuh3`：
   <https://data.sfgov.org/Public-Safety/Fire-Department-and-Emergency-Medical-Services-Dis/nuek-vuh3>
2. Socrata metadata API：<https://data.sfgov.org/api/views/nuek-vuh3>
3. Harbor Framework repository：<https://github.com/harbor-framework/harbor>
4. 本仓库的 Harbor/pi Harness 说明：[`docs/pi-harness.md`](pi-harness.md)
5. 本题设计说明：[`docs/sffd-rolling-surge-demo.md`](sffd-rolling-surge-demo.md)
6. 数据、查询和 hash 清单：[`tasks/sffd-rolling-surge-01/data_manifest.json`](../tasks/sffd-rolling-surge-01/data_manifest.json)

### Agent Benchmark

1. Liu et al., *AgentBench: Evaluating LLMs as Agents*, ICLR 2024：
   <https://arxiv.org/abs/2308.03688>
2. Mialon et al., *GAIA: a benchmark for General AI Assistants*, ICLR 2024：
   <https://arxiv.org/abs/2311.12983>
3. Zhou et al., *WebArena: A Realistic Web Environment for Building Autonomous Agents*, ICLR 2024：
   <https://arxiv.org/abs/2307.13854>
4. Xie et al., *OSWorld: Benchmarking Multimodal Agents for Open-Ended Tasks in Real Computer Environments*, NeurIPS 2024：
   <https://arxiv.org/abs/2404.07972>
5. Yao et al., *τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains*（Sierra Research 项目）：
   <https://arxiv.org/abs/2406.12045>
6. Trivedi et al., *AppWorld: A Controllable World of Apps and People for Benchmarking Interactive Coding Agents*, ACL 2024：
   <https://arxiv.org/abs/2407.18901>
7. Lu et al., *ToolSandbox: A Stateful, Conversational, Interactive Evaluation Benchmark for LLM Tool Use Capabilities*：
   <https://arxiv.org/abs/2408.04682>
8. Liu et al., *CRMArena: Understanding the Capacity of LLM Agents to Perform Professional CRM Tasks in Realistic Environments*：
   <https://arxiv.org/abs/2411.02305>
9. *The BrowserGym Ecosystem for Web Agent Research*（2024）：
   <https://arxiv.org/abs/2412.05467>
10. *AssistantBench: Can Web Agents Solve Realistic and Time-Consuming Tasks?*：
   <https://arxiv.org/abs/2407.15711>
11. *InterCode*（interactive coding with execution feedback, 2023）：
   <https://arxiv.org/abs/2306.14898>
12. Debenedetti et al., *AgentDojo: A Dynamic Environment to Evaluate Prompt Injection Attacks and Defenses for LLM Agents*：
   <https://arxiv.org/abs/2406.13352>

### EMS 运筹/仿真

1. Jagtenberg, van den Berg & van der Mei, *Benchmarking online dispatch algorithms for Emergency Medical Services*, European Journal of Operational Research 258(2), 715–725, 2017（online 2016）：
   <https://doi.org/10.1016/j.ejor.2016.08.061>
2. Allen, Pearn & Monks, *Developing an OpenAI Gym-compatible framework and simulation environment for testing Deep Reinforcement Learning agents solving the Ambulance Location Problem*：
   <https://arxiv.org/abs/2101.04434>
3. Brotcorne, Laporte & Semet, *Ambulance location and relocation models*, European Journal of Operational Research 147(3), 2003：
   <https://doi.org/10.1016/S0377-2217(02)00364-8>
4. Gendreau, Laporte & Semet, *A dynamic model and parallel tabu search heuristic for real-time ambulance relocation*, Parallel Computing 27, 2001：
   <https://doi.org/10.1016/S0167-8191(01)00103-X>

---

**答辩使用建议**：先讲第 0、1、3 节建立问题，再用第 4、5 节证明数据可行性，
最后用第 6、7、9 节解释工程可信度和诚实边界。不要先展示模型排名；先说明
“单窗口 pilot”与“10 窗口正式实验”的区别，通常更容易让评委理解结果的科学
含义。
