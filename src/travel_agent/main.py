"""CLI 入口(click)。M6 最小版:run 单轮全流程;M7 扩展 chat/eval/replay/config。

说明:原计划用 typer(click 的上层封装),但 typer 在本环境(Python 3.13.13)
位置参数解析异常,已改用其底层 click(行为验证可用,API 语义一致)。

运行方式:
    uv run python -m travel_agent.main run "从上海出发,2026-10-01 去杭州玩3天,2人,预算6000"
"""

import uuid

import click

from travel_agent.supervisor.graph import build_graph, get_interrupt


@click.group(help="旅行规划 Multi-Agent 系统")
def app():
    """旅行规划 Multi-Agent 系统。"""


@app.command()
@click.argument("user_input")
@click.option("--auto-adjust/--no-auto-adjust", default=False,
              help="预算/天气调整免确认(默认 false,调整方案需用户确认)")
def run(user_input: str, auto_adjust: bool):
    """单轮全流程:输入需求,输出结构化行程(挂起时打印待答问题并退出)。"""
    graph = build_graph()
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = graph.invoke({"user_input": user_input, "auto_adjust": auto_adjust},
                          config)

    pending = get_interrupt(result)
    if pending is not None:
        click.echo(f"[挂起] {pending.get('title', '')}")
        if pending.get("kind") == "clarify":
            for q in pending.get("questions", []):
                click.echo(f"  - {q}")
        else:
            click.echo(f"  {pending.get('detail', '')}")
            click.echo(f"  选项: {pending.get('options', '')}")
        click.echo("提示:交互式追问/确认请使用 chat 命令(M7 提供)")
        raise SystemExit(2)

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


if __name__ == "__main__":
    app()
