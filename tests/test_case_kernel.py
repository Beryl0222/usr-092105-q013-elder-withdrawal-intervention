import unittest
from datetime import datetime, timedelta

from src.case_kernel import (
    EventStore, CaseProjector, Sweeper, CaseCommands,
    TellerView, CustomerNotice, ExternalBusinessView, PoliceDisclosureGate,
    deadline_for, POLICY_MINUTES,
)

T0 = datetime.fromisoformat("2026-09-20T14:00:00+08:00")
CASE = "WC-20260920-013"
CUST = "C-8899"


def iso(dt):
    return dt.isoformat()


def open_case(store, amount="CNY 1,200,000", purpose="购房"):
    return store.append({
        "event_id": "evt-092105-013-0001",
        "event_type": "TRANSACTION_FLAGGED",
        "aggregate_type": "withdrawal_case",
        "aggregate_id": CASE,
        "occurred_at": iso(T0),
        "version": 1,
        "summary": "大额取款开案：陪同者代答且阻止客户接电话",
        "data": {
            "channel": "branch",
            "customer_id": CUST,
            "requested_amount_ccy": amount,
            "stated_purpose": purpose,
            "companion_present": True,
            "trigger_codes": ["companion.answers_for_customer", "companion.blocks_phone"],
        },
    })


class IdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.store = EventStore()
        open_case(self.store)

    def test_same_event_id_retry_is_duplicate_and_keeps_original_time(self):
        retry = {
            "event_id": "evt-092105-013-0001",
            "event_type": "TRANSACTION_FLAGGED",
            "aggregate_type": "withdrawal_case",
            "aggregate_id": CASE,
            # 支付系统 9 分钟后重推，但 occurred_at 仍是原始时间
            "occurred_at": iso(T0),
            "received_at": iso(T0 + timedelta(minutes=9)),
            "version": 1,
            "summary": "大额取款开案：陪同者代答且阻止客户接电话",
            "data": {"channel": "branch", "customer_id": CUST,
                     "requested_amount_ccy": "CNY 1,200,000", "stated_purpose": "购房",
                     "companion_present": True,
                     "trigger_codes": ["companion.answers_for_customer", "companion.blocks_phone"]},
        }
        result = self.store.append(retry)
        self.assertEqual(result.status, "duplicate")
        events = self.store.events_for_case(CASE)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["occurred_at"], iso(T0))
        self.assertEqual(self.store.metrics["duplicates"], 1)

    def test_same_event_id_different_payload_is_conflict_not_overwrite(self):
        bad = {
            "event_id": "evt-092105-013-0001",
            "event_type": "TRANSACTION_FLAGGED",
            "aggregate_type": "withdrawal_case",
            "aggregate_id": CASE,
            "occurred_at": iso(T0),
            "version": 1,
            "summary": "篡改内容的重推",
            "data": {"channel": "branch", "customer_id": CUST,
                     "requested_amount_ccy": "CNY 9.00", "companion_present": False},
        }
        self.assertEqual(self.store.append(bad).status, "conflict")
        self.assertEqual(len(self.store.events_for_case(CASE)), 1)

    def test_natural_key_duplicate_with_new_event_id_is_ignored(self):
        cmd = CaseCommands(self.store)
        r1 = cmd.record_signal(CASE, iso(T0 + timedelta(minutes=2)), "fact",
                               "companion.answers_for_customer",
                               "陪同者三次替客户回答资金用途")
        r2 = self.store.append({
            "event_id": "evt-pay-resend-xyz",
            "event_type": "SIGNAL_RECORDED",
            "aggregate_type": "observed_signal",
            "aggregate_id": r1.ref and f"sig-{CASE}-01",
            "occurred_at": iso(T0 + timedelta(minutes=2)),
            "version": 1,
            "summary": "支付侧换 ID 重发",
            "case_id": CASE,
            "data": {"grade": "fact", "code": "companion.answers_for_customer",
                     "detail": "陪同者三次替客户回答资金用途", "source": "teller"},
        })
        self.assertEqual(r1.status, "accepted")
        self.assertEqual(r2.status, "duplicate")
        signals = [e for e in self.store.events_for_case(CASE) if e["event_type"] == "SIGNAL_RECORDED"]
        self.assertEqual(len(signals), 1)

    def test_invalid_event_rejected(self):
        bad = {"event_id": "x", "event_type": "WRONG", "aggregate_type": "withdrawal_case",
               "aggregate_id": CASE, "occurred_at": iso(T0), "version": 0, "summary": "坏"}
        self.assertEqual(self.store.append(bad).status, "invalid")


