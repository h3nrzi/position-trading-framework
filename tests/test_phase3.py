"""Phase 3 routing acceptance cases; all writes use disposable state files."""

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "phase3.py"
SPEC = importlib.util.spec_from_file_location("phase3", SCRIPT)
phase3 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(phase3)
METRICS = {"current_price": 100, "support_1": 90, "support_2": 80, "extreme_discount": 60}
INVALIDATION = {"price": 57.6, "condition": "WEEKLY_CLOSE_BELOW"}


def fixture(count=4, reserve=True):
    state = json.loads((ROOT / "state.factory.json").read_bytes())
    state.update(current_phase=phase3.PHASE, current_step=phase3.INPUT, active_market="crypto_spot")
    state["portfolio"]["total_budget"] = 1000
    assets = [("BTC", "core", 300), ("ETH", "core", 200),
              ("SOL", "growth", 100), ("LINK", "growth", 100)][:count]
    if reserve:
        assets += [("USDT", "reserve", 150), ("USDC", "reserve", 150)]
    state["portfolio"]["allocations"] = [
        {"symbol": symbol, "layer": layer, "amount": amount, "weight_pct": amount / 10}
        for symbol, layer, amount in assets]
    state["macro_score"] = {"total_score": -35, "verdict": "DEFENSIVE_CASH",
                            "last_updated": "2026-10-18"}
    return state


def through_orders(state, symbol):
    state = phase3.transition(state, "metrics", symbol=symbol, payload=METRICS)
    return phase3.transition(state, "approve-orders", symbol=symbol, approved=True)


def complete(state, symbol):
    state = through_orders(state, symbol)
    state = phase3.transition(state, "propose-invalidation", symbol=symbol, payload=INVALIDATION)
    return phase3.transition(state, "approve-invalidation", symbol=symbol, approved=True)


def legacy_populated():
    state = fixture()
    state.update(current_phase=phase3.PHASE4, current_step="WEEKLY_DEEP_SEARCH")
    state["positions"] = [
        {"symbol": a["symbol"], "layer": a["layer"], "allocated_budget": a["amount"],
         "tranches": [{"tranche": n, "pct": pct, "price": price,
                       "amount": a["amount"] * pct / 100, "status": "PLANNED_BLOCKED_MACRO"}
                      for n, pct, price in ((1, 30, 90), (2, 40, 80), (3, 30, 60))],
         "average_entry": None, "invalidation_price": 57.6,
         "invalidation_condition": "WEEKLY_CLOSE_BELOW", "status": "PLANNED_BLOCKED_MACRO"}
        for a in state["portfolio"]["allocations"] if a["layer"] != "reserve"]
    state["weekly_logs"] = [{"date": "2026-10-18", "verdict": "HOLD", "status": "FINALIZED"}]
    return state


