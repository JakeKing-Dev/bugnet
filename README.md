# BugNet

**Your monitoring says the system is healthy. BugNet says whether its records are true.**

It compares what a system recorded against a source the system does not write,
and stops the component at fault when they disagree.

No dependencies. Python 3.10+.

---

## The incident this came from

A bot I wrote traded small amounts of real money. One day its records said
**+$0.51**. The account had gone **down**.

Nothing was broken. No exception was raised. Every component reported healthy.
The cause was that losing positions whose price went quiet near the close were
dropped without ever being recorded, while winners — closed earlier — were
recorded normally. The books weren't inaccurate. They were *incomplete*, and
every check I had read those same books.

The part that took longer to appreciate: those records were also the evidence
deciding which strategies were allowed to trade real money. Survivorship bias,
sitting inside the bookkeeping, quietly promoting whatever lost in a way the
system couldn't see.

## The failure class

**Silent completeness failure:** work that happens but never gets recorded.

It is invisible to almost every tool you already run, because they all examine
what *was* written down:

- Schema and data-quality checks validate the rows that exist. They cannot
  validate a row that was never inserted.
- Error trackers see raised exceptions. Nothing was raised.
- Metrics and anomaly detection see the shape of what arrived. A 7% shortfall
  in a noisy series is not an anomaly.
- Orchestrators know a task *ran*. They don't know its outcome was recorded.

The only way to see it is to ask something that does not depend on the system
being checked.

## Two minutes

```python
import time
from bugnet import BugNet, AUTHORITY
from bugnet.adapters.memory import InMemoryAuthority

bucket = InMemoryAuthority()          # the outside truth
net = BugNet(authority=bucket)

net.opened("file-07.jsonl", "importer", closes_at=time.time() + 60)
# ... the importer skips it. No error. Nothing downstream can tell.

net.step()
net.blocks("importer")
# "BugNet: 1 unit(s) of work passed their deadline with no recorded outcome..."
```

A worked example in a non-trading domain:

```bash
python examples/pipeline.py      # ten files land, nine are imported, one vanishes
```

## The rules it is built on

1. **Check against an outside truth.** A check that compares a system's numbers
   with the same system's numbers passes while both are wrong.
2. **A warning nobody acts on is not a control.** Before Knight Capital lost
   $460M in 45 minutes, its systems had sent 97 alert emails. A `STOP` blocks
   the component at fault from starting new work. Winding down is always
   permitted — a component frozen holding resources is usually worse than one
   stopping in an orderly way.
3. **Fallbacks quietly become the main path.** Record where each outcome came
   from, and raise when the fallback stops being a fallback.
4. **Errors that are caught and hidden are faults too.**
5. **Every unit of work must end in a recorded outcome.** A record that drops
   events is a record that lies.

## What it checks

| Check | Compared against | Level |
|---|---|---|
| Every unit of work ends in a recorded outcome | your own book of open work | **STOP** |
| Work the authority settled that nothing recorded | the authority | **STOP** |
| Outside aggregate vs the sum of recorded outcomes | the ledger | **STOP** |
| Repeated hidden errors in one component | error counter | **STOP** |
| Outcomes overdue but not yet written off | open work | squeak |
| Fallback share above a threshold | resolution sources | squeak |

Live faults clear themselves when the check passes again. Faults about things
that already happened cannot clear themselves — they need a human
(`touch BUGNET_OK`), and acknowledging also writes the lost work off explicitly
rather than letting it disappear quietly.

## Measured, not asserted

A fault detector should be able to say how well it detects faults. The harness
injects known faults and reports what was caught and how long it took:

```bash
python bench/benchmark.py
```

| scenario | faults injected | detected | time to detect |
|---|---:|:---:|---:|
| dropped outcomes @ 1% | 3 | yes | 35 min |
| dropped outcomes @ 5% | 18 | yes | 35 min |
| dropped outcomes @ 20% | 40 | yes | 36 min |
| unrecorded work x1 | 1 | yes | 26 min |
| unrecorded work x5 | 5 | yes | 26 min |
| ledger drift +25.00 on 1,000 | 1 | yes | 6 min |
| ledger drift −25.00 on 1,000 | 1 | yes | 6 min |
| ledger drift +0.50 on 10 | 1 | yes | 6 min |

**False positives: 0 STOPs across 10 clean runs**, including 5% fallback
resolutions and a live ledger.

Detection here is deterministic, not statistical: above the configured
threshold a fault is always caught, so the honest questions are *how long*,
*how small*, and *how often it is wrong when nothing is wrong*. Time to detect
is dominated by `resolve_hard` — the grace period before overdue work is called
lost — which is yours to set.

**Reconciliation sensitivity.** Tolerance is `max(0.15, 2% of total)`, so the
blind spot scales with the ledger. This is a real limit, measured rather than
hidden:

| ledger total | smallest drift detected |
|---:|---:|
| 10 | 0.20 |
| 100 | 2.04 |
| 1,000 | 20.41 |
| 10,000 | 204.08 |

If you need absolute rather than proportional accuracy, set
`recon_tolerance_frac = 0` and carry the false-positive cost.

## When not to use this

Use something else if your failures look like:

- **Invalid data that did get written** — Great Expectations, Soda, dbt tests.
- **Exceptions and crashes** — Sentry.
- **Latency, saturation, freshness** — Prometheus, Grafana, your APM.
- **Schema drift and volume anomalies** — Monte Carlo, Bigeye, Metaplane.

BugNet covers one thing those miss: work that completed and was never recorded,
and aggregates that stopped agreeing with the records. If that isn't your
problem, you don't need it.

## Integrating

Four touch points:

```python
net.opened(unit_id, component, closes_at=..., value=...)   # work started
net.resolved(unit_id, outcome, source=AUTHORITY, value=...) # outcome recorded
net.note_error(component, exc)                              # where errors are swallowed
net.step()                                                  # in your main loop
if net.blocks(component): ...                               # before new work
```

You supply an `Authority` (`resolve`, `settled_since`) and optionally a
`Ledger` (`total`). Both are a handful of lines against a real system — see
`bugnet/adapters/memory.py`.

If you find yourself implementing `Authority` by reading the same records
BugNet is checking, stop. That is the failure this library exists to catch.

## Known limits

- **Reconciliation only runs while the system is idle.** Work in flight looks
  exactly like a gap. If your system is never idle, that check will rarely run
  — `report()` prints how many times it has actually passed, and if that number
  stays at zero the check is decoration. Pass `idle_fn` if you have a better
  notion of quiet.
- **Time to detect is bounded below by your grace period.** Shortening it
  catches faults sooner and risks calling slow work lost.
- **It cannot tell you an outcome was recorded *wrongly*** — only that it was
  recorded, and that the aggregate agrees. A systematically wrong but
  self-consistent record will pass.
- **The authority is assumed truthful.** If it lies, so does BugNet.
- Faults keyed to past events accumulate until acknowledged, by design.

## Status

v0.1.0. 25 tests, CI on Python 3.10–3.13, benchmark enforced in CI.
Built from one real incident and generalised; it has not yet been run against a
second production system. If you try it somewhere real, I'd like to hear what
it missed.

MIT.
