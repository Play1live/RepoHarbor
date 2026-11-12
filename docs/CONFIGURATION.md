# Configuration reference (schema 1)

RepoHarbor `0.1.0` is an **unreleased development build**. It supports manually configured HTTPS Git remotes; provider API discovery, dashboard access, and additional optional features are not yet implemented.

The canonical service configuration is a YAML document supplied through **`REPOHARBOR_CONFIG`**. The image also accepts `TZ`, `REPOHARBOR_TOKEN_SOURCE`, `REPOHARBOR_BACKUP_ROOT`, and `REPOHARBOR_SECRETS_DIR`. Unknown YAML keys are rejected, rather than silently ignored. The schema requires the integer `schema_version: 1`.

For installation, host mounts, and Portainer instructions, see the [deployment guide](../deploy/README.md). For the supported source and storage behavior, refer to the actual [`deploy/compose.example.yaml`](../deploy/compose.example.yaml) and application source.

## Configuration schema

| Key | Supported value and default |
|---|---|
| `schema_version` | Required integer `1`. Other versions are rejected. |
| `schedule` | Five-field cron expression; default `0 3 * * *`. |
| `credentials` | List of unique lowercase credential identifiers; default `[]`. |
| `manual_git_urls` | List of manual HTTPS Git sources; default `[]`. Each requires `alias` and `url`; `web_url` and `credential` are optional. |
| `storage.global_gib` | Soft global size budget in GiB, or `null` for unlimited (default). |
| `storage.default_repo_gib` | Soft budget per repository in GiB, or `null` for unlimited (default). |
| `storage.overrides` | Mapping from configured source aliases to replacement per-repository GiB budgets; default `{}`. |
| `storage.during_transfer` | `finish` (default) or `abort`. |
| `storage.min_free_gib` | Minimum physical free-space reserve; default `2`. Cannot be `null`. |
| `recovery.automatic_repair` | Boolean; default `true`. |
| `recovery.max_repair_attempts_per_incident` | Integer from `1` through `3`; default `3`. |
| `features` | Recognizes `web_ui`, `integrity_check`, `auto_retry`, `manual_run`, `restore_helper`, and `extended_logging`; every specified value must be `false` in this build. |

An example configuration with invented sources and values:

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

`REPOHARBOR_CONFIG` can be provided in a Docker Compose multiline environment entry as in the [example stack](../deploy/compose.example.yaml). Use `repoharbor config validate` to validate the configuration and referenced credentials; this command does **not** download from a remote or create backup state.

## Environment and credential modes

| Variable | Behavior |
|---|---|
| `REPOHARBOR_TOKEN_SOURCE` | **Required:** exactly `env` or `file`, even if no credentials are configured. |
| `TZ` | IANA timezone for cron; default `UTC`. Example: `Etc/UTC`. |
| `REPOHARBOR_BACKUP_ROOT` | Absolute backup-root path; default `/backups`. |
| `REPOHARBOR_SECRETS_DIR` | Absolute secret-file directory; default `/run/secrets/repoharbor`. |
| `SSL_CERT_FILE` | Optional path to a trusted custom CA bundle for private HTTPS servers; TLS verification remains active. |

For a configured credential named `example_primary`:

- In **`env` mode**, the token is read from `REPOHARBOR_TOKEN_EXAMPLE_PRIMARY`.
- In **`file` mode**, the token is read from the regular UTF-8 file `/run/secrets/repoharbor/example_primary`, mounted read-only. Its trailing CR/LF is stripped.

A configured secret found in both the chosen and opposite modes is rejected. Credential names use lowercase letters, digits, `_`, and `-`, and must be unique. Because `-` and `_` map to the same environment variable name, ambiguous names are rejected. Empty or malformed credential values are rejected. Tokens are never intended to be embedded in URLs or persisted in metadata. Credential rotation requires a restart.

## Source identity, selection, and scope

