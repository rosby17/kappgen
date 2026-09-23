"""Read-only audit: what would be lost if this server's disk disappeared?

Run this on the production host before a migration:

    cd backend && python scripts/storage_audit.py
    cd backend && python scripts/storage_audit.py --json    # machine-readable

It answers three questions with numbers rather than assumptions:

1. Is R2 actually switched on right now? Uploads need five environment
   variables, and a single missing one turns every upload into a silent
   no-op while the app keeps working perfectly on local disk.
2. What does the database think is where — how many renders and thumbnails
   have a remote copy, and how many only ever existed on this machine.
3. What is on this disk that no code path ever uploads anywhere: the image
   libraries (including the whole public/community library, which is read
   live off those folders), B-roll, sound effects, music, overlays,
   thumbnail references, logos and cloned voices.

Writes nothing, uploads nothing, deletes nothing.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import (  # noqa: E402
    R2_ACCOUNT_ID,
    R2_ACCESS_KEY_ID,
    R2_BUCKET_NAME,
    R2_FREE_TIER_CAP_BYTES,
    R2_PUBLIC_URL_BASE,
    R2_SECRET_ACCESS_KEY,
    STORAGE_PATH,
)

# Directories under storage/ that no upload path covers. Each entry is
# (path relative to STORAGE_PATH or a channel dir, what losing it costs).
CHANNEL_ASSET_DIRS = {
    "library": "Bibliothèque d'images de la chaîne — alimente aussi la bibliothèque publique",
    "broll": "Clips B-roll uploadés",
    "sfx": "Effets sonores",
    "music": "Musiques uploadées",
    "overlays": "Incrustations",
    "thumbnail_references": "Références de style des miniatures",
}
ROOT_ASSET_DIRS = {
    "voices": "Voix",
    "voice_previews": "Extraits de voix",
    "voice_clone_uploads": "Enregistrements de clonage vocal",
    "uploads": "Fichiers uploadés en attente de traitement",
    "staging": "Imports en cours",
    "recap": "Sources des récapitulatifs",
    "facecam": "Projets facecam",
    "trash": "Rendus archivés (contient les miniatures purgées)",
}


def human(size: int) -> str:
    value = float(size)
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if value < 1024 or unit == "To":
            return f"{value:,.1f} {unit}".replace(",", " ")
        value /= 1024
    return f"{value:.1f} To"


def measure(path: Path) -> tuple:
    """(bytes, file count) for a directory tree. Uses scandir and ignores
    unreadable entries: an audit must never fail on one bad permission."""
    total = 0
    files = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat().st_size
                            files += 1
                    except OSError:
                        continue
        except (OSError, FileNotFoundError):
            continue
    return total, files


def r2_status() -> dict:
    required = {
        "R2_ACCOUNT_ID": R2_ACCOUNT_ID,
        "R2_ACCESS_KEY_ID": R2_ACCESS_KEY_ID,
        "R2_SECRET_ACCESS_KEY": R2_SECRET_ACCESS_KEY,
        "R2_BUCKET_NAME": R2_BUCKET_NAME,
        "R2_PUBLIC_URL_BASE": R2_PUBLIC_URL_BASE,
    }
    # Presence only — never echo a key into a log or a terminal history.
    missing = [name for name, value in required.items() if not value]
    return {
        "configured": not missing,
        "missing_variables": missing,
        "cap_bytes": R2_FREE_TIER_CAP_BYTES,
    }


def configured_providers() -> list:
    """Every object store usable right now, destination first.

    B2 and R2 both exist while the migration runs: renders uploaded before
    it are on B2, new ones land on R2, and an audit that knew about only one
    would report the other's copies as missing — the single number a
    migration decision leans on, wrong in the dangerous direction.
    """
    from src.utils import b2_storage, r2_storage

    found = []
    if r2_storage.is_r2_configured():
        found.append({"name": "R2", "module": r2_storage, "bucket": r2_storage.R2_BUCKET_NAME})
    if b2_storage.is_b2_configured():
        found.append({"name": "B2", "module": b2_storage, "bucket": b2_storage.B2_BUCKET_NAME})
    return found


def asset_backup_report() -> dict:
    """What scripts/backup_assets.py actually holds, per store.

    Without this the audit only counted what no AUTOMATIC upload path
    covers, and kept calling a finished, deliberate backup "jamais
    sauvegardé".
    """
    stores = []
    for provider in configured_providers():
        try:
            client = provider["module"]._get_client()
            paginator = client.get_paginator("list_objects_v2")
            objects = 0
            total = 0
            for page in paginator.paginate(Bucket=provider["bucket"], Prefix="asset-backups/"):
                for obj in page.get("Contents", []):
                    objects += 1
                    total += obj["Size"]
            stores.append({"name": provider["name"], "objects": objects, "bytes": total})
        except Exception as exc:
            # An audit must still produce its other numbers if a store is
            # unreachable.
            stores.append({"name": provider["name"], "objects": 0, "bytes": 0, "error": str(exc)})
    return {"stores": stores, "best_bytes": max([s["bytes"] for s in stores], default=0)}


def database_report() -> dict:
    from sqlalchemy import func

    from src.db.models import Video
    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        by_backend = dict(
            db.query(Video.storage_backend, func.count(Video.id)).group_by(Video.storage_backend).all()
        )
        done = db.query(Video).filter(Video.output_path.isnot(None))
        remote_bytes = (
            db.query(func.coalesce(func.sum(Video.output_size_bytes), 0))
            .filter(Video.storage_backend.in_(("b2", "r2")))
            .scalar()
        )
        local_bytes = (
            db.query(func.coalesce(func.sum(Video.output_size_bytes), 0))
            .filter(Video.storage_backend.notin_(("b2", "r2")))
            .scalar()
        )
        return {
            "videos_by_backend": {str(k): int(v) for k, v in by_backend.items()},
            "videos_with_output": done.count(),
            "thumbnails_without_remote_copy": db.query(Video).filter(Video.thumbnail_storage_url.is_(None)).count(),
            "thumbnails_with_remote_copy": db.query(Video).filter(Video.thumbnail_storage_url.isnot(None)).count(),
            "remote_render_bytes": int(remote_bytes or 0),
            "local_render_bytes": int(local_bytes or 0),
        }
    finally:
        db.close()


def disk_report() -> dict:
    channels_root = STORAGE_PATH / "channels"
    per_kind = {name: {"bytes": 0, "files": 0} for name in CHANNEL_ASSET_DIRS}
    loose_channel_files = {"bytes": 0, "files": 0}
    channel_count = 0

    if channels_root.is_dir():
        for channel_dir in channels_root.iterdir():
            if not channel_dir.is_dir():
                continue
            channel_count += 1
            for kind in CHANNEL_ASSET_DIRS:
                sub = channel_dir / kind
                if sub.is_dir():
                    size, files = measure(sub)
                    per_kind[kind]["bytes"] += size
                    per_kind[kind]["files"] += files
            # Logos and avatars sit directly in the channel directory,
            # alongside the videos/ subtree that R2 already covers.
            try:
                for entry in channel_dir.iterdir():
                    if entry.is_file():
                        loose_channel_files["bytes"] += entry.stat().st_size
                        loose_channel_files["files"] += 1
            except OSError:
                pass

    roots = {}
    for name in ROOT_ASSET_DIRS:
        path = STORAGE_PATH / name
        if path.is_dir():
            size, files = measure(path)
            roots[name] = {"bytes": size, "files": files}

    videos_bytes, videos_files = (0, 0)
    if channels_root.is_dir():
        for channel_dir in channels_root.iterdir():
            videos_dir = channel_dir / "videos"
            if videos_dir.is_dir():
                size, files = measure(videos_dir)
                videos_bytes += size
                videos_files += files

    return {
        "channels": channel_count,
        "channel_assets": per_kind,
        "channel_loose_files": loose_channel_files,
        "root_dirs": roots,
        "videos_tree": {"bytes": videos_bytes, "files": videos_files},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit local vs R2 storage before a migration.")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    from src.utils import b2_storage

    report = {
        "storage_path": str(STORAGE_PATH),
        "r2": r2_status(),
        # B2 still holds everything uploaded before the R2 migration, so its
        # state belongs in the report too — otherwise those copies read as
        # missing for as long as the two stores coexist.
        "b2_configured": b2_storage.is_b2_configured(),
        "database": database_report(),
        "disk": disk_report(),
        "asset_backup": asset_backup_report(),
    }

    unprotected = sum(v["bytes"] for v in report["disk"]["channel_assets"].values())
    unprotected += report["disk"]["channel_loose_files"]["bytes"]
    unprotected += sum(v["bytes"] for v in report["disk"]["root_dirs"].values())
    report["never_uploaded_bytes"] = unprotected

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    r2 = report["r2"]
    print("=" * 66)
    print("AUDIT DE SAUVEGARDE KAPPGEN")
    print("=" * 66)
    print(f"Dossier de stockage : {report['storage_path']}")
    print()

    print("── Stockage objet " + "─" * 48)
    if r2["configured"]:
        print("  R2 : configuré — les envois sont actifs.")
    else:
        print("  R2 : NON configuré — aucun envoi vers R2.")
        print(f"       Variables manquantes : {', '.join(r2['missing_variables'])}")
    # B2 still holds everything uploaded before the migration; ignoring it
    # would report those copies as missing.
    print(f"  B2 : {'configuré (migration en cours)' if report['b2_configured'] else 'non configuré'}")
    if not r2["configured"] and not report["b2_configured"]:
        print("  AUCUN stockage distant actif : tout reste sur ce disque.")
    if r2["cap_bytes"]:
        used = report["database"]["remote_render_bytes"]
        print(f"  Plafond configuré : {human(r2['cap_bytes'])} — utilisé {human(used)}")
        if used >= r2["cap_bytes"]:
            print("  ATTENTION : plafond atteint, les nouveaux rendus restent en local.")
    else:
        print("  Plafond : aucun.")
    print()

    db_report = report["database"]
    print("── Base de données " + "─" * 47)
    print(f"  Vidéos avec un rendu : {db_report['videos_with_output']}")
    for backend, count in sorted(db_report["videos_by_backend"].items()):
        print(f"    storage_backend={backend or 'non défini'} : {count}")
    print(f"  Rendus distants (B2+R2) : {human(db_report['remote_render_bytes'])}")
    print(f"  Rendus en local : {human(db_report['local_render_bytes'])}")
    print(f"  Miniatures avec copie distante : {db_report['thumbnails_with_remote_copy']}")
    print(f"  Miniatures SANS copie distante : {db_report['thumbnails_without_remote_copy']}")
    print()

    disk = report["disk"]
    print("── Sur ce disque UNIQUEMENT (jamais envoyé nulle part) " + "─" * 11)
    print(f"  {disk['channels']} chaîne(s) inspectée(s)")
    rows = []
    for kind, label in CHANNEL_ASSET_DIRS.items():
        entry = disk["channel_assets"][kind]
        if entry["files"]:
            rows.append((f"channels/*/{kind}", entry, label))
    loose = disk["channel_loose_files"]
    if loose["files"]:
        rows.append(("channels/*/ (logos, avatars)", loose, "Logos et avatars de chaînes"))
    for name, label in ROOT_ASSET_DIRS.items():
        entry = disk["root_dirs"].get(name)
        if entry and entry["files"]:
            rows.append((f"{name}/", entry, label))

    if rows:
        width = max(len(r[0]) for r in rows)
        for path, entry, label in sorted(rows, key=lambda r: -r[1]["bytes"]):
            print(f"  {path.ljust(width)}  {human(entry['bytes']).rjust(11)}  {entry['files']:>7} fichier(s)  {label}")
    else:
        print("  (rien trouvé — vérifie que STORAGE_PATH pointe bien sur le bon volume)")
    print()
    print(f"  TOTAL de ces dossiers : {human(report['never_uploaded_bytes'])}")
    print()

    backup = report["asset_backup"]
    print("── Sauvegarde manuelle de ces assets (asset-backups/) " + "─" * 12)
    if not backup["stores"]:
        print("  Aucun stockage distant configuré.")
    for store in backup["stores"]:
        if store.get("error"):
            print(f"  {store['name']} : injoignable ({store['error']})")
        elif store["objects"] == 0:
            print(f"  {store['name']} : aucune. Lance backup_assets.py --execute")
        else:
            print(f"  {store['name']} : {store['objects']} objet(s), {human(store['bytes'])}")
    gap = report["never_uploaded_bytes"] - backup["best_bytes"]
    if backup["best_bytes"] and gap <= 1024 * 1024:
        print("  Couvre l'intégralité des dossiers listés ci-dessus.")
    elif backup["best_bytes"]:
        print(f"  Écart avec le disque : {human(gap)} — relance backup_assets.py --execute.")
    print()
    print("── Verdict " + "─" * 55)
    if not r2["configured"] and not report["b2_configured"]:
        print("  Une migration maintenant perdrait TOUT le contenu de ce disque,")
        print("  rendus et miniatures compris : aucun stockage distant n'est actif.")
    else:
        gap = report["never_uploaded_bytes"] - report["asset_backup"]["best_bytes"]
        if gap > 1024 * 1024:
            print(f"  Les rendus sont protégés, mais {human(gap)} d'assets n'ont aucune")
            print("  copie distante. À sauvegarder avant de changer de serveur.")
        elif db_report["thumbnails_without_remote_copy"]:
            print(f"  Assets et rendus sauvegardés. Restent {db_report['thumbnails_without_remote_copy']}")
            print("  miniature(s) sans copie distante : leur fichier local n'existe plus,")
            print("  elles devront être régénérées (ou reprises depuis YouTube).")
        else:
            print("  Rien d'exposé : tout ce qui compte a une copie distante.")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
