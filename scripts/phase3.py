"""Persist the three Phase 3 steps, one allocated Core/Growth symbol at a time.

Single-writer local helper; it plans entries and never places exchange orders.
"""

import argparse
import copy
import json
import math
import os
from pathlib import Path
import stat
import tempfile


PHASE = "PHASE_3_TECHNICAL_LEVELS"
PHASE4 = "PHASE_4_WEEKLY_AUTOPILOT"
INPUT = "INPUT_CHART_METRICS"
ORDERS = "CALCULATE_LIMIT_ORDERS"
INVALIDATION = "SET_INVALIDATION"
COMPLETE = "COMPLETE"
STEPS = (INPUT, ORDERS, INVALIDATION, COMPLETE)
LEGACY_FINAL = {"ACTIVE_PENDING_ORDERS", "PLANNED_BLOCKED_MACRO"}


def positive(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def has_plan(position):
    tranches = position.get("tranches", [])
    return (len(tranches) == 3
            and [t.get("tranche") for t in tranches] == [1, 2, 3]
            and all(positive(t.get("price")) and positive(t.get("amount"))
                    and positive(t.get("pct")) for t in tranches))


def has_invalidation(position):
    price = position.get("invalidation_price")
    condition = position.get("invalidation_condition")
    return (positive(price) or (price is None and isinstance(condition, str)
            and bool(condition.strip()) and condition != "WEEKLY_CLOSE_BELOW"))


def progress(position):
    """New checkpoints are authoritative; legacy final records remain untouched."""
    if "phase3" in position:
        step = position["phase3"]["step"]
        if step not in STEPS:
            raise ValueError("Unknown Phase 3 checkpoint.")
        if step in (INVALIDATION, COMPLETE) and not has_plan(position):
            raise ValueError("Checkpoint lacks its approved order plan.")
        if step == COMPLETE and not has_invalidation(position):
            raise ValueError("Completed checkpoint lacks invalidation.")
        if step != COMPLETE and any(t.get("status") not in
                {"PENDING", "PLANNED_BLOCKED_MACRO"} for t in position.get("tranches", [])):
            raise ValueError("Existing execution record cannot be rewritten in Phase 3.")
        return step
    if position.get("status") in LEGACY_FINAL:
        if has_plan(position) and has_invalidation(position):
            return COMPLETE
        raise ValueError("Ambiguous legacy final record; clarify its approvals first.")
    if position.get("status", "PLANNING") != "PLANNING":
        raise ValueError("Existing execution record cannot be rewritten in Phase 3.")
    if any(t.get("status") not in {"PENDING", "PLANNED_BLOCKED_MACRO"}
           for t in position.get("tranches", [])):
        raise ValueError("Existing execution record cannot be rewritten in Phase 3.")
    return ORDERS if has_plan(position) else INPUT


def assets_and_positions(state):
    allocations = state["portfolio"]["allocations"]
    symbols = [a["symbol"] for a in allocations]
    if len(symbols) != len(set(symbols)):
        raise ValueError("Duplicate allocation symbols require clarification.")
    positions = state["positions"]
    keys = [p["symbol"] for p in positions]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate position symbols require clarification.")
    eligible = [a for a in allocations if a["layer"] in ("core", "growth")]
    if any(not positive(a["amount"]) for a in eligible):
        raise ValueError("Core/Growth allocations must have positive budgets.")
    by_symbol = {p["symbol"]: p for p in positions}
    for asset in eligible:
        position = by_symbol.get(asset["symbol"])
        if position and position.get("layer") != asset["layer"]:
            raise ValueError("Position layer conflicts with its allocation.")
    return eligible, by_symbol


def route(state):
    eligible, positions = assets_and_positions(state)
    remaining = [a["symbol"] for a in eligible
                 if a["symbol"] not in positions or progress(positions[a["symbol"]]) != COMPLETE]
    symbol = remaining[0] if remaining else None
    step = progress(positions[symbol]) if symbol in positions else INPUT
    return {"symbol": symbol, "step": step if symbol else None, "remaining": remaining}


def execution_statuses(state):
    """Apply the existing macro permission limits without changing the score."""
    macro = state["macro_score"]
    score = macro.get("total_score")
    if (not isinstance(score, (int, float)) or isinstance(score, bool)
            or not math.isfinite(score) or macro.get("verdict") not in
            {"DEFENSIVE_CASH", "NEUTRAL_ACCUMULATION", "BULLISH_AGGRESSIVE"}):
        raise ValueError("A confirmed macro assessment is required.")
    if score < -20 or macro["verdict"] == "DEFENSIVE_CASH":
        return ["PLANNED_BLOCKED_MACRO"] * 3
    if score < 60 or macro["verdict"] == "NEUTRAL_ACCUMULATION":
        return ["PENDING", "PENDING", "PLANNED_BLOCKED_MACRO"]
    return ["PENDING"] * 3


def sync_route(state):
    current = route(state)
    if current["symbol"] is None:
        state.update(current_phase=PHASE4, current_step="WEEKLY_DEEP_SEARCH")
    else:
        state["current_step"] = current["step"]


def transition(source, action="resume", *, symbol=None, payload=None, approved=False):
    """Return a new state, or reject without modifying the caller's state.

    Approvals bind to both an action and a symbol; a stale yes can never approve
    the next symbol or the next decision. Replayed approvals are exact no-ops.
    """
    state = copy.deepcopy(source)
    if state["current_phase"] != PHASE:
        # Resume of an existing populated Phase 4 must not backfill or reroute it.
        if action == "resume":
            return state
        # A repeated final confirmation after the automatic Phase 4 transition.
        if state["current_phase"] == PHASE4 and action in ("approve-orders", "approve-invalidation"):
            _, positions = assets_and_positions(state)
            if approved is True and symbol in positions and progress(positions[symbol]) == COMPLETE:
                return state
        raise ValueError("Phase 3 is not active.")

    current = route(state)
    if action == "resume":
        sync_route(state)
        return state
    if action == "finish":
        if current["remaining"]:
            raise ValueError("Phase 4 is blocked until every eligible asset is complete.")
        sync_route(state)
        return state
    if action not in ("metrics", "approve-orders", "propose-invalidation", "approve-invalidation"):
        raise ValueError("Unknown Phase 3 action.")
    eligible, positions = assets_and_positions(state)
    position = positions.get(symbol)
    if action.startswith("approve-"):
        if approved is not True:
            raise ValueError("Explicit approval of the displayed decision is required.")
        checkpoint = progress(position) if position else INPUT
        if checkpoint == COMPLETE or (action == "approve-orders" and checkpoint == INVALIDATION):
            return state
    if symbol != current["symbol"] or symbol is None:
        raise ValueError("Action must name the current incomplete eligible symbol.")
    required = {"metrics": INPUT, "approve-orders": ORDERS,
                "propose-invalidation": INVALIDATION, "approve-invalidation": INVALIDATION}[action]
    if current["step"] != required:
        raise ValueError("Action does not match the persisted per-asset step.")
    asset = next(a for a in eligible if a["symbol"] == symbol)
    if position is None:
        position = {"symbol": symbol, "layer": asset["layer"],
                    "allocated_budget": asset["amount"], "status": "PLANNING"}
        state["positions"].append(position)
    checkpoint = position.setdefault("phase3", {"step": current["step"]})

    if action == "metrics":
        names = ("current_price", "support_1", "support_2", "extreme_discount")
        if not isinstance(payload, dict) or any(not positive(payload.get(n)) for n in names):
            raise ValueError("Four positive chart metrics are required.")
        checkpoint["chart_metrics"] = {n: payload[n] for n in names}
        percentages = [state["portfolio"]["entry_tranches"][f"tranche_{n}_pct"] for n in (1, 2, 3)]
        if not all(positive(p) for p in percentages) or not math.isclose(sum(percentages), 100):
            raise ValueError("Persisted tranche percentages must total 100.")
        statuses = execution_statuses(state)
        position["current_price_reference"] = payload["current_price"]
        position["tranches"] = [
            {"tranche": n, "pct": pct, "price": payload[key],
             "amount": asset["amount"] * pct / 100, "status": status}
            for n, pct, key, status in zip((1, 2, 3), percentages, names[1:], statuses)
        ]
        position["average_entry"] = asset["amount"] / sum(
            t["amount"] / t["price"] for t in position["tranches"])
        checkpoint["step"] = ORDERS
    elif action == "approve-orders":
        if not has_plan(position):
            raise ValueError("An order plan must be displayed before approval.")
        checkpoint["step"] = INVALIDATION
    elif action == "propose-invalidation":
        if (not isinstance(payload, dict)
                or (payload.get("price") is not None and not positive(payload["price"]))
                or not isinstance(payload.get("condition"), str) or not payload["condition"].strip()):
            raise ValueError("Provide an invalidation condition and optional positive price.")
        checkpoint["invalidation_proposal"] = {"price": payload.get("price"),
                                                "condition": payload["condition"]}
    else:
        proposal = checkpoint.get("invalidation_proposal")
        if proposal is None:
            raise ValueError("Display and persist an invalidation proposal before approval.")
        position["invalidation_price"] = proposal["price"]
        position["invalidation_condition"] = proposal["condition"]
        statuses = execution_statuses(state)
        for tranche, status in zip(position["tranches"], statuses):
            # Never lift a previously persisted macro block during routing.
            if tranche["status"] != "PLANNED_BLOCKED_MACRO":
                tranche["status"] = status
        position["status"] = ("PLANNED_BLOCKED_MACRO" if all(
            t["status"] == "PLANNED_BLOCKED_MACRO" for t in position["tranches"])
            else "ACTIVE_PENDING_ORDERS")
        checkpoint["step"] = COMPLETE
    sync_route(state)
    return state


def update_file(destination, action="resume", **kwargs):
    """Publish result and routing together; failures leave the old JSON intact."""
    destination = Path(destination).resolve(strict=True)
    if destination == Path(__file__).resolve().parents[1] / "state.factory.json":
        raise ValueError("The factory snapshot cannot be changed.")
    original = destination.read_bytes()
    state = transition(json.loads(original), action, **kwargs)
    data = (json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    if state == json.loads(original):
        return state
    staged_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=f".{destination.name}.phase3-",
                                         suffix=".tmp", delete=False) as staged:
            staged_path = Path(staged.name)
            staged.write(data)
            staged.flush()
            os.fchmod(staged.fileno(), stat.S_IMODE(destination.stat().st_mode))
            os.fsync(staged.fileno())
        os.replace(staged_path, destination)
    finally:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("resume", "metrics", "approve-orders",
                                          "propose-invalidation", "approve-invalidation", "finish"))
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--symbol")
    parser.add_argument("--payload", type=Path, help="JSON metrics or invalidation proposal file")
    parser.add_argument("--approve", action="store_true", help="Only after explicit user approval")
    args = parser.parse_args()
    try:
        payload = json.loads(args.payload.read_bytes()) if args.payload else None
        state = update_file(args.state, args.action, symbol=args.symbol,
                            payload=payload, approved=args.approve)
        current = route(state) if state["current_phase"] == PHASE else {"symbol": None, "remaining": []}
        print(json.dumps({**current, "phase": state["current_phase"], "step": state["current_step"]},
                         ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Phase 3 unchanged: {error}\n")


if __name__ == "__main__":
    main()
