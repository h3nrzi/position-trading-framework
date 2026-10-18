# Batch 03: trading plans and user-reported execution

The framework has no exchange connection. It plans and records user statements; it never submits, cancels, rejects or fills an exchange order. All reported execution is **user-reported, not independently verified**. This contract takes precedence over ambiguous historical wording. No macro formula, allocation, tranche percentage, market configuration or factory snapshot changes.

## Root cause

Phase 3 previously wrote `PENDING` for hypothetical tranches and `ACTIVE_PENDING_ORDERS` after plan approval. Phase 4 then reused that same tranche `status` for `FILLED` / `PENDING`, described filled orders as “active,” and overwrote `average_entry` with actual purchase averages even though Phase 3 had used it for the hypothetical full-plan average. Its `ACTIVATE_TRANCHE` wording tied action to price arrival without an explicit macro/evidence gate. The existing simulation state also combines hypothetical `average_entry` values with historical zero-holding snapshots. The overlapping names and absent evidence boundary made plan, order and holding interpretations ambiguous.

## State fields and meanings

Existing schema version `1.0.0` remains readable. New position records use:

| Field | Values / meaning |
| --- | --- |
| `positions[].status` | `PLAN_DRAFT`: either the table or invalidation still awaits approval. `PLAN_CONFIRMED`: both decisions explicitly approved for this symbol. Neither means an exchange order exists. |
| `positions[].phase3.step` | Existing routing/approval checkpoints. Table approval advances to `SET_INVALIDATION`; invalidation approval advances to `COMPLETE`. Only `COMPLETE` permits `PLAN_CONFIRMED`. |
| `tranches[].status` | `PLANNED`: a hypothetical tranche without a persisted macro block. `PLANNED_BLOCKED_MACRO`: hypothetical tranche with a persisted block. These are plan permissions, never execution states. Current macro restrictions also apply to `PLANNED`. |
| `potential_average_entry` | Hypothetical weighted average if all planned amounts buy at all planned prices. Not a holding or invested capital. |
| `tranches[].execution` | Optional latest confirmed user report for one order per tranche. Absence means `UNKNOWN`, never `NOT_SUBMITTED`. Phase 3 never creates this field. |

Execution statuses:

| Status | Explicit meaning |
| --- | --- |
| `UNKNOWN` | Current exchange execution status is unknown. No active order, absence of orders, or completed trade can be inferred. Previously reported cumulative fills remain recorded. |
| `NOT_SUBMITTED` | User explicitly reports no submitted order and no fills for this tranche, as of the report. Requires quantity/cost zero. |
| `SUBMITTED` | User reports an order submitted and still open, possibly partially filled. Cumulative filled quantity/cost must be supplied, including explicit zero when unfilled. Not an independently verified exchange acknowledgment. |
| `FILLED` | User reports the order fully filled and supplies positive actual cumulative quantity and cost. Planned quantity/price cannot substitute for execution details. |
| `CANCELLED` | User reports cancellation completed. Any prior partial fills remain. A cancellation request alone is insufficient. |
| `REJECTED` | User reports the order rejected without fills. Requires quantity/cost zero. |

Each stored `execution` object requires `source: "USER_REPORTED"`, `confirmed: true`, a nonempty `evidence` string (the user's explicit statement or description of their evidence), nonempty `reported_at` (user's report date/time), `status`, and finite nonnegative `filled_quantity` and `filled_cost`. Quantity and cost are cumulative, both zero or both positive. Cost is actual buy cost in the portfolio currency; actual average is cost / quantity. Even an explicit `UNKNOWN` report needs confirmation; its numbers retain any previously reported fills and do not assert a complete current total.

A clear user statement is sufficient evidence for this local ledger. The agent must not manufacture `source`, evidence, or confirmation from a chart, research, old simulation labels, plan approval or silence. First display the report with its symbol/tranche, status, quantity and cost, then obtain explicit user confirmation of that report. A scoped statement covering several tranches may be confirmed together, then applied to only those named tranches. Otherwise ask one focused question at a time. The CLI cannot authenticate conversation provenance; these flags enforce the local contract, not independent verification.

## Allowed transitions

Plan lifecycle:

1. `metrics` creates `PLAN_DRAFT` with `phase3.step = CALCULATE_LIMIT_ORDERS`, planned tranches and a potential average. It creates no execution record.
2. `approve-orders --approve` confirms only the displayed hypothetical table for the named symbol. Status remains `PLAN_DRAFT`, checkpoint becomes `SET_INVALIDATION`.
3. `propose-invalidation` persists a proposal without approving it.
4. `approve-invalidation --approve` confirms the displayed decision, stores its price/condition and changes to `PLAN_CONFIRMED` / `COMPLETE`. Persisted blocks stay blocked; current macro may add blocks, never silently remove them.
5. After all eligible Core/Growth plans complete, Phase 4 starts even with **zero reported orders**. Reserve remains excluded. Replayed symbol-specific approvals are no-ops. No plan-editing/unblocking workflow is added.

Execution lifecycle (every write requires the evidence and confirmation above):

| From | Allowed next status |
| --- | --- |
| Missing record / `UNKNOWN` | Any defined execution status, including another `UNKNOWN`; remembered submission still forbids `NOT_SUBMITTED` |
| `NOT_SUBMITTED` | Any defined execution status; a later report may describe submission and a terminal outcome that occurred between audits |
| `SUBMITTED` | `SUBMITTED`, `UNKNOWN`, `FILLED`, `CANCELLED`, `REJECTED` (only if zero fills); never `NOT_SUBMITTED` |
| `FILLED`, `CANCELLED`, `REJECTED` | Same terminal status and same cumulative quantity/cost only; exact replay is a no-op |

An `UNKNOWN` report preserves `last_known_status` (`SUBMITTED` or `NOT_SUBMITTED`) when available. This small continuity field is maintained by the helper, not supplied as new evidence; a known submission cannot be changed to `NOT_SUBMITTED` by passing through `UNKNOWN`.

Cumulative filled quantity/cost cannot decrease or be erased, even on `UNKNOWN` or cancellation. Increases require both more quantity and more cost. Terminal changes, corrections, replacement orders and resubmission after cancellation/rejection require clarification and a separately scoped change; this batch deliberately rejects them instead of rewriting history. Unknown/contradictory status names, checkpoint conflicts, duplicate symbols/tranche IDs, missing evidence or invalid quantities fail without writing. An existing execution report cannot be erased by Phase 3 replanning.

A user can report activity that violated a macro block. Record the report as historical external activity, retain the plan block, and explain the discrepancy. Recording it grants no permission for another order. Execution reports do not change routing, approval, planned prices/amounts, invalidation, macro score or allocations.

## Legacy compatibility

No automatic migration or rewrite. `state.json`, its simulation values, `average_entry` and historical weekly logs remain untouched. Old complete plans (`ACTIVE_PENDING_ORDERS` or `PLANNED_BLOCKED_MACRO` with valid table and invalidation) retain Batch 02's historical plan-confirmation interpretation and routing. `PLANNING` remains a draft; missing approvals are not invented. An incomplete legacy final record is an error. Old execution-like position labels without approval checkpoints display plan status `UNKNOWN`, not confirmed.

Old `ACTIVE_PENDING_ORDERS`, tranche `PENDING`, `FILLED`, `CANCELLED` or `REJECTED` alone establish **no exchange evidence**. They remain stored verbatim and read as execution `UNKNOWN` until a new explicit confirmed user report is recorded in `execution`. Existing simulation quantities, old `average_entry` and weekly snapshots cannot establish actual holdings. Historical prose such as “no purchases” in a simulation log is preserved for review, not automatically promoted into current confirmed reports. A legacy blocked tranche remains blocked even after the macro regime improves.

New `potential_average_entry` and the read-only audit's hypothetical average never use old `average_entry` as actual cost basis. Auditing recomputes the potential average from confirmed plan amounts/prices without modifying any stored values. A report adds/replaces only the selected tranche's `execution` object, preserving every other legacy field.

## Phase 4 audit and action rules

`scripts/order_status.py audit` is read-only. It separates plan confirmation, current/persisted macro blocks, execution source/status and reported fills. It never checks for fills by comparing current price with target price.

