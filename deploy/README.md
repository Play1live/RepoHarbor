# RepoHarbor 0.1.0 — Docker Compose, Portainer, and NAS deployment

This guide covers the **current, unreleased development build**. No real NAS/Portainer deployment has yet been accepted. The commands below are operator instructions, not claims that deployment or a production restore has already been performed. All example hosts, source repositories, paths, and credential names are fictional and must be checked for your environment.

## 1. Requirements and host storage

- Linux Docker host, Docker Compose v2, and BuildKit to build the image locally. The Portainer instructions assume a **standalone Docker endpoint**, not Swarm.
- Python 3.12, Git, and CA certificates are included in the container. A host-side offline restore requires a Git installation on the restore host.
- The container runs as **UID/GID 10001:10001**, without added Linux capabilities, an application listener, or a Docker healthcheck. Automated image checks cover Linux/amd64; verify other architectures separately.
- A pre-existing, persistent backup bind mount backed by a trusted local filesystem supporting `flock`, atomic rename, and `fsync`. SMB/NFS do not have guaranteed consistency for an active RepoHarbor backup root.

**Verify the intended storage device before creating directories.** The example below assumes that `/srv/repoharbor` itself is a dedicated, mounted filesystem:

```sh
mountpoint -q /srv/repoharbor
findmnt --mountpoint /srv/repoharbor -o SOURCE,TARGET,FSTYPE,OPTIONS
df -h /srv/repoharbor
```

Compare the actual device and mount options with the intended storage. If the filesystem is mounted somewhere else, adapt the commands and all bind paths. `findmnt -T` by itself can show only the host's root filesystem; a Docker bind mount cannot prove that the correct NAS volume is mounted behind the host path.

On the verified filesystem, prepare dedicated directories with appropriate ownership:

```sh
sudo install -d -o 10001 -g 10001 -m 0750 /srv/repoharbor/backups
sudo install -d -o 10001 -g 10001 -m 0700 /srv/repoharbor/secrets
sudo -u '#10001' -g '#10001' test -w /srv/repoharbor/backups
```

Check the target NAS's ACL and UID/GID mapping as well. Do not recursively change permissions or initialize over existing backups to fix a startup error. RepoHarbor writes its mirrors and metadata in `/backups` but only reads its secret files. The Compose example sets `create_host_path: false` so Docker will not silently create missing host paths.

## 2. Build the image and prepare the stack

Run Compose commands from the **repository root**:

```sh
docker compose -f deploy/compose.example.yaml build
docker run --rm repoharbor:local-test --version
```

The version command should print `0.1.0`. `repoharbor:local-test` is a local, mutable **test image**, not a released registry image. The build needs access to the pinned Python packages and Debian package sources. If an authorized TLS-intercepting build proxy requires a custom CA, the Dockerfile supports a temporary BuildKit secret:

```sh
docker build --secret id=build_ca,src=/path/to/trusted-ca.pem -t repoharbor:local-test .
```

Do not disable certificate validation. Start with [`compose.example.yaml`](compose.example.yaml) and adapt the host paths, sources, schedule, and budgets. Keep its `restart: unless-stopped`, `stop_grace_period: 5m`, non-root user, and security settings. Do **not** add an application port, healthcheck, or `INITIALIZE_*` environment variable. Docker init is intentionally omitted for NAS compatibility: on the tested NAS it prevents startup with `/sbin/docker-init: permission denied`.

## 3. Configure sources and credentials

The configuration is embedded as multiline YAML in the **`REPOHARBOR_CONFIG`** environment variable. The local timezone is controlled separately with `TZ`. The configuration is validated at startup and does **not** hot-reload; restart/redeploy the service to apply changes.

The supplied Compose example defaults to `REPOHARBOR_TOKEN_SOURCE: file`, an empty credentials list, and a **fictional public source URL** (`code.example.org`). The read-only secrets directory must exist even when no token file is needed. Replace the placeholder source with a real permitted repository or set `manual_git_urls: []` to start with no backup sources.

### Recommended: file-based credentials

For a fictional private repository, replace the **value** of `REPOHARBOR_CONFIG` with the following YAML. Keep `REPOHARBOR_TOKEN_SOURCE: file` in the Compose environment and retain a suitable `TZ` setting:

```yaml
schema_version: 1
schedule: "0 3 * * *"
credentials: [example_primary]
manual_git_urls:
  - alias: example-demo
    url: https://code.example.org/team/demo.git
    web_url: https://code.example.org/team/demo
    credential: example_primary
storage:
  global_gib: 50
  default_repo_gib: 2
  overrides:
    example-demo: 4
  during_transfer: finish
  min_free_gib: 2
recovery:
  automatic_repair: true
  max_repair_attempts_per_incident: 3
features:
  web_ui: false
```

