"""涉老取款紧急干预服务。

设计原则：
- 年龄、金额或家属反对只触发核实，绝不直接否定客户交易能力；
- 保护性措施必须带理由、期限与升级责任人；届满后按最新有效决定执行，
  没有有效决定时措施自动失效、默认放行，并登记升级责任失守；
- 事件按 event_id 幂等去重，业务时限一律以 occurred_at（事实发生时间）为准，
  接收时间单独记录，来源系统重试不会推迟任何期限；
- 未经确认的主观标签只留在案件卷宗内，不进入其他银行业务；
- 警方案情披露分级：无合法授权仅提供最低限度信息，持合法授权方可调取
  完整证据与资金去向，所有调取均留痕。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from src.intervention import events as ev
from src.intervention.disclosure import LegalAuthorization, validate_authorization
from src.intervention.model import (
    Appeal,
    AuthorityResponse,
    CaseProjection,
    ContactAttempt,
    Decision,
    Hold,
    Review,
    Signal,
    WorkItem,
)
from src.intervention.policy import InterventionPolicy, Role
from src.intervention.store import EventStore


@dataclass
class IngestResult:
    """单条事件的接收结果。duplicate 也算成功（幂等重试）。"""

    event_id: str
    status: str  # accepted / duplicate / rejected
    errors: list[str]

    @property
    def ok(self) -> bool:
        return self.status in ("accepted", "duplicate")


@dataclass
class DisclosureRecord:
    """披露留痕。"""

    case_id: str
    at: datetime
    level: str  # minimal / denied / full
    document_no: str | None


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def _effective_outcome(case: CaseProjection, now: datetime) -> tuple[str, str, object]:
    """当前应执行的结果：(outcome, basis, 相关对象)。

    outcome ∈ pending / hold / release / decline。
    保护性措施届满后按最新有效决定执行；无有效决定时措施失效、默认放行——
    年龄、金额或家属反对本身永远不足以否定客户交易能力。
    """
    decision = case.latest_decision()
    if decision is not None:
        return decision.outcome, "decision", decision
    hold = case.latest_hold()
    if hold is not None and not hold.lifted:
        if now < hold.expires_at:
            return "hold", "hold", hold
        return "release", "hold_expired_default", hold
    return "pending", "verification", None


def _escalation_breach(case: CaseProjection, now: datetime) -> bool:
    """保护性措施届满前既无有效决定也未解除，即升级责任失守。"""
    hold = case.latest_hold()
    if hold is None:
        return False
    decided_in_time = any(
        not d.invalidated and d.occurred_at <= hold.expires_at for d in case.decisions
    )
    lifted_in_time = (
        hold.lifted and hold.lifted_at is not None and hold.lifted_at <= hold.expires_at
    )
    if decided_in_time or lifted_in_time:
        return False
    if hold.lifted and hold.lifted_at is not None and hold.lifted_at > hold.expires_at:
        return True
    return now >= hold.expires_at


class InterventionService:
    """涉老取款紧急干预服务：事件接收、案件投影与各类视图。"""

    def __init__(
        self,
        policy: InterventionPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.policy = policy or InterventionPolicy()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.store = EventStore()
        self._disclosure_log: list[DisclosureRecord] = []

    # ---------- 事件接收 ----------

    def ingest(self, envelope: dict, received_at: datetime | None = None) -> IngestResult:
        """接收一条事件。重复投递（同 event_id 同内容）幂等返回 duplicate。"""
        received_at = received_at or self.clock()
        if not isinstance(envelope, dict):
            return IngestResult("<非法事件>", "rejected", ["事件必须为 JSON 对象"])
        event_id = envelope.get("event_id", "<缺失>")
        errors = ev.validate_envelope(envelope)
        if errors:
            return IngestResult(event_id, "rejected", errors)
        existing = self.store.by_id(event_id)
        if existing is not None:
            if EventStore.canonical(existing.envelope) == EventStore.canonical(envelope):
                return IngestResult(event_id, "duplicate", [])
            return IngestResult(
                event_id, "rejected", ["同一 event_id 携带不同内容，疑似冲突，请来源系统核对后重发"]
            )
        if self.store.has_version(
            envelope["aggregate_type"], envelope["aggregate_id"], envelope["version"]
        ):
            return IngestResult(
                event_id,
                "rejected",
                [f"业务对象 {envelope['aggregate_id']} 的版本 {envelope['version']} 已存在，版本冲突"],
            )
        errors = self._check_business_rules(envelope)
        if errors:
            return IngestResult(event_id, "rejected", errors)
        self.store.commit(envelope, received_at)
        return IngestResult(event_id, "accepted", [])

    def ingest_all(
        self, envelopes: list[dict], received_at: datetime | None = None
    ) -> list[IngestResult]:
        return [self.ingest(e, received_at=received_at) for e in envelopes]

    def _check_business_rules(self, envelope: dict) -> list[str]:
        event_type = envelope["event_type"]
        payload = envelope["payload"]
        T = ev.EventType
        R = Role
        if event_type == T.TRANSACTION_FLAGGED:
            # 触发只启动核实，永远允许登记；不得据此否定客户交易能力
            return []
        case = self._project_cases().get(payload["case_id"])
        if case is None:
            return [
                f"案件不存在：{payload['case_id']}（若为乱序到达，请先补送 TRANSACTION_FLAGGED 后重试）"
            ]
        occurred = ev.parse_time(envelope["occurred_at"])
        max_hold_hours = int(self.policy.max_hold_duration.total_seconds() // 3600)

        if event_type == T.SUPERVISOR_REVIEWED:
            if payload["reviewer_role"] not in (R.SUPERVISOR, R.COMPLIANCE):
                return ["主管复核须由网点主管或分行合规作出"]
            return []

        if event_type == T.HOLD_APPLIED:
            errors = []
            if payload["decider_role"] not in (R.SUPERVISOR, R.COMPLIANCE):
                errors.append("保护性措施须由网点主管或以上层级决定")
            if timedelta(minutes=payload["duration_minutes"]) > self.policy.max_hold_duration:
                errors.append(f"保护性措施期限不得超过 {max_hold_hours} 小时")
            if payload["escalation_owner_role"] not in self.policy.escalation_ladder:
                errors.append("升级责任人必须在升级责任链内")
            if not case.reviews:
                errors.append("采取保护性措施前须完成主管复核")
            if case.active_hold(occurred) is not None:
                errors.append("已存在生效中的保护性措施，请先解除或延期")
            return errors

        if event_type == T.HOLD_EXTENDED:
            hold = case.holds.get(envelope["aggregate_id"])
            if hold is None:
                return ["保护性措施不存在，无法延期"]
            if hold.lifted:
                return ["保护性措施已解除，无法延期"]
            if occurred >= hold.expires_at:
                return ["保护性措施已届满，须重新申请并说明理由"]
            if timedelta(minutes=payload["duration_minutes"]) > self.policy.max_hold_duration:
                return [f"保护性措施期限不得超过 {max_hold_hours} 小时"]
            return []

        if event_type == T.HOLD_LIFTED:
            hold = case.holds.get(envelope["aggregate_id"])
            if hold is None:
                return ["保护性措施不存在，无法解除"]
            if hold.lifted:
                return ["保护性措施已解除，请勿重复操作"]
            return []

        if event_type == T.DECISION_RECORDED:
            errors = []
            if payload["decider_role"] not in (R.SUPERVISOR, R.COMPLIANCE, R.APPEALS_OFFICER):
                errors.append("最终决定须由网点主管、分行合规或申诉专员作出")
            if payload["outcome"] == "decline":
                confirmed_by_review = any(
                    r.conclusion == "concern_confirmed" for r in case.reviews
                )
                confirmed_by_authority = any(
                    a.disposition == "fraud_confirmed" for a in case.authority_responses
                )
                if not (confirmed_by_review or confirmed_by_authority):
                    errors.append(
                        "止付决定必须基于已确认事实（主管复核确认风险或警方确认诈骗）；"
                        "年龄、金额或家属反对本身不足以否定客户交易能力"
                    )
            return errors

        if event_type == T.FUNDS_RELEASED:
            outcome, _, _ = _effective_outcome(case, occurred)
            if outcome != "release":
                return ["当前无有效放行决定，支付系统不得放款"]
            return []

        if event_type == T.APPEAL_FILED:
            if case.latest_hold() is None and not case.decisions:
                return ["本案尚无保护性措施或最终决定，无需申诉"]
            return []

        if event_type == T.APPEAL_REVIEWED:
            if case.appeal is None:
                return ["本案尚无申诉记录"]
            if case.appeal.reviewed:
                return ["申诉已复核，请勿重复处理"]
            errors = []
            if payload["reviewer_role"] != R.APPEALS_OFFICER:
                errors.append("申诉复核须由申诉专员作出")
            latest = case.latest_decision()
            if latest is not None and payload["reviewer_id"] == latest.decider_id:
                errors.append("申诉复核人不得为原决定人")
            return errors

        if event_type in (T.SIGNAL_CONFIRMED, T.SIGNAL_RETRACTED):
            if envelope["aggregate_id"] not in case.signals:
                return [f"信号不存在：{envelope['aggregate_id']}"]
            return []

        return []

    # ---------- 案件投影 ----------

    def _project_cases(self) -> dict[str, CaseProjection]:
        """从事件存储确定性重建案件投影。事件量小，重建换取幂等与乱序收敛。"""
        cases: dict[str, CaseProjection] = {}
        T = ev.EventType
        for stored in self.store.all():
            env = stored.envelope
            event_type = env["event_type"]
            payload = env.get("payload") or {}
            occurred = ev.parse_time(env["occurred_at"])
            if event_type == T.TRANSACTION_FLAGGED:
                case_id = payload["case_id"]
                if case_id not in cases:
                    cases[case_id] = CaseProjection(
                        case_id=case_id,
                        customer_id=payload["customer_id"],
                        amount=payload["amount"],
                        currency=payload.get("currency", "CNY"),
                        declared_purpose=payload.get("declared_purpose", ""),
                        destination=payload.get("destination"),
                        channel=payload.get("channel", "柜面"),
                        trigger_kind=payload["trigger"]["kind"],
                        trigger_detail=payload["trigger"].get("detail", ""),
                        flagged_at=occurred,
                    )
                continue
            case = cases.get(payload.get("case_id"))
            if case is None:
                continue  # 未通过业务校验的事件不会入库，此处仅为防御
            if event_type == T.SELF_EXPRESSION_RECORDED:
                case.self_expression = {
                    "statement": payload["statement"],
                    "expressed_independently": payload["expressed_independently"],
                    "companion_present": payload.get("companion_present", False),
                    "recorded_by": payload.get("recorded_by", ""),
                    "occurred_at": occurred,
                }
            elif event_type == T.COMPANION_PROFILE_RECORDED:
                case.companion = {
                    "relationship": payload["relationship"],
                    "answers_for_customer": payload.get("answers_for_customer", False),
                    "rushing": payload.get("rushing", False),
                    "blocks_contact": payload.get("blocks_contact", False),
                    "note": payload.get("note", ""),
                    "occurred_at": occurred,
                }
            elif event_type == T.CONTACT_ATTEMPTED:
                case.contact_attempts.append(
                    ContactAttempt(
                        channel=payload["channel"],
                        target=payload["target"],
                        result=payload["result"],
                        note=payload.get("note", ""),
                        occurred_at=occurred,
                    )
                )
            elif event_type == T.SUPERVISOR_REVIEWED:
                case.reviews.append(
                    Review(
                        reviewer_id=payload["reviewer_id"],
                        reviewer_role=payload["reviewer_role"],
                        conclusion=payload["conclusion"],
                        rationale=payload["rationale"],
                        occurred_at=occurred,
                    )
                )
            elif event_type == T.SIGNAL_RECORDED:
                case.signals[env["aggregate_id"]] = Signal(
                    signal_id=env["aggregate_id"],
                    kind=payload["kind"],
                    observation=payload["observation"],
                    assessment=payload.get("assessment"),
                    confirmed=False,
                    retracted=False,
                    occurred_at=occurred,
                    version=env["version"],
                )
            elif event_type == T.SIGNAL_CONFIRMED:
                case.signals[env["aggregate_id"]].confirmed = True
            elif event_type == T.SIGNAL_RETRACTED:
                case.signals[env["aggregate_id"]].retracted = True
            elif event_type == T.HOLD_APPLIED:
                case.holds[env["aggregate_id"]] = Hold(
                    hold_id=env["aggregate_id"],
                    reason=payload["reason"],
                    applied_at=occurred,
                    expires_at=occurred + timedelta(minutes=payload["duration_minutes"]),
                    escalation_owner_role=payload["escalation_owner_role"],
                    decider_id=payload["decider_id"],
                    version=env["version"],
                )
            elif event_type == T.HOLD_EXTENDED:
                hold = case.holds[env["aggregate_id"]]
                hold.reason = payload["reason"]
                hold.expires_at = occurred + timedelta(minutes=payload["duration_minutes"])
                hold.escalation_owner_role = payload["escalation_owner_role"]
                hold.version = env["version"]
            elif event_type == T.HOLD_LIFTED:
                hold = case.holds[env["aggregate_id"]]
                hold.lifted = True
                hold.lift_basis = payload["basis"]
                hold.lifted_at = occurred
            elif event_type == T.DECISION_RECORDED:
                case.decisions.append(
                    Decision(
                        outcome=payload["outcome"],
                        basis=payload["basis"],
                        decider_id=payload["decider_id"],
                        decider_role=payload["decider_role"],
                        occurred_at=occurred,
                        version=env["version"],
                    )
                )
            elif event_type == T.FUNDS_RELEASED:
                case.funds_released = True
                case.payment_ref = payload["payment_ref"]
            elif event_type == T.APPEAL_FILED:
                case.appeal = Appeal(
                    filed_by=payload["filed_by"],
                    channel=payload["channel"],
                    grounds=payload["grounds"],
                    filed_at=occurred,
                )
            elif event_type == T.APPEAL_REVIEWED:
                case.appeal.reviewed = True
                case.appeal.review_outcome = payload["outcome"]
                case.appeal.reviewer_id = payload["reviewer_id"]
                case.appeal.reviewed_at = occurred
                if payload["outcome"] == "overturn":
                    latest = case.latest_decision()
                    if latest is not None:
                        latest.invalidated = True
            elif event_type == T.AUTHORITY_RESPONDED:
                case.authority_responses.append(
                    AuthorityResponse(
                        authority=payload["authority"],
                        reference=payload["reference"],
                        disposition=payload["disposition"],
                        note=payload.get("note", ""),
                        occurred_at=occurred,
                    )
                )
        return cases

    # ---------- 柜员界面：下一动作与倒计时 ----------

    def _next_action(self, case: CaseProjection, now: datetime) -> WorkItem | None:
        policy = self.policy
        if case.appeal is not None and not case.appeal.reviewed:
            return WorkItem(
                case.case_id,
                "申诉复核：核实客户申诉理由并作出结论",
                Role.APPEALS_OFFICER,
                case.appeal.filed_at + policy.appeal_review_sla,
            )
        if (
            case.appeal is not None
            and case.appeal.reviewed
            and case.appeal.review_outcome == "overturn"
            and case.latest_decision() is None
        ):
            return WorkItem(
                case.case_id,
                "依据申诉复核结论登记最终决定",
                Role.APPEALS_OFFICER,
                case.appeal.reviewed_at + policy.decision_sla,
            )
        outcome, basis, obj = _effective_outcome(case, now)
        if outcome == "hold":
            return WorkItem(
                case.case_id,
                "届满前完成最终决定，并确保客户已取得书面理由与申诉入口",
                obj.escalation_owner_role,
                obj.expires_at,
            )
        if outcome == "release":
            if case.funds_released:
                return None
            if basis == "hold_expired_default":
                return WorkItem(
                    case.case_id,
                    "保护性措施已届满：无有效决定，默认放行，并登记升级责任失守",
                    Role.SUPERVISOR,
                    obj.expires_at + policy.payment_execution_sla,
                    breach=True,
                )
            return WorkItem(
                case.case_id,
                "支付系统执行放行",
                Role.PAYMENT_SYSTEM,
                obj.occurred_at + policy.payment_execution_sla,
            )
        if outcome == "decline":
            return WorkItem(
                case.case_id,
                "向客户送达书面理由与申诉入口",
                Role.TELLER,
                obj.occurred_at + policy.notice_sla,
            )
        # 核实流程
        if case.self_expression is None or not case.self_expression["expressed_independently"]:
            return WorkItem(
                case.case_id,
                "请客户本人独立陈述取款用途与意愿（陪同者不得代答）",
                Role.TELLER,
                case.flagged_at + policy.self_expression_sla,
            )
        if case.companion_interference() and not case.customer_confirmed():
            return WorkItem(
                case.case_id,
                "单独联系客户本人核实意愿（必要时联系预留紧急联系人）",
                Role.TELLER,
                case.flagged_at + policy.contact_sla,
            )
        if not case.reviews:
            return WorkItem(
                case.case_id,
                "主管复核：结合自主表达、陪同关系、交易上下文与现场信号作出结论",
                Role.SUPERVISOR,
                case.flagged_at + policy.supervisor_review_sla,
            )
        latest = case.latest_review()
        if latest.conclusion == "need_more_facts":
            return WorkItem(
                case.case_id,
                "补充核实材料后再次提交主管复核",
                Role.TELLER,
                latest.occurred_at + policy.contact_sla,
            )
        return WorkItem(
            case.case_id,
            "登记最终决定（放行或申请保护性措施）",
            Role.SUPERVISOR,
            latest.occurred_at + policy.decision_sla,
        )

    def teller_worklist(self, now: datetime | None = None) -> list[WorkItem]:
        """柜员界面工单：按截止时间排序的下一动作（含责任人与倒计时）。"""
        now = now or self.clock()
        items = []
        for case in self._project_cases().values():
            item = self._next_action(case, now)
            if item is not None:
                items.append(item)
        items.sort(key=lambda i: (i.deadline, i.case_id))
        return items

    # ---------- 案件视图 ----------

    @staticmethod
    def _status_text(case: CaseProjection, outcome: str, basis: str) -> str:
        if case.funds_released:
            return "已结案（已放行）"
        if outcome == "decline":
            if case.appeal is not None and not case.appeal.reviewed:
                return "已止付（申诉复核中）"
            return "已止付"
        if outcome == "release":
            return "待放行执行" if basis == "decision" else "保护性措施已届满，默认放行"
        if outcome == "hold":
            return "保护性措施中"
        return "核实中"

    @staticmethod
    def _capability_text(outcome: str) -> str:
        if outcome == "decline":
            return "本案交易已止付（仅限本案，不构成对客户交易能力的否定）"
        return "未受影响"

    def case_view(self, case_id: str, now: datetime | None = None) -> dict | None:
        """网点内部案件视图：状态、交易能力、下一动作。"""
        now = now or self.clock()
        case = self._project_cases().get(case_id)
        if case is None:
            return None
        outcome, basis, obj = _effective_outcome(case, now)
        item = self._next_action(case, now)
        view = {
            "case_id": case.case_id,
            "customer_id": case.customer_id,
            "status": self._status_text(case, outcome, basis),
            "transaction_capability": self._capability_text(outcome),
            "effective_outcome": outcome,
            "outcome_basis": basis,
            "flagged_at": case.flagged_at.isoformat(),
            "escalation_breach": _escalation_breach(case, now),
            "next_action": item.as_dict(now) if item else None,
            "counts": {
                "signals": len(case.signals),
                "contact_attempts": len(case.contact_attempts),
                "holds": len(case.holds),
                "decisions": len(case.decisions),
            },
        }
        hold = case.latest_hold()
        if hold is not None:
            view["current_hold"] = {
                "hold_id": hold.hold_id,
                "reason": hold.reason,
                "applied_at": hold.applied_at.isoformat(),
                "expires_at": hold.expires_at.isoformat(),
                "escalation_owner_role": hold.escalation_owner_role,
                "lifted": hold.lifted,
            }
        decision = case.latest_decision()
        if decision is not None:
            view["latest_decision"] = {
                "outcome": decision.outcome,
                "basis": decision.basis,
                "decider_id": decision.decider_id,
                "occurred_at": decision.occurred_at.isoformat(),
            }
        if case.appeal is not None:
            view["appeal"] = {
                "filed_by": case.appeal.filed_by,
                "filed_at": case.appeal.filed_at.isoformat(),
                "reviewed": case.appeal.reviewed,
                "review_outcome": case.appeal.review_outcome,
            }
        return view

    # ---------- 客户书面理由与申诉入口 ----------

    def customer_notice(self, case_id: str, now: datetime | None = None) -> dict | None:
        """生成客户书面理由：只含事实与正式理由，未确认主观标签一律不出现。"""
        now = now or self.clock()
        case = self._project_cases().get(case_id)
        if case is None:
            return None
        outcome, basis, obj = _effective_outcome(case, now)
        facts = [
            f"本次取款（金额 {case.amount} {case.currency}）因{ev.TRIGGER_TEXT[case.trigger_kind]}"
            "进入核实程序；该因素仅用于启动核实，不代表对您交易能力的否定。"
        ]
        if case.declared_purpose:
            facts.append(f"您申报的取款用途：{case.declared_purpose}。")
        for sig in case.signals.values():
            if sig.confirmed and not sig.retracted:
                facts.append(f"现场核实事实：{sig.observation}。")
        for attempt in case.contact_attempts:
            facts.append(
                f"{_fmt(attempt.occurred_at)} 通过{ev.CHANNEL_TEXT.get(attempt.channel, attempt.channel)}"
                f"联系{ev.TARGET_TEXT.get(attempt.target, attempt.target)}："
                f"{ev.RESULT_TEXT.get(attempt.result, attempt.result)}。"
            )
        measure, reason, expires, anchor = self._measure_text(outcome, basis, obj)
        return {
            "case_id": case.case_id,
            "issued_at": now.isoformat(),
            "facts": facts,
            "current_measure": measure,
            "measure_reason": reason,
            "measure_expires_at": expires.isoformat() if expires else None,
            "appeal_channel": self.policy.appeal_channel,
            "appeal_deadline": (anchor + self.policy.appeal_window).isoformat() if anchor else None,
        }

    @staticmethod
    def _measure_text(outcome: str, basis: str, obj: object):
        if outcome == "hold":
            return (
                f"自{_fmt(obj.applied_at)}起对本次取款采取保护性止付，期限至{_fmt(obj.expires_at)}。",
                obj.reason,
                obj.expires_at,
                obj.applied_at,
            )
        if outcome == "decline":
            return "本次取款已止付。", obj.basis, None, obj.occurred_at
        if outcome == "release":
            anchor = obj.occurred_at if basis == "decision" else None
            return "本次取款已放行。", None, None, anchor
        return "本次取款正在核实中，暂未采取限制措施。", None, None, None

    # ---------- 其他银行业务视图（主观标签隔离） ----------

    def shared_risk_view(self, case_id: str, now: datetime | None = None) -> dict | None:
        """供行内其他业务使用的视图：只含已确认事实，未确认主观标签一律隔离。"""
        now = now or self.clock()
        case = self._project_cases().get(case_id)
        if case is None:
            return None
        outcome, basis, _ = _effective_outcome(case, now)
        confirmed = [s for s in case.signals.values() if s.confirmed and not s.retracted]
        return {
            "case_id": case.case_id,
            "status": self._status_text(case, outcome, basis),
            "transaction_capability": self._capability_text(outcome),
            "confirmed_observations": [s.observation for s in confirmed],
            "confirmed_assessments": [s.assessment for s in confirmed if s.assessment],
        }

    # ---------- 警方分级披露 ----------

    def police_disclosure(
        self,
        case_id: str,
        authorization: LegalAuthorization | None = None,
        now: datetime | None = None,
    ) -> dict:
        """无授权仅提供最低限度信息；持合法授权方可调取完整证据与资金去向。"""
        now = now or self.clock()
        case = self._project_cases().get(case_id)
        if authorization is None:
            self._disclosure_log.append(DisclosureRecord(case_id, now, "minimal", None))
            return {
                "level": "minimal",
                "case_exists": case is not None,
                "liaison_channel": "警银联络专线（本网点）",
                "hint": "完整证据与资金去向须持合法授权文件",
            }
        errors = validate_authorization(authorization, case_id, now)
        if errors or case is None:
            if case is None:
                errors = errors + [f"案件不存在：{case_id}"]
            self._disclosure_log.append(
                DisclosureRecord(case_id, now, "denied", authorization.document_no)
            )
            return {"level": "denied", "errors": errors}
        view = {
            "level": "full",
            "case_id": case_id,
            "document_no": authorization.document_no,
            "timeline": self._timeline(case_id),
        }
        if "evidence" in authorization.covers:
            view["evidence"] = self._evidence_file(case)
        if "fund_flow" in authorization.covers:
            view["fund_destination"] = case.destination
        self._disclosure_log.append(
            DisclosureRecord(case_id, now, "full", authorization.document_no)
        )
        return view

    def disclosure_log(self) -> list[DisclosureRecord]:
        return list(self._disclosure_log)

    def _timeline(self, case_id: str) -> list[dict]:
        return [
            {
                "event_id": s.envelope["event_id"],
                "event_type": s.envelope["event_type"],
                "aggregate_type": s.envelope["aggregate_type"],
                "aggregate_id": s.envelope["aggregate_id"],
                "occurred_at": s.envelope["occurred_at"],
                "summary": s.envelope["summary"],
            }
            for s in self.store.all()
            if (s.envelope.get("payload") or {}).get("case_id") == case_id
        ]

    @staticmethod
    def _evidence_file(case: CaseProjection) -> dict:
        """完整卷宗（含未确认主观标签），仅持合法授权后向警方提供。"""
        return {
            "self_expression": case.self_expression,
            "companion": case.companion,
            "signals": [
                {
                    "signal_id": s.signal_id,
                    "observation": s.observation,
                    "assessment": s.assessment,
                    "confirmed": s.confirmed,
                    "retracted": s.retracted,
                    "occurred_at": s.occurred_at.isoformat(),
                }
                for s in case.signals.values()
            ],
            "contact_attempts": [
                {
                    "channel": a.channel,
                    "target": a.target,
                    "result": a.result,
                    "note": a.note,
                    "occurred_at": a.occurred_at.isoformat(),
                }
                for a in case.contact_attempts
            ],
            "reviews": [
                {
                    "reviewer_id": r.reviewer_id,
                    "conclusion": r.conclusion,
                    "rationale": r.rationale,
                    "occurred_at": r.occurred_at.isoformat(),
                }
                for r in case.reviews
            ],
            "holds": [
                {
                    "hold_id": h.hold_id,
                    "reason": h.reason,
                    "applied_at": h.applied_at.isoformat(),
                    "expires_at": h.expires_at.isoformat(),
                    "lifted": h.lifted,
                }
                for h in case.holds.values()
            ],
            "decisions": [
                {
                    "outcome": d.outcome,
                    "basis": d.basis,
                    "decider_id": d.decider_id,
                    "invalidated": d.invalidated,
                    "occurred_at": d.occurred_at.isoformat(),
                }
                for d in case.decisions
            ],
            "authority_responses": [
                {
                    "authority": a.authority,
                    "reference": a.reference,
                    "disposition": a.disposition,
                    "occurred_at": a.occurred_at.isoformat(),
                }
                for a in case.authority_responses
            ],
        }