class TriggerNotNegationTest(unittest.TestCase):
    def setUp(self):
        self.store = EventStore()
        open_case(self.store)
        self.cmd = CaseCommands(self.store)

    def test_impression_alone_cannot_support_hold(self):
        sig = self.cmd.record_signal(CASE, iso(T0 + timedelta(minutes=2)), "impression",
                                     "staff.feels_controlled", "柜员感觉客户可能被控制")
        r = self.cmd.apply_hold(
            CASE, iso(T0 + timedelta(minutes=5)), [sig.ref],
            "SUPERVISOR_45MIN", approver="主管 王芳",
            customer_notified_at=iso(T0 + timedelta(minutes=6)))
        self.assertEqual(r.status, "invalid")
        self.assertIn("主观印象", r.detail)

    def test_family_objection_and_unreachable_only_trigger_verification(self):
        c1 = self.cmd.record_contact(CASE, iso(T0 + timedelta(minutes=3)),
                                     "trusted_contact", "call", "conflicting_opinion",
                                     note="家属反对取款")
        c2 = self.cmd.record_contact(CASE, iso(T0 + timedelta(minutes=4)),
                                     "customer_alone", "callback", "unreachable")
        r = self.cmd.apply_hold(
            CASE, iso(T0 + timedelta(minutes=6)), [c1.ref, c2.ref],
            "SUPERVISOR_45MIN", approver="主管 王芳",
            customer_notified_at=iso(T0 + timedelta(minutes=6)))
        self.assertEqual(r.status, "invalid")
        self.assertIn("家属反对", r.detail)

    def test_blocked_phone_fact_supports_initial_hold_with_three_elements(self):
        contact = self.cmd.record_contact(CASE, iso(T0 + timedelta(minutes=2)),
                                          "customer_alone", "in_person", "blocked_on_site",
                                          note="陪同者阻止客户接听银行回拨")
        r = self.cmd.apply_hold(
            CASE, iso(T0 + timedelta(minutes=6)), [contact.ref],
            "SUPERVISOR_45MIN", approver="主管 王芳(S042)",
            customer_notified_at=iso(T0 + timedelta(minutes=6)),
            appeal_code="APL-013-7")
        self.assertEqual(r.status, "accepted")
        hold_evt = next(e for e in self.store.events_for_case(CASE) if e["event_type"] == "HOLD_APPLIED")
        self.assertEqual(hold_evt["data"]["deadline_at"], iso(T0 + timedelta(minutes=45)))

    def test_consistent_customer_purchase_statement_releases_despite_family(self):
        # 真实购房：客户单独表达清楚、一致 → 即使家属反对也放行
        self.cmd.record_contact(CASE, iso(T0 + timedelta(minutes=2)),
                                "trusted_contact", "call", "conflicting_opinion", note="子女反对")
        stmt = self.cmd.customer_statement(
            CASE, iso(T0 + timedelta(minutes=4)),
            "我知道取 120 万，是给XX楼盘的购房首付，监管账户和合同我都带了", True)
        self.assertEqual(stmt.status, "accepted")
        r = self.cmd.release(CASE, iso(T0 + timedelta(minutes=8)), "verification_passed",
                             actor=("teller", "T118", "李明"))
        self.assertEqual(r.status, "accepted")
        state = CaseProjector(self.store).state(CASE, T0 + timedelta(minutes=8))
        self.assertEqual(state.status, "CLOSED_RELEASED")


