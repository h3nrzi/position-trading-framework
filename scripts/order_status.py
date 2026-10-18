"""Audit plans read-only, or record a confirmed user report; never contact exchanges."""

import argparse
import copy
import json
from pathlib import Path

try:
    from scripts import phase3, status_contract as contract
except ModuleNotFoundError:
    import phase3
    import status_contract as contract


def audit(state, prices=None):
    """Return separate plan/execution views. Missing evidence never means zero holdings."""
    prices = prices or {}
    _, positions = phase3.assets_and_positions(state)
    permissions = phase3.plan_statuses(state)
    result = []
    for position in positions.values():
        contract.validate_position(position)
        complete = phase3.has_plan(position) and phase3.has_invalidation(position)
        if position.get("status") in phase3.LEGACY_FINAL and not complete:
            raise ValueError("Ambiguous legacy final record; clarify its approvals first.")
        if "phase3" in position:
            phase3.progress(position)
        if position.get("status") in contract.PLAN_STATUSES:
            confirmed = position["status"] == "PLAN_CONFIRMED"
        else:
            confirmed = complete and (position.get("phase3", {}).get("step") == phase3.COMPLETE
                                      or position.get("status") in phase3.LEGACY_FINAL)
        rows = []
        for tranche in position.get("tranches", []):
            execution = contract.execution(tranche)
            blocked = (tranche["status"] == "PLANNED_BLOCKED_MACRO"
                       or permissions[tranche["tranche"] - 1] == "PLANNED_BLOCKED_MACRO")
            rows.append({"tranche": tranche["tranche"], "blocked_macro": blocked,
                         "execution_status": execution["status"],
                         "source": execution.get("source"),
                         "reported_at": execution.get("reported_at"),
                         "last_known_status": execution.get("last_known_status"),
                         "can_review_entry": confirmed and not blocked
                         and execution["status"] == "NOT_SUBMITTED",
                         "reported_filled_quantity": execution["filled_quantity"],
                         "reported_filled_cost": execution["filled_cost"]})
        known = phase3.has_plan(position) and all(r["execution_status"] != "UNKNOWN" for r in rows)
        quantity = sum(r["reported_filled_quantity"] for r in rows)
        cost = sum(r["reported_filled_cost"] for r in rows)
        price = prices.get(position["symbol"])
        if price is not None and not phase3.positive(price):
            raise ValueError("A current positive price is required for PnL.")
        potential = (sum(t["amount"] for t in position["tranches"]) / sum(
            t["amount"] / t["price"] for t in position["tranches"])) if phase3.has_plan(position) else None
        draft = position.get("status", "PLANNING") in {"PLAN_DRAFT", "PLANNING"}
        result.append({"symbol": position["symbol"],
                       "plan_status": "PLAN_CONFIRMED" if confirmed else "PLAN_DRAFT" if draft else "UNKNOWN",
                       "legacy_record": position.get("status") not in contract.PLAN_STATUSES,
                       "potential_average_entry": potential,
                       "tranches": rows, "execution_totals_known": known,
                       "reported_filled_quantity": quantity, "reported_invested_capital": cost,
                       "holding_quantity": quantity if known else None,
                       "invested_capital": cost if known else None,
                       "actual_average_entry": cost / quantity if known and quantity else None,
                       "unrealized_pnl": quantity * price - cost
                       if known and quantity and price is not None else None})
    return result


def report(source, *, symbol, tranche, payload, approved=False):
    """Record one cumulative order snapshot only after user evidence and confirmation."""
    state = copy.deepcopy(source)
    if state.get("current_phase") != phase3.PHASE4:
        raise ValueError("Execution reports are recorded in Phase 4, separately from plan approval.")
    audit(state)  # Validate all records before writing any report.
    if approved is not True:
        raise ValueError("Explicit user confirmation of this symbol/tranche report is required.")
    if not isinstance(payload, dict):
        raise ValueError("Provide the displayed user execution report.")
    contract.validate_execution(payload)
    _, positions = phase3.assets_and_positions(state)
    position = positions.get(symbol)
    if position is None or type(tranche) is not int:
        raise ValueError("Name an existing symbol and tranche.")
    matches = [t for t in position.get("tranches", []) if t["tranche"] == tranche]
    if len(matches) != 1:
        raise ValueError("Name an existing unique tranche.")
    target = matches[0]
    previous = contract.execution(target)
    old, new = previous["status"], payload["status"]
    last_known = previous.get("last_known_status", old)
    if old in contract.TERMINAL and (new != old or any(
            payload[k] != previous[k] for k in ("filled_quantity", "filled_cost"))):
        raise ValueError("Terminal execution cannot be replaced; clarify contradictory evidence.")
    if last_known == "SUBMITTED" and new == "NOT_SUBMITTED":
        raise ValueError("An existing submitted order needs a cancellation/rejection report.")
    if any(payload[k] < previous[k] for k in ("filled_quantity", "filled_cost")):
        raise ValueError("A report cannot erase previously reported fills.")
    if ((payload["filled_quantity"] > previous["filled_quantity"])
            != (payload["filled_cost"] > previous["filled_cost"])):
        raise ValueError("Additional fills require both additional quantity and cost.")
    target["execution"] = copy.deepcopy(payload)
    target["execution"].pop("last_known_status", None)
    if new == "UNKNOWN" and last_known in {"NOT_SUBMITTED", "SUBMITTED"}:
        target["execution"]["last_known_status"] = last_known
    audit(state)
    return state


def update_file(destination, **kwargs):
    return phase3.persist(destination, lambda state: report(state, **kwargs))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("audit", "report"))
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--symbol")
    parser.add_argument("--tranche", type=int)
    parser.add_argument("--payload", type=Path)
    parser.add_argument("--prices", type=Path, help='Current prices, e.g. {"BTC": 100}; never fill evidence')
    parser.add_argument("--approve", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "report":
            if args.prices is not None:
                raise ValueError("--prices is audit-only; prices are never execution evidence.")
            if args.payload is None:
                raise ValueError("A user report payload is required.")
            state = update_file(args.state, symbol=args.symbol, tranche=args.tranche,
                                payload=json.loads(args.payload.read_bytes()), approved=args.approve)
        else:
            state = json.loads(args.state.read_bytes())
        prices = json.loads(args.prices.read_bytes()) if args.prices else None
        print(json.dumps(audit(state, prices), ensure_ascii=False, allow_nan=False))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        parser.exit(1, f"Status operation failed: {error}\n")


if __name__ == "__main__":
    main()
