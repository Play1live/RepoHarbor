# RepoHarbor

**Automated Git Repository Backup · Entwicklungsstand 0.1.0**

RepoHarbor ist ein Python-Dienst für Git-Mirror-Backups in einem Linux-Container. Er sichert ausdrücklich konfigurierte HTTPS-Quellen auf einem persistenten lokalen Dateisystem, etwa direkt auf einem NAS. **0.1.0 ist ein Entwicklungsstand, kein veröffentlichtes oder für produktiven NAS-Betrieb abgenommenes Release.** Das Image wird derzeit selbst gebaut.

## Was dieser Stand leistet

- Vollständige Bare-Mirrors mit `git clone --mirror`, Referenzvergleich und direkten Updates mit atomarem Fetch und Prune; keine zusätzliche Arbeitskopie im Backup.
- Manuelle Quellen mit stabilen Aliasnamen, konservativer URL-Deduplizierung und benannten Credentials aus genau einem Modus: `env` oder `file`.
- Fünffeldriger Cron mit Zeitzone/DST, ohne Nachholen verpasster Termine und ohne parallele oder aufgestaute Sicherungsdurchgänge.
- Weiche Global-/Repo-Speicherbudgets, Ausnahmen und physische Freiplatzreserve. Ausgeschlossene Quellen, temporäre Daten und Quarantänen zählen weiter mit.
- Atomare Zustandsdateien, exklusive Prozesssperre, Crash-Reconciliation, begrenzte Reparatur ohne Überschreiben der Git-Referenzen und begrenzte Partial-Quarantäne.
- Unprivilegierter Docker-Betrieb ohne Web-Listener oder Healthcheck; lokale Git-/HTTPS-, Fault-Injection-, Security- und Docker-Tests mit CI-Gates.

GitHub-/GitLab-Repositories können als manuelle HTTPS-Quellen eingetragen werden. **Automatische Provider-Discovery, Anbieter-ID-Deduplizierung, Metadatenfilter, automatische Rename-/Transfer-Erkennung und Web-Dashboard sind noch nicht implementiert.** Optionale Feature-Flags akzeptieren in diesem Stand nur `false`; es gibt keinen zusätzlichen manuellen Backup-Trigger oder Restore-Assistenten.

## Einstieg

Für den Containerbetrieb werden ein Linux-Docker-Endpunkt, Docker Compose v2 und ein vorhandener, korrekt gemounteter und beschreibbarer Backup-Pfad benötigt. Python und Git sind im Image enthalten. Die automatisierten Docker-Prüfungen laufen auf Linux/amd64; andere Architekturen und reale NAS-/Portainer-Installationen sind separat abzunehmen.

Aus dem Projekt-Root lässt sich zunächst ohne Backup-Mount bauen und die CLI prüfen:

```sh
docker build -t repoharbor:local-test .
docker run --rm repoharbor:local-test --version
docker run --rm -e REPOHARBOR_TOKEN_SOURCE=env \
  -e 'REPOHARBOR_CONFIG=schema_version: 1' \
  repoharbor:local-test config validate
```

`repoharbor:local-test` ist ein lokales Testimage, kein zugesagtes Registry-Image. Vor einem Dienststart die [Deployment-Anleitung](deploy/README.md) durcharbeiten und [deploy/compose.example.yaml](deploy/compose.example.yaml) anpassen. Die Beispielquelle `code.example.org` ist fiktiv. Nach dem Start initialisiert beziehungsweise prüft der Dienst den lokalen Zustand und wartet auf den nächsten zukünftigen Cron-Termin.

`config validate` prüft Konfiguration und konfigurierte Credentials, startet aber keinen Git-Download und schreibt keine Runtime-Daten. Auch `docker compose config --quiet` ersetzt keinen echten Backup- und Restore-Test.

## Aufbau und Zustand

Konfiguration/Credentials → Scheduler → sequenzielle Backup-Engine/Git-CLI. Persistenz, Recovery und Speicherüberwachung sind getrennte Module. Eine OS-Dateisperre verhindert eine zweite aktive Instanz am selben Backup-Root.

```text
src/repoharbor/       Konfiguration, Credentials, CLI, Scheduler, Engine,
                     Git, Speicher, Persistenz und Recovery
/backups/data.json   Zentrales Lauf-/Operationsjournal und Schedulerzustand
/backups/manual/<alias>/
  metadaten.json     Identität, Auswahl, Zeiten, Fehler, Speicher und Integrität
  repository.git/   Gültiger Bare-Mirror
  quarantine/       Höchstens ein automatisch bewahrter Erstclone je Quelle
deploy/             Compose-Beispiel und Betriebsanleitung
docs/               Konfigurations-, Sicherheits- und Testreferenz
tests/              Unit-, Git-/HTTPS-, Crash- und Docker-Prüfungen
scripts/            Separates Coverage-Gate
```

Beim Erstclone kann vorübergehend `repository.git.partial/` existieren. Ein nur strukturell gesundes Partial beweist keinen vollständigen Download. Unklare Fälle bleiben bewahrt und werden als `manual_attention_required` sichtbar; Betreiberaktionen beschreibt die [Sicherheitsreferenz](docs/SECURITY.md).

## Datensicherheit und Grenzen

Ein Mirror enthält Git-Objekte und spiegelbare Referenzen einschließlich normaler Commit-Historie. **Er ist kein historischer Backup-Snapshot:** Erfolgreiches Spiegeln übernimmt gelöschte Branches/Tags und Force-Pushes; unerreichbare alte Objekte können später verschwinden. Ein Backup ersetzt keine zusätzliche Strategie gegen unerwünschte Historienänderungen.

Nicht gesichert werden Git-LFS-Payloads, eigenständige Submodul-Inhalte, Issues, Pull-/Merge-Requests, Hosting-Artefakte oder nie gepushte lokale Daten. Ausgeschlossene oder nicht erreichbare Quellen werden lokal nicht automatisch gelöscht. Nicht mehr verfügbare Git-Objekte können eine Reparatur unmöglich machen.

Die Dateisystemgarantien für flock, fsync und atomare Umbenennung müssen erfüllt sein; SMB/NFS werden als aktiver Backup-Root nicht als konsistent garantiert. Ein Container-Bind-Mount beweist **nicht**, dass der gewünschte NAS-Datenträger auf dem Host tatsächlich gemountet ist. Speicherbudgets sind keine harten Quotas. Secrets gehören weder in URLs noch ins Repository; `file` mit geschütztem Read-only-Mount ist die empfohlene Credential-Variante.

## Dokumentation und Entwicklung

- [Docker-/Portainer-Betrieb und Offline-Restore](deploy/README.md)
- [Konfigurationsschema, Defaults und Auswahl manueller Quellen](docs/CONFIGURATION.md)
- [Sicherheit, Journal, Recovery und sichere Betreiberaktionen](docs/SECURITY.md)
- [Tests, Coverage-Gates und CI](docs/TESTING.md)
- [Changelog des Entwicklungsstands](Changelog.md)

Für lokale Entwicklung: Python 3.12+ und Git. Die CI prüft Python 3.12 und 3.13. Mindestabdeckung: 90 % Lines, 85 % Branches und 95 % Lines je kritischem Modul; die exakten Befehle stehen in der Testreferenz. Releases, Image-Publishing, Merge und produktive Bereitstellung benötigen jeweils eine gesonderte Freigabe.
