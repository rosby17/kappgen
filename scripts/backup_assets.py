"""Back up (and restore) everything on disk that no upload path covers.

Renders and thumbnails already go to B2 on their own. These do not, and
until they do a server migration — or a dead disk — takes them with it:
every channel's image library (which is also what the public/community
library is read from), B-roll, sound effects, music, overlays, thumbnail
style references, logos and voices.

    cd backend && python3 scripts/backup_assets.py                # dry-run
    cd backend && python3 scripts/backup_assets.py --execute      # upload
    cd backend && python3 scripts/backup_assets.py --restore      # bring back

Safe to re-run: a file whose object already exists on B2 with the same size
is skipped, so a second pass only uploads what changed. Nothing is ever
deleted, locally or remotely — restoring never overwrites a local file that
is already there and the right size.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import STORAGE_PATH  # noqa: E402
from src.utils import b2_storage  # noqa: E402
from storage_audit import CHANNEL_ASSET_DIRS, ROOT_ASSET_DIRS, human  # noqa: E402

# Kept apart from the video/thumbnail keys so an asset backup can never be
# confused with a render, and so the whole set can be restored (or audited)
# under one prefix after a migration.
BACKUP_PREFIX = "asset-backups"


def targets() -> list:
    """Every (local directory, object prefix) pair worth backing up.

    Channel assets are walked per channel so the object layout mirrors the
    disk exactly — channels/{id}/library/... — which is what makes a restore
    onto a fresh server a straight copy with no path rewriting.
    """
    pairs = []
    channels_root = STORAGE_PATH / "channels"
    if channels_root.is_dir():
        for channel_dir in sorted(channels_root.iterdir()):
            if not channel_dir.is_dir():
                continue
            for kind in CHANNEL_ASSET_DIRS:
                sub = channel_dir / kind
                if sub.is_dir():
                    pairs.append((sub, f"{BACKUP_PREFIX}/channels/{channel_dir.name}/{kind}"))
            # Logos and avatars sit loose in the channel directory; the
            # videos/ subtree is deliberately excluded (already on B2).
            loose = [f for f in channel_dir.iterdir() if f.is_file()]
            if loose:
                pairs.append((channel_dir, f"{BACKUP_PREFIX}/channels/{channel_dir.name}", True))
    for name in ROOT_ASSET_DIRS:
        path = STORAGE_PATH / name
        if path.is_dir():
            pairs.append((path, f"{BACKUP_PREFIX}/{name}"))
    return pairs


def files_of(entry) -> list:
    """Files to copy for one target. A 3-tuple means "this directory's own
    files only, do not descend" — used for the channel directory itself, so
    logos are picked up without dragging videos/ along."""
    local_dir, prefix = entry[0], entry[1]
    shallow = len(entry) > 2 and entry[2]
    if shallow:
        return [(f, f"{prefix}/{f.name}") for f in local_dir.iterdir() if f.is_file()]
    out = []
    for f in local_dir.rglob("*"):
        if f.is_file():
            out.append((f, f"{prefix}/{f.relative_to(local_dir).as_posix()}"))
    return out


def run_backup(execute: bool) -> int:
    client = b2_storage._get_client() if execute else None
    uploaded = skipped = failed = 0
    uploaded_bytes = pending_bytes = 0

    for entry in targets():
        for local_path, key in files_of(entry):
            try:
                size = local_path.stat().st_size
            except OSError:
                continue
            remote_size = b2_storage.object_size_if_exists(key)
            if remote_size == size:
                skipped += 1
                continue
            if not execute:
                pending_bytes += size
                uploaded += 1
                continue
            try:
                client.upload_file(str(local_path), b2_storage.B2_BUCKET_NAME, key)
                uploaded += 1
                uploaded_bytes += size
                if uploaded % 100 == 0:
                    print(f"  … {uploaded} fichier(s) envoyé(s) ({human(uploaded_bytes)})", flush=True)
            except Exception as exc:
                failed += 1
                print(f"  ÉCHEC {local_path}: {exc}", flush=True)

    print()
    if execute:
        print(f"Envoyés : {uploaded} fichier(s), {human(uploaded_bytes)}")
    else:
        print(f"À envoyer : {uploaded} fichier(s), {human(pending_bytes)}")
    print(f"Déjà présents sur B2 (ignorés) : {skipped}")
    if failed:
        print(f"Échecs : {failed} — relance la commande, les fichiers déjà envoyés seront ignorés.")
    return 1 if failed else 0


def run_restore() -> int:
    """Download the whole backup prefix back under STORAGE_PATH.

    Used on the new server after a migration. A local file that already
    exists with the right size is left alone, so an interrupted restore can
    simply be re-run.
    """
    client = b2_storage._get_client()
    paginator = client.get_paginator("list_objects_v2")
    restored = skipped = failed = 0
    restored_bytes = 0
    for page in paginator.paginate(Bucket=b2_storage.B2_BUCKET_NAME, Prefix=f"{BACKUP_PREFIX}/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            rel = key[len(BACKUP_PREFIX) + 1:]
            if not rel:
                continue
            dest = STORAGE_PATH / rel
            if dest.exists() and dest.stat().st_size == obj["Size"]:
                skipped += 1
                continue
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                client.download_file(b2_storage.B2_BUCKET_NAME, key, str(dest))
                restored += 1
                restored_bytes += obj["Size"]
                if restored % 100 == 0:
                    print(f"  … {restored} fichier(s) restauré(s) ({human(restored_bytes)})", flush=True)
            except Exception as exc:
                failed += 1
                print(f"  ÉCHEC {key}: {exc}", flush=True)
    print()
    print(f"Restaurés : {restored} fichier(s), {human(restored_bytes)}")
    print(f"Déjà présents localement (ignorés) : {skipped}")
    if failed:
        print(f"Échecs : {failed}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Sauvegarde vers B2 des assets qu'aucun envoi ne couvre.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--execute", action="store_true", help="envoyer réellement (sinon simulation)")
    group.add_argument("--restore", action="store_true", help="retélécharger la sauvegarde dans STORAGE_PATH")
    args = parser.parse_args()

    if not b2_storage.is_b2_configured():
        print("B2 n'est pas configuré : rien à faire. Vérifie les variables B2_* du service.")
        return 2

    print(f"Stockage : {STORAGE_PATH}")
    print(f"Préfixe B2 : {BACKUP_PREFIX}/")
    print()
    if args.restore:
        return run_restore()
    if not args.execute:
        print("SIMULATION — rien n'est envoyé. Ajoute --execute pour lancer.\n")
    return run_backup(execute=args.execute)


if __name__ == "__main__":
    raise SystemExit(main())