class DeadlineAndDecisionTest(unittest.TestCase):
    def setUp(self):
        self.store = EventStore()
        open_case(self.store)
        self.cmd = CaseCommands(self.store)
        contact = self.cmd.record_contact(CASE, iso(T0 + timedelta(minutes=2)),
                                          "customer_alone", "in_person", "blocked_on_site")
        self.hold = self.cmd.apply_hold(
            CASE, iso(T0 + timedelta(minutes=6)), [contact.ref],
            "SUPERVISOR_45MIN", approver="主管 王芳(S042)",
            customer_notified_at=iso(T0 + timedelta(minutes=6)), appeal_code="APL-013-7")
        self.sweeper = Sweeper(self.store)

    def test_no_decision_at_decline_default_releases_and_records_responsible(self):
        # 截止前一秒：仍在 HELD
        self.assertEqual(CaseProjector(self.store).state(CASE, T0 + timedelta(minutes=44, seconds=59)).status, "HELD")
        created = self.sweeper.sweep(T0 + timedelta(minutes=46))
        self.assertEqual(len(created), 2)
        expired = next(e for e in created if e["event_type"] == "HOLD_EXPIRED")
        released = next(e for e in created if e["event_type"] == "FUNDS_RELEASED")
        self.assertEqual(expired["data"]["responsible_role"], "supervisor")
        self.assertEqual(released["data"]["release_reason"], "hold_expired")
        # 届满时间保留为真实截止时刻（45 分），不是扫描时刻（46 分）
        self.assertEqual(expired["occurred_at"], iso(T0 + timedelta(minutes=45)))
        state = CaseProjector(self.store).state(CASE, T0 + timedelta(minutes=46))
        self.assertEqual(state.status, "CLOSED_RELEASED")

    def test_sweep_is_idempotent(self):
        first = self.sweeper.sweep(T0 + timedelta(minutes=50))
        second = self.sweeper.sweep(T0 + timedelta(minutes=60))
        self.assertEqual(len(first), 2)
        self.assertEqual(second, [])
        releases = [e for e in self.store.events_for_case(CASE) if e["event_type"] == "FUNDS_RELEASED"]
        self.assertEqual(len(releases), 1)

    def test_latest_effective_release_decision_is_executed_mechanically(self):
        # 主管在截止前作出 release，但没有再点“放款”——届满扫描机械执行
        self.cmd._append(CASE, f"fd-{CASE}", "final_decision", "SUPERVISOR_REVIEWED",
                         iso(T0 + timedelta(minutes=40)),
                         {"decision": "release", "reviewer": "主管 王芳(S042)",
                          "basis_refs": ["evt-contact"], "written_reason": "单独沟通后确认购房意愿真实"},
                         "主管复核放行", ("supervisor", "S042", "王芳"))
        # 需要真实存在的依据引用
        created = self.sweeper.sweep(T0 + timedelta(minutes=46))
        reasons = [e["data"]["release_reason"] for e in created if e["event_type"] == "FUNDS_RELEASED"]
        self.assertEqual(reasons, ["supervisor_release"])

    def test_continue_without_new_hold_does_not_extend(self):
        self.cmd._append(CASE, f"fd-{CASE}", "final_decision", "SUPERVISOR_REVIEWED",
                         iso(T0 + timedelta(minutes=40)),
                         {"decision": "continue", "reviewer": "主管 王芳(S042)",
                          "basis_refs": ["evt-contact"]},
                         "主管要求继续（但没有新三要素延续）", ("supervisor", "S042", "王芳"))
        created = self.sweeper.sweep(T0 + timedelta(minutes=46))
        self.assertTrue(any(e["event_type"] == "HOLD_EXPIRED" for e in created))


