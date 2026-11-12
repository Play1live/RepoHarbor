# Changelog

## 0.1.0 — Entwicklungsstand, nicht veröffentlicht

- Manuelle HTTPS-Git-Mirror-Backups mit benannten Env-/File-Credentials, Cron-Zeitplanung ohne Backfill/Overlap sowie weichen Global-/Repo-Speicherbudgets und Freiplatzreserve.
- Atomare Zustandsdateien, Prozesssperre, Crash-Recovery, begrenzte nicht-destruktive Reparatur und Partial-Quarantäne; kapazitätsbedingt verschobene Reparaturen werden als `repair_paused` statt als endgültig fehlgeschlagen angezeigt. Vorhandene Daten bleiben erhalten.
- Unprivilegierter Docker-Dienst mit CLI, Compose-/Portainer-Anleitung, Offline-Restore und automatisierten Test-/Security-/Coverage-Gates. Provider-Discovery und Dashboard fehlen; keine Snapshots, LFS-/Submodul-Payloads oder Hosting-Metadaten. Reale NAS-/Portainer-Abnahme und Release-Freigabe stehen aus.
