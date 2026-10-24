"""Download, inspect, and re-host a single app's IPA."""
from __future__ import annotations

import hashlib
import io
import plistlib
import re
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from PIL import Image

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)


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


def download_google_drive(file_id: str, dest_path: Path) -> None:
    """Download an IPA from Google Drive, handling large file virus warnings."""
    session = requests.Session()
    headers = {"User-Agent": USER_AGENT}
    base_url = "https://drive.usercontent.google.com/download"
    params = {"id": file_id, "export": "download"}

    response = session.get(base_url, params=params, headers=headers, stream=True, timeout=120)
    response.raise_for_status()

    first_chunk = next(response.iter_content(chunk_size=16384), b"")

    # Si le téléchargement commence par 'PK', c'est directement l'archive ZIP/IPA
    if first_chunk.startswith(b"PK"):
        with open(dest_path, "wb") as f:
            f.write(first_chunk)
            for chunk in response.iter_content(chunk_size=1048576):
                f.write(chunk)
        return

    # Sinon, Google a renvoyé une page HTML (avertissement de taille ou quota)
    html_chunks = [first_chunk]
    for chunk in response.iter_content(chunk_size=16384):
        html_chunks.append(chunk)
    html_text = b"".join(html_chunks).decode("utf-8", errors="ignore")
    soup = BeautifulSoup(html_text, "html.parser")

    if "too many users" in html_text.lower() or "quota" in html_text.lower():
        raise RuntimeError(f"Quota de téléchargement dépassé sur Google Drive pour le fichier {file_id}.")

    # Recherche du formulaire de confirmation pour fichier volumineux
    form = soup.find("form", id="download-form") or soup.find("form", action=re.compile(r"/download"))
    if form:
        action = form.get("action", base_url)
        if not action.startswith("http"):
            action = urljoin(base_url, action)

        form_data = {
            inp["name"]: inp.get("value", "")
            for inp in form.find_all("input")
            if inp.get("name")
        }
        method = form.get("method", "get").lower()

        req_fn = session.post if method == "post" else session.get
        kw = {"data": form_data} if method == "post" else {"params": form_data}

        with req_fn(action, headers=headers, stream=True, timeout=240, **kw) as stream_resp:
            stream_resp.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in stream_resp.iter_content(chunk_size=1048576):
                    f.write(chunk)
    else:
        # Recherche alternative d'un bouton ou lien de confirmation
        confirm_btn = soup.find("a", href=re.compile(r"confirm=")) or soup.find("a", id="uc-download-link")
        if confirm_btn and confirm_btn.get("href"):
            confirm_url = urljoin(base_url, confirm_btn["href"])
            with session.get(confirm_url, headers=headers, stream=True, timeout=240) as stream_resp:
                stream_resp.raise_for_status()
                with open(dest_path, "wb") as f:
                    for chunk in stream_resp.iter_content(chunk_size=1048576):
                        f.write(chunk)
        else:
            title = soup.title.string.strip() if soup.title and soup.title.string else "Page HTML reçue"
            raise RuntimeError(f"Google Drive a renvoyé du HTML au lieu du fichier IPA ({title}).")

    if not zipfile.is_zipfile(dest_path):
        preview = dest_path.read_text(errors="ignore")[:300]
        raise RuntimeError(f"Le fichier téléchargé n'est pas une archive ZIP/IPA valide : {preview}")


def download_via_requests(url: str, dest_path: Path) -> None:
    headers = {"User-Agent": USER_AGENT}
    with requests.get(url, stream=True, timeout=180, headers=headers, allow_redirects=True) as response:
        response.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=1048576):
                f.write(chunk)


def download_ipa(card, dest_path: Path) -> None:
    """Download an app's IPA, routing to Google Drive handler or direct download."""
    drive_file_id = getattr(card, "drive_file_id", None)
    download_url = getattr(card, "download_url", "")

    if drive_file_id:
        download_google_drive(drive_file_id, dest_path)
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