class AppealTest(unittest.TestCase):
    def setUp(self):
        self.store = EventStore()
        open_case(self.store)
        self.cmd = CaseCommands(self.store)
        contact = self.cmd.record_contact(CASE, iso(T0 + timedelta(minutes=2)),
                                          "hotline_96110", "call", "alert_hit")
        self.cmd.apply_hold(CASE, iso(T0 + timedelta(minutes=6)), [contact.ref],
                            "SUPERVISOR_45MIN", approver="主管 王芳(S042)",
                            customer_notified_at=iso(T0 + timedelta(minutes=6)),
                            appeal_code="APL-013-7")
        self.sweeper = Sweeper(self.store)

    def test_appeal_upheld_releases_immediately(self):
        self.cmd.file_appeal(CASE, iso(T0 + timedelta(minutes=10)), "APL-013-7", "qr_code")
        r = self.cmd.decide_appeal(CASE, iso(T0 + timedelta(minutes=25)), "APL-013-7",
                                   upheld=True, reviewer="上级行值班岗 赵磊(S201)",
                                   written_reason="客户出示购房合同编号且表述一致，撤销措施")
        self.assertEqual(r.status, "accepted")
        self.cmd.release(CASE, iso(T0 + timedelta(minutes=25)), "appeal_upheld",
                         actor=("independent_reviewer", "S201", "赵磊"))
        state = CaseProjector(self.store).state(CASE, T0 + timedelta(minutes=25))
        self.assertEqual(state.status, "CLOSED_RELEASED")

    def test_appeal_timeout_auto_releases_even_if_hold_window_longer(self):
        # 48h 段中提出申诉，2h 未决即解除
        hold2 = self.cmd.extend_hold(
            CASE, iso(T0 + timedelta(minutes=44)), f"hold-{CASE}-01",
            ["evt-092105-013-0001"], "EXTENSION_4H", approver="主管 王芳(S042)")
        # 需要先有 L2 才能进 48h；这里仅测试 4h 段中的申诉超时
        self.assertEqual(hold2.status, "accepted")
        self.cmd.file_appeal(CASE, iso(T0 + timedelta(minutes=70)), "APL-013-7", "hotline")
        state = CaseProjector(self.store).state(CASE, T0 + timedelta(minutes=71))
        self.assertEqual(state.status, "PENDING_APPEAL")
        created = self.sweeper.sweep(T0 + timedelta(minutes=200))
        self.assertTrue(any(e["data"].get("release_reason") == "appeal_timeout"
                            for e in created if e["event_type"] == "FUNDS_RELEASED"))
        self.assertEqual(CaseProjector(self.store).state(CASE, T0 + timedelta(minutes=200)).status,
                         "CLOSED_RELEASED")

    def test_appeal_must_be_independent_reviewer(self):
        self.cmd.file_appeal(CASE, iso(T0 + timedelta(minutes=10)), "APL-013-7")
        r = self.cmd.decide_appeal(CASE, iso(T0 + timedelta(minutes=20)), "APL-013-7",
                                   upheld=False, reviewer="主管 王芳(S042)",
                                   written_reason="维持", actor=("supervisor", "S042", "王芳"))
        self.assertEqual(r.status, "invalid")


class StopPaymentLegalityTest(unittest.TestCase):
    def setUp(self):
        self.store = EventStore()
        open_case(self.store)
        self.cmd = CaseCommands(self.store)

    def test_stop_requires_customer_request_evidence_or_legal_order(self):
        r1 = self.cmd.stop_payment(CASE, iso(T0 + timedelta(minutes=30)),
                                   "customer_request", approver="支行长 陈敏",
                                   customer_notified_at=iso(T0 + timedelta(minutes=30)))
        self.assertEqual(r1.status, "invalid")
        r2 = self.cmd.stop_payment(CASE, iso(T0 + timedelta(minutes=30)),
                                   "legal_order", approver="支行长 陈敏",
                                   customer_notified_at=iso(T0 + timedelta(minutes=30)))
        self.assertEqual(r2.status, "invalid")
        r3 = self.cmd.stop_payment(CASE, iso(T0 + timedelta(minutes=30)),
                                   "legal_order", approver="支行长 陈敏",
                                   customer_notified_at=iso(T0 + timedelta(minutes=30)),
                                   legal_case_number="(2026)反诈冻字第0920号")
        self.assertEqual(r3.status, "accepted")
        state = CaseProjector(self.store).state(CASE, T0 + timedelta(minutes=30))
        self.assertEqual(state.status, "STOP_PAYMENT")


