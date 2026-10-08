"""消融跑分:基线 + 逐个关闭功能开关,量化每个功能对端到端通过率的贡献。

复用 run_eval.run_case(单 case 干净环境)逐条跑分,自己组织"开关切换 +
汇总对比";不打印完整评测报告,只输出一张消融对照表,供 docs/retro.md
引用实测数据。

用法(经 CLI):uv run python -m travel_agent.main ablate
"""

from dataclasses import dataclass

from rich.console import Console
from rich.table import Table

from travel_agent.config import settings
from travel_agent.eval.cases import load_cases, validate_cases
from travel_agent.eval.run_eval import PASS_THRESHOLD, run_case
from travel_agent.memory.checkpointer import build_checkpointer

# 消融开关:字段名 → 功能名(报告展示用)。顺序即报告顺序。
ABLATION_SWITCHES = (
    ("ablate_budget_adjust", "预算超支自动调整"),
    ("ablate_weather", "天气影响行程变更"),
    ("ablate_queue", "消息队列解耦"),
    ("ablate_small_gate", "质量门把关"),
)


@dataclass
class AblationRow:
    """一次消融配置的结果:标签 + 通过/总数(通过率由属性算出)。"""

    label: str
    passed: int
    total: int

    @property
    def rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


def _set_all(value: bool) -> None:
    """把 4 个消融开关全部置为同一值。"""
    for field, _ in ABLATION_SWITCHES:
        setattr(settings, field, value)


def _count(cases, checkpointer) -> tuple[int, int]:
    """跑完给定 case,返回 (通过数, 总数)。"""
    runs = [run_case(case, checkpointer) for case in cases]
    return sum(1 for r in runs if r.passed), len(runs)


def run_ablation(cases_dir: str | None = None, checkpointer=None) -> list[AblationRow]:
    """基线(全功能)+ 逐个关闭开关,各跑一遍全量评测,返回对比行。

    跑完恢复原开关状态,不污染同进程后续调用(测试友好)。
    """
    cases = load_cases(cases_dir)
    if validate_cases(cases):
        raise SystemExit(2)
    checkpointer = checkpointer or build_checkpointer(in_memory=True)

    original = {field: getattr(settings, field) for field, _ in ABLATION_SWITCHES}
    rows: list[AblationRow] = []
    try:
        _set_all(False)
        rows.append(AblationRow("基线(全功能)", *_count(cases, checkpointer)))
        for field, label in ABLATION_SWITCHES:
            _set_all(False)
            setattr(settings, field, True)
            rows.append(AblationRow(f"关掉 {label}", *_count(cases, checkpointer)))
    finally:
        for field, value in original.items():
            setattr(settings, field, value)
    return rows


def print_ablation(rows: list[AblationRow]) -> None:
    """打印消融对照表 + 一句话解读(承重功能 vs 架构/兜底层)。"""
    console = Console()
    base = rows[0]
    table = Table(title="消融对照(端到端通过率)")
    table.add_column("配置", no_wrap=True)
    table.add_column("通过", justify="right", no_wrap=True)
    table.add_column("通过率", justify="right", no_wrap=True)
    table.add_column("相对基线", justify="right", no_wrap=True)
    for i, row in enumerate(rows):
        style = "green" if row.rate >= PASS_THRESHOLD else "red"
        if i == 0:
            delta = "(基线)"
        else:
            diff = row.passed - base.passed
            delta = f"{diff:+d} case" if diff else "不变"
        table.add_row(row.label, f"{row.passed}/{row.total}",
                      f"[{style}]{row.rate:.0%}[/{style}]", delta)
    console.print(table)

    drops = [r.label for r in rows[1:] if r.passed < base.passed]
    unchanged = [r.label for r in rows[1:] if r.passed == base.passed]
    if drops:
        console.print(f"[bold red]承重功能(关掉即掉分):[/bold red] {'、'.join(drops)}")
    if unchanged:
        console.print(f"[bold]架构/兜底层(关掉不影响 happy-path 通过率):[/bold] "
                      f"{'、'.join(unchanged)}")
    if not drops:
        console.print("[yellow]提示:所有功能关闭均未掉分,需审视评测 case 是否覆盖了这些功能[/yellow]")
