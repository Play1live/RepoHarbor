# RepoHarbor 0.1.0 mit Docker Compose / Portainer

Diese Anleitung beschreibt das tatsächlich vorhandene Entwicklungsgrundgerüst. **Eine reale NAS-/Portainer-Abnahme ist noch nicht erfolgt.** Die Befehle für Start, Updates und Wiederherstellung sind Betreiberanweisungen, keine Behauptung eines bereits ausgeführten Deployments. Alle Quellen, Credential-Namen und Hostpfade in den Beispielen sind fiktiv und müssen bewusst angepasst werden.

## 1. Voraussetzungen und Host-Mount

- Linux-Docker-Endpunkt, Docker Compose v2 und BuildKit für den lokalen Image-Build. Portainer nur an einem Docker-Standalone-Endpunkt verwenden; Swarm-Deployment ist hier nicht beschrieben oder getestet.
- Das Image enthält Python 3.12, Git-CLI und CA-Zertifikate. Auf dem Docker-Host ist für den Containerbetrieb kein zusätzliches Python nötig; ein Host-Restore mit Git benötigt dort Git.
- Der Prozess läuft als UID/GID **10001:10001**, ohne besondere Capabilities, ohne HTTP-Listener und ohne Docker-Healthcheck. Die automatisierte Image-Abnahme betrifft Linux/amd64; andere Plattformen separat prüfen.
- Ein persistenter, vorhandener Backup-Bind-Mount auf einem verlässlichen lokalen Dateisystem mit flock/fsync/atomarem Rename. SMB/NFS sind als aktiver Datenbestand nicht konsistent garantiert.

**Zuerst den gewünschten Datenträger auf dem Host prüfen, erst danach Verzeichnisse anlegen.** Das Beispiel nimmt `/srv/repoharbor` als dedizierten Host-Mountpunkt an:

```sh
mountpoint -q /srv/repoharbor
findmnt --mountpoint /srv/repoharbor -o SOURCE,TARGET,FSTYPE,OPTIONS
df -h /srv/repoharbor
```

Die Ausgabe mit dem tatsächlich vorgesehenen Datenträger und seinen Mountoptionen vergleichen. Wenn die NAS-Struktur einen anderen Mountpunkt benutzt, diese Prüfung und alle Pfade entsprechend anpassen. Ein erfolgreiches `findmnt -T` kann sonst lediglich das Root-Dateisystem anzeigen. Ein Container kann einen falsch vorbereiteten Hostordner erfolgreich als Bind-Mount sehen: **Das beweist keinen korrekten NAS-Mount.** Die Anwendung prüft den Container-Mount, nicht die Identität des Host-Datenträgers.

Auf dem bereits bestätigten Datenträger dedizierte neue Verzeichnisse vorbereiten, beispielsweise mit Linux-Werkzeugen:

```sh
sudo install -d -o 10001 -g 10001 -m 0750 /srv/repoharbor/backups
sudo install -d -o 10001 -g 10001 -m 0700 /srv/repoharbor/secrets
sudo -u '#10001' -g '#10001' test -w /srv/repoharbor/backups
```

NAS-ACLs und UID-Mapping müssen dieselben Rechte erlauben. Vorhandene Backup-Bestände zunächst prüfen; keine pauschale rekursive Neuinitialisierung oder Rechteänderung als Fehlerbehebung durchführen. Der Dienst darf Metadaten und Mirrors im Backup-Root schreiben, Secrets dagegen nur lesen. Der Beispiel-Stack verwendet `create_host_path: false`, damit fehlende Hostpfade nicht stillschweigend angelegt werden.

## 2. Image bauen und Stack vorbereiten

Alle folgenden Compose-Befehle werden aus dem **Projekt-Root** ausgeführt:

```sh
docker compose -f deploy/compose.example.yaml build
docker run --rm repoharbor:local-test --version
```

Die CLI meldet `0.1.0`. `repoharbor:local-test` ist ausschließlich ein lokaler Testtag; ein fertiges Registry-Release wird nicht vorausgesetzt. Der Build benötigt Netzwerkzugriff auf die gepinnten Basis-/Python-Pakete und Debian-Paketquellen. Falls ein vertrauenswürdiger Build-Proxy ein eigenes CA-Bundle benötigt, unterstützt der Dockerfile optional `--secret id=build_ca,src=/path/to/trusted-ca.pem` beim `docker build`; TLS-Prüfung nicht abschalten.