class ViewTest(unittest.TestCase):
    def setUp(self):
        self.store = EventStore()
        open_case(self.store)
        self.cmd = CaseCommands(self.store)

    def test_teller_view_countdown_owner_and_next_action(self):
        view = TellerView(self.store).render(CASE, T0 + timedelta(minutes=2))
        self.assertEqual(view["status"], "OPENED")
        self.assertEqual(view["owner"], "teller")
        self.assertEqual(view["seconds_left"], 13 * 60)
        self.assertIn("单独沟通", view["next_action"])

        sig = self.cmd.record_contact(CASE, iso(T0 + timedelta(minutes=3)),
                                      "customer_alone", "in_person", "blocked_on_site")
        self.cmd.apply_hold(CASE, iso(T0 + timedelta(minutes=6)), [sig.ref],
                            "SUPERVISOR_45MIN", approver="主管 王芳(S042)",
                            customer_notified_at=iso(T0 + timedelta(minutes=6)),
                            appeal_code="APL-013-7")
        view2 = TellerView(self.store).render(CASE, T0 + timedelta(minutes=40))
        self.assertEqual(view2["status"], "HELD")
        self.assertEqual(view2["owner"], "主管 王芳(S042)")
        self.assertEqual(view2["seconds_left"], 5 * 60)
        self.assertIn("默认放行", view2["default_outcome"])

    def test_customer_notice_has_plain_reasons_deadline_and_appeal(self):
        sig = self.cmd.record_signal(CASE, iso(T0 + timedelta(minutes=2)), "fact",
                                     "companion.blocks_phone", "现场陪同的人不让您接听我们的核对电话")
        self.cmd.apply_hold(CASE, iso(T0 + timedelta(minutes=6)), [sig.ref],
                            "SUPERVISOR_45MIN", approver="主管 王芳(S042)",
                            customer_notified_at=iso(T0 + timedelta(minutes=6)),
                            appeal_code="APL-013-7")
        notice = CustomerNotice(self.store).render(CASE, T0 + timedelta(minutes=10))
        self.assertIn("不是拒绝", notice["headline"])
        self.assertEqual(notice["reasons_in_plain_language"], ["现场陪同的人不让您接听我们的核对电话"])
        self.assertEqual(notice["deadline_at"], iso(T0 + timedelta(minutes=45)))
        self.assertEqual(notice["appeal_code"], "APL-013-7")
        self.assertIn("专线电话", notice["appeal_channels"])

    def test_external_view_empty_until_legal_authority_and_cleared_after_release(self):
        ext = ExternalBusinessView(self.store)
        self.assertEqual(ext.for_customer(CUST, T0 + timedelta(minutes=5))["visible"], [])
        # 案件暂停中、但无警方受案：其他业务依然什么都看不到
        sig = self.cmd.record_signal(CASE, iso(T0 + timedelta(minutes=2)), "fact",
                                     "companion.rushes", "陪同者持续催促快点办")
        self.cmd.apply_hold(CASE, iso(T0 + timedelta(minutes=6)), [sig.ref],
                            "SUPERVISOR_45MIN", approver="主管 王芳(S042)",
                            customer_notified_at=iso(T0 + timedelta(minutes=6)))
        self.assertEqual(ext.for_customer(CUST, T0 + timedelta(minutes=10))["visible"], [])
        # 放行结案后立即为空
        self.cmd.release(CASE, iso(T0 + timedelta(minutes=12)), "verification_passed")
        self.assertEqual(ext.for_customer(CUST, T0 + timedelta(minutes=13))["visible"], [])

    def test_police_gate_levels(self):
        gate = PoliceDisclosureGate(self.store)
        self.assertFalse(gate.request(CASE, "L1")["allowed"])
        no_num = gate.request(CASE, "L2")
        self.assertFalse(no_num["allowed"])
        ok_l2 = gate.request(CASE, "L2", case_number="A2026-0920-15",
                             now=T0 + timedelta(minutes=10))
        self.assertTrue(ok_l2["allowed"])
        self.assertNotIn("all_events", ok_l2)
        self.assertIn("fact_signals", ok_l2)
        # 增加一条 impression：L2 不出
        self.cmd.record_signal(CASE, iso(T0 + timedelta(minutes=3)), "impression",
                               "staff.feels_controlled", "柜员主观感觉")
        l2 = gate.request(CASE, "L2", case_number="A2026-0920-15", now=T0 + timedelta(minutes=10))
        self.assertEqual(l2["fact_signals"], [])
        l3 = gate.request(CASE, "L3", case_number="(2026)调证字第0920号",
                          scope="本案交易与资金去向", now=T0 + timedelta(minutes=10))
        self.assertTrue(l3["allowed"])
        self.assertIn("all_events", l3)
        self.assertEqual(l3["funds_tracing"], "按文书范围提供")


