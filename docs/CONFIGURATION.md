# Configuration contract (foundation, schema 1)

This development version supports manual HTTPS sources, not provider discovery or a dashboard. Run `repoharbor --version`, `repoharbor config validate`, or `repoharbor` (service). Validation reads the configured credentials but does not contact remotes or write backup data. Service startup reconciles local state, then waits for future cron minutes.

Use `deploy/compose.example.yaml` as the stack definition. Before deploying, create the actual persistent backup and secret directories with appropriate UID/GID permissions (image defaults to 10001:10001). Replace the fictional bind paths and source URL. Bind mounts must already exist; the service refuses an unmounted `/backups` in Docker. NAS/Portainer operation requires separate acceptance testing. There is no healthcheck, UI listener, database or initialization flag.

`REPOHARBOR_CONFIG` contains YAML with required integer `schema_version: 1`. Unknown keys fail validation. Supported keys:

| Key | Contract |
| --- | --- |
| `schedule` | Five cron fields, default `0 3 * * *`; timezone comes from `TZ` (default UTC). |
| `credentials` | List of unique lowercase credential names. |
| `manual_git_urls` | List of objects with required stable `alias` and HTTPS `url`, optional `web_url` and `credential`. |
| `storage` | `global_gib`, `default_repo_gib`, `overrides` (alias to GiB), `during_transfer` (`finish` or `abort`), `min_free_gib` (default 2). |
| `recovery` | `automatic_repair` (default true), `max_repair_attempts_per_incident` (integer 1–3, default 3). |
| `features` | `web_ui`, `integrity_check`, `auto_retry`, `manual_run`, `restore_helper`, `extended_logging`: only false is supported in this development step. |

Budget defaults are unlimited (`null`), except free-space reserve. `0` prohibits starting new operations; one GiB is exactly 1073741824 bytes, fractional values truncate to whole bytes. Overrides replace the repository default. Usage includes excluded sources, journal, metadata and partials. These are soft limits; `finish` permits overshoot. Physical reserve or `abort` can stop an active transfer. Re-measurement at subsequent scheduled runs automatically resumes eligible work. No data is deleted to free space.

Set `REPOHARBOR_TOKEN_SOURCE` explicitly to `env` or `file`, even for an empty credential list. For credential `example_primary`, env mode reads `REPOHARBOR_TOKEN_EXAMPLE_PRIMARY`; file mode reads `/run/secrets/repoharbor/example_primary`. Secret files are read-only mounted, regular UTF-8 files, with trailing CR/LF removed. A configured secret present in both modes is rejected. Names with `-` map to `_` in env keys; resulting collisions are rejected. Rotation requires a service restart. Optional `REPOHARBOR_SECRETS_DIR` and `REPOHARBOR_BACKUP_ROOT` must be absolute paths.

URLs normalize scheme/hostname and default port, and remove trailing slashes. Path case and `.git` suffix remain significant. Identical normalized URLs use the first alias; conflicting credentials fail. Aliases are stable identities and backup paths (`manual/<alias>`); changing an existing alias URL, or reassigning its URL to another alias, fails closed. Removing a source marks it excluded and keeps its mirror. Adding back the same alias/URL resumes at the next scheduled run. Provider-ID deduplication and rename/transfer handling are future work.

Cron supports stars, numbers, ranges, lists and positive steps. Sunday is 0 or 7. Restricted month-day and weekday use OR semantics. Missing DST wall-clock minutes are skipped; the second occurrence of a repeated minute is skipped. No downtime replay; a tick during an active run is skipped without queuing. Mirrors follow remote deletions and force pushes; they are not historical snapshots.

Restore offline with `git clone --no-hardlinks /backups/manual/example-demo/repository.git ./restored-demo`. Git objects and normal history are present; LFS payloads, submodule contents and hosting metadata are outside scope.
