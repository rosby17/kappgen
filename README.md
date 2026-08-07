# Cahier des charges — NicheCut (MVP à jour)

> Destiné à un développeur ou un outil de codage IA. Contient tout le nécessaire pour démarrer sans allers-retours.

---

## 1. Résumé du projet

SaaS qui transforme un script texte ou un fichier audio déjà prêt en vidéo longue durée (10 minutes à 3 heures), pour des YouTubeurs de niches à contenu long format (spiritualité, religion, philosophie) où la synchronisation image/parole n'est pas nécessaire.

**Principe central du produit** : l'utilisateur configure son style de montage **une seule fois par chaîne** (sous-titres, logo, musique, style d'images) — comme un préréglage. Ensuite, il ne revoit plus jamais ces réglages : il envoie des scripts ou audios, tout se génère automatiquement en arrière-plan, et il consulte seulement le résultat. Il peut gérer plusieurs chaînes YouTube en parallèle, chacune avec sa propre niche et son propre style — **une chaîne = un pipeline configuré une fois, réutilisé à l'infini**.

Objectif d'usage typique : l'utilisateur soumet dix ou vingt scripts/audios le soir, ferme l'ordinateur, et retrouve ses vidéos prêtes le lendemain matin.

**Ce que ce n'est PAS** : pas un éditeur vidéo manuel complet (pas de CapCut-like avec toutes les fonctionnalités de montage). NicheCut résout un seul problème de montage bien précis, répété en masse, pour un format de vidéo spécifique — pas de réglages à refaire à chaque vidéo, pas de synchronisation mot-à-mot, pas de format vertical, pas de découpage en segments parallèles pour le MVP.

---

## 2. Stack technique retenue

| Composant | Choix | Justification |
|---|---|---|
| Langage backend | Python 3.11+ | Écosystème mature pour FFmpeg et traitement média |
| Framework API | FastAPI | Léger, rapide à mettre en place |
| Base de données | **SQLite** via SQLAlchemy | Zéro serveur à installer, fichier unique `data/app.db` |
| Traitement en arrière-plan | Boucle de polling simple (`queue_runner.py`) | Un seul utilisateur au départ, pas besoin de Redis/Celery |
| Moteur de rendu | FFmpeg (binaire local) | Gratuit, open source, filtres avancés |
| Sous-titres | Génération ASS (libass) / Burn-in Pillow | Style avancé et karaoké mot-à-mot (`{\k...}`) |
| Stockage fichiers | Système de fichiers local | Pas de coût cloud tant qu'on est en local/VPS unique |
| Frontend Web UI | React + Vite + Tailwind CSS | Interface NicheCut moderne dark mode |

---

## 3. Modèle de données

**Channel** (= pipeline, configuré une seule fois)
```
id                UUID
name              string
niche             string
created_at        datetime
subtitle_style    JSON  { font, size, color, outline_color, outline_width, position, karaoke }
branding          JSON  { logo_path, channel_name_text }
music_preference  JSON  { enabled, track_id_or_style, volume }
image_style       JSON  { source: "library" | "ai_generated", style_prompt, library_path }
effects_config    JSON  { grain, color_grade, zoom_min_pct, zoom_max_pct }
```

**Video** (une génération, rattachée à une chaîne)
```
id                  UUID
channel_id          UUID (FK -> Channel)
input_type          enum: text | audio
script_text         text (rempli si input_type = text ou titre issu du fichier audio)
audio_input_path    string (rempli si input_type = audio)
status              enum: queued | rendering | done | failed
created_at          datetime
started_at          datetime (nullable)
finished_at         datetime (nullable)
output_path         string (nullable jusqu'à "done")
source_assets_path  string (voix off, images utilisées, .ass, snapshot de la config)
error_message       string (nullable)
```

---

## 4. Politique de stockage des fichiers

- **Vidéo finale (`output.mp4`)** : livrée au téléchargement, conservée sur disque sans purge automatique pour le MVP.
- **Assets source** (voix off audio, images utilisées, fichier `.ass`, script, copie de la config de chaîne au moment du rendu) : **toujours conservés** pour permettre de régénérer ou modifier une vidéo sans repayer la voix off ou la génération d'images.

---

## 5. Structure des calques (Composition Moteur FFmpeg)

Calques vidéo de bas en haut :
1. **Diaporama d'images** — base visuelle, effet Ken Burns (zoom 15-20%) et transitions.
2. **Grain / bruit** — overlay léger de texture (`noise`).
3. **Étalonnage couleur** — appliqué sur le composite des deux calques précédents.
4. **Sous-titres** — incrustés par-dessus (style ASS).
5. **Logo** — overlay fixe dans le coin de l'écran.
6. **Nom de chaîne (texte)** — filigrane / watermark textuel.

Pistes audio :
1. **Voix off** — piste principale (générée ou importée).
2. **Musique de fond** — optionnelle, volume réduit (~15-20%), fondu.

---

## 6. Logique du moteur de rendu

### 6.1 Entrée : script ou audio (`voiceover.py`)
- **`input_type = text`** : Le script est envoyé à l'API Izivoice (`generate_speech`) pour générer la voix off et le timing des sous-titres.
- **`input_type = audio`** : L'utilisateur fournit directement un ou plusieurs fichiers audio. Ils sont utilisés comme voix off et transcrits (`speech_to_text`) pour les sous-titres.

### 6.2 Cadence d'affichage des images (`pacing.py`)
- **Accroche (0 – 20s)** : 3-4s par image
- **Mise en route (20s – 2min)** : 6-8s par image
- **Corps début (2min – 30min)** : 12-20s par image
- **Corps long (30min et +)** : 30-45s par image

### 6.3 Rebouclage des images (`image_pool.py`)
Lecture façon "shuffle" avec alternance de la direction du zoom (zoom-in / zoom-out) pour ne jamais répéter une image sans avoir parcouru tout le pool.

---

## 7. Clés et accès à fournir (.env)

```env
IZIVOICE_API_KEY=            # Voix off + musique de fond
AI_IMAGE_PROVIDER_API_KEY=   # Génération d'images IA
AI_IMAGE_PROVIDER_ENDPOINT=  # URL de l'API du fournisseur d'images
```

---

## 8. Lancement & Utilisation locale

```bash
# 1. Installer l'environnement
source .venv/bin/activate
pip install -r requirements.txt

# 2. Exécuter les tests automatisés
.venv/bin/pytest tests/test_pipeline_end_to_end.py

# 3. Démarrer l'application NicheCut (API FastAPI + Web UI)
./scripts/run_local_test.sh
```

Accédez à l'interface sur **`http://localhost:8000`**.
