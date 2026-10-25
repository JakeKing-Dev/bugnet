"""A second domain, to show the abstraction is not trading-shaped.

An ingest pipeline: files land in a bucket, a worker imports each one into a
warehouse. The authority is the bucket — it knows what arrived, and the
importer does not get to decide that. The ledger is the warehouse row count.

The failure shown here is the one conventional monitoring misses: the importer
runs without error, every row it writes is valid, dashboards are green, and one
file is never imported at all. Nothing that reads the warehouse can see a file
that isn't in it.

    python examples/pipeline.py
"""

from __future__ import annotations

import logging
import sys

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from bugnet import AUTHORITY, BugNet, Config          # noqa: E402
from bugnet.adapters.memory import InMemoryAuthority, InMemoryLedger  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(message)s")

clock = [1_700_000_000.0]
now = lambda: clock[0]          # noqa: E731


def tick(net: BugNet, seconds: float) -> None:
    clock[0] += seconds
    net.step(clock[0])


def main() -> int:
    bucket = InMemoryAuthority(now_fn=now)      # what actually arrived
    warehouse = InMemoryLedger(start=0.0)       # rows we can count from outside
    net = BugNet(authority=bucket, ledger=warehouse,
                 config=Config(resolve_hard=600.0, recon_min_interval=600.0),
                 now_fn=now)

    print("Ten files land. The importer handles nine of them.\n")

    for i in range(10):
        name = f"events-2026-10-20-{i:02d}.jsonl"
        rows = 100.0
        bucket.settle(name, "arrived", at=now())        # the bucket knows
        net.opened(name, "importer", closes_at=now() + 60, value=rows)

        if i == 7:
            # the importer skipped this one. No exception. No bad rows.
            # Nothing downstream can notice, because the rows simply are not
            # there to be checked.
            continue

        warehouse.move(rows)
        net.resolved(name, "imported", source=AUTHORITY, value=rows)
        tick(net, 30)

    print(f"Importer reports: 9 files, {int(warehouse.total())} rows. No errors.")
    print(f"Any check that reads the warehouse: all rows valid. Green.\n")
    print("Now BugNet gets to look:\n")

    for _ in range(40):
        tick(net, 60)
        if net.blocks("importer"):
            break

    blocked = net.blocks("importer")
    print(f"\nblocks('importer') -> {blocked}\n")
    for line in net.report():
        print(line)

    print("\nThe importer is blocked from starting new work until a human looks.")
    print("Acknowledging writes the file off explicitly, rather than letting it")
    print("disappear quietly, which is the whole point.")
    net.acknowledge()
    print(f"\nafter acknowledgement: blocks('importer') -> {net.blocks('importer')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
