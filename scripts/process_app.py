"""Download, inspect, and re-host a single app's IPA."""
from __future__ import annotations

import hashlib
import io
import plistlib
import subprocess
import zipfile
from pathlib import Path

import requests
from PIL import Image


def sha256_and_size(file_path: Path) -> tuple[str, int]:
    """Compute the sha256 hex digest and byte size of a file."""
    hasher = hashlib.sha256()
    size = 0
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1048576), b""):
            hasher.update(chunk)
            size += len(chunk)
    return hasher.hexdigest(), size


def extract_bundle_info(ipa_path: Path) -> tuple[str, str, str]:
    """Read CFBundleIdentifier, CFBundleShortVersionString, and MinimumOSVersion from an .ipa."""
    with zipfile.ZipFile(ipa_path) as ipa:
        info_plist_names = [
            name
            for name in ipa.namelist()
            if name.startswith("Payload/")
            and name.endswith(".app/Info.plist")
            and name.count("/") == 2
        ]
        if not info_plist_names:
            raise ValueError(f"No top-level Info.plist found in {ipa_path}")
        plist_bytes = ipa.read(info_plist_names[0])
    plist = plistlib.loads(plist_bytes)
    return (
        plist["CFBundleIdentifier"],
        plist["CFBundleShortVersionString"],
        plist.get("MinimumOSVersion", "12.0"),
    )


def convert_icon_to_png(icon_bytes: bytes) -> bytes:
    """Re-encode an icon (webp or otherwise) as PNG bytes."""
    image = Image.open(io.BytesIO(icon_bytes)).convert("RGBA")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def choose_downloader(download_url: str) -> str:
    """Return 'requests' for direct usercontent links, 'gdown' for classic drive links."""
    if "drive.usercontent.google.com" in download_url:
        return "requests"
    if "drive.google.com" in download_url:
        return "gdown"
    return "requests"


def download_via_gdown(file_id: str, dest_path: Path) -> None:
    import gdown

    output = gdown.download(f"https://drive.google.com/uc?id={file_id}", str(dest_path), quiet=False)
    if not output or not dest_path.exists() or dest_path.stat().st_size < 1000:
        raise RuntimeError(f"gdown n'a pas pu télécharger de fichier valide pour l'ID {file_id}")


def download_via_requests(url: str, dest_path: Path) -> None:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
        )
    }
    with requests.get(url, stream=True, timeout=120, headers=headers, allow_redirects=True) as response:
        response.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=1048576):
                f.write(chunk)


def download_ipa(card, dest_path: Path) -> None:
    """Download an app's IPA, routing to the right downloader for its host."""
    download_url = getattr(card, "download_url", "")
    drive_file_id = getattr(card, "drive_file_id", None)

    if choose_downloader(download_url) == "gdown" and drive_file_id:
        try:
            download_via_gdown(drive_file_id, dest_path)
            return
        except Exception as exc:
            print(f"Échec de gdown ({exc}), bascule sur le lien direct via requests...")
            fallback_url = f"https://drive.usercontent.google.com/download?id={drive_file_id}&export=download"
            download_via_requests(fallback_url, dest_path)
    else:
        download_via_requests(download_url, dest_path)


def upload_release_asset(repo: str, tag: str, asset_path: Path, title: str) -> str:
    """Create (if needed) a GitHub release and upload the IPA as its asset."""
    view = subprocess.run(
        ["gh", "release", "view", tag, "--repo", repo],
        capture_output=True,
        text=True,
    )
    if view.returncode != 0:
        subprocess.run(
            [
                "gh", "release", "create", tag,
                "--repo", repo,
                "--title", title,
                "--notes", f"Automated build for {title}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    subprocess.run(
        ["gh", "release", "upload", tag, str(asset_path), "--repo", repo, "--clobber"],
        check=True,
        capture_output=True,
        text=True,
    )
    return f"https://github.com/{repo}/releases/download/{tag}/{asset_path.name}"