class Phase3Tests(unittest.TestCase):
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

    def test_one_asset_enters_phase4_only_after_both_approvals(self):
        state = fixture(1, False)
        state = phase3.transition(state, "metrics", symbol="BTC", payload=METRICS)
        self.assertEqual(state["current_step"], phase3.ORDERS)
        self.assertEqual(state["current_phase"], phase3.PHASE)
        state = phase3.transition(state, "approve-orders", symbol="BTC", approved=True)
        self.assertEqual(state["current_step"], phase3.INVALIDATION)
        state = phase3.transition(state, "propose-invalidation", symbol="BTC", payload=INVALIDATION)
        self.assertEqual(state["current_phase"], phase3.PHASE)
        self.assertNotIn("invalidation_price", state["positions"][0])
        state = phase3.transition(state, "approve-invalidation", symbol="BTC", approved=True)
        self.assertEqual((state["current_phase"], state["current_step"]),
                         (phase3.PHASE4, "WEEKLY_DEEP_SEARCH"))
        self.assertEqual(phase3.route(state)["remaining"], [])
        self.assertEqual(state["positions"][0]["phase3"]["step"], phase3.COMPLETE)

    def test_four_assets_once_preserving_completed_results_and_reserves(self):
        state = fixture()
        protected = copy.deepcopy({k: state[k] for k in ("portfolio", "macro_score", "weekly_logs")})
        completed = []
        for index, symbol in enumerate(("BTC", "ETH", "SOL", "LINK")):
            self.assertEqual(phase3.route(state)["symbol"], symbol)
            state = complete(state, symbol)
            self.assertEqual(state["positions"][:index], completed)
            completed = copy.deepcopy(state["positions"])
            self.assertEqual(len(state["positions"]), index + 1)
            self.assertEqual(state["current_phase"], phase3.PHASE4 if index == 3 else phase3.PHASE)
            if index < 3:
                self.assertEqual(state["current_step"], phase3.INPUT)
        self.assertEqual([p["symbol"] for p in state["positions"]], ["BTC", "ETH", "SOL", "LINK"])
        self.assertEqual({k: state[k] for k in protected}, protected)

    def test_reserve_only_skips_technical_planning(self):
        state = phase3.transition(fixture(0))
        self.assertEqual(state["current_phase"], phase3.PHASE4)
        self.assertEqual(state["positions"], [])
        with self.assertRaises(ValueError):
            phase3.transition(fixture(), "metrics", symbol="USDT", payload=METRICS)

    def test_replayed_approvals_do_not_duplicate_or_approve_next_decision(self):
        state = through_orders(fixture(), "BTC")
        self.assertEqual(phase3.transition(state, "approve-orders", symbol="BTC", approved=True), state)
        with self.assertRaisesRegex(ValueError, "proposal"):
            phase3.transition(state, "approve-invalidation", symbol="BTC", approved=True)
        state = phase3.transition(state, "propose-invalidation", symbol="BTC", payload=INVALIDATION)
        state = phase3.transition(state, "approve-invalidation", symbol="BTC", approved=True)
        self.assertEqual(phase3.transition(state, "approve-invalidation", symbol="BTC", approved=True), state)
        self.assertEqual(phase3.route(state)["symbol"], "ETH")
        with self.assertRaisesRegex(ValueError, "persisted"):
            phase3.transition(state, "approve-orders", symbol="ETH", approved=True)
        with self.assertRaises(ValueError):
            phase3.transition(state, "metrics", symbol="BTC", payload=METRICS)
        final = complete(fixture(1), "BTC")
        self.persist(final)
        original = self.path.read_bytes()
        phase3.update_file(self.path, "approve-invalidation", symbol="BTC", approved=True)
        self.assertEqual(self.path.read_bytes(), original)

    def test_restart_from_each_step_uses_only_persisted_state(self):
        state = fixture()
        for step, action, kwargs in (
            (phase3.INPUT, "metrics", {"payload": METRICS}),
            (phase3.ORDERS, "approve-orders", {"approved": True}),
            (phase3.INVALIDATION, "propose-invalidation", {"payload": INVALIDATION}),
            (phase3.INVALIDATION, "approve-invalidation", {"approved": True}),
        ):
            with self.subTest(step=step, action=action):
                self.persist(state)
                resumed = phase3.update_file(self.path)
                self.assertEqual(resumed["current_step"], step)
                # A separate process has no access to this test's conversation memory.
                result = subprocess.run([sys.executable, "-B", str(SCRIPT), "resume", "--state", str(self.path)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["symbol"], "BTC")
                state = phase3.update_file(self.path, action, symbol="BTC", **kwargs)
        self.assertEqual(phase3.route(state)["symbol"], "ETH")

    def test_per_asset_checkpoint_repairs_stale_global_step(self):
        state = through_orders(fixture(), "BTC")
        state["current_step"] = phase3.INPUT
        resumed = phase3.transition(state)
        self.assertEqual(resumed["current_step"], phase3.INVALIDATION)
        self.assertEqual(resumed["positions"], state["positions"])

    def test_next_existing_partial_asset_resumes_its_checkpoint(self):
        later = through_orders(fixture(), "BTC")["positions"][0]
        later.update(symbol="ETH", allocated_budget=200)
        state = fixture()
        state["positions"] = [later]
        state = complete(state, "BTC")
        self.assertEqual(phase3.route(state)["symbol"], "ETH")
        self.assertEqual(state["current_step"], phase3.INVALIDATION)
        self.assertEqual(state["positions"][0], later)

    def test_premature_phase4_and_implicit_approvals_rejected_without_writes(self):
        for state in (fixture(), phase3.transition(fixture(), "metrics", symbol="BTC", payload=METRICS),
                      through_orders(fixture(), "BTC")):
            self.persist(state)
            original = self.path.read_bytes()
            with self.assertRaisesRegex(ValueError, "Phase 4"):
                phase3.update_file(self.path, "finish")
            with self.assertRaisesRegex(ValueError, "Explicit approval"):
                phase3.update_file(self.path, "approve-orders", symbol="BTC")
            with self.assertRaises(ValueError):
                phase3.update_file(self.path, "approve-invalidation", symbol="ETH", approved=True)
            self.assertEqual(self.path.read_bytes(), original)

    def test_legacy_completed_and_partial_records_keep_confirmed_results(self):
        state = complete(fixture(), "BTC")
        del state["positions"][0]["phase3"]
        state["positions"][0]["status"] = "PLANNED_BLOCKED_MACRO"
        legacy = copy.deepcopy(state["positions"][0])
        state = complete(state, "ETH")
        self.assertEqual(state["positions"][0], legacy)
        self.assertEqual(phase3.route(state)["remaining"], ["SOL", "LINK"])
        draft = through_orders(fixture(), "BTC")
        del draft["positions"][0]["phase3"]
        draft["positions"][0]["status"] = "PLANNING"
        draft["current_step"] = phase3.INVALIDATION
        resumed = phase3.transition(draft)
        self.assertEqual(resumed["current_step"], phase3.ORDERS)
        self.assertEqual(resumed["positions"], draft["positions"])
        partial = fixture()
        partial["current_step"] = phase3.ORDERS
        partial["positions"] = [{"symbol": "BTC", "layer": "core", "status": "PLANNING"}]
        self.assertEqual(phase3.transition(partial)["current_step"], phase3.INPUT)

    def test_existing_populated_phase4_is_byte_preserved(self):
        # Fixed legacy fixture keeps this regression independent of future live trading data.
        state = legacy_populated()
        self.persist(state)
        original = self.path.read_bytes()
        phase3.update_file(self.path)
        self.assertEqual(self.path.read_bytes(), original)
        # A copy in Phase 3 demonstrates legacy completion detection, too.
        state.update(current_phase=phase3.PHASE, current_step=phase3.INPUT)
        positions = copy.deepcopy(state["positions"])
        resumed = phase3.transition(state)
        self.assertEqual(resumed["current_phase"], phase3.PHASE4)
        self.assertEqual(resumed["positions"], positions)

    def test_legacy_active_pending_record_also_counts_as_completed(self):
        state = legacy_populated()
        state.update(current_phase=phase3.PHASE, current_step=phase3.INPUT)
        for position in state["positions"]:
            position["status"] = "ACTIVE_PENDING_ORDERS"
        self.assertEqual(phase3.transition(state)["current_phase"], phase3.PHASE4)

    def test_final_cli_reports_phase4_step(self):
        state = through_orders(fixture(1), "BTC")
        state = phase3.transition(state, "propose-invalidation", symbol="BTC", payload=INVALIDATION)
        self.persist(state)
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "approve-invalidation",
                                 "--state", str(self.path), "--symbol", "BTC", "--approve"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"symbol": None, "remaining": [],
                         "phase": phase3.PHASE4, "step": "WEEKLY_DEEP_SEARCH"})

    def test_duplicate_or_ambiguous_legacy_data_requires_clarification(self):
        states = []
        duplicate_allocation = fixture()
        duplicate_allocation["portfolio"]["allocations"].append(
            copy.deepcopy(duplicate_allocation["portfolio"]["allocations"][0]))
        states.append(duplicate_allocation)
        duplicate_position = through_orders(fixture(), "BTC")
        duplicate_position["positions"] *= 2
        states.append(duplicate_position)
        ambiguous = complete(fixture(), "BTC")
        del ambiguous["positions"][0]["phase3"]
        ambiguous["positions"][0]["invalidation_price"] = None
        states.append(ambiguous)
        executed = through_orders(fixture(), "BTC")
        executed["positions"][0]["tranches"][0]["status"] = "FILLED"
        states.append(executed)
        for state in states:
            self.persist(state)
            original = self.path.read_bytes()
            with self.assertRaises(ValueError):
                phase3.update_file(self.path)
            self.assertEqual(self.path.read_bytes(), original)

    def test_macro_restrictions_and_existing_blocks_survive_confirmation(self):
        for score, verdict, blocked in ((-35, "DEFENSIVE_CASH", [True] * 3),
                                         (-20, "NEUTRAL_ACCUMULATION", [False, False, True]),
                                         (59, "NEUTRAL_ACCUMULATION", [False, False, True]),
                                         (60, "BULLISH_AGGRESSIVE", [False] * 3)):
            with self.subTest(score=score):
                state = fixture(1)
                state["macro_score"].update(total_score=score, verdict=verdict)
                state = complete(state, "BTC")
                position = state["positions"][0]
                self.assertEqual([t["status"] == "PLANNED_BLOCKED_MACRO" for t in position["tranches"]], blocked)
                self.assertEqual([t["pct"] for t in position["tranches"]], [30, 40, 30])
                self.assertEqual([t["amount"] for t in position["tranches"]], [90, 120, 90])
                self.assertAlmostEqual(position["potential_average_entry"], 300 / (90 / 90 + 120 / 80 + 90 / 60))
        state = through_orders(fixture(1), "BTC")
        state["macro_score"].update(total_score=80, verdict="BULLISH_AGGRESSIVE")
        state = phase3.transition(state, "propose-invalidation", symbol="BTC", payload=INVALIDATION)
        state = phase3.transition(state, "approve-invalidation", symbol="BTC", approved=True)
        self.assertTrue(all(t["status"] == "PLANNED_BLOCKED_MACRO" for t in state["positions"][0]["tranches"]))

    def test_invalid_inputs_and_missing_macro_never_publish_partial_state(self):
        cases = [({**METRICS, "support_1": 0}, fixture()),
                 ({**METRICS, "support_1": float("nan")}, fixture()), (None, fixture())]
        no_macro = fixture()
        no_macro["macro_score"]["verdict"] = None
        cases.append((METRICS, no_macro))
        for metrics, state in cases:
            self.persist(state)
            original = self.path.read_bytes()
            with self.assertRaises(ValueError):
                phase3.update_file(self.path, "metrics", symbol="BTC", payload=metrics)
            self.assertEqual(self.path.read_bytes(), original)

    def test_qualitative_invalidation_is_not_approved_by_proposal(self):
        state = through_orders(fixture(1), "BTC")
        proposal = {"price": None, "condition": "Confirmed change of macro thesis"}
        state = phase3.transition(state, "propose-invalidation", symbol="BTC", payload=proposal)
        self.assertEqual(state["current_step"], phase3.INVALIDATION)
        state = phase3.transition(state, "approve-invalidation", symbol="BTC", approved=True)
        self.assertIsNone(state["positions"][0]["invalidation_price"])
        self.assertEqual(state["positions"][0]["invalidation_condition"], proposal["condition"])

    def test_atomic_publish_failure_and_retry(self):
        for operation in ("fsync", "replace"):
            self.persist(fixture())
            original = self.path.read_bytes()
            with mock.patch.object(phase3.os, operation, side_effect=OSError("injected failure")):
                with self.assertRaises(OSError):
                    phase3.update_file(self.path, "metrics", symbol="BTC", payload=METRICS)
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(list(self.path.parent.glob(".*.phase3-*.tmp")), [])
            phase3.update_file(self.path, "metrics", symbol="BTC", payload=METRICS)
            resumed = phase3.update_file(self.path)
            self.assertEqual(resumed["current_step"], phase3.ORDERS)
            self.assertEqual(len(resumed["positions"]), 1)


if __name__ == "__main__":
    unittest.main()
