# 简历要点(resume_bullet)

> 用途:把整个项目提炼成可粘贴进简历 / 作品集 / 面试自我介绍的内容。
> 文中的数字均为**实测值**(评测通过率 100%、消融掉分 1/15、102 条测试),可直接引用。

## 一句话简介

基于 LangGraph + MCP 的旅行规划 Multi-Agent 系统:Supervisor 统一编排**需求分析师 / 预算规划师 / 行程执行者**三个 Agent,内置「预算超支自动调整」「天气影响行程变更」两条决策规则,端到端评测通过率 100%。

## 简历要点(可复制,6 条)

- **多 Agent 编排**:用 LangGraph 实现 9 节点状态机(条件路由、`interrupt` 挂起续跑、checkpoint 持久化),Supervisor 调度 3 个专业 Agent,支持多轮追问与"超支待确认"交互闭环。
- **MCP 工具接入**:基于 FastMCP 自建 weather / flight / hotel 三个数据源 Server,经 langchain-mcp-adapters 接入;消息队列抽象层解耦(Redis Streams + 内存兜底),单源故障自动降级、不拖垮全局。
- **两条业务决策规则**:①预算超支按优先级链自动调整并留痕;②天气变化自动改期/改室内并留痕。**消融实验量化贡献**:关掉任一规则,通过率从 100% 降至 93%。
- **可靠性工程**:超时 / 重试 / 熔断 / 权限分级 / 成本护栏 / 全链路打点六件套,统一 `wrap_tool_call` 包装,CostGuard 限成本。
- **评测体系**:自建 15 条 case(正常 / 边界 / 故障 / 安全 / 决策五类,含 prompt 注入与诱骗预订),端到端通过率 100%;pytest 102 条全绿。
- **技术栈**:LangGraph / LangChain / FastMCP / Redis / Pydantic / SQLite / pytest;DeepSeek 结构化输出(function_calling 适配其协议差异)。

## 面试可讲的三个亮点(建议展开)

1. **多轮交互闭环怎么做** — 状态机 + `interrupt()` 挂起 + checkpoint 续跑:信息缺失追问、超支方案待确认、`r` 修改回退重规划,全程可断点续跑(存 SQLite)。
2. **评测驱动开发** — 先定 15 条 case 的断言契约,首跑只过 47%,逐类揪出解析 / 提示词 / 编码 bug 修到 100%;再跑消融实验定位"承重功能"。
3. **工程可靠性** — 面对真实 DeepSeek 的协议差异(json_schema 不支持)、Windows GBK 编码、单数据源故障,用 function_calling 适配、ASCII 符号、降级兜底逐一化解。
