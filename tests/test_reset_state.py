"""Factory-reset regressions. Every reset destination is a disposable fixture."""

import copy
import importlib.util
import json
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "reset_state.py"
FACTORY = ROOT / "state.factory.json"
SPEC = importlib.util.spec_from_file_location("reset_state", SCRIPT)
reset_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reset_module)

EXPECTED_FACTORY = {
    "version": "1.0.0",
    "current_phase": "PHASE_1_ONBOARDING",
    "current_step": "SELECT_MARKET",
    "active_market": None,
    "portfolio": {
        "total_budget": 0,
        "currency": "USD",
        "risk_profile": "balanced",
        "allocations": [],
        "entry_tranches": {
            "tranche_1_pct": 30,
            "tranche_2_pct": 40,
            "tranche_3_pct": 30,
        },
    },
    "macro_score": {"last_updated": None, "total_score": 0, "verdict": None},
    "positions": [],
    "weekly_logs": [],
}


def populated_fixture():
    """A fixed populated fixture, independent of the user's current runtime data."""
    state = copy.deepcopy(EXPECTED_FACTORY)
    state["active_market"] = "crypto_spot"
    state["portfolio"]["total_budget"] = 1000
    assets = (
        ("BTC", "core", 30), ("ETH", "core", 20),
        ("SOL", "growth", 10), ("LINK", "growth", 10),
        ("USDT", "reserve", 15), ("USDC", "reserve", 15),
    )
    state["portfolio"]["allocations"] = [
        {"symbol": symbol, "layer": layer, "weight_pct": weight, "amount": weight * 10}
        for symbol, layer, weight in assets
    ]
    state["macro_score"] = {
        "last_updated": "2026-10-18", "total_score": -35, "verdict": "DEFENSIVE_CASH"
    }
    state["positions"] = [
        {
            "symbol": symbol, "layer": layer, "allocated_budget": weight * 10,
            "tranches": [
                {"tranche": index, "pct": pct, "price": price,
                 "amount": weight * 10 * pct / 100, "status": "PLANNED_BLOCKED_MACRO"}
                for index, (pct, price) in enumerate(((30, 100), (40, 80), (30, 60)), 1)
            ],
            "average_entry": None, "invalidation_price": 57.6,
            "invalidation_condition": "WEEKLY_CLOSE_BELOW",
            "status": "PLANNED_BLOCKED_MACRO",
        }
        for symbol, layer, weight in assets if layer != "reserve"
    ]
    state["weekly_logs"] = [
        {"date": "2026-10-18", "macro_notes": "Fixture baseline",
         "positions_snapshot": [], "verdict": "HOLD"}
    ]
    return state


class ResetStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.destination = Path(self.directory.name) / "state.json"
        self.original = (ROOT / "state.json").read_bytes()
        self.destination.write_bytes(self.original)

    def tearDown(self):
        # This is an assertion only; tests never restore or write the real state.
        self.assertEqual((ROOT / "state.json").read_bytes(), self.original)

    def assert_factory(self):
        self.assertEqual(self.destination.read_bytes(), FACTORY.read_bytes())
        self.assertEqual(json.loads(self.destination.read_bytes()), EXPECTED_FACTORY)

    def assert_original(self):
        self.assertEqual(self.destination.read_bytes(), self.original)
        json.loads(self.destination.read_bytes())
        self.assertEqual(list(self.destination.parent.glob(".*.reset-*.tmp")), [])

    def test_canonical_defaults_match_schema_1_0(self):
        self.assertEqual(json.loads(FACTORY.read_bytes()), EXPECTED_FACTORY)

    def test_populated_phases_restore_exact_factory_state(self):
        # Populate fixtures at the final input step of each phase, with only the
        # runtime sections that can already exist at that point in the workflow.
        source = populated_fixture()
        for phase, step in (
            ("PHASE_1_ONBOARDING", "CALCULATE_TRANCHES"),
            ("PHASE_2_MACRO_DEEP_SEARCH", "SCORE_MACRO"),
            ("PHASE_3_TECHNICAL_LEVELS", "SET_INVALIDATION"),
            ("PHASE_4_WEEKLY_AUTOPILOT", "WEEKLY_DEEP_SEARCH"),
        ):
            with self.subTest(phase=phase):
                fixture = copy.deepcopy(source)
                fixture.update(current_phase=phase, current_step=step)
                if phase in ("PHASE_1_ONBOARDING", "PHASE_2_MACRO_DEEP_SEARCH"):
                    fixture["macro_score"] = copy.deepcopy(EXPECTED_FACTORY["macro_score"])
                    fixture["positions"] = []
                if phase != "PHASE_4_WEEKLY_AUTOPILOT":
                    fixture["weekly_logs"] = []
                if phase == "PHASE_3_TECHNICAL_LEVELS":
                    fixture["positions"] = fixture["positions"][:1]
                    fixture["positions"][0].update(invalidation_price=None, status="PLANNING")
                self.destination.write_text(json.dumps(fixture), encoding="utf-8")
                reset_module.reset_state(self.destination)
                self.assert_factory()

    def test_unknown_fields_are_removed_at_every_depth_and_reset_is_idempotent(self):
        fixture = populated_fixture()
        fixture["temporary_analysis"] = {"discard": True}
        fixture["portfolio"]["draft"] = ["discard"]
        fixture["portfolio"]["entry_tranches"]["override"] = 99
        fixture["macro_score"]["explanation"] = "discard"
        self.destination.write_text(json.dumps(fixture), encoding="utf-8")
        reset_module.reset_state(self.destination)
        self.assert_factory()
        first_reset = self.destination.read_bytes()
        reset_module.reset_state(self.destination)
        self.assertEqual(self.destination.read_bytes(), first_reset)

    def test_cli_works_from_another_directory_and_preserves_permissions(self):
        self.destination.chmod(0o640)
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--state", str(self.destination)],
            cwd=self.directory.name, capture_output=True, text=True, check=True,
        )
        self.assertIn("Factory state restored:", result.stdout)
        self.assert_factory()
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o640)

    def test_cli_requires_an_explicit_destination(self):
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT)], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--state", result.stderr)
        self.assert_original()

    def test_factory_snapshot_cannot_be_the_destination(self):
        factory = self.destination.parent / "state.factory.json"
        before = FACTORY.read_bytes()
        factory.write_bytes(before)
        with mock.patch.object(reset_module, "FACTORY_PATH", factory):
            with self.assertRaisesRegex(ValueError, "factory snapshot"):
                reset_module.reset_state(factory)
        self.assertEqual(factory.read_bytes(), before)

    def test_missing_destination_is_not_created(self):
        missing = self.destination.parent / "missing.json"
        with self.assertRaises(FileNotFoundError):
            reset_module.reset_state(missing)
        self.assertFalse(missing.exists())
        self.assert_original()

    def test_invalid_or_missing_factory_leaves_destination_unchanged(self):
        factory = self.destination.parent / "invalid-factory.json"
        for contents in (b'{"incomplete":', b"null"):
            with self.subTest(contents=contents):
                factory.write_bytes(contents)
                with mock.patch.object(reset_module, "FACTORY_PATH", factory):
                    with self.assertRaises(ValueError):
                        reset_module.reset_state(self.destination)
                self.assert_original()
        factory.unlink()
        with mock.patch.object(reset_module, "FACTORY_PATH", factory):
            with self.assertRaises(FileNotFoundError):
                reset_module.reset_state(self.destination)
        self.assert_original()

    def test_staging_sync_and_replace_failures_preserve_the_original(self):
        for operation in ("fchmod", "fsync", "replace"):
            with self.subTest(operation=operation):
                with mock.patch.object(
                    reset_module.os, operation, side_effect=OSError("injected failure")
                ):
                    with self.assertRaises(OSError):
                        reset_module.reset_state(self.destination)
                self.assert_original()

    def test_partial_staging_write_failure_preserves_the_original(self):
        create_staged = reset_module.tempfile.NamedTemporaryFile

        def failing_staged_file(**kwargs):
            staged = create_staged(**kwargs)
            write = staged.write

            def partial_write(data):
                write(data[:12])
                staged.flush()
                raise OSError("injected partial staging write")

            staged.write = partial_write
            return staged

        with mock.patch.object(
            reset_module.tempfile, "NamedTemporaryFile", side_effect=failing_staged_file
        ):
            with self.assertRaises(OSError):
                reset_module.reset_state(self.destination)
        self.assert_original()

    def test_keyboard_interrupt_before_replacement_preserves_the_original(self):
        with mock.patch.object(reset_module.os, "replace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                reset_module.reset_state(self.destination)
        self.assert_original()

    def test_process_killed_before_or_after_replacement_leaves_complete_json(self):
        # Pause at the actual commit boundary, then kill the subprocess. A forced
        # kill can strand a staging file; TemporaryDirectory owns its cleanup.
        child = '''
import importlib.util, sys, threading
from pathlib import Path
spec = importlib.util.spec_from_file_location("reset_state", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
replace = module.os.replace
def paused_replace(source, destination):
    if sys.argv[3] == "after":
        replace(source, destination)
    print("ready", flush=True)
    threading.Event().wait()
module.os.replace = paused_replace
module.reset_state(Path(sys.argv[2]))
'''
        for boundary in ("before", "after"):
            with self.subTest(boundary=boundary):
                self.destination.write_bytes(self.original)
                process = subprocess.Popen(
                    [sys.executable, "-B", "-c", child, str(SCRIPT),
                     str(self.destination), boundary],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        self.assertTrue(selector.select(timeout=10), "child did not stage reset")
                        self.assertEqual(process.stdout.readline().strip(), "ready")
                finally:
                    process.kill()
                    process.communicate(timeout=10)
                expected = self.original if boundary == "before" else FACTORY.read_bytes()
                self.assertEqual(self.destination.read_bytes(), expected)
                json.loads(self.destination.read_bytes())


if __name__ == "__main__":
    unittest.main()
