"""Separate, fail-closed line/branch/per-critical-module coverage gate."""

import json
import sys
from pathlib import Path

CRITICAL = {
    "persistence",
    "recovery",
    "storage",
    "credentials",
    "git_backup",
    "selection",
}


def percent(covered, total):
    return 100 * covered / total if total else 100.0


def check(report):
    totals = report["totals"]
    lines = percent(totals["covered_lines"], totals["num_statements"])
    branches = percent(totals["covered_branches"], totals["num_branches"])
    failures = []
    if lines < 90 or branches < 85 or not report["meta"]["branch_coverage"]:
        failures.append("overall coverage below 90% lines / 85% branches")
    found = set()
    for filename, data in report["files"].items():
        module = Path(filename).stem
        if module in CRITICAL:
            found.add(module)
            summary = data["summary"]
            value = percent(summary["covered_lines"], summary["num_statements"])
            print(f"{module}: {value:.2f}% lines")
            if value < 95:
                failures.append(f"{module} below 95% lines")
    if found != CRITICAL:
        failures.append("missing critical modules")
    print(f"Total: {lines:.2f}% lines, {branches:.2f}% branches")
    if failures:
        raise ValueError("; ".join(failures))


if __name__ == "__main__":
    check(
        json.loads(
            Path(sys.argv[1] if len(sys.argv) > 1 else "coverage.json").read_text()
        )
    )