class AuthorityWindowTest(unittest.TestCase):
    def setUp(self):
        self.store = EventStore()
        open_case(self.store)
        self.cmd = CaseCommands(self.store)
        self.hotline = self.cmd.record_contact(CASE, iso(T0 + timedelta(minutes=2)),
                                               "hotline_96110", "call", "alert_hit")
        self.cmd.apply_hold(CASE, iso(T0 + timedelta(minutes=6)), [self.hotline.ref],
                            "SUPERVISOR_45MIN", approver="主管 王芳(S042)",
                            customer_notified_at=iso(T0 + timedelta(minutes=6)))

    def test_48h_window_requires_l2_case_number(self):
        r = self.cmd.extend_hold(CASE, iso(T0 + timedelta(minutes=40)),
                                 f"hold-{CASE}-01", [self.hotline.ref],
                                 "AUTHORITY_48H", approver="主管 王芳(S042)")
        self.assertEqual(r.status, "invalid")

    def test_48h_window_clock_starts_at_authority_response_and_default_releases(self):
        # 45 分届满前主管凭具体事实延续 4h
        ext4h = self.cmd.extend_hold(CASE, iso(T0 + timedelta(minutes=44)),
                                     f"hold-{CASE}-01", [self.hotline.ref],
                                     "EXTENSION_4H", approver="主管 王芳(S042)")
        self.assertEqual(ext4h.status, "accepted")
        # T0+3h 警方受案（L2），主管据此进入 48h 窗口
        l2 = self.store.append({
            "event_id": "evt-police-l2-0001",
            "event_type": "AUTHORITY_RESPONDED",
            "aggregate_type": "final_decision",
            "aggregate_id": f"fd-{CASE}",
            "occurred_at": iso(T0 + timedelta(hours=3)),
            "version": 1,
            "summary": "警方受案并登记协查",
            "case_id": CASE,
            "actor": {"role": "police"},
            "data": {"authority_level": "L2", "case_number": "A2026-0920-15"},
        })
        self.assertEqual(l2.status, "accepted")
        ext48 = self.cmd.extend_hold(CASE, iso(T0 + timedelta(hours=3, minutes=5)),
                                     ext4h.ref, [self.hotline.ref],
                                     "AUTHORITY_48H", approver="主管 王芳(S042)",
                                     authority_level="L2")
        self.assertEqual(ext48.status, "accepted")
        ext_evt = next(e for e in self.store.events_for_case(CASE) if e["event_type"] == "HOLD_EXTENDED"
                       and e["data"]["deadline_key"] == "AUTHORITY_48H")
        # 48h 从受案到达时刻起算，而不是 T0
        self.assertEqual(ext_evt["data"]["deadline_at"], iso(T0 + timedelta(hours=51)))
        # 48h 届满无 L3、无有效决定 → 默认放行，责任人是反诈联络人
        created = Sweeper(self.store).sweep(T0 + timedelta(hours=52))
        expired = [e for e in created if e["event_type"] == "HOLD_EXPIRED"]
        self.assertTrue(expired)
        self.assertEqual(expired[0]["data"]["responsible_role"], "fraud_liaison")


class AccelerationTest(unittest.TestCase):
    def test_house_purchase_acceleration_halves_clock(self):
        self.assertEqual(deadline_for(T0, "SUPERVISOR_45MIN", accelerated=True),
                         T0 + timedelta(minutes=22, seconds=30))
        store = EventStore()
        open_case(store, purpose="购房")
        cmd = CaseCommands(store)
        sig = cmd.record_contact(CASE, iso(T0 + timedelta(minutes=2)),
                                 "hotline_96110", "call", "alert_hit")
        g = cmd.grant_acceleration(CASE, iso(T0 + timedelta(minutes=3)), approver="主管 王芳(S042)")
        self.assertEqual(g.status, "accepted")
        r = cmd.apply_hold(CASE, iso(T0 + timedelta(minutes=4)), [sig.ref],
                           "SUPERVISOR_45MIN", approver="主管 王芳(S042)",
                           customer_notified_at=iso(T0 + timedelta(minutes=4)),
                           appeal_code="APL-013-7", accelerated=True)
        self.assertEqual(r.status, "accepted")
        view = TellerView(store).render(CASE, T0 + timedelta(minutes=20))
        # 加速后 22:30 截止，20 分时还剩 2:30
        self.assertEqual(view["seconds_left"], 150)


if __name__ == "__main__":
    unittest.main()
