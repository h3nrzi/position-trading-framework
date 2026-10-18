# Batch 02: Phase 3 routing review

Implementation is ready for review. **Human evaluation is pending**; passing tests and the walkthrough below do not establish acceptance.

The required global `~/.codex/skills/ticket-walkthrough/SKILL.md` was absent, as was a copy under `~/.agents/skills`. This guide is a manually prepared fallback, not a claim that the unavailable skill ran.

## Contract and transitions

Only `positions[].phase3` is new and optional. Its step persists the approval boundary, chart inputs, and pending invalidation proposal. Current symbol and remaining symbols are derived from Core/Growth allocations in allocation order. A completed asset is never appended or recalculated on replay. Reserve is excluded.

```text
BTC: INPUT_CHART_METRICS
  -> persist metrics + proposed orders
  -> CALCULATE_LIMIT_ORDERS (wait for explicit order approval)
  -> SET_INVALIDATION (persist/display proposal; wait for explicit approval)
  -> COMPLETE
ETH: INPUT_CHART_METRICS -> same three steps -> COMPLETE
SOL: INPUT_CHART_METRICS -> same three steps -> COMPLETE
LINK: INPUT_CHART_METRICS -> same three steps -> COMPLETE
  -> PHASE_4_WEEKLY_AUTOPILOT / WEEKLY_DEEP_SEARCH
```

For an already partially processed next asset, resume its checkpoint instead of restarting it. Replayed confirmations retain their original symbol and decision type and are no-ops. Legacy complete records are recognized without modification; ambiguous legacy data raises an error without writing. Existing Phase 4 is not backfilled or rerouted.

## Recorded preflight

- Environment: Python 3.14.7; repository baseline `887bfd3`; 2026-10-18 UTC.
- `python3 -B -m unittest discover -s tests -v`: **29 passed** (17 Phase 3, 12 unchanged Batch 01).
- `git diff --check`: passed.
- Every shell block in this guide was executed in one disposable review session: exit 0, three expected gate rejections, successful replay/compatibility byte comparisons, and the four-asset preservation assertion passed. This is agent preflight evidence; checkpoints A–F still await the user's observations.
- Review checked approval boundaries, legacy inference, repeat delivery, atomic persistence, and scope. CLI final-step reporting was corrected and covered by a regression test. Regression fixtures do not depend on the current live simulation.
- Live `state.json` SHA-256 before/after: `8fc23ad3c7497c04a104e94231691e11405050c92ffefe14d50b3318cdcd1376`.
- Factory SHA-256 before/after: `b58fbb92072b3374d0858d56ed8e0a6d03c28a36c0bebf3d08db64e96d921a9f`.
- No changes to reset tooling, factory defaults, market configuration, or Phase 1/2/4 instructions. No commit or push.

## First action: prepare disposable review states

From the repository root, run this block. It creates files under the system temporary directory; it never writes the repository simulation. Test fixture values are synthetic and are not proposed trades.

```sh
BATCH02_REVIEW_DIR=$(mktemp -d "${TMPDIR:-/tmp}/phase3-batch02.XXXXXX")
python3 -B - "$BATCH02_REVIEW_DIR" <<'PY'
import importlib.util
import json
from pathlib import Path
import shutil
import sys

target = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("review_cases", "tests/test_phase3.py")
cases = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cases)
for name, state in (("four", cases.fixture()), ("one", cases.fixture(1, False)),
                    ("legacy", cases.legacy_populated())):
    (target / f"{name}.json").write_text(json.dumps(state, indent=2))
(target / "metrics.json").write_text(json.dumps(cases.METRICS))
(target / "invalidation.json").write_text(json.dumps(cases.INVALIDATION))
shutil.copyfile("state.json", target / "existing.json")
shutil.copyfile("state.json", target / "existing.before.json")
print(target)
PY
p3() { python3 -B scripts/phase3.py "$@" --state "$BATCH02_REVIEW_DIR/four.json"; }
p3 resume
```

Checkpoint A: output must name BTC, `INPUT_CHART_METRICS`, remaining BTC/ETH/SOL/LINK. USDT/USDC must be absent from remaining. User observation: **pending**.

## Gates, recovery, and one completed asset

```sh
p3 finish
p3 approve-orders --symbol BTC --approve
```

Both must exit unsuccessfully without changing the fixture: Phase 4 is premature and there is no order plan to approve.

```sh
p3 metrics --symbol BTC --payload "$BATCH02_REVIEW_DIR/metrics.json"
p3 resume
python3 -m json.tool "$BATCH02_REVIEW_DIR/four.json"
p3 approve-orders --symbol BTC
```

Checkpoint B: BTC remains in `CALCULATE_LIMIT_ORDERS`, with one position and proposed amounts 90/120/90. The last command lacks explicit approval and must fail. Closing the terminal is optional; every invocation starts a new process, so `resume` demonstrates persisted recovery. User observation: **pending**.

After inspecting the synthetic order table, explicitly approve that table:

