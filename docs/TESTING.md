# Testing, quality gates, and CI

RepoHarbor `0.1.0` is an **unreleased development build**. The test suite checks isolated local Git repositories, an authenticated loopback HTTPS server, crash/recovery behavior, and a Linux Docker image. Tests do **not** certify an actual NAS or Portainer deployment.

## Local prerequisites and commands

Install **Python 3.12+**, **Git**, and the **OpenSSL CLI** (for the synthetic HTTPS integration tests). From the repository root:

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

The version is defined in `pyproject.toml`; the CLI reports the installed package version. `requirements-dev.in` lists direct development requirements and `requirements-dev.txt` pins the resolved development dependencies. The coverage configuration includes the application modules and entry points; do not remove failing code from measurement to meet a threshold.

### Required coverage thresholds

| Metric | Minimum |
|---|---:|
| Total line coverage | **90%** |
| Total branch coverage | **85%** |
| Line coverage for **each** critical module (`credentials`, `persistence`, `git_backup`, `recovery`, `storage`) | **95%** |

`scripts/check_coverage.py` enforces these independently using `coverage.json`, including a check that branch-coverage data is present. A green pytest result alone does not replace the separate coverage gate.

## What the suite tests

| Test area | Covered scenarios |
|---|---|
| Git integration | First mirror clone, no-op checks, default-branch changes, annotated tags, branch deletion/pruning, force-pushes, remote outages, and offline restore. |
| HTTPS and credentials | Real local Git smart-HTTP over loopback TLS, a temporary trusted certificate, `GIT_ASKPASS` challenges, both `env` and `file` modes, redaction and token rotation behavior. |
| Recovery and fault injection | SIGTERM/SIGKILL during clone/fetch/repair, journal checkpoints, metadata writes, interrupted/owned/unowned partials, quarantine replay, bounded repair, and idempotency. |
| Storage and scheduler | Global/per-repository budgets, free-space reserve, capacity pause/resume, five-second transfer monitoring, DST behavior, missed ticks, and durable overlap counts. |
| State and security | Corrupt/missing metadata, OS locking, unknown configuration fields, URL validation, secret handling, and error-code redaction. |
| Docker | CLI/version/config validation, required bind mount, non-root execution, no listening socket or healthcheck, graceful shutdown, and recreation with retained data. |

Tests use synthetic repositories and fictional credentials, not production tokens, live user repositories, or NAS data. An isolated test Git transport may enable local file access; the production product has HTTPS-only transport.

## Docker and Compose smoke tests

Install Docker with Compose v2 and access to a Linux/amd64 Docker endpoint. From the repository root:

```sh
docker compose -f deploy/compose.example.yaml config --quiet
docker build -t repoharbor:local-test .
python3 tests/docker/smoke.py
```

The Docker smoke test verifies the CLI, mount requirement, unprivileged user, headless operation, graceful SIGTERM, and state retention after container recreation. It is **not** an end-to-end test of a real NAS filesystem, token provider API, or Portainer installation.

For an authorized TLS-intercepting build proxy, the Dockerfile supports an optional BuildKit CA secret:

```sh
docker build --secret id=build_ca,src=/path/to/trusted-ca.pem -t repoharbor:local-test .
```

The trusted CA is made available only during build-time package installation; do not disable TLS verification or add private CA keys to the repository.

## GitHub Actions workflow

The [CI workflow](../.github/workflows/ci.yml) runs for pull requests targeting `main`, pushes to `main`, and manual dispatch. Checkout/setup actions use pinned revisions and the workflow has read-only repository permissions. It does not merge pull requests or publish Docker images.

These are the six **required check names** to select in branch protection:

| Check | Purpose |
|---|---|
| `lint-static` | Ruff lint/format verification and strict mypy. |
| `unit-and-integration (3.12)` | Python 3.12 tests and coverage gates. |
| `unit-and-integration (3.13)` | Python 3.13 tests and coverage gates. |
| `fault-injection` | Independent crash, signal, recovery, and journal fault tests. |
| `security` | Dependency audit, Bandit medium/high findings, and pinned Gitleaks secret scanning. |
| `docker-build-smoke` | Compose validation, image build, and Docker smoke test. |

The workflow uploads test/coverage artifacts for review. They are **generated reports**, not product source files and should not be committed. The repository administrator must configure required checks and deletion/force-push protection in GitHub **Settings → Rules → Rulesets**; CI cannot grant itself branch-protection permissions.

### Verified baseline (not a permanent guarantee)

For the reviewed development commit `d3d8080` on 2026-11-12, the associated [GitHub Actions run](https://github.com/Play1live/RepoHarbor/actions/runs/43288735934) reported **201 passed tests** on each of Python 3.12 and 3.13, **99.30% line coverage**, **98.30% branch coverage**, and 49 passed fault-injection tests. The six CI checks were successful at that commit. Future commits must be validated using their **own** completed CI runs; local and CI coverage values can vary slightly with timing.

## Documentation and production acceptance

The [root README](../README.md) summarizes the architecture and supported features. The [deployment guide](../deploy/README.md) provides host and Portainer setup plus offline restore, while [SECURITY.md](SECURITY.md) documents the data-preservation rules.

Before production use, an operator must separately verify the actual host NAS mount, filesystem locking and durability, UID/GID and ACL permissions, selected credential mode, a real scheduled backup, recorded timestamps, controlled container restart, and a complete offline restore. Automated validation or an apparently healthy Docker container is **not** evidence of a successful backup.
