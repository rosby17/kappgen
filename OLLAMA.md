# Ollama via Cloudflare Access

Le texte et la vision utilisent respectivement OLLAMA_MODEL et OLLAMA_VISION_MODEL.
La vision doit utiliser un modèle multimodal (qwen3.5), pas qwen3-coder.
Les appels sont comptabilisés à 0 USD et autorisés par le mode API gratuites.

## Mac

1. Démarrer `OLLAMA_HOST=127.0.0.1:11434 ollama serve` et vérifier les modèles avec `ollama list`.
2. Créer un tunnel Cloudflare nommé, avec un domaine dédié et une origine
   `http://127.0.0.1:11434` (httpHostHeader: localhost:11434).
3. Protéger ce domaine avec une application Cloudflare Access et une politique
   **Service Auth** limitée au service token du VPS. Ne pas ajouter de bypass public.
4. Lancer `OLLAMA_TUNNEL_NAME=<nom> bash scripts/start_ollama_tunnel.sh` depuis la racine du dépôt backend.

Ollama local ignore la clé Bearer : OLLAMA_API_KEY seul ne protège pas l'origine.
Le lanceur n'ouvre donc plus de tunnel rapide public sans contrôle d'accès.

## Backend / Coolify

Configurer dans les secrets de l'application, puis redéployer API et worker :

```dotenv
OLLAMA_BASE_URL=https://ollama.votre-domaine.fr
OLLAMA_ACCESS_CLIENT_ID=<service-token-client-id>
OLLAMA_ACCESS_CLIENT_SECRET=<service-token-secret>
OLLAMA_MODEL=qwen3.5:latest
OLLAMA_VISION_MODEL=qwen3.5:latest
```

OLLAMA_API_KEY reste disponible si un proxy authentifiant Bearer est utilisé.
Choisir Ollama en premier dans l'ordre IA de l'admin. L'URL peut aussi être
renseignée dans le compte fournisseur admin ; les secrets Access restent côté serveur.

## Validation

- Sans identifiants, le domaine doit refuser l'accès à `/api/tags`.
- Depuis le VPS, avec les deux en-têtes CF-Access-Client-Id et
  CF-Access-Client-Secret, `/api/tags` doit répondre et contenir le modèle configuré.
- Le contrôle admin vérifie la présence du modèle puis une génération native
  courte via `/api/chat` (jusqu'à 60 secondes de chargement). Le vert valide ce contrôle texte,
  pas une génération longue ni la vision.
- Générer un script puis analyser une image avec Ollama prioritaire.
- Mac éteint/en veille ou tunnel coupé : statut en erreur et repli selon l'ordre IA.

Références : https://docs.ollama.com/api/authentication et
https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/
