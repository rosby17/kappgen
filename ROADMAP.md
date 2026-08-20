# Roadmap

## Application desktop (Mac + Windows) — idée future

Objectif : proposer NicheCut en app desktop téléchargeable (comme CapCut), que les utilisateurs installent et utilisent en local, chacun consommant les ressources de sa propre machine — sans backend vidéo à gérer/héberger de notre côté.

### Architecture envisagée
- **Shell desktop** : Electron (ou Tauri) avec `electron-builder` pour générer les deux cibles depuis une seule config :
  - `.dmg` pour Mac
  - installeur `.exe` (NSIS) pour Windows
- **Backend Python (FastAPI)** : packagé en sidecar local via PyInstaller, démarré/arrêté automatiquement par l'app. Un binaire par OS (pas de cross-compilation simple depuis Mac vers Windows — prévoir CI type GitHub Actions buildant sur les deux OS, ou VM Windows).
- **Base de données** : remplacer Postgres par SQLite en mode local (pas de service à installer côté utilisateur).
- **ffmpeg + polices** (~40 fonts actuellement dans le Dockerfile) : à bundler dans l'installeur, par plateforme.
- **IA (API Anthropic)** : reste hybride — le pipeline vidéo est 100% local, mais les appels IA (scripts, titres, etc.) continuent de passer par un backend cloud léger qu'on garde et qu'on peut facturer à l'usage. Décision prise : on ne demande pas à chaque utilisateur sa propre clé API Anthropic.

### Points d'attention identifiés
- Poids de l'installeur estimé entre 250 et 500 Mo (Python embarqué + ffmpeg + polices), sur les deux plateformes.
- Signature de code nécessaire avant diffusion publique large :
  - Certificat Apple Developer (~99$/an) pour Mac (éviter l'avertissement "app non fiable").
  - Certificat Authenticode pour Windows.
- Séparer dans le code backend actuel les routes "pipeline vidéo" (à rendre 100% locales) des routes "IA" (à garder pointées vers le backend cloud), avec URL configurable selon l'environnement (local vs cloud).

### Statut
Idée à explorer plus tard, pas encore priorisée. Reprendre cette section quand on voudra lancer le prototype.
