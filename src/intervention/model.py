"""案件投影与对外视图的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Signal:
    """现场可观察信号。observation 为可观察事实，assessment 为主观标签。"""

    signal_id: str
    kind: str
    observation: str
    assessment: str | None
    confirmed: bool
    retracted: bool
    occurred_at: datetime
    version: int


@dataclass
class ContactAttempt:
    channel: str
    target: str
    result: str
    note: str
    occurred_at: datetime


@dataclass
class Review:
    reviewer_id: str
    reviewer_role: str
    conclusion: str
    rationale: str
    occurred_at: datetime


@dataclass
class Hold:
    """保护性止付：必须有理由、期限与升级责任人。"""

    hold_id: str
    reason: str
    applied_at: datetime
    expires_at: datetime
    escalation_owner_role: str
    decider_id: str
    version: int
    lifted: bool = False
    lift_basis: str | None = None
    lifted_at: datetime | None = None


@dataclass
class Decision:
    """最终决定。被申诉推翻后置为 invalidated，不再作为有效决定。"""

    outcome: str  # release / decline
    basis: str
    decider_id: str
    decider_role: str
    occurred_at: datetime
    version: int
    invalidated: bool = False


@dataclass
class Appeal:
    filed_by: str
    channel: str
    grounds: str
    filed_at: datetime
    reviewed: bool = False
    review_outcome: str | None = None
    reviewer_id: str | None = None
    reviewed_at: datetime | None = None


@dataclass
class AuthorityResponse:
    authority: str
    reference: str
    disposition: str
    note: str
    occurred_at: datetime


@dataclass
class CaseProjection:
    """由事件流折叠出的案件全貌（内部卷宗）。"""

    case_id: str
    customer_id: str
    amount: float
    currency: str
    declared_purpose: str
    destination: str | None  # 资金去向，仅持合法授权方可向警方披露
    channel: str
    trigger_kind: str
    trigger_detail: str
    flagged_at: datetime
    self_expression: dict | None = None
    companion: dict | None = None
    contact_attempts: list[ContactAttempt] = field(default_factory=list)
    reviews: list[Review] = field(default_factory=list)
    signals: dict[str, Signal] = field(default_factory=dict)
    holds: dict[str, Hold] = field(default_factory=dict)
    decisions: list[Decision] = field(default_factory=list)
    funds_released: bool = False
    payment_ref: str | None = None
    appeal: Appeal | None = None
    authority_responses: list[AuthorityResponse] = field(default_factory=list)

    def latest_hold(self) -> Hold | None:
        if not self.holds:
            return None
        return max(self.holds.values(), key=lambda h: (h.applied_at, h.version))

    def active_hold(self, at: datetime) -> Hold | None:
        hold = self.latest_hold()
        if hold is not None and not hold.lifted and at < hold.expires_at:
            return hold
        return None

    def latest_decision(self) -> Decision | None:
        """最新有效决定：未被申诉推翻的最高版本。"""
        valid = [d for d in self.decisions if not d.invalidated]
        return valid[-1] if valid else None

    def latest_review(self) -> Review | None:
        return self.reviews[-1] if self.reviews else None

    def companion_interference(self) -> bool:
        """陪同者存在代答、催促或阻断联系行为。"""
        c = self.companion
        if not c:
            return False
        return bool(
            c.get("answers_for_customer") or c.get("rushing") or c.get("blocks_contact")
        )

    def customer_confirmed(self) -> bool:
        """客户本人意愿已获得直接确认（独立陈述或成功联系）。"""
        if self.self_expression and self.self_expression.get("expressed_independently"):
            return True
        return any(a.result == "reached" for a in self.contact_attempts)


@dataclass
class WorkItem:
    """柜员界面的下一动作：动作、责任人、截止时间与倒计时。"""

    case_id: str
    action: str
    owner_role: str
    deadline: datetime
    breach: bool = False  # 已发生升级责任失守

    def countdown_seconds(self, now: datetime) -> int:
        return max(0, int((self.deadline - now).total_seconds()))

    def is_overdue(self, now: datetime) -> bool:
        return now > self.deadline

    def as_dict(self, now: datetime) -> dict:
        return {
            "case_id": self.case_id,
            "action": self.action,
            "owner_role": self.owner_role,
            "deadline": self.deadline.isoformat(),
            "countdown_seconds": self.countdown_seconds(now),
            "overdue": self.is_overdue(now),
            "breach": self.breach,
        }
