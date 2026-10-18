"""Batch 03 regressions. Live state and factory bytes are never modified."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts import order_status, phase3
from test_phase3 import fixture, complete, through_orders, METRICS

ROOT = Path(__file__).resolve().parents[1]


def evidence(status, quantity=0, cost=0):
    return {"status": status, "source": "USER_REPORTED", "confirmed": True,
            "evidence": f"User explicitly reports {status}, cumulative quantity {quantity}, cost {cost}",
            "reported_at": "2026-10-18", "filled_quantity": quantity, "filled_cost": cost}


def record(state, status, quantity=0, cost=0, tranche=1):
    return order_status.report(state, symbol="BTC", tranche=tranche,
                               payload=evidence(status, quantity, cost), approved=True)


class OrderStatusTests(unittest.TestCase):
    def setUp(self):
        self.live = (ROOT / "state.json").read_bytes()
        self.factory = (ROOT / "state.factory.json").read_bytes()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.json"

    def tearDown(self):
        self.assertEqual((ROOT / "state.json").read_bytes(), self.live)
        self.assertEqual((ROOT / "state.factory.json").read_bytes(), self.factory)

    def persist(self, state):
        self.path.write_text(json.dumps(state), encoding="utf-8")
        return self.path.read_bytes()

    def test_draft_plan_has_no_execution_and_only_potential_average(self):
        state = phase3.transition(fixture(1), "metrics", symbol="BTC", payload=METRICS)
        position = state["positions"][0]
        self.assertEqual(position["status"], "PLAN_DRAFT")
        self.assertNotIn("average_entry", position)
        self.assertGreater(position["potential_average_entry"], 0)
        self.assertTrue(all("execution" not in t for t in position["tranches"]))
        self.assertEqual(order_status.audit(state)[0]["plan_status"], "PLAN_DRAFT")
        self.assertIsNone(order_status.audit(state)[0]["invested_capital"])

    def test_confirming_table_still_needs_invalidation_and_submits_nothing(self):
        state = through_orders(fixture(1), "BTC")
        self.assertEqual(state["positions"][0]["status"], "PLAN_DRAFT")
        self.assertEqual(state["positions"][0]["phase3"]["step"], phase3.INVALIDATION)
        self.assertTrue(all(t["execution_status"] == "UNKNOWN"
                            for t in order_status.audit(state)[0]["tranches"]))

    def test_confirmation_and_phase4_preserve_plan_with_zero_recorded_orders(self):
        state = fixture(1)
        state["macro_score"].update(total_score=80, verdict="BULLISH_AGGRESSIVE")
        state = complete(state, "BTC")
        self.assertEqual(state["current_phase"], phase3.PHASE4)
        self.assertEqual(state["positions"][0]["status"], "PLAN_CONFIRMED")
        self.assertEqual([t["status"] for t in state["positions"][0]["tranches"]], ["PLANNED"] * 3)
        audit = order_status.audit(state)[0]
        self.assertFalse(audit["execution_totals_known"])
        self.assertIsNone(audit["holding_quantity"])
        self.assertFalse(any(t["can_review_entry"] for t in audit["tranches"]))
        self.assertFalse(any(t["execution_status"] == "SUBMITTED" for t in audit["tranches"]))

    def test_defensive_confirmation_and_actual_report_never_lift_macro_blocks(self):
        state = complete(fixture(1), "BTC")
        plan = copy.deepcopy(state["positions"][0])
        state = record(state, "SUBMITTED")
        self.assertEqual(state["positions"][0]["status"], "PLAN_CONFIRMED")
        row = order_status.audit(state)[0]["tranches"][0]
        self.assertEqual(row["execution_status"], "SUBMITTED")
        self.assertTrue(row["blocked_macro"])
        self.assertFalse(row["can_review_entry"])
        del state["positions"][0]["tranches"][0]["execution"]
        self.assertEqual(state["positions"][0], plan)

    def test_submitted_report_audited_without_investing_planned_amount(self):
        state = complete(fixture(1), "BTC")
        for n in (1, 2, 3):
            state = record(state, "NOT_SUBMITTED", tranche=n)
        state = record(state, "SUBMITTED")
        audit = order_status.audit(state, {"BTC": 100})[0]
        self.assertEqual(audit["holding_quantity"], 0)
        self.assertEqual(audit["invested_capital"], 0)
        self.assertIsNone(audit["actual_average_entry"])
        self.assertIsNone(audit["unrealized_pnl"])

    def test_fill_uses_reported_execution_price_and_cost_not_plan(self):
        state = complete(fixture(1), "BTC")
        plan = copy.deepcopy(state["positions"][0])
        for n in (1, 2, 3):
            state = record(state, "NOT_SUBMITTED", tranche=n)
        state = record(state, "FILLED", 2, 170)
        audit = order_status.audit(state, {"BTC": 100})[0]
        self.assertEqual(audit["holding_quantity"], 2)
        self.assertEqual(audit["invested_capital"], 170)
        self.assertEqual(audit["actual_average_entry"], 85)
        self.assertEqual(audit["unrealized_pnl"], 30)
        self.assertEqual(state["positions"][0]["potential_average_entry"], plan["potential_average_entry"])
        self.assertEqual(state["positions"][0]["invalidation_price"], plan["invalidation_price"])
        self.assertEqual(state["positions"][0]["tranches"][0]["price"], 90)

    def test_price_touch_does_not_fill_or_modify_state(self):
        state = complete(fixture(1), "BTC")
        before = copy.deepcopy(state)
        audit = order_status.audit(state, {"BTC": 60})[0]
        self.assertEqual(state, before)
        self.assertTrue(all(t["execution_status"] == "UNKNOWN" for t in audit["tranches"]))
        self.persist(state)
        original = self.path.read_bytes()
        with self.assertRaises(ValueError):
            order_status.update_file(self.path, symbol="BTC", tranche=1,
                                     payload={"status": "FILLED", "price_touch": 60}, approved=True)
        self.assertEqual(self.path.read_bytes(), original)

    def test_unknown_preserves_uncertainty_and_previously_reported_partial_fills(self):
        state = record(complete(fixture(1), "BTC"), "SUBMITTED", 0.5, 40)
        with self.assertRaisesRegex(ValueError, "erase"):
            record(state, "UNKNOWN")
        state = record(state, "UNKNOWN", 0.5, 40)
        audit = order_status.audit(state, {"BTC": 100})[0]
        self.assertIsNone(audit["holding_quantity"])
        self.assertIsNone(audit["unrealized_pnl"])
        self.assertEqual(audit["reported_filled_quantity"], 0.5)
        self.assertEqual(audit["reported_invested_capital"], 40)
        self.assertFalse(audit["tranches"][0]["can_review_entry"])

    def test_evidence_confirmation_and_precise_target_required(self):
        state = complete(fixture(1), "BTC")
        cases = [("BTC", 1, evidence("FILLED", 1, 80), False),
                 ("ETH", 1, evidence("FILLED", 1, 80), True),
                 ("BTC", 4, evidence("FILLED", 1, 80), True)]
        for field, value in (("source", "PRICE_TOUCH"), ("evidence", ""),
                             ("reported_at", ""), ("confirmed", False),
                             ("filled_cost", None), ("filled_quantity", True)):
            payload = evidence("FILLED", 1, 80)
            payload[field] = value
            cases.append(("BTC", 1, payload, True))
        for symbol, tranche, payload, approve in cases:
            with self.subTest(payload=payload, symbol=symbol, tranche=tranche):
                before = self.persist(state)
                with self.assertRaises(ValueError):
                    order_status.update_file(self.path, symbol=symbol, tranche=tranche,
                                             payload=payload, approved=approve)
                self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_combinations_and_unknown_names_fail_safely(self):
        baseline = complete(fixture(1), "BTC")
        cases = []
        for edit in (lambda p: p.update(status="ACTIVE"),
                     lambda p: p.update(status="PLAN_DRAFT"),
                     lambda p: p["phase3"].update(step=phase3.ORDERS),
                     lambda p: p["tranches"][0].update(status="FILLED"),
                     lambda p: p["tranches"][0].update(execution=evidence("PENDING")),
                     lambda p: p["tranches"][0].update(execution=evidence("REJECTED", 1, 80)),
                     lambda p: p["tranches"][0].update(execution=evidence("FILLED")),
                     lambda p: p["tranches"][1].update(tranche=1)):
            state = copy.deepcopy(baseline)
            edit(state["positions"][0])
            cases.append(state)
        for state in cases:
            before = self.persist(state)
            with self.assertRaises(ValueError):
                order_status.audit(state)
            with self.assertRaises(ValueError):
                order_status.update_file(self.path, symbol="BTC", tranche=1,
                                         payload=evidence("SUBMITTED"), approved=True)
            self.assertEqual(self.path.read_bytes(), before)

    def test_execution_is_never_erased_by_phase3_replanning(self):
        state = phase3.transition(fixture(1), "metrics", symbol="BTC", payload=METRICS)
        state["positions"][0]["tranches"][0]["execution"] = evidence("SUBMITTED")
        with self.assertRaisesRegex(ValueError, "execution record"):
            phase3.transition(state)

    def test_cancelled_rejected_and_terminal_transitions(self):
        baseline = complete(fixture(1), "BTC")
        state = record(record(baseline, "SUBMITTED", 1, 80), "CANCELLED", 1, 80)
        self.assertEqual(order_status.audit(state)[0]["reported_invested_capital"], 80)
        for status in ("FILLED", "CANCELLED", "REJECTED"):
            quantity, cost = (1, 80) if status == "FILLED" else (0, 0)
            terminal = record(baseline, status, quantity, cost)
            self.assertEqual(record(terminal, status, quantity, cost), terminal)
            with self.assertRaisesRegex(ValueError, "Terminal"):
                record(terminal, "SUBMITTED", quantity, cost)
        with self.assertRaisesRegex(ValueError, "cancellation"):
            record(record(baseline, "SUBMITTED"), "NOT_SUBMITTED")
        uncertain = record(record(baseline, "SUBMITTED"), "UNKNOWN")
        self.assertEqual(uncertain["positions"][0]["tranches"][0]["execution"]["last_known_status"],
                         "SUBMITTED")
        with self.assertRaisesRegex(ValueError, "cancellation"):
            record(uncertain, "NOT_SUBMITTED")

    def test_macro_gate_uses_current_regime_and_preserves_persisted_blocks(self):
        state = fixture(1)
        state["macro_score"].update(total_score=80, verdict="BULLISH_AGGRESSIVE")
        state = complete(state, "BTC")
        for n in (1, 2, 3):
            state = record(state, "NOT_SUBMITTED", tranche=n)
        self.assertTrue(all(t["can_review_entry"] for t in order_status.audit(state)[0]["tranches"]))
        state["macro_score"].update(total_score=0, verdict="NEUTRAL_ACCUMULATION")
        self.assertEqual([t["can_review_entry"] for t in order_status.audit(state)[0]["tranches"]],
                         [True, True, False])
        state["macro_score"].update(total_score=-35, verdict="DEFENSIVE_CASH")
        self.assertFalse(any(t["can_review_entry"] for t in order_status.audit(state)[0]["tranches"]))
        blocked = complete(fixture(1), "BTC")
        blocked["macro_score"].update(total_score=80, verdict="BULLISH_AGGRESSIVE")
        blocked = record(blocked, "NOT_SUBMITTED")
        self.assertTrue(order_status.audit(blocked)[0]["tranches"][0]["blocked_macro"])

    def test_existing_simulation_is_read_only_and_legacy_fill_is_not_execution(self):
        state = json.loads(self.live)
        before = copy.deepcopy(state)
        results = order_status.audit(state)
        self.assertEqual(state, before)
        self.assertEqual(len(results), 4)
        self.assertTrue(all(r["legacy_record"] for r in results))
        self.assertTrue(all(r["plan_status"] == "PLAN_CONFIRMED" for r in results))
        self.assertTrue(all(r["invested_capital"] is None for r in results))
        state["positions"][0]["tranches"][0]["status"] = "FILLED"
        state["positions"][0]["status"] = "ACTIVE_PENDING_ORDERS"
        audit = order_status.audit(state)[0]
        self.assertEqual(audit["tranches"][0]["execution_status"], "UNKNOWN")
        self.assertIsNone(audit["actual_average_entry"])
        self.assertGreater(audit["potential_average_entry"], 0)
        self.assertEqual(state["weekly_logs"], before["weekly_logs"])

    def test_reporting_legacy_execution_preserves_every_plan_and_simulation_field(self):
        legacy = json.loads(self.live)
        updated = record(legacy, "FILLED", 0.001, 80)
        report = updated["positions"][0]["tranches"][0].pop("execution")
        self.assertEqual(updated, legacy)
        self.assertEqual(report["filled_cost"], 80)

    def test_execution_transition_matrix_and_phase_boundary(self):
        baseline = complete(fixture(1), "BTC")
        statuses = ("UNKNOWN", "NOT_SUBMITTED", "SUBMITTED", "FILLED", "CANCELLED", "REJECTED")
        for old in statuses:
            previous = record(baseline, old, 1 if old == "FILLED" else 0,
                              80 if old == "FILLED" else 0)
            for new in statuses:
                allowed = ((old in {"UNKNOWN", "NOT_SUBMITTED"})
                           or (old == "SUBMITTED" and new != "NOT_SUBMITTED")
                           or (old in {"FILLED", "CANCELLED", "REJECTED"} and old == new))
                with self.subTest(old=old, new=new):
                    if allowed:
                        updated = record(previous, new, 1 if new == "FILLED" else 0,
                                         80 if new == "FILLED" else 0)
                        self.assertEqual(updated["positions"][0]["tranches"][0]["execution"]["status"], new)
                    else:
                        with self.assertRaises(ValueError):
                            record(previous, new, 1 if new == "FILLED" else 0,
                                   80 if new == "FILLED" else 0)
        draft = phase3.transition(fixture(1), "metrics", symbol="BTC", payload=METRICS)
        with self.assertRaisesRegex(ValueError, "Phase 4"):
            record(draft, "SUBMITTED")

    def test_atomic_failure_cli_audit_and_record_on_disposable_copy(self):
        state = complete(fixture(1), "BTC")
        original = self.persist(state)
        with mock.patch.object(phase3.os, "replace", side_effect=OSError("injected failure")):
            with self.assertRaises(OSError):
                order_status.update_file(self.path, symbol="BTC", tranche=1,
                                         payload=evidence("SUBMITTED"), approved=True)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(list(self.path.parent.glob(".*.phase3-*.tmp")))
        payload = self.path.parent / "report.json"
        payload.write_text(json.dumps(evidence("SUBMITTED")), encoding="utf-8")
        command = [sys.executable, "-B", str(ROOT / "scripts/order_status.py")]
        result = subprocess.run(command + ["report", "--state", str(self.path), "--symbol", "BTC",
                                          "--tranche", "1", "--payload", str(payload), "--approve"],
                                cwd=self.directory.name, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)[0]["tranches"][0]["execution_status"], "SUBMITTED")
        original = self.path.read_bytes()
        result = subprocess.run(command + ["audit", "--state", str(self.path)],
                                cwd=self.directory.name, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