[compose.example.yaml](compose.example.yaml) ist die Ausgangsdefinition. Hostpfade, Quelle, Zeitplan und Budgets vor dem Start anpassen. `init: true` verwaltet Kindprozesse und ist keine Dateninitialisierungsvariable. `restart: unless-stopped`, `stop_grace_period: 5m`, unprivilegierter Benutzer und die Sicherheitsoptionen bleiben erhalten. Keine Ports, `healthcheck:`-Einträge oder `INITIALIZE_*`-Variablen hinzufügen.

## 3. Konfiguration und Credential-Modi

Komplexe Einstellungen stehen im mehrzeiligen YAML-Wert **`REPOHARBOR_CONFIG`**, die Zeitzone in `TZ`. Die Anwendung validiert beim Start; Änderungen benötigen einen Neustart beziehungsweise ein Stack-Redeploy. Es gibt kein Hot-Reload.

Das vorhandene Compose-Beispiel benutzt `REPOHARBOR_TOKEN_SOURCE: file`, eine leere Credential-Liste und eine öffentliche fiktive Quelle. Dafür muss das gemountete Secrets-Verzeichnis zwar existieren, aber keine Token-Datei enthalten. Ohne bewusst hinterlegte manuelle Quellen (`manual_git_urls: []`) werden keine Repositories gesichert.

### Empfohlen: `file`

Für eine private manuelle Quelle beispielsweise den Inhalt von `REPOHARBOR_CONFIG` durch Folgendes ersetzen; `TZ: Europe/Berlin` und `REPOHARBOR_TOKEN_SOURCE: file` bleiben im Environment erhalten:

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

Der Dienst erwartet `/run/secrets/repoharbor/example_primary`, im Beispiel also die Hostdatei `/srv/repoharbor/secrets/example_primary`. Sie enthält nur den Token, keine URL und keine YAML-Anweisung. Mit einem geschützten Editor anlegen oder bewusst rotieren, beispielsweise:

```sh
sudoedit /srv/repoharbor/secrets/example_primary
sudo chown 10001:10001 /srv/repoharbor/secrets/example_primary
sudo chmod 0400 /srv/repoharbor/secrets/example_primary
```

Der Secrets-Bind-Mount bleibt read-only. Keine Tokenwerte in Git, Screenshots, Logs oder Beispiel-Stacks übernehmen. Trailing CR/LF wird beim Lesen entfernt; fehlende oder leere Credentials lassen die Validierung scheitern. Für Rotation den Token geschützt ändern und den Dienst neu starten.

### Alternativ: `env`

Im bestehenden Service den gesamten `environment`-Block durch dieses Beispiel ersetzen und **den Secrets-Bind-Mount entfernen**. Den Backup-Bind-Mount und die übrigen Service-Einstellungen beibehalten:

