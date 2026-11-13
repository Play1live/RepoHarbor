import copy
import importlib.util
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def gate():
    spec = importlib.util.spec_from_file_location(
        "coverage_gate", "scripts/check_coverage.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_coverage_gate_fails_on_each_independent_threshold(gate):
    report = {
        "meta": {"branch_coverage": True},
        "totals": {
            "covered_lines": 90,
            "num_statements": 100,
            "covered_branches": 85,
            "num_branches": 100,
        },
        "files": {
            f"src/repoharbor/{name}.py": {
                "summary": {"covered_lines": 95, "num_statements": 100}
            }
            for name in gate.CRITICAL
        },
    }
    gate.check(report)
    assert gate.percent(0, 0) == 100
    for change in (
        lambda r: r["totals"].update(covered_lines=89),
        lambda r: r["totals"].update(covered_branches=84),
        lambda r: r["meta"].update(branch_coverage=False),
        lambda r: r["files"].pop("src/repoharbor/recovery.py"),
        lambda r: r["files"]["src/repoharbor/recovery.py"]["summary"].update(
            covered_lines=94
        ),
    ):
        invalid = copy.deepcopy(report)
        change(invalid)
        with pytest.raises(ValueError):
            gate.check(invalid)


def test_gitignore_documentation_is_opt_in_and_artifacts_ignored():
    ignored = [
        "AGENTS.md",
        "agents.md",
        "CLAUDE.md",
        "notes.MD",
        "docs/unknown.md",
        "docs/unknown.markdown",
        "nested/README.md",
        "nested/CHANGELOG.md",
        "data.json",
        "a/metadaten.json",
        "a/repository.git/config",
        "a/repository.git.partial/objects/test",
        "coverage.json",
        "test-results/junit.xml",
        ".env",
        ".env.local",
        "secrets/token",
        ".venv/bin/python",
    ]
    allowed = [
        "README.md",
        "CHANGELOG.md",
        "deploy/README.md",
        "docs/CONFIGURATION.md",
        "docs/TESTING.md",
        "docs/SECURITY.md",
        ".env.example",
        "tests/example.py",
    ]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        input="\n".join(ignored + allowed) + "\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert set(result.stdout.splitlines()) == set(ignored)
    # Uploaded task documents must never accidentally enter the index.
    tracked = subprocess.check_output(["git", "ls-files"], text=True).splitlines()
    assert not any(Path(p).parts[0] == "attachments" for p in tracked)