```sh
p3 approve-orders --symbol BTC --approve
p3 approve-orders --symbol BTC --approve
p3 resume
p3 propose-invalidation --symbol BTC --payload "$BATCH02_REVIEW_DIR/invalidation.json"
p3 resume
python3 -m json.tool "$BATCH02_REVIEW_DIR/four.json"
```

Checkpoint C: repeated order confirmation must leave BTC in `SET_INVALIDATION`, with one position. The proposal must survive resume and must not mark BTC complete. Inspect the displayed proposal: 57.6 with `WEEKLY_CLOSE_BELOW`. User observation: **pending**.

After explicitly approving that synthetic invalidation decision:

```sh
p3 approve-invalidation --symbol BTC --approve
cp "$BATCH02_REVIEW_DIR/four.json" "$BATCH02_REVIEW_DIR/after-btc.json"
p3 approve-invalidation --symbol BTC --approve
cmp "$BATCH02_REVIEW_DIR/four.json" "$BATCH02_REVIEW_DIR/after-btc.json"
p3 resume
```

Checkpoint D: `cmp` succeeds; ETH is current at `INPUT_CHART_METRICS`; BTC is complete and all its tranches remain `PLANNED_BLOCKED_MACRO`. User observation: **pending**.

## Remaining assets and final transition

For these synthetic fixtures, the following loop explicitly approves each fixture's displayed plan and invalidation. Inspect the fixture values above before running it.

```sh
for symbol in ETH SOL LINK; do
  p3 metrics --symbol "$symbol" --payload "$BATCH02_REVIEW_DIR/metrics.json"
  p3 approve-orders --symbol "$symbol" --approve
  p3 propose-invalidation --symbol "$symbol" --payload "$BATCH02_REVIEW_DIR/invalidation.json"
  p3 approve-invalidation --symbol "$symbol" --approve
done
python3 -B - "$BATCH02_REVIEW_DIR" <<'PY'
import json
from pathlib import Path
import sys
folder = Path(sys.argv[1])
before = json.loads((folder / "after-btc.json").read_text())
after = json.loads((folder / "four.json").read_text())
assert after["positions"][0] == before["positions"][0]
assert [p["symbol"] for p in after["positions"]] == ["BTC", "ETH", "SOL", "LINK"]
assert after["current_phase"] == "PHASE_4_WEEKLY_AUTOPILOT"
assert after["current_step"] == "WEEKLY_DEEP_SEARCH"
print("Four assets complete; BTC preserved; no Reserve position")
PY
```

Checkpoint E: Phase 3 continues after ETH and SOL; only LINK completion enters Phase 4. No position or tranche becomes executed; macro blocks remain. User observation: **pending**.

## Single asset and populated state compatibility

```sh
python3 -B scripts/phase3.py metrics --state "$BATCH02_REVIEW_DIR/one.json" --symbol BTC --payload "$BATCH02_REVIEW_DIR/metrics.json"
python3 -B scripts/phase3.py approve-orders --state "$BATCH02_REVIEW_DIR/one.json" --symbol BTC --approve
python3 -B scripts/phase3.py propose-invalidation --state "$BATCH02_REVIEW_DIR/one.json" --symbol BTC --payload "$BATCH02_REVIEW_DIR/invalidation.json"
python3 -B scripts/phase3.py approve-invalidation --state "$BATCH02_REVIEW_DIR/one.json" --symbol BTC --approve
cp "$BATCH02_REVIEW_DIR/legacy.json" "$BATCH02_REVIEW_DIR/legacy.before.json"
python3 -B scripts/phase3.py resume --state "$BATCH02_REVIEW_DIR/legacy.json"
cmp "$BATCH02_REVIEW_DIR/legacy.json" "$BATCH02_REVIEW_DIR/legacy.before.json"
python3 -B scripts/phase3.py resume --state "$BATCH02_REVIEW_DIR/existing.json"
cmp "$BATCH02_REVIEW_DIR/existing.json" "$BATCH02_REVIEW_DIR/existing.before.json"
python3 -B -m unittest discover -s tests -v
```

Checkpoint F: the single asset enters Phase 4 only on final approval; both populated Phase 4 copies remain byte-identical; all 29 tests pass. User observation: **pending**.

Additional automated checks cover legacy partial plans, stale global steps, a later already-partial asset, duplicate/ambiguous legacy records, qualitative invalidation, macro boundary permissions, and injected fsync/replace failures with retry.

## Decisions and limits

No product decision is outstanding for the supplied state. Legacy terminal status plus complete plan/invalidation is accepted as historical confirmation because the old schema recorded no approval flags. A legacy draft lacking approval evidence asks again; malformed or duplicated records require focused clarification instead of destructive repair. Zero/negative Core/Growth budgets are rejected rather than silently skipped.

Atomic persistence assumes one local writer and filesystem atomic replacement. Concurrent conversations and exchange execution remain outside this batch. There is no order-plan editing feature added. Human acceptance and observations for checkpoints A–F remain pending; report the first mismatch with its fixture path, command, actual output, and expected checkpoint. The unavailable global walkthrough skill remains the sole handoff tooling limitation.
