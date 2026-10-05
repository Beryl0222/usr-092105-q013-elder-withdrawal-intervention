"""时限、责任人与其他业务规则的集中配置。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta


class Role:
    """案件处理中的责任角色。"""

    TELLER = "柜员"
    SUPERVISOR = "网点主管"
    COMPLIANCE = "分行合规"
    APPEALS_OFFICER = "申诉专员"
    PAYMENT_SYSTEM = "支付系统"
    POLICE_LIAISON = "警银联络人"


@dataclass(frozen=True)
class InterventionPolicy:
    """干预时限与升级责任链。所有期限自对应事件的 occurred_at 起算。"""

    self_expression_sla: timedelta = timedelta(minutes=30)   # 请客户本人独立陈述
    contact_sla: timedelta = timedelta(minutes=20)           # 单独联系客户本人
    supervisor_review_sla: timedelta = timedelta(hours=2)    # 主管复核
    decision_sla: timedelta = timedelta(minutes=30)          # 复核后登记最终决定
    notice_sla: timedelta = timedelta(minutes=15)            # 向客户送达书面理由
    payment_execution_sla: timedelta = timedelta(minutes=10)  # 支付系统执行放行
    max_hold_duration: timedelta = timedelta(hours=72)       # 单次保护性措施最长期限
    appeal_window: timedelta = timedelta(days=7)             # 客户可申诉窗口
    appeal_review_sla: timedelta = timedelta(hours=24)       # 申诉复核时限
    appeal_channel: str = "网点现场、客服热线或手机银行“紧急干预申诉”入口"
    # 升级责任链：保护措施到期未处理时沿链逐级上报
    escalation_ladder: tuple[str, ...] = field(
        default=(Role.TELLER, Role.SUPERVISOR, Role.COMPLIANCE)
    )
