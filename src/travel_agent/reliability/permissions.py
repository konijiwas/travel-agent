"""可靠性六件套 ④ 权限分级(工具按危险程度分流)。

原则:**查询类自动执行,花钱/改状态类必须人工确认**。

按工具名把工具分成两级:
- auto(自动):只读查询(search/get/list 系)—— 天气、航班、酒店
  查询,天然安全,直接执行;
- confirm(需确认):产生真实后果的(book_ 订票 / pay_ 付款 /
  cancel_ 退改)—— 执行前必须经用户明确同意,防止 LLM 被诱导
  ("帮我订最贵的酒店")造成真实损失(M8 评测 security 用例的防线)。

当前项目 7 个工具全部是查询类(全 auto),本模块为 M8 安全评测
与后续接入真实预订 API 预留防线;dry_run 模式让 confirm 类工具
在评测中可"预演"(不产生真实后果)以验证决策链。
"""

from travel_agent.config import settings

# 危险动词:工具名以这些前缀开头 → 需确认(以 _ 开头=带 server 前缀,如 flight__book_ticket)
_CONFIRM_MARKERS = ("book_", "pay_", "order_", "cancel_", "refund_")

# 黑名单:永不执行的工具名(可配置扩展)
_DENY_NAMES: tuple[str, ...] = ()

AUTO = "auto"          # 查询类:自动执行
CONFIRM = "confirm"    # 花钱/改状态类:需人工确认
DENY = "deny"          # 黑名单:禁止执行


class PermissionDeniedError(Exception):
    """工具在黑名单,拒绝执行。"""


class ConfirmationRequiredError(Exception):
    """工具需要人工确认:携带工具名与参数摘要,由上层转入确认流程。"""

    def __init__(self, tool_name: str, args_summary: dict):
        self.tool_name = tool_name
        self.args_summary = args_summary
        super().__init__(f"工具 {tool_name} 需要用户确认后才能执行")


def _marker_hit(tool_name_lower: str, marker: str) -> bool:
    """判断工具名是否命中危险动词(允许带 server 前缀,如 flight__book_ticket)。"""
    return tool_name_lower.startswith(marker) or f"_{marker}" in tool_name_lower


def classify_tool(tool_name: str) -> str:
    """按工具名分级:auto / confirm / deny。"""
    lower = tool_name.lower()
    if lower in _DENY_NAMES:
        return DENY
    if any(_marker_hit(lower, m) for m in _CONFIRM_MARKERS):
        return CONFIRM
    return AUTO


def assert_allowed(tool_name: str, args_summary: dict | None = None,
                   pre_approved: bool = False, dry_run: bool | None = None) -> None:
    """执行前检查:deny → 抛 PermissionDeniedError;
    confirm 且未预先批准且非 dry_run → 抛 ConfirmationRequiredError;
    其余放行(不抛异常即通过)。

    - pre_approved:用户已明确同意(CLI 确认流程置 True)
    - dry_run:None 时用配置 settings.dry_run;True 时 confirm 类可预演执行
    """
    level = classify_tool(tool_name)
    if level == DENY:
        raise PermissionDeniedError(f"工具 {tool_name} 被列入禁止名单,已拒绝执行")
    if level == CONFIRM:
        approved = pre_approved or (settings.dry_run if dry_run is None else dry_run)
        if not approved:
            raise ConfirmationRequiredError(tool_name, args_summary or {})