The service expects the token in `/run/secrets/repoharbor/example_primary`, corresponding here to the fictional host path `/srv/repoharbor/secrets/example_primary`. The file must contain **only the token**, not a URL or YAML configuration:

```sh
sudoedit /srv/repoharbor/secrets/example_primary
sudo chown 10001:10001 /srv/repoharbor/secrets/example_primary
sudo chmod 0400 /srv/repoharbor/secrets/example_primary
```

Keep the secrets bind mount read-only. Do not commit, print, screenshot, or log token values. The application removes trailing CR/LF when loading a secret file; a missing, unreadable, or empty configured credential causes validation to fail. Restart the service after token rotation.

### Alternative: environment-based credentials

Replace the **entire `environment` block** of the service with this example and **remove the secrets bind mount**. Keep the backup mount and the other service options:

```yaml
environment:
  TZ: UTC
  REPOHARBOR_TOKEN_SOURCE: env
  REPOHARBOR_TOKEN_EXAMPLE_PRIMARY: ${REPOHARBOR_TOKEN_EXAMPLE_PRIMARY:?Provide the token securely}
  REPOHARBOR_CONFIG: |
    schema_version: 1
    schedule: "0 3 * * *"
    credentials: [example_primary]
    manual_git_urls:
      - alias: example-demo
        url: https://code.example.org/team/demo.git
        credential: example_primary
    storage:
      global_gib: 50
      default_repo_gib: 2
      overrides: {}
      during_transfer: finish
      min_free_gib: 2
    recovery:
      automatic_repair: true
      max_repair_attempts_per_incident: 3
    features:
      web_ui: false
```

Supply the interpolated variable through a protected local mechanism or the Portainer stack environment; do not insert real values in tracked YAML. Environment secrets are visible to authorized Docker/Portainer administrators. RepoHarbor explicitly supports **one credential mode per container**. A configured secret present in both the active environment and the opposite file mode causes a validation error; remove the old values when switching modes.

Multiple named credentials are supported. Each configured source may reference one credential name; for example, `example_primary` maps to `REPOHARBOR_TOKEN_EXAMPLE_PRIMARY`. If a private Git server uses its own CA, mount a trusted certificate bundle read-only and set `SSL_CERT_FILE` to its path. TLS verification must remain enabled. See the [configuration reference](../docs/CONFIGURATION.md) for normalization rules, supported fields, and defaults.

### GitHub sources alongside manual sources

