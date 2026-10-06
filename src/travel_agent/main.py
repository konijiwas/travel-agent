"""CLI 入口(click)。M6:run 单轮全流程;M7:chat 交互闭环 + replay 回放 + config show。

说明:原计划用 typer(click 的上层封装),但 typer 在本环境(Python 3.13.13)
位置参数解析异常,已改用其底层 click(行为验证可用,API 语义一致)。

运行方式:
    uv run python -m travel_agent.main run "从上海出发,2026-10-01 去杭州玩3天,2人,预算6000"
    uv run python -m travel_agent.main chat                 # 交互式(追问/确认/修改闭环)
    uv run python -m travel_agent.main replay <trace_id>    # 回放一次规划的打点轨迹
    uv run python -m travel_agent.main config show          # 查看当前配置
"""

import uuid

import click
from langgraph.types import Command

from travel_agent.config import settings
from travel_agent.memory.jsonl_writer import read_trace
from travel_agent.reliability import CostGuard, Tracer, get_guard, set_guard, set_tracer
from travel_agent.supervisor.graph import build_graph, get_interrupt

MAX_CHAT_ROUNDS = 10   # chat 单次会话最多交互轮数(防死循环,正常 2-3 轮)


def _start_trajectory(session_id: str) -> None:
    """轨迹初始化:invoke 之前换新账本与 tracer(节点上下文会继承)。"""
    set_guard(CostGuard())
    set_tracer(Tracer(trace_id=session_id))


@click.group(help="旅行规划 Multi-Agent 系统")
def app():
    """旅行规划 Multi-Agent 系统。"""


def _print_final(result: dict) -> None:
    """打印最终行程(chat/run 共用)。"""
    final = result.get("final_output")
    if final is None:
        click.echo(f"运行失败:{result.get('errors')}")
        raise SystemExit(1)

    bp = final.budget_plan
    it = final.itinerary
    click.echo(f"状态: {result.get('status')}")
    if bp is not None:
        click.echo(f"预算总额: {bp.total_budget:.0f} 元")
    if it is not None:
        click.echo(f"行程天数: {len(it.days)} 天")
        for day in it.days:
            acts = "; ".join(f"{a.start_time} {a.name}" for a in day.activities)
            click.echo(f"  {day.date} | {acts} | {day.weather_note}")
    click.echo(f"调整说明: {final.summary}")
    click.echo(f"数据时间: {final.data_timestamp}")
    guard = get_guard()
    if guard.total_tokens:
        click.echo(f"本次成本: ~${guard.cost_usd:.4f}(token {guard.total_tokens})")


@app.command()
@click.argument("user_input")
@click.option("--auto-adjust/--no-auto-adjust", default=False,
              help="预算/天气调整免确认(默认 false,调整方案需用户确认)")
def run(user_input: str, auto_adjust: bool):
    """单轮全流程:输入需求,输出结构化行程(挂起时打印待答问题并退出)。"""
    thread_id = str(uuid.uuid4())
    _start_trajectory(thread_id)
    graph = build_graph()
    config = {"configurable": {"thread_id": thread_id}}
    result = graph.invoke({"user_input": user_input, "auto_adjust": auto_adjust,
                           "session_id": thread_id}, config)

    pending = get_interrupt(result)
    if pending is not None:
        click.echo(f"[挂起] {pending.get('title', '')}")
        if pending.get("kind") == "clarify":
            for q in pending.get("questions", []):
                click.echo(f"  - {q}")
        else:
            click.echo(f"  {pending.get('detail', '')}")
            click.echo(f"  选项: {pending.get('options', '')}")
        click.echo("提示:交互式追问/确认请使用 chat 命令")
        raise SystemExit(2)

    _print_final(result)


@app.command()
@click.option("--auto-adjust/--no-auto-adjust", default=False,
              help="预算/天气调整免确认(默认 false,调整方案需用户确认)")
@click.option("--session", "session_id", default=None,
              help="续跑已有会话(填上次 chat 输出的 session id,从挂起点继续)")
