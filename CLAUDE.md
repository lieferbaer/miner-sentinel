# MinerSentinel (Fork lieferbaer) – Hinweise für Claude Code

**Vor jeder Arbeit `docs/UEBERGABE-CLAUDE-CODE.md` vollständig lesen.** Dort stehen Infrastruktur, Geräte-Fakten,
alle bisherigen Entscheidungen mit Gründen und die Fehlerhistorie.

Wichtigste Regeln (Details in der Übergabe, Abschnitt 1):
- Deutsch, sehr kurz, „MinerGerät“ statt „Worker“, keine ungefragten Extras.
- Vor größeren Änderungen Plan erklären und Freigabe abwarten.
- Erst prüfen, dann behaupten.
- Takt-Messungen startet nur der Nutzer.
- Auf dem VPS lb-db.de nur `/opt/minersentinel` und die eigenen Container anfassen – keine anderen Seiten/Configs.
- Keine Passwörter eingeben; Secrets (`.env`, `backend/acinfinity_data/`) nie committen.
- Arbeitsbranch: `vps-stand`. Push macht der Nutzer über GitHub Desktop.
- Rückfragen an die Cowork-Session: `docs/claude/FRAGEN-AN-COWORK.md`.
