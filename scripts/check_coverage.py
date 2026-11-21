"""Separate, fail-closed line/branch/per-critical-module coverage gate."""

import json
import sys
from pathlib import Path

CRITICAL = {
    "catalog",
    "engine",
    "events",
    "log_files",
    "mounts",
    "control",
    "persistence",
    "recovery",
    "restore",
    "storage",
    "credentials",
    "git_backup",
    "integrity",
    "selection",
    "gitlab",
}
WEB_AND_RETRY = {"dashboard_http", "dashboard_data", "retry"}


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
        if module in CRITICAL | WEB_AND_RETRY:
            found.add(module)
            summary = data["summary"]
            value = percent(summary["covered_lines"], summary["num_statements"])
            print(f"{module}: {value:.2f}% lines")
            threshold = 95 if module in CRITICAL else 90
            if value < threshold:
                failures.append(f"{module} below {threshold}% lines")
    if found != CRITICAL | WEB_AND_RETRY:
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
