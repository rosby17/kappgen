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

## Montage plus stylé : bruitages (SFX), illustrations réelles, motion design

Point de départ : une vidéo montrant un système d'édition piloté par Claude Code (multi-agents : suppression des blancs/répétitions, sous-titres animés, SFX, illustrations, motion design) pour du contenu **avec visage** (rushes humains). Décision : ce n'est pas notre pipeline actuel, ce sera une **interface séparée** (voir section "Pipeline montage facecam" plus bas). Dans l'immédiat, on améliore le pipeline **sans visage** existant (avatar/narration) sur trois points.

### 1. Bruitages (SFX) cohérents avec le script — en cours
Absent aujourd'hui. Plan (recherche déjà faite sur le code existant, patterns à réutiliser) :
- Nouvelle table `ChannelSoundEffect` (un rang par clip uploadé), sur le modèle de `CommunityLibraryImageTag` — pas un blob JSON comme `music_preference`, pour permettre à un pas de matching LLM de requêter les effets indépendamment du reste des réglages de la chaîne.
- Upload par chaîne : même route/pattern que l'upload de musique (`POST /{channel_id}/music`), fichiers sur disque local (`STORAGE_PATH/channels/{id}/sfx/`), jamais sur B2 (comme les autres assets créateur).
- Matching : appel LLM (`generate_text()` de `src/pipeline/ai_text.py`, pattern JSON structuré déjà utilisé dans `scene_director.py`) sur la transcription (mots + timestamps, déjà disponibles dans `transcript_info` au même endroit du pipeline que le mixage audio) → liste `{start, sfx_tag}` en piochant uniquement dans les effets réellement uploadés pour la chaîne. Si aucun SFX n'est uploadé pour une chaîne, l'étape est sautée (pas de coût, pas d'appel LLM inutile).
- Mixage : étendre `build_studio_mix_filter`/`mix_audio_tracks` (`src/pipeline/audio_mixer.py`) pour accepter des paires `(sfx_path, start_time)` en plus de voix+musique, via `adelay` + un `amix` à N entrées au lieu de 2.

### 2. Recherche d'illustrations réelles (Google Images) — à faire après les SFX
En complément des sources actuelles (bibliothèque, génération IA), pas en remplacement — utile pour des logos de marque, captures d'écran, visages réels que les banques d'images stock ne couvrent pas.

### 3. Motion design (titres animés, transitions stylées) — décision d'architecture prise
Après avoir regardé l'outil utilisé dans la vidéo de référence (**HyperFrames**, github.com/heygen-com/hyperframes, HeyGen, open source Apache 2.0 — "Write HTML, render video", pensé pour être piloté par un agent Claude), décision : **adoption progressive, pas de bascule complète du moteur de rendu.**

Pourquoi pas une bascule complète (déjà débattu, tranché) :
- Le pipeline ffmpeg/Python actuel est en production, vient d'être stabilisé (bug OOM à 58 clips, bug de bulle de sous-titres, bug de miniature à la publication — tous corrigés cette semaine) ; une réécriture totale remet tout ce travail à risque en une seule fois.
- HyperFrames rend le HTML/CSS via un navigateur headless (Chromium) — plus lourd en RAM/CPU qu'un filtre ffmpeg direct, alors qu'on vient justement de corriger un crash mémoire du worker.
- Ajoute un second runtime de production (Node.js + npm + Chromium) à côté du Python existant, avec son propre cycle de dépendances et de bugs de compatibilité.
- Miser tout le rendu vidéo sur un projet externe (roadmap HeyGen hors de notre contrôle) crée un point de défaillance unique, plutôt que d'en réduire un.

Plan d'adoption incrémentale :
- Utiliser HyperFrames **isolé**, uniquement pour produire des clips d'animation/titres courts (petits MP4/WebM transparents — kinetic type, titres qui apparaissent, lower-thirds), généré séparément puis superposé (overlay) sur la sortie du pipeline ffmpeg actuel via `image_overlays`/`overlay=` (mécanisme déjà existant dans `assembler.py`).
- Skills HyperFrames pertinentes à exploiter plus tard : `/motion-graphics` (kinetic type courts), `/hyperframes-animation` + `/hyperframes-keyframes` (animations GSAP/CSS/WAAPI), `/media-use` (recoupe avec le chantier SFX/illustrations ci-dessus, à évaluer comme alternative à la solution maison une fois le premier jet en place).
- Pas de migration des sous-titres/karaoké/mixage/stockage B2/miniatures — ça reste sur le pipeline actuel.
- Si après quelques semaines l'usage isolé tient la route, migration progressive **chaîne par chaîne** envisageable, jamais d'un coup sur toute la production.

### Statut
SFX en cours de conception (recherche du code existant faite). Illustrations réelles et motion design (HyperFrames en overlay isolé) : à venir, dans cet ordre.

## Pipeline montage facecam (rushes humains) — idée future, interface séparée

Inspiré de la même vidéo de référence (système multi-agents Claude Code pour du montage de rush humain brut : suppression des blancs par niveau sonore, dédoublonnage des prises répétées, sous-titres animés, SFX, illustrations, motion design, boucle de retours horodatés frame par frame). Distinct du pipeline actuel (avatar/narration, sans visage) — sera une **interface séparée** dans KappGen pour les créateurs qui filment leur propre visage.

Phases envisagées (à détailler quand ce chantier sera priorisé) :
1. Montage parole : transcription (Izivoice, déjà intégré) + détection de silence par niveau sonore (`ffmpeg silencedetect`, pas par les mots) + détection de prises répétées via passe LLM sur la transcription.
2. Sous-titres + SFX : réutilise le moteur ASS existant + le chantier SFX ci-dessus.
3. Illustrations réelles (recherche Google Images/logos/captures d'écran).
4. Motion design : candidat naturel pour HyperFrames (skills `/talking-head-recut`, `/embedded-captions`, `/motion-graphics`).
5. Boucle de retours horodatés (frame par frame) qui ne redéclenche que l'étape concernée.

### Statut
Idée future, pas encore priorisée — reprendre après les chantiers SFX/illustrations/motion design du pipeline actuel.