GitHub.com REST discovery is available in this development build. Use the complete [GitHub configuration example](../docs/CONFIGURATION.md#github-discovery-and-selection) as the value of `REPOHARBOR_CONFIG`; retain the existing Compose mount, non-root user, restart policy and credential mode. No extra application port, init setting, config-file mount or service is required. GitLab, Enterprise servers, SSH and the dashboard remain unimplemented.

For the example's two credential names, file mode expects `example_personal` and `example_team` in the protected read-only secrets mount. In env mode, supply `REPOHARBOR_TOKEN_EXAMPLE_PERSONAL` and `REPOHARBOR_TOKEN_EXAMPLE_TEAM` securely and omit the secret mount. Do not configure both sources. Never paste real tokens into committed YAML or URLs.

Classic PATs need sufficient repository read access; fine-grained PATs need access to each selected repository plus metadata and contents read permissions. Organization approval or SSO may also be required. Restricted fine-grained tokens do not discover repositories outside their grants, even with `mode: all`. Multiple provider entries reference these existing named credentials; the first in-scope configured provider determines Git authentication.

The default selection is `selected` with an empty list. Choose repositories explicitly or deliberately opt into `all`/`all_except`; configure visibility, forks and archived repositories as needed. Explicit exclusion and property/scope filters always take precedence. Changes take effect after restart and are applied at future scheduled runs.

GitHub mirrors live under `/backups/github/github.com/<repository-id>/`; manual paths remain under `/backups/manual/<alias>/`. Restore an intact discovered mirror offline using the same procedure, for example:

```sh
git clone --no-hardlinks /path/to/backups/github/github.com/654321/repository.git ./restored-example
```

IDs here are fictional. Read the actual ID and path from your metadata. Renames and transfers retain that ID directory. Excluded, unavailable and conflicted mirrors remain stored and count against storage limits.

Inspect `data.json.discovery` when GitHub sources do not run. `GITHUB_UNAUTHORIZED` indicates an invalid/expired token; `GITHUB_FORBIDDEN` indicates insufficient permission; rate-limit, API/network and invalid-page failures pause the entire GitHub part while independent manual sources continue. Rate-limit retry times persist across restart; startup itself does not trigger new discovery. Current GitHub selection must be confirmed before remote repair, so `repair_deferred: DISCOVERY_REQUIRED` can persist until a successful scheduled discovery. These states do not prove remote deletion.

Manual/API identity collisions block automatic assignment (`manual_identity_conflict`). An unresolved manual GitHub address pauses discovery, and a new manual alias for a known provider mirror is withheld (`PROVIDER_IDENTITY_CONFLICT`). Resolve the source configuration without deleting or transferring backup data. Details and limitations are in the [identity rules](../docs/CONFIGURATION.md#stable-paths-and-manual-conflicts).

## 4. Scheduling, limits, and status

`0 3 * * *` means 03:00 every day in `TZ`. Cron uses five fields with numbers, ranges, lists, and steps. Following Vixie cron, when either day field begins with `*`, including `*/1`, `*/2`, or a wildcard-leading list, **both** day fields must match. For example, `0 3 */2 * 1` matches Mondays with an odd month day. If neither day field begins with `*`, matching uses **OR**, including explicit full ranges such as `1-31` and `0-7`. See the [configuration reference](../docs/CONFIGURATION.md#scheduling-and-storage-behavior). Nonexistent DST wall-clock minutes do not run; the repeated minute is run at most once. Missed ticks after downtime are **not** replayed; a scheduled tick during an active run is skipped and recorded durably.

Storage values are in GiB (`1 GiB = 1,073,741,824 bytes`). `null` disables an individual soft size budget and `0` prevents starting new operations. Per-repository overrides use stable source aliases. The global budget includes all data in the backup root, including excluded repositories, metadata, journals, partial clones, and quarantine. A per-repository budget counts its entire source directory.

**These are soft budgets, not filesystem quotas.** With `during_transfer: finish` (the default), a running transfer may exceed the configured size budget; `abort` can interrupt it when a limit is detected. The physical free-space reserve takes priority in either mode. Git stop signals are polled frequently, while capacity checks run at most every five seconds. Real disk exhaustion is still possible without host-side quotas. Storage is measured before and after operations and rechecked on the next regular run; valid backups are never deleted automatically to reclaim capacity.

| Backup state | Meaning and operator action |
|---|---|
| `updated` / `up_to_date` | Successful mirror change or successful check with no changes. |
| `global_limit_paused` / `repo_limit_reached` / `FREE_SPACE_RESERVE` | A new operation was skipped because of capacity; check usage and wait for the next scheduled run. |
| `repair_paused` with `repair_deferred` | A needed repair is waiting for storage capacity; no repair failure attempt is consumed. |
| `interrupted` | An operation was interrupted or has not been proven complete; recovery inspects it. |
| `repair_failed` | Automatic repair failed or could not proceed; preserve the data and investigate. |
| `manual_attention_required` (metadata field) | An unknown partial clone, occupied quarantine, or conflict blocks further downloads until explicitly resolved. |

Selection, remote availability, and integrity are separate metadata dimensions. `last_checked_at` is only advanced by a successful remote check; `last_updated_at` only by a proven successful changed mirror. A skipped or failed run must not be reported as a successful backup.

## 5. Validate, start, and inspect

```sh
docker compose -f deploy/compose.example.yaml config --quiet
docker compose -f deploy/compose.example.yaml run --rm --no-deps repoharbor config validate
```

`docker compose config --quiet` avoids printing expanded secret values. `repoharbor config validate` checks configuration and declared credentials **without contacting remotes or changing the backup state**. Because the Compose service declares bind mounts, the configured host paths must still exist for the `compose run` command. Neither validation step proves the expected NAS device is mounted or that a real backup can be restored.

After confirming host mount, storage permissions, and credential settings, start the service:

```sh
docker compose -f deploy/compose.example.yaml up -d --no-build
docker compose -f deploy/compose.example.yaml logs --tail 100 repoharbor
```

The first start safely initializes missing state in an empty root, inventories existing data, performs necessary crash reconciliation, and then waits for **future** cron ticks. Startup recovery may repair a damaged existing mirror, but it does not catch up with missed scheduled backups. A `ready; waiting for future schedule` log entry **does not mean a backup has completed**. This development build has no supported `run-now` command. For supervised acceptance, temporarily schedule a small fictional test repository with `* * * * *`, confirm a successful run, then restore the intended schedule and restart.

To inspect scheduler/global storage state without loosening host permissions:

```sh
docker compose -f deploy/compose.example.yaml exec -T repoharbor python -c \
  'import json; from pathlib import Path; d=json.loads(Path("/backups/data.json").read_text()); print(d["global_storage"]); print(d["scheduler"])'
```

Each `manual/<alias>/metadaten.json` records identity, selected status, source availability, timestamps, errors (the most recent 20 plus counters), storage, and integrity/repair state. An active container and a successful Docker restart **do not demonstrate backup freshness**.

## 6. Portainer and controlled updates

The image must exist on the **same Docker endpoint managed by Portainer**. Do not assume the web-based stack editor builds the image. Build it on the endpoint first, then paste or import the Compose definition in Portainer and **remove the `build:` block**, leaving `image: repoharbor:local-test`. Bind paths refer to the Docker host, not the browser computer. No registry publication or automatic image availability is assumed.

If the image is built on another trusted machine, an operator may transfer it explicitly:

```sh
docker image save -o /tmp/repoharbor-local-test.tar repoharbor:local-test
# Transfer the archive securely to the destination Docker host.
docker image load -i /tmp/repoharbor-local-test.tar
```

These commands **do not publish** the image. Configure Portainer environment variables and read-only secrets, inspect the entire stack, and deploy only after reviewing the target endpoint. Specific NAS or Portainer UI versions have not been production-validated.

Before updating a deployment, verify the current backup state and an offline restore. Stop the service, build/import the intentionally chosen image, and recreate it with the **same persistent mounts**:

```sh
docker compose -f deploy/compose.example.yaml stop --timeout 300 repoharbor
docker compose -f deploy/compose.example.yaml up -d --no-build --force-recreate
```

SIGTERM prevents new Git work and stops the active Git process; unfinished work is reconciled on restart. The mutable `local-test` tag is not a reproducible release identifier. When rolling back, verify that the older version understands the existing state schema. Do not delete or reinitialize `data.json`, repository metadata, mirrors, or the lock file in an attempt to bypass an error.

## 7. Quarantine and troubleshooting

Only an initial clone with **durable proof of Git completion** and a fresh integrity check may become a valid mirror. A known interrupted initial clone can be journaled and moved to one preserved `quarantine/initial-clone` slot; a new clone begins no sooner than the next regular scheduled tick. Quarantined data is never automatically rotated or deleted.

For `manual_attention_required`, **stop the service before touching backup data**. Inspect metadata and the global journal and preserve both the partial and any existing quarantine. An operator may deliberately move conflicting content to a **unique, non-overwriting** archive directory under `<backup-root>/operator-archive/`, outside `manual/`; these archived bytes still count toward the global budget. Never overwrite a valid mirror, remove the lock file as a supposed unlock operation, or fabricate successful timestamps. If a pending quarantine slot has gone missing, restore those recorded bytes from a preserved archive or leave the case blocked. See [SECURITY.md](../docs/SECURITY.md) for the safety rules.

| Symptom | Safe checks |
|---|---|
| Invalid configuration | YAML indentation, `schema_version: 1`, supported fields, alias and credential names; optional feature flags must remain false. |
| Git access denied / token expired | Source permission, credential mode, token rotation; never put credentials into a remote URL or debug output. |
| TLS error | Server certificate and trusted CA bundle; never disable verification. |
| Mount / `EACCES` | Actual host filesystem, ACLs, UID/GID 10001, read-only secret bind; do not switch to a privileged container. |
| `ENOSPC` or a budget pause | Free space and every preserved directory, including quarantine and operator archives; do not mass-delete mirrors. |
| Process lock | Stop the other running instance. The OS releases the lock on process exit; the lock file's presence does **not** mean a lock is still held. |
| `repair_failed` / invalid JSON | Preserve data and investigate manually; missing objects can only be recovered if the source still supplies them. |

## 8. Offline restoration and acceptance

With Git installed on the restore host and read permission on a verified local mirror, clone into a **new destination directory**:

```sh
git clone --no-hardlinks \
  /srv/repoharbor/backups/manual/example-demo/repository.git \
  ./restored-example
```

This restore does not need access to the original server or its token. Git project files and ordinary Git history are available if the mirror is valid and the default branch exists. For a different existing branch, use `git clone -b <branch> --no-hardlinks ...`. An empty repository or missing default branch will not automatically produce a checked-out working tree. Never modify the mirror to perform a restore.

A mirror follows remote branch/tag deletion and force-pushes. It does **not** provide historical snapshots or preserve Git LFS payloads, submodule contents, issues, pull/merge requests, or hosting metadata. Plan separate protection for any of those requirements.

**Before production use**, accept the actual host mount, filesystem behavior, user permissions, chosen authentication mode, a real scheduled backup, recorded timestamps, container recreation, and an offline restore on the target NAS. Automated local tests and green CI are not a substitute for that acceptance test.