def chat(auto_adjust: bool, session_id: str | None):
    """交互式规划:信息缺失时追问,超支方案待确认,支持 r 修改回退重规划。

    首次运行:输入需求 → 挂起时逐一回答 → 直到交付(每轮会话 id 在
    交付后打印,可用 --session 续跑)。
    """
    graph = build_graph()
    thread_id = session_id or str(uuid.uuid4())
    _start_trajectory(thread_id)
    config = {"configurable": {"thread_id": thread_id}}

    if session_id is None:
        user_input = click.prompt("请输入您的旅行需求", type=str)
        result = graph.invoke(
            {"user_input": user_input, "auto_adjust": auto_adjust,
             "session_id": thread_id}, config)
    else:
        # 续跑:直接恢复,挂起的问题会重新呈现(不传新输入)
        click.echo(f"续跑会话 {session_id} …")
        result = graph.invoke(None, config)

    for _ in range(MAX_CHAT_ROUNDS):
        pending = get_interrupt(result)
        if pending is None:
            break
        if pending.get("kind") == "clarify":
            click.echo(f"[追问] {pending.get('title', '')}")
            answers = [click.prompt(f"  {q}", type=str)
                       for q in pending.get("questions", [])]
            result = graph.invoke(Command(resume="\n".join(answers)), config)
        else:
            click.echo(f"[确认] {pending.get('title', '')}")
            click.echo(f"  {pending.get('detail', '')}")
            choice = click.prompt(f"  {pending.get('options', '')}", type=str,
                                  default="y").strip().lower()
            result = graph.invoke(Command(resume=choice), config)

    if get_interrupt(result) is not None:
        click.echo("交互轮数已达上限,请用 --session 续跑本会话")
        click.echo(f"session: {thread_id}")
        raise SystemExit(2)

    _print_final(result)
    click.echo(f"session: {thread_id}")


@app.command()
@click.argument("trace_id")
@click.option("--limit", default=50, help="最多显示的事件条数")
def replay(trace_id: str, limit: int):
    """回放一次规划的打点轨迹(按 trace_id,时间线展示)。"""
    events = read_trace(trace_id)
    if not events:
        click.echo(f"未找到 trace {trace_id} 的记录(目录:{settings.trace_dir})")
        click.echo("提示:trace_id 即会话 id,每次 chat 交付后会打印")
        raise SystemExit(1)
    click.echo(f"trace {trace_id} 共 {len(events)} 条事件:")
    for ev in events[-limit:]:
        indent = "  " if ev.get("parent_span_id") else ""
        line = (f"{ev['started_at'][11:23]} [{ev['kind']}/{ev['status']}] "
                f"{indent}{ev['name']} {ev['duration_ms']:.0f}ms")
        if ev.get("attempts", 1) > 1:
            line += f"(尝试 {ev['attempts']} 次)"
        if ev.get("error"):
            line += f" | 错误: {ev['error'][:100]}"
        click.echo(line)


@app.command(name="eval")
@click.option("--cases", "cases_dir", default=None,
              help="case 文件目录(默认 eval/cases)")
@click.option("--verbose", is_flag=True, help="失败 case 打印 replay 命令")
def eval_cmd(cases_dir: str | None, verbose: bool):
    """跑评测集:15 条 case 端到端打分,通过率 ≥80% 为达标(退出码 0)。"""
    from travel_agent.eval.run_eval import PASS_THRESHOLD, run_eval

    _, summary = run_eval(cases_dir, verbose=verbose)
    raise SystemExit(0 if summary["rate"] >= PASS_THRESHOLD else 1)


@app.command(name="config")
@click.argument("action")
def config_cmd(action: str):
    """查看当前配置:config show"""
    if action != "show":
        click.echo("用法: config show")
        raise SystemExit(2)
    for key in [
        "eval_mode", "deepseek_model", "structured_method",
        "queue_backend", "redis_url", "auto_adjust",
        "max_clarify_rounds", "max_retry_nodes",
        "tool_timeout_s", "tool_retry_max", "retry_base_delay_s",
        "retry_max_delay_s", "breaker_failure_threshold",
        "breaker_recovery_s", "cost_limit_usd", "cost_token_limit",
        "dry_run", "db_path", "trace_dir",
        "ablate_budget_adjust", "ablate_weather", "ablate_queue",
    ]:
        click.echo(f"{key} = {getattr(settings, key)}")


if __name__ == "__main__":
    app()
