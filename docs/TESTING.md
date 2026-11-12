# Development checks

Initial development version: 0.1.0 (not a stable release). Install Python 3.12+, Git and the OpenSSL CLI (temporary certificates for loopback HTTPS tests), then:

```sh
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pip install --no-build-isolation --no-deps -e .
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/pytest --cov --cov-report=xml --cov-report=json --cov-report=term-missing --junitxml=test-results/junit.xml
.venv/bin/python scripts/check_coverage.py coverage.json
.venv/bin/bandit -r src -ll
.venv/bin/pip-audit --local --skip-editable
.venv/bin/pytest tests/fault_injection
```

The lock file pins all development dependencies; `requirements-dev.in` records direct requirements. Product version is canonical in `pyproject.toml`; CLI reads installed package metadata. All application modules, including entrypoints and askpass, count toward coverage. The separate gate enforces 90% lines, 85% branches, and 95% lines individually for credentials, persistence, Git, recovery and storage. No coverage exclusions are used.

Tests use synthetic temporary local Git remotes. They cover clone, no-op checks, ref/HEAD changes, deletion, annotated tags, force push, unavailable sources, exclusion/resumption, offline restore, missing-object repair, corrupt JSON, write faults and OS locks. Repair-pause regression tests distinguish `repair_paused` from exhausted `repair_failed`, cover global/repository/free-space constraints across restart, preserve quarantines and refs, and resume at the next regular run after capacity returns. Additional regression coverage includes bounded quarantine/retry, interrupted repair attempt accounting, immediate durable overlap writes with concurrent journal updates, transfer scan frequency, final-source budget overshoot, and a loopback-only smart-HTTPS server with a temporary trusted certificate and real askpass challenges in both credential modes. Docker validation is tested to leave the mount empty and startup without a mount must fail. Process tests use IPC barriers for SIGTERM/SIGKILL during active clone/fetch transport and journal/Git/promotion/metadata checkpoints; no real tokens or NAS data are used.

```sh
docker compose -f deploy/compose.example.yaml config --quiet
docker build -t repoharbor:local-test .
python3 tests/docker/smoke.py
```

For TLS-intercepting build proxies, an optional BuildKit secret `--secret id=build_ca,src=/path/to/trusted-ca.pem` supplies a CA bundle only during dependency installation, without storing it in the image. TLS verification remains enabled.

The Docker harness checks non-root identity, version/config commands, initialization on an empty bind mount, no listener/healthcheck, graceful SIGTERM and data persistence across recreate. CI runs Python 3.12/3.13, independent fault-injection, SAST, dependency audit, pinned Gitleaks, and Docker build/smoke jobs with read-only GitHub permissions. PR tests never publish images. Required checks must be configured separately in repository settings; the workflow cannot grant itself branch-protection permissions.

The final documentation for development version 0.1.0 is available in the [root README](../README.md), [deployment guide](../deploy/README.md) and [Changelog.md](../Changelog.md). No release date or registry publication is claimed. No real NAS/Portainer or production-provider acceptance is implied by these local/CI tests.

For manual branch protection, require all six actual check names: `lint-static`, `unit-and-integration (3.12)`, `unit-and-integration (3.13)`, `fault-injection`, `security`, `docker-build-smoke`. In the repository's Settings → Rules → Rulesets, target `main`, require pull requests and successful checks, block force pushes and deletions, and review bypass permissions. A denied API read cannot confirm those settings; a repository administrator must verify them before a later merge. The CI itself does not modify rulesets or merge/publish anything.

Coverage values can differ slightly between local and CI runs, for example because thread timing changes which control-flow branches are observed. Report the actual final-commit CI gate output separately for Python 3.12/3.13 instead of copying a local percentage into the PR as a CI result. The thresholds apply independently to every run.