`reported_filled_quantity` and `reported_invested_capital` summarize explicit reports and may be incomplete. `holding_quantity`, `invested_capital`, `actual_average_entry` and `unrealized_pnl` are `null` if any tranche has unknown execution or the plan is incomplete. When all execution snapshots are known, only their actual filled quantities/costs count; submitted unfilled orders invest zero. With no fills there is no actual entry average or PnL (`null`). With fills but no supplied current price, PnL is `null`. Otherwise estimated PnL is quantity × current price − filled cost. Figures are user-reported snapshots as of the reports, not independently reconciled current account balances.

For a weekly log, copy the audit snapshot with the audit date; retain source/unknown markers and preserve historical logs. Weekly progression can continue with explicitly recorded uncertainty, but cannot produce an entry instruction from it. `can_review_entry` is true only for a confirmed plan, an unblocked tranche under both current and persisted macro permissions, and a user-confirmed `NOT_SUBMITTED` report. New weekly signals use `REVIEW_ENTRY` for manual consideration only when this gate is true. A price touch alone does not satisfy it. Historical `ACTIVATE_TRANCHE` log entries remain readable; do not emit that label for new signals. `HOLD` does not imply active orders. Rebalance/exit discussion requires actual reported holdings, and never changes execution status or claims a cancellation/sale occurred.

## Local commands and verification

Python 3.10+, standard library only, from the repository root; use disposable copies for review:

```sh
python3 -B -m unittest discover -s tests -v
python3 -B scripts/order_status.py audit --state state.json
```

The second command is read-only and should show four legacy confirmed plans, all macro-blocked, execution `UNKNOWN` and unknown actual totals. The existing simulation log remains intact.

For a report on an existing Phase 4 **disposable copy**, create a JSON file containing the explicitly confirmed user report, e.g.:

```json
{
  "status": "SUBMITTED",
  "source": "USER_REPORTED",
  "confirmed": true,
  "evidence": "User reports BTC tranche 1 is open at the exchange, with no purchases so far.",
  "reported_at": "2026-10-18",
  "filled_quantity": 0,
  "filled_cost": 0
}
```

```sh
python3 -B scripts/order_status.py report --state /tmp/batch03-state.json --symbol BTC --tranche 1 --payload /tmp/batch03-report.json --approve
```

`--approve` is valid only after the displayed report is explicitly confirmed. `--prices` is audit-only and accepts a JSON map such as `{"BTC": 100}`. Neither report dates nor evidence are independently verified. Writes use the same single-writer atomic staging/fsync/replace helper as Phase 3, reject the factory snapshot and require an existing explicit state path. Validation/I/O failure before replacement leaves the prior file intact. No concurrent-writer coordination is added.

Remaining scope limits: one order snapshot per tranche; no exchange integration, automatic reconciliation, order replacement, sale/transfer ledger, fee accounting, evidence authentication, status correction workflow or plan editing. Partial buys may be reported through cumulative quantities/costs while the order is open or cancelled. Unknown status preserves uncertainty and previously reported fills. The weekly wizard still prepares its narrative/log/action under these instructions; the helper does not implement a scheduler or execute actions.


## Recorded local verification (2026-10-18)

- `python3 -B -m unittest discover -s tests -v`: **46 passed** (29 existing Batch 01/02 cases, 17 Batch 03 cases including the transition matrix). Existing tests retain coverage; legacy fixtures now explicitly use legacy status names and new plans assert `potential_average_entry`.
- `git diff --check`: passed. Local code review checked approval boundaries, macro gates, legacy preservation, transition rules and atomic failure behavior.
- Read-only audit of the supplied state: four legacy confirmed plans, 12 macro-blocked tranches, execution `UNKNOWN`; actual holding/capital/average/PnL totals `null`.
- Both `state.json` and `state.factory.json` are byte-identical to their original committed contents. State SHA-256: `8fc23ad3c7497c04a104e94231691e11405050c92ffefe14d50b3318cdcd1376`; factory SHA-256: `b58fbb92072b3374d0858d56ed8e0a6d03c28a36c0bebf3d08db64e96d921a9f`.
- Human review remains pending. First action: run the read-only audit command above and inspect that the existing simulation plans are blocked and no actual execution is asserted. Then run the regression suite; use only disposable copies for report experiments. No commit or push performed.