```yaml
environment:
  TZ: Europe/Berlin
  REPOHARBOR_TOKEN_SOURCE: env
  REPOHARBOR_TOKEN_EXAMPLE_PRIMARY: ${REPOHARBOR_TOKEN_EXAMPLE_PRIMARY:?Token geschützt bereitstellen}
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

Die referenzierte Variable vor Compose über einen geschützten lokalen Mechanismus bereitstellen beziehungsweise im Portainer-Stack als Environment-Variable setzen; keinen echten Wert in diese Datei schreiben. Environment-Secrets sind für berechtigte Docker-/Portainer-Administratoren einsehbar. `env` und `file` sind für konfigurierte Credentials gegenseitig ausschließend: Eine zugleich gemountete gleichnamige Secret-Datei führt zu einem Fehler. Beim Wechsel auf `file` die entsprechenden Token-Envs entfernen.

Mehrere Credentials können namentlich gelistet werden; jede Quelle referenziert optional genau eines. Beispielname `example_primary` wird auf `REPOHARBOR_TOKEN_EXAMPLE_PRIMARY` abgebildet. Weitere Details zu Namen, URL-Normalisierung und gültigen Feldern stehen in der [Konfigurationsreferenz](../docs/CONFIGURATION.md). Für eigene HTTPS-CAs kann `SSL_CERT_FILE` auf ein zusätzlich read-only gemountetes vertrauenswürdiges CA-Bundle zeigen; Zertifikatsprüfung bleibt aktiv.

## 4. Zeitplan, Speicher und Status

`0 3 * * *` bedeutet täglich 03:00 in `TZ`. Fünf Cron-Felder unterstützen Listen, Bereiche und Schritte. Sind Monatstag und Wochentag eingeschränkt, gilt ODER. Nicht existente DST-Minuten entfallen; die zweite Ausführung einer wiederholten lokalen Minute wird ausgelassen. Neustarts holen keine Termine nach. Läuft noch ein Durchgang, wird der neue Termin übersprungen und unmittelbar im zentralen Journal gezählt.

Budgets werden in GiB angegeben (1 GiB = 1073741824 Bytes). `null` deaktiviert bewusst das jeweilige Größenlimit; `0` blockiert neue Arbeit. Ein Override gilt für einen stabilen Alias. Alle Daten im Backup-Root zählen global, auch ausgeschlossene Repositories, Journal, Metadaten, Partials und Quarantänen; das Repo-Budget zählt den gesamten eigenen Repo-Ordner.

**Das sind weiche Speicherbudgets, keine Dateisystemquotas.** Bei `finish` darf ein begonnener Transfer das Größenbudget überschreiten; bei `abort` wird eine erkannte Überschreitung unterbrochen. Physische Freiplatzreserve hat Vorrang. Während Transfers wird Kapazität höchstens alle fünf Sekunden geprüft, Stop-Signale werden schneller gepollt. Ein volles Dateisystem kann ohne echte Quotas nicht garantiert verhindert werden. Vor und nach Operationen wird neu gemessen; freier gewordene Kapazität wird beim nächsten regulären Termin berücksichtigt. Die Anwendung löscht keine gültigen Backups zur Platzgewinnung.

| Backup-Zustand | Bedeutung / nächste Handlung |
| --- | --- |
| `updated` / `up_to_date` | Erfolgreicher neuer/geänderter Mirror beziehungsweise unveränderte erfolgreiche Prüfung. |
| `global_limit_paused` / `repo_limit_reached` / `FREE_SPACE_RESERVE` | Neue Operationen sind wegen Kapazität ausgelassen; Speicher/Budget prüfen, nächsten Termin abwarten. |
| `repair_paused` mit `repair_deferred` | Notwendige Reparatur wartet auf Kapazität; kein verbrauchter Fehlversuch und kein endgültiger Reparaturfehler. |
| `interrupted` | Unterbrochene oder noch nicht abschließend bestätigte Operation; Recovery bewertet den Bestand. |
| `repair_failed` | Automatische Reparatur fehlgeschlagen oder nicht möglich; Daten bewahren und Ursache manuell prüfen. |
| `manual_attention_required` als Zusatzfeld | Unbekanntes Partial, volle Quarantäne oder Konflikt blockiert weitere Downloads; siehe sichere Betreiberaktion. |

Auswahl, Quellenverfügbarkeit und Integrität sind separate Metadaten-Dimensionen. Ein lokaler Schreibfehler beweist keinen Verlust der Quelle. `last_checked_at` bezeichnet die letzte erfolgreiche Remote-Prüfung, `last_updated_at` nur einen erfolgreich neuen/geänderten Mirror. Bei Skip oder Fehler wird kein Erfolg nachgetragen. Ein repariertes Objekt allein bedeutet keine neue Git-Referenzänderung.

## 5. Validieren, starten und kontrollieren

```sh
docker compose -f deploy/compose.example.yaml config --quiet
docker compose -f deploy/compose.example.yaml run --rm --no-deps repoharbor config validate
```

`config --quiet` vermeidet die Ausgabe aufgelöster Secrets. `config validate` liest konfigurierte Credentials, kontaktiert aber keine Git-Quelle und schreibt keine Backup-Daten. **Beide Prüfungen ersetzen keine produktive Backup-Abnahme.** Sie prüfen weder den richtigen Host-Datenträger noch die Offline-Wiederherstellbarkeit eines tatsächlich gesicherten Repositories.

Erst nach korrekter Host-/Secret-Konfiguration bewusst starten:

```sh
docker compose -f deploy/compose.example.yaml up -d --no-build
docker compose -f deploy/compose.example.yaml logs --tail 100 repoharbor
```

Der erste Start legt nur sicher fehlende Dateien an, inventarisiert bestehende Daten und führt nötige Recovery aus. Anlassbezogene Recovery kann einen beschädigten bestehenden Mirror reparieren; sie ist kein Nachholen einer regulären Sicherung. Die Meldung `ready; waiting for future schedule` bedeutet, dass der Scheduler wartet, nicht dass bereits ein Backup erfolgreich war. Es gibt keinen öffentlichen `run-now`-Befehl. Für eine erste beaufsichtigte Abnahme kann bewusst ein kleines Testrepository mit `* * * * *` gewählt werden; danach wieder den gewünschten Zeitplan setzen und neu starten.

Zustand mit dem Containerbenutzer lesen, ohne Host-Dateirechte zu lockern:

```sh
docker compose -f deploy/compose.example.yaml exec -T repoharbor python -c \
  'import json; from pathlib import Path; d = json.loads(Path("/backups/data.json").read_text()); print(d["global_storage"]); print(d["scheduler"])'
