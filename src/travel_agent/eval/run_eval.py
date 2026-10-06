"""评测跑分器:case 干净环境循环 + Rich 报告(M8 门槛:通过率 ≥80%)。

每个 case 的干净环境:熔断器清零、新会话、新账本/tracer、故障注入
(MOCK_FAULT 环境变量,MCP 子进程继承)。跑图时挂起按 case 预置
answers 依次恢复;答案用完仍挂起 → 按 waiting_user 记结果。

用法:
    uv run python -m travel_agent.main eval [--cases eval/cases]
"""

import os
import time
import uuid

from langgraph.types import Command
from rich.console import Console
from rich.table import Table

from travel_agent.eval.cases import EvalCase, load_cases, validate_cases
from travel_agent.eval.checks import evaluate
from travel_agent.eval.metrics import EvalRun, build_facts, run_metrics
from travel_agent.memory.checkpointer import build_checkpointer
from travel_agent.memory.jsonl_writer import read_trace
from travel_agent.reliability import (
    CostGuard,
    Tracer,
    get_guard,
    reset_breakers,
    set_guard,
    set_tracer,
)
from travel_agent.supervisor.graph import build_graph, get_interrupt

PASS_THRESHOLD = 0.80   # 方案文档:M8 发布门槛 = 端到端通过率 ≥80%


def run_case(case: EvalCase, checkpointer) -> EvalRun:
    """单 case 干净环境运行:注入故障 → 跑图(挂起自动恢复)→ 判分。"""
    # ── 干净环境 ──
    reset_breakers()
    session_id = str(uuid.uuid4())
    set_guard(CostGuard())
    set_tracer(Tracer(trace_id=session_id))
    if case.fault != "none":
        os.environ["MOCK_FAULT"] = case.fault      # 子进程继承,故障注入生效
    else:
        os.environ.pop("MOCK_FAULT", None)

    graph = build_graph(checkpointer)
    config = {"configurable": {"thread_id": session_id}}
    interrupts: list[str] = []
    errors: list[str] = []
    no_crash = True
    state = None
    answers = list(case.answers)
    answer_idx = 0
    t0 = time.perf_counter()

    try:
        state = graph.invoke(
            {"user_input": case.input, "auto_adjust": case.auto_adjust,
             "session_id": session_id}, config)
        # 挂起循环:按预置 answers 依次恢复;答案用完仍挂起 → 停(waiting_user)
        while True:
            pending = get_interrupt(state)
            if pending is None:
                break
            interrupts.append(pending.get("kind", "?"))
            if answer_idx >= len(answers):
                break
            state = graph.invoke(Command(resume=answers[answer_idx]), config)
            answer_idx += 1
    except Exception as exc:                      # 任何异常都不中断整场评测
        no_crash = False
        errors.append(f"未捕获异常:{type(exc).__name__}: {exc}")
    finally:
        os.environ.pop("MOCK_FAULT", None)        # 恢复环境,不影响下一个 case

    wall = time.perf_counter() - t0
    if state is not None:
        errors.extend(str(e) for e in (state.get("errors") or []))

    events = read_trace(session_id)
    facts = build_facts(state or {}, events, interrupts, errors, no_crash)
    outcome = evaluate(facts, case.checks)
    metrics = run_metrics(events, wall, get_guard().snapshot())
    return EvalRun(case=case, facts=facts, passed=outcome.passed,
                   outcome=outcome, trace_id=session_id,
                   errors=errors, interrupts=interrupts, **metrics)


def run_eval(cases_dir: str | None = None, checkpointer=None,
             verbose: bool = False) -> tuple[list[EvalRun], dict]:
    """跑全部 case,打印 Rich 报告,返回 (runs, 汇总)。

    case 格式错误(校验问题)在开跑前整体暴露,不浪费评测时间。
    """
    cases = load_cases(cases_dir)
    problems = validate_cases(cases)
    if problems:
        console = Console()
        console.print("[red]case 文件校验未通过:[/red]")
        for p in problems:
            console.print(f"  - {p}")
        raise SystemExit(2)

    console = Console()
    # 未配置 API Key 时自动切 fake 离线模式(开箱即用;填 Key 后重跑同一
    # 套评测即真实 LLM 评测,case 与判分零改动)
    from travel_agent.config import settings
    if settings.eval_mode != "fake" and not settings.deepseek_api_key:
        settings.eval_mode = "fake"
        console.print("[yellow]未检测到 DeepSeek API Key,自动切换 fake 离线模式评测"
                      "(填 .env 后重跑即真实 LLM 评测)[/yellow]")
    console.print(f"评测开始:共 {len(cases)} 条 case"
                  f"(通过率门槛 {PASS_THRESHOLD:.0%},模式 {settings.eval_mode})")
    checkpointer = checkpointer or build_checkpointer(in_memory=True)
    runs = [run_case(case, checkpointer) for case in cases]

    # ── 逐条结果表 ──
    table = Table(title="评测结果")
    table.add_column("case", style="cyan", no_wrap=True)
    table.add_column("类别", no_wrap=True)
    table.add_column("结果", no_wrap=True)
    table.add_column("断言", no_wrap=True)
    table.add_column("耗时s", justify="right", no_wrap=True)
    table.add_column("工具", justify="right", no_wrap=True)
    table.add_column("成本$", justify="right", no_wrap=True)
    table.add_column("trace", no_wrap=True)
    for run in runs:
        style = "green" if run.passed else "red"
        table.add_row(
            run.case.task_id, run.case.category,
            f"[{style}]{'PASS' if run.passed else 'FAIL'}[/{style}]",
            f"{run.outcome.passed_count}/{run.outcome.total}",
            f"{run.latency_s:.1f}", str(run.tool_calls),
            f"{run.cost_usd:.5f}", run.trace_id[:8],
        )
    console.print(table)

    # ── 失败明细 ──
    for run in runs:
        if run.passed:
            continue
        console.print(f"[red]{run.case.task_id} 失败[/red](期望:{run.case.expected})")
        for result in run.outcome.failed:
            console.print(
                f"  ✗ {result.fact} {result.operator} {result.expected!r} "
                f"→ 实际 {result.actual!r} {result.note}")
        if run.errors:
            console.print(f"  错误: {'; '.join(run.errors)[:200]}")

    # ── 汇总 ──
    by_category: dict[str, dict] = {}
    for run in runs:
        stats = by_category.setdefault(
            run.case.category, {"total": 0, "passed": 0})
        stats["total"] += 1
        stats["passed"] += int(run.passed)
    total = len(runs)
    passed = sum(1 for r in runs if r.passed)
    rate = passed / total if total else 0.0
    console.print("\n[bold]分类通过率:[/bold]")
    for cat, stats in sorted(by_category.items()):
        cr = stats["passed"] / stats["total"]
        console.print(f"  {cat}: {stats['passed']}/{stats['total']}"
                      f" ({cr:.0%})")
    ok = rate >= PASS_THRESHOLD
    color = "green" if ok else "red"
    console.print(
        f"[bold {color}]总通过率: {passed}/{total} ({rate:.0%})"
        f" —— {'达标 ✓' if ok else f'未达标(门槛 {PASS_THRESHOLD:.0%}),'
        f'按失败明细修复后重跑(每 case 有 trace_id 可回放)'}[/bold {color}]")
    if verbose:
        console.print("\n[bold]失败 case 回放:[/bold]")
        for run in runs:
            if not run.passed:
                console.print(
                    f"  uv run python -m travel_agent.main replay {run.trace_id}")
    return runs, {"total": total, "passed": passed, "rate": rate,
                  "by_category": by_category}