Each `manual_git_urls` entry needs a stable `alias` (for example `example-demo`) and a credential-free `https://` Git URL. The optional `web_url` describes the repository web page; it does not change the Git transport URL.

Source URLs are normalized conservatively: scheme/host case, default HTTPS port, and trailing slashes are normalized; path case and the `.git` suffix remain significant. Query parameters, URL-embedded username/password, unsupported protocols, and suspicious paths are rejected.

Identical normalized URLs are deduplicated using the **first** alias, provided credentials do not conflict. Aliases identify persistent directories under `manual/<alias>`. Reassigning an existing alias to a different URL, or its URL to another alias, fails closed. Removing a source from the configuration marks the existing repository as excluded; it does **not** delete the mirror. Restoring the same alias/URL makes it eligible again on the next scheduled run.

Provider-owned immutable repository IDs, organization/collaborator discovery, property filters, and automatic rename/transfer handling remain future features. The current manual alias is not a provider ID.

## Scheduling and storage behavior

Cron uses five fields (`minute hour day-of-month month day-of-week`) and accepts stars, values, lists, ranges, and positive steps. Sunday is `0` or `7`. Restricted day-of-month **and** day-of-week fields use traditional **OR** semantics. An IANA timezone determines local firing times. Missing DST minutes are skipped; a repeated local minute fires at most once. Missed execution times during downtime are not replayed, and ticks during an active backup are skipped without a queue.

Budgets use binary GiB (`1 GiB = 1,073,741,824 bytes`); fractional inputs are truncated to whole bytes. `0` means no new transfer may start. An override replaces the default per-repository budget; it does not add to it. Global usage includes **all** backup-root data, including excluded sources, journals, metadata, partials, and quarantines.

With the default `during_transfer: finish`, size budgets are evaluated before/after operations, but a running transfer is allowed to exceed them. The physical reserve may interrupt the transfer. With `abort`, configured size budgets are also checked during transfer. Stop signals are polled approximately every 100 milliseconds; capacity is checked at most once every five seconds during Git operations. All measurements are point-in-time, **not hard quotas**, and eligibility is checked afresh on future scheduled runs. No existing backup is automatically deleted to free space.

## Runtime state and recovery

- `data.json` is the central, durable run/operation journal and scheduler state.
- `manual/<alias>/metadaten.json` contains repository identity and selection, source availability, backup status, timestamps, storage measurements, errors, and integrity data. It is **outside** the Git mirror.
- `manual/<alias>/repository.git/` is the valid bare mirror. A partial first clone can exist as `repository.git.partial/`; an interrupted partial may be preserved in `quarantine/initial-clone/`.

| State | Meaning |
|---|---|
| `updated` / `up_to_date` | Successful changed mirror or successful no-op remote check. |
| `global_limit_paused`, `repo_limit_reached`, `FREE_SPACE_RESERVE` | A new operation was withheld by a capacity condition. |
| `repair_paused` + `repair_deferred` | Repair deferred for storage, without consuming a failed attempt; reevaluated at the next regular run. |
| `repair_failed` | Genuine exhausted repair attempts or an unrecoverable/unavailable repair condition. |
| `interrupted` | Operation was interrupted or not proven complete. |
| `manual_attention_required` | Data-preservation conflict, unknown partial, or occupied/missing quarantine requiring explicit operator action. |

`last_attempt_at`, `last_checked_at`, and `last_updated_at` have different meanings; skipped or failed operations must not create a false successful check or update. Repairs do not themselves imply a new Git reference update. An exhausted genuine repair does not become `repair_paused` simply because free space is also low.

For controlled recovery and operator actions, read [SECURITY.md](SECURITY.md). An offline restore from an intact mirror is performed with Git:

```sh
git clone --no-hardlinks /backups/manual/example-demo/repository.git ./restored-example
```

This restores regular Git objects and refs, not LFS payloads, separate submodules, issues, or hosting metadata. See the [deployment guide](../deploy/README.md) and [changelog](../Changelog.md). No published image or production NAS acceptance is implied.