```

Pro Quelle liegen URL, Zeiten, Fehlerzähler, Speicher- und Reparaturstatus in `/backups/manual/<alias>/metadaten.json`, neben dem Bare-Mirror `repository.git/`. Fehlerhistorien sind auf 20 Einträge begrenzt, der Gesamtzähler bleibt erhalten. Containerstatus `running` und Docker-Restart-Policy sagen nichts über die Aktualität dieser Backups aus.

## 6. Portainer und kontrollierte lokale Updates

Das Image muss auf **demselben Docker-Endpunkt** vorhanden sein, den Portainer verwaltet. Der Web-Stackeditor wird hier nicht als Build-System vorausgesetzt: zuerst außerhalb des Editors bauen, dann im Editor die Compose-Definition verwenden und den `build:`-Block entfernen. `image: repoharbor:local-test` bleibt erhalten. Bind-Pfade beziehen sich auf den Docker-Endpunkt, nicht auf den Browser-PC. Keine automatische Image-Veröffentlichung oder Registry-Pull-Verfügbarkeit annehmen.

Bei getrenntem Build-Rechner kann ein Betreiber ein lokales Image-Archiv bewusst übertragen und am Ziel importieren:

```sh
docker image save -o /tmp/repoharbor-local-test.tar repoharbor:local-test
# Datei geschützt zum Ziel-Docker-Endpunkt übertragen.
docker image load -i /tmp/repoharbor-local-test.tar
```

Diese Schritte veröffentlichen kein Image; sie ersetzen keine Prüfung des Zielsystems. In Portainer die Environment-Variablen beziehungsweise geschützten Secret-Bind-Mounts setzen und die gesamte Definition vor einer bewussten Bereitstellung kontrollieren. Konkrete NAS-Menüs oder eine Portainer-Version wurden nicht abgenommen.

Vor einem lokalen Update vorhandene Daten und einen Offline-Restore prüfen. Den Dienst stoppen, das bewusst ausgewählte Image bauen/importieren und mit denselben Bind-Mounts neu erstellen:

```sh
docker compose -f deploy/compose.example.yaml stop --timeout 300 repoharbor
docker compose -f deploy/compose.example.yaml up -d --no-build --force-recreate
```

Stop sendet SIGTERM; die Anwendung verhindert neue Git-Kommandos und beendet den laufenden Git-Prozess kontrolliert. Unfertige Arbeit wird bei Wiederstart reconciliert. Der lokale Testtag ist veränderlich und kein unveränderliches Release-Artefakt. Bei einem Rollback vorher prüfen, ob der ältere Code die vorhandenen Zustandsfelder unterstützt. Unbekannte/defekte Zustände werden nicht durch Löschen oder Neuinitialisierung repariert; `data.json`, Metadaten und Mirrors erhalten.

## 7. Quarantäne und Troubleshooting

Nur ein gesundes Partial mit dauerhaft nachgewiesenem Git-Abschluss darf zum gültigen Mirror werden. Ein nachweislich unterbrochener Erstclone kann journalgesichert in den einen Platz `quarantine/initial-clone` verschoben werden. Ein neuer Clone folgt frühestens am nächsten regulären Termin, sofern das Budget reicht. Keine automatische Rotation oder Löschung dieser Daten.

Bei `manual_attention_required` den Dienst stoppen, Journal und Metadaten prüfen und Partial sowie vorhandene Quarantäne erhalten. Eine bewusste, nicht überschreibende Archivierung in einen eindeutigen Betreiberordner unter `<backup-root>/operator-archive/` außerhalb von `manual/` ist möglich; diese Daten zählen global weiter. Niemals einen gültigen Mirror ersetzen, die Lockdatei als vermeintliches Entsperren löschen oder JSON-Erfolgszeitpunkte setzen. Fehlende pending-Quarantänedaten aus einem erhaltenen Archiv wiederherstellen oder den Fall blockiert manuell untersuchen. Die vollständigen Schritte stehen in [SECURITY.md](../docs/SECURITY.md).

| Problem | Sichere Prüfung |
| --- | --- |
| Konfiguration ungültig | YAML-Einrückung, `schema_version: 1`, bekannte Felder, Alias- und Credential-Namen prüfen; alle optionalen Features bleiben aus. |
| Git-Zugriff / Token abgelaufen | Leserechte, Credential-Modus und Tokenrotation prüfen; keine Credentials in URLs oder Debug-Ausgaben einsetzen. Der aktuelle Dienst macht keine Provider-API-Discovery. |
| TLS-Fehler | HTTPS-Endpunkt und vertrauenswürdiges CA-Bundle prüfen; Verifikation nicht deaktivieren. |
| Mount / `EACCES` | Host-Datenträger, ACLs, UID/GID 10001 und Read-only-Secret-Mount prüfen; nicht auf einen Root-Container ausweichen. |
| `ENOSPC` / Reserve / Budget | Freien Platz und alle bewahrten Daten einschließlich Quarantänen prüfen; keine automatische oder pauschale Mirror-Löschung. |
| Prozesssperre | Tatsächliche zweite Instanz stoppen. Das OS gibt den Lock nach Prozessende frei; die Existenz der Lockdatei bedeutet nicht „gesperrt“. |
| `repair_failed` / kaputtes JSON | Daten erhalten und manuell untersuchen. Fehlende Objekte sind nur reparierbar, soweit die Quelle sie noch bereitstellt. |

## 8. Offline-Wiederherstellung und Abnahme

Mit installiertem Host-Git und passenden Leserechten in ein **neues separates Zielverzeichnis** klonen:

```sh
git clone --no-hardlinks \
  /srv/repoharbor/backups/manual/example-demo/repository.git \
  ./restored-example
