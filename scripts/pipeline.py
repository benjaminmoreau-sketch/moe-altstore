"""Orchestrates a full pipeline run: scrape, diff, process, rebuild apps.json."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from curl_cffi import requests
import yaml

from build_source import build_source
from process_app import (
    convert_icon_to_png,
    download_ipa,
    extract_bundle_info,
    sha256_and_size,
    upload_release_asset,
)
from scrape import scrape_tracked_apps

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "apps.yaml"
STATE_PATH = REPO_ROOT / "state" / "state.json"
ICONS_DIR = REPO_ROOT / "icons"
APPS_JSON_PATH = REPO_ROOT / "apps.json"

# Récupère automatiquement "benjaminmoreau-sketch/moe-altstore" dans l'environnement GitHub Actions
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "benjaminmoreau-sketch/moe-altstore")


def load_config() -> list[dict]:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)["apps"]


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    with open(STATE_PATH) as f:
        return json.load(f)


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def apps_needing_processing(config_apps: list[dict], scraped: dict, state: dict) -> list[dict]:
    """Return config entries whose scraped data_modified differs from cached state."""
    needing = []
    for app_config in config_apps:
        card = scraped.get(app_config["app_id"])
        if card is None:
            continue
        cached = state.get(app_config["slug"])
        if cached is None or cached.get("data_modified") != card.data_modified:
            needing.append(app_config)
    return needing


def process_single_app(app_config: dict, card, tmp_dir: Path) -> dict:
    """Download, inspect, and re-host one app's IPA. Returns a new state entry."""
    ipa_path = tmp_dir / f"{app_config['slug']}.ipa"
    download_ipa(card, ipa_path)

    bundle_identifier, plist_
