# travel-agent — 旅行规划 Multi-Agent 系统

基于 **LangGraph + MCP** 的旅行规划系统:Supervisor 总控调度**需求分析师 / 预算规划师 / 行程执行者**三个 Agent,输入目的地 / 日期 / 预算 / 偏好,输出结构化行程(航班、酒店、每日安排、预算明细、调整说明)。

两大核心亮点:**预算超支自动调整** · **天气影响行程变更**。

## 快速开始(5 步)

1. 安装 uv(PowerShell):
   ```powershell
   irm https://astral.sh/uv/install.ps1 | iex
   ```
2. 复制 `.env.example` 为 `.env`,填入 `DEEPSEEK_API_KEY`(platform.deepseek.com → API Keys)
3. 安装依赖:
   ```powershell
   uv sync
   ```
4. (可选)安装 [Memurai](https://www.memurai.com/get-memurai)(Windows 版 Redis);不装也能跑,系统自动降级内存队列
5. 运行:
   ```powershell
   uv run python -m travel_agent.main run "下周六带两个孩子去杭州玩三天,预算6000,上海出发"
   ```

## 命令一览

| 命令 | 用途 |
| --- | --- |
| `uv run python -m travel_agent.main run "需求"` | 单轮全流程 |
| `uv run python -m travel_agent.main chat` | 交互模式(追问 / 确认 / 修改闭环) |
| `uv run python -m travel_agent.main eval --cases eval/cases` | 跑评测集 |
| `uv run python -m travel_agent.main replay --trace-id xxx` | 回放定位 |
| `uv run python -m travel_agent.main config show` | 查看当前配置 |

## 架构(分层)

```
用户输入(CLI) → Supervisor(LangGraph 状态机 + 消息队列) → 三个专业 Agent
             → MCP Server(weather / flight / hotel) → 数据源(Mock 先行)
反馈回路:变更事件经队列回传 → 重规划 / 用户确认 → 质量门 → 交付
```

详细设计见 [docs/spec.md](docs/spec.md) 与 [docs/architecture.html](docs/architecture.html)。