```

Der Restore braucht weder Quellserver noch Zugangstoken. Dateien und normale Git-Historie sind nutzbar, sofern der Mirror intakt ist und ein gültiger Standardbranch existiert. Für einen anderen vorhandenen Branch kann `git clone -b <branch> --no-hardlinks ...` benutzt werden. Ein leeres Repository oder fehlender Defaultbranch erzeugt nicht automatisch ausgecheckte Projektdateien. Niemals in den Mirror zurückschreiben oder dessen Remote auf das Restore-Ziel umstellen.

Mirror-Prune und Force-Pushes übernehmen bewusst Änderungen der Quelle; dies ist kein historischer Backup-Snapshot. LFS-Payloads, Submodul-Inhalte, Issues, Pull-/Merge-Requests und Hosting-Artefakte fehlen. Zusätzlich benötigte Rückgriffspunkte müssen außerhalb von RepoHarbor geplant werden.

Vor produktiver Nutzung sind Host-Mount, Rechte, beide gewählten Zugangspfade, ein echter planmäßiger Lauf, Zeitstempel, Container-Neuerstellung und ein Offline-Restore auf dem konkreten NAS separat abzunehmen. Lokale Tests und eine grüne CI sind dafür notwendige technische Nachweise, keine behauptete NAS-Abnahme.
