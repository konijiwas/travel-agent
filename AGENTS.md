# AGENTS.md — 本项目编码规则(给 AI 编码代理与协作者)

## WHAT

旅行规划 Multi-Agent 系统:Supervisor(LangGraph)编排需求分析师 / 预算规划师 / 行程执行者三个 Agent,
MCP 接入天气 / 航班 / 酒店数据源,消息队列解耦,内置"预算超支自动调整"与"天气影响行程变更"两条决策规则。

## WHY(不可改的架构决策,勿擅自推翻)

1. 编排用 LangGraph StateGraph;LLM 统一走 langchain-openai(DeepSeek,OpenAI 兼容);
2. MCP Server 用 FastMCP;客户端用 langchain-mcp-adapters;
3. 队列只依赖 `queue/base.py` 的 MessageQueue 抽象(Redis Streams + 内存兜底两实现);
4. 数据模型全部 Pydantic(`schemas/`),Agent 结构化输出用 `with_structured_output`;
5. 可靠性六件套统一经 `reliability/__init__.py` 的 `wrap_tool_call` 包装;
6. 数据源 Mock 先行,契约在 `mcp_servers/*/contracts.py`;换真实 API 只改 server 内部,契约不动。

## HOW

- 版本锁定:`langgraph>=1.2,<2.0` / `langchain>=1.3,<1.4` / `langchain-mcp-adapters 0.3.x` / `fastmcp 4.x`(已适配 4.x Client 新接口:直接传 FastMCP 实例);升级前必须跑全量测试;
- 运行:`uv run python -m travel_agent.main <cmd>`;测试:`uv run pytest`;
- LLM 调用一律经 `llm/client.py` 的 `get_llm` / `get_structured_llm`,禁止绕过直接实例化;
- 工具调用必须经 `wrap_tool_call`;消息 / 事件走 queue 抽象(消融开关除外);
- 配置一律从 `config.py` 的 `settings` 读取,禁止硬编码 key / 超时 / 开关。

## TEST

- 每里程碑对应 `tests/` 全绿才进入下一里程碑;改 prompt 必须重跑对应 Agent 测试;
- 契约测试(`test_mcp_contracts`)失败 = 契约变更,需同步 schemas 与文档;
- 评测通过率 ≥80% 为发布门槛(M8)。
