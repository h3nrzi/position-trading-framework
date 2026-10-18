"""Small local contract: plan permission and user-reported execution are separate."""

import math


PLAN_STATUSES = {"PLAN_DRAFT", "PLAN_CONFIRMED"}
LEGACY_POSITIONS = {"PLANNING", "ACTIVE_PENDING_ORDERS", "PLANNED_BLOCKED_MACRO",
                    "PENDING", "FILLED", "CANCELLED", "REJECTED"}
PLAN_TRANCHES = {"PLANNED", "PLANNED_BLOCKED_MACRO"}
LEGACY_TRANCHES = {"PENDING", "FILLED", "CANCELLED", "REJECTED"}
EXECUTION_STATUSES = {"UNKNOWN", "NOT_SUBMITTED", "SUBMITTED", "FILLED", "CANCELLED", "REJECTED"}
TERMINAL = {"FILLED", "CANCELLED", "REJECTED"}


def number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def validate_execution(record):
    if not isinstance(record, dict) or record.get("status") not in EXECUTION_STATUSES:
        raise ValueError("Unknown execution status; clarify the user report.")
    if (record.get("source") != "USER_REPORTED" or record.get("confirmed") is not True
            or not text(record.get("evidence")) or not text(record.get("reported_at"))):
        raise ValueError("Execution needs explicit confirmed user-reported evidence and report date.")
    quantity, cost = record.get("filled_quantity"), record.get("filled_cost")
    if not number(quantity) or not number(cost) or ((quantity == 0) != (cost == 0)):
        raise ValueError("Report cumulative filled_quantity and filled_cost together (zero or positive).")
    if record["status"] in {"NOT_SUBMITTED", "REJECTED"} and quantity != 0:
        raise ValueError("Unsubmitted/rejected orders cannot contain fills.")
    if record["status"] == "FILLED" and quantity == 0:
        raise ValueError("A reported fill needs actual quantity and cost, never planned amounts.")
    if "last_known_status" in record and (record["status"] != "UNKNOWN"
            or record["last_known_status"] not in {"NOT_SUBMITTED", "SUBMITTED"}):
        raise ValueError("Invalid last known execution status.")


def validate_position(position):
    if not isinstance(position, dict):
        raise ValueError("Invalid position record.")
    status = position.get("status", "PLANNING")
    if status not in PLAN_STATUSES | LEGACY_POSITIONS:
        raise ValueError("Unknown position status; clarify without rewriting it.")
    if "phase3" in position:
        checkpoint = position["phase3"]
        if not isinstance(checkpoint, dict) or checkpoint.get("step") not in {
                "INPUT_CHART_METRICS", "CALCULATE_LIMIT_ORDERS", "SET_INVALIDATION", "COMPLETE"}:
            raise ValueError("Unknown Phase 3 checkpoint.")
        if ((status == "PLANNING" and checkpoint["step"] == "COMPLETE")
                or (status in {"ACTIVE_PENDING_ORDERS", "PLANNED_BLOCKED_MACRO"}
                    and checkpoint["step"] != "COMPLETE")):
            raise ValueError("Contradictory legacy plan status and approval checkpoint.")
    tranches = position.get("tranches", [])
    if not isinstance(tranches, list) or any(not isinstance(t, dict) for t in tranches):
        raise ValueError("Invalid tranche records.")
    ids = [t.get("tranche") for t in tranches]
    if any(type(n) is not int or n not in (1, 2, 3) for n in ids) or len(ids) != len(set(ids)):
        raise ValueError("Invalid or duplicate tranche identifiers.")
    if status in PLAN_STATUSES:
        step = position.get("phase3", {}).get("step")
        expected = "COMPLETE" if status == "PLAN_CONFIRMED" else None
        if (expected and step != expected) or (not expected and step not in {
                "INPUT_CHART_METRICS", "CALCULATE_LIMIT_ORDERS", "SET_INVALIDATION"}):
            raise ValueError("Contradictory plan status and approval checkpoint.")
    for tranche in tranches:
        allowed = PLAN_TRANCHES if status in PLAN_STATUSES else PLAN_TRANCHES | LEGACY_TRANCHES
        if tranche.get("status") not in allowed:
            raise ValueError("Contradictory tranche status; plan and execution must be separate.")
        if "execution" in tranche:
            validate_execution(tranche["execution"])
    # Actual activity can be reported against a draft or blocked plan. This is
    # historical evidence of an external action, never permission to execute it.


def execution(tranche):
    """Missing legacy/new evidence is unknown, including legacy FILLED/PENDING."""
    if "execution" in tranche:
        validate_execution(tranche["execution"])
        return tranche["execution"]
    return {"status": "UNKNOWN", "filled_quantity": 0, "filled_cost": 0}
