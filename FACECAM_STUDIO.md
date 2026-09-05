# Facecam Studio

Facecam reprend les fonctions de montage d’Editing OS dans l’identité KappGen :
import ordonné des rushs, décisions de coupe par timecode, transcription navigable,
habillages réglables, charte de la chaîne, retours par version, exports et vérification.

## Déploiement

Déployer backend et worker avant le frontend. `init_db()` ajoute la colonne JSON
`videos.facecam_settings`. L’état `review` laisse le montage hors de la file jusqu’à
sa validation depuis le studio. Les anciens rushs Facecam restent compatibles ;
une nouvelle analyse crée leur projet au premier passage dans le moteur.

FFmpeg doit disposer de libass, libx264 et loudnorm (présents dans l’image Debian
existante). Faster-whisper reste le transcripteur. Les nouvelles cartes Facecam
utilisent Pillow + FFmpeg, sans navigateur ; les compositions HyperFrames utilisées
ailleurs dans KappGen sont conservées.

## Fonctionnement

`storage/facecam/{video_id}/project.json` garde les décisions dans les coordonnées
des rushs originaux. Les mutations sont validées, versionnées et réservées au
propriétaire du projet. Les modifications pendant un rendu sont refusées. Le dernier
état enregistré peut être annulé (20 états conservés). Les coupes désactivées
conservent le passage ; les coupes manuelles et les nouveaux habillages s’ajoutent
aux propositions. L’aperçu affiche explicitement les rushs ou une version rendue.

Chaque rendu conserve un MP4 et un instantané du projet, de la transcription
recalée et de la vérification. Les retours sont attachés à leur version. Les exports
SRT, texte et JSON correspondent à la version sélectionnée. Les médias protégés
prennent en charge les requêtes HTTP Range.

Les réglages de couleur et de police proviennent de la chaîne puis peuvent être
adaptés au montage. Le logo de chaîne est conservé. Trois traitements de cartes
sont disponibles : épuré, impact et éditorial. Les formats original, vertical, carré
et horizontal préservent toute l’image avec marges si nécessaire. Le master applique
une meilleure qualité d’encodage et une normalisation audio.

## Limites explicites

Il s’agit d’une adaptation intégrée, pas d’une copie complète du logiciel local :
l’éditeur d’images-clés HyperFrames, les 1 600 cartes de sa bibliothèque, le bureau
pixel-art, les captures de sites et la gestion Git/Finder ne sont pas intégrés.
Le B-roll utilise les fournisseurs existants de KappGen et reste désactivé par défaut.
Les cartes créées ici proposent des transitions de fondu. Les notes servent à la
révision humaine ; elles ne déclenchent pas seules un montage par IA.

Les archives vidéo restent locales au worker et ne sont pas purgées automatiquement.
Prévoir leur stockage durable avant une utilisation à grande échelle. La mise en ligne
du dernier rendu réutilise le stockage B2 existant. Les tests simulent les appels de
transcription, de facturation et de fournisseurs ; aucun service payant n’a été appelé
pour les tests.

## Vérification

`python -m pytest tests/test_facecam_studio.py -q`

Tests de coupes et timecodes, limites des réglages, format SRT, écritures atomiques,
accès propriétaire, lecture Range, conflit de révision, notes, annulation, validation
et mise en file, rendu FFmpeg réel avec sous-titres et carte, instantané de version.

L’attribution MIT du projet de référence se trouve dans
`third_party/EDITING_OS_LICENSE`.
