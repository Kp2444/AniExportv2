"""
Web GUI
"""

from __future__ import annotations

import argparse
import gzip
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading
import time
from typing import Any, Dict, List, Optional
import urllib.parse
import webbrowser
import xml.etree.ElementTree as ET
import uuid
from datetime import datetime

# Import core normalization and generation logic
from generate_mal_xml import (
    MIN_REQUEST_INTERVAL,
    AniListReadError,
    anilist_delete_entry,
    anilist_existing_entries,
    anilist_viewer,
    anilist_write_entry,
    backfill_anilist_ids,
    build_undo_snapshot,
    DIFF_FIELDS,
    iter_undo_payloads,
    iter_write_payloads,
    list_undo_snapshots,
    load_undo_snapshot,
    prune_undo_snapshots,
    record_created_entry,
    save_undo_snapshot,
    resolve_one,
    _normalize_status,
    current_interval,
    BATCH_SIZE,
    resolve_many_entries,
    lookup_by_mal_id,
    search_candidates,
    build_mal_xml,
    build_mal_manga_xml,
    deduplicate_entries,
    resolve_anime_entries,
    resolve_manga_entries,
    sanitize_title_line,
    set_rate_limit_notifier,
)

BASE_DIR = Path(__file__).resolve().parent
EXPORT_MEDIA_TYPE = "anime"

# Automatically detect output/ directory
_EXPORT_DIR = BASE_DIR / "output" if (BASE_DIR / "output").is_dir() else BASE_DIR

# Anime keeps the original names so existing links and scripts still work; manga
# gets its own pair so one media can never overwrite or impersonate the other.
EXPORT_XML_PATH = _EXPORT_DIR / "myanimelist_export.xml"
EXPORT_GZ_PATH = _EXPORT_DIR / "myanimelist_export.xml.gz"
EXPORT_MANGA_XML_PATH = _EXPORT_DIR / "myanimelist_manga_export.xml"
EXPORT_MANGA_GZ_PATH = _EXPORT_DIR / "myanimelist_manga_export.xml.gz"

# Only one network run at a time: two concurrent runs would interleave their
# writes to the same export files and fight over the rate limit.
_run_lock = threading.Lock()
_active_runs: Dict[str, Dict[str, threading.Event]] = {}
_runs_guard = threading.Lock()

# The payloads of the rows that failed in the most recent write, so "retry
# failed" can re-send exactly those. One run's worth only: a new run replaces
# it, so nothing accumulates. The token is never part of it - a retry needs one
# re-supplied, exactly like the write.
_last_failures: Dict[str, Any] = {"run_id": None, "payloads": [], "snapshot": None}
_failures_guard = threading.Lock()


def _remember_failures(run_id: str, payloads: List[Dict[str, Any]],
                       snapshot: Optional[str]) -> None:
    with _failures_guard:
        _last_failures.update({"run_id": run_id, "payloads": payloads, "snapshot": snapshot})


def _take_failures(run_id: str) -> Optional[Dict[str, Any]]:
    """The stored payloads for one run id, cleared as they are handed out.

    The run's snapshot name travels with them so a retry keeps appending to the
    snapshot the original run wrote, rather than starting a second one that
    knows nothing about the prior state.
    """
    with _failures_guard:
        if not run_id or _last_failures.get("run_id") != run_id or not _last_failures.get("payloads"):
            return None
        return {"payloads": _last_failures["payloads"], "snapshot": _last_failures.get("snapshot")}


MAX_PREVIEW_BYTES = 5 * 1024 * 1024
ARCHIVE_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.")


def _read_failure_message(exc: Exception) -> str:
    """Why the preview refuses rather than guessing."""
    return ("Could not read your existing AniList list, so this write was not attempted: "
            f"{exc}. Writing without it would add second copies of entries you already have.")


def _write_policy(data: Dict[str, Any]) -> str:
    """Only an explicit "overwrite" weakens the guard; anything else is safe."""
    return "overwrite" if data.get("policy") == "overwrite" else "safe"


def _row_overrides(data: Dict[str, Any]) -> Dict[Any, Dict[str, str]]:
    """{anilist_id: {field: "mine"|"clear"}}, as the confirm modal sends it.

    Filtered rather than trusted: this arrives over HTTP and decides whether a
    guard applies, so only the known fields and known actions get through.
    """
    raw = data.get("overrides")
    if not isinstance(raw, dict):
        return {}
    allowed = {name for name, _ in DIFF_FIELDS}
    out: Dict[Any, Dict[str, str]] = {}
    for key, fields in raw.items():
        if not isinstance(fields, dict):
            continue
        try:
            media_id = int(key)
        except (TypeError, ValueError):
            continue
        # "clear" only means something for completedAt; anywhere else it would
        # be a silent no-op, so it is dropped rather than accepted.
        picked = {f: v for f, v in fields.items()
                  if f in allowed and (v == "mine" or (v == "clear" and f == "completedAt"))}
        if picked:
            out[media_id] = picked
    return out


def _register_run() -> tuple:
    run_id = uuid.uuid4().hex
    control = {"cancel": threading.Event(), "pause": threading.Event()}
    with _runs_guard:
        _active_runs[run_id] = control
    return run_id, control


def _unregister_run(run_id: str) -> None:
    with _runs_guard:
        _active_runs.pop(run_id, None)


def _archive_paths(media_type: str) -> tuple:
    xml_path, gz_path = _export_paths(media_type)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = xml_path.name[:-len(".xml")] if xml_path.name.endswith(".xml") else xml_path.stem
    return (xml_path.with_name(f"{stem}_{stamp}.xml"),
            gz_path.with_name(f"{stem}_{stamp}.xml.gz"))


def _write_archive(media_type: str, xml_bytes: bytes, gz_bytes: bytes) -> str:
    """Keep a timestamped copy beside the live export so a good run is never lost."""
    archive_xml, archive_gz = _archive_paths(media_type)
    archive_xml.write_bytes(xml_bytes)
    archive_gz.write_bytes(gz_bytes)
    return archive_xml.name


def _latest_archive(media_type: str) -> Optional[str]:
    xml_path, _ = _export_paths(media_type)
    stem = xml_path.name[:-len(".xml")] if xml_path.name.endswith(".xml") else xml_path.stem
    try:
        names = sorted(q.name for q in xml_path.parent.glob(stem + "_*.xml"))
    except OSError:
        return None
    return names[-1] if names else None


def _export_paths(media_type: str) -> tuple:
    """Resolve the (xml, gz) pair for a media type. Reads module globals on every
    call so tests can patch them."""
    if media_type == "manga":
        return EXPORT_MANGA_XML_PATH, EXPORT_MANGA_GZ_PATH
    return EXPORT_XML_PATH, EXPORT_GZ_PATH


def _export_filenames(media_type: str) -> tuple:
    if media_type == "manga":
        return "myanimelist_manga_export.xml", "myanimelist_manga_export.xml.gz"
    return "myanimelist_export.xml", "myanimelist_export.xml.gz"

MAX_IMPORT_BYTES = 20 * 1024 * 1024


def _import_text(node: Any) -> str:
    return (node.text or "").strip() if node is not None else ""


def _import_int(node: Any, default: int = 0) -> int:
    try:
        return int(_import_text(node))
    except (TypeError, ValueError):
        return default


def _import_date(value: str) -> Optional[str]:
    """Keep only values the builders will accept back."""
    if value == "0000-00-00":
        return value
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    return value


def parse_mal_xml(raw: str) -> Dict[str, Any]:
    """Read a MyAnimeList export back into the rows this app works with.

    Returns {ok, media_type, entries, skipped}. Raises ValueError with a
    user-facing message for anything that is not a MAL export.
    """
    if "<!DOCTYPE" in raw[:4096].upper():
        # ElementTree expands internal entities; a local file is low risk but
        # the guard costs one line.
        raise ValueError("XML with a DOCTYPE declaration is not accepted")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"Not valid XML: {exc}") from None
    if root.tag != "myanimelist":
        raise ValueError(f"Expected a <myanimelist> document, found <{root.tag}>")

    export_type = _import_int(root.find("./myinfo/user_export_type"), 0)
    if export_type == 2:
        media_type = "manga"
    elif export_type == 1:
        media_type = "anime"
    else:
        media_type = "manga" if root.find("manga") is not None else "anime"

    entries: List[Dict[str, Any]] = []
    skipped = 0
    for node in root.findall("manga" if media_type == "manga" else "anime"):
        if media_type == "manga":
            mal_id = _import_int(node.find("manga_mangadb_id"))
            title = _import_text(node.find("manga_title"))
            counts = {"chapters": _import_int(node.find("manga_chapters")),
                      "volumes": _import_int(node.find("manga_volumes")),
                      "my_read_chapters": _import_int(node.find("my_read_chapters")),
                      "my_read_volumes": _import_int(node.find("my_read_volumes"))}
        else:
            mal_id = _import_int(node.find("series_animedb_id"))
            title = _import_text(node.find("series_title"))
            counts = {"episodes": _import_int(node.find("series_episodes")),
                      "my_watched_episodes": _import_int(node.find("my_watched_episodes"))}
        if mal_id <= 0:
            skipped += 1
            continue
        try:
            status = _normalize_status(_import_text(node.find("my_status")), media_type)
        except ValueError:
            skipped += 1
            continue

        entry: Dict[str, Any] = {
            "id": mal_id,
            "anilist_id": 0,
            "title": title or f"MAL {mal_id}",
            "type": "Manga" if media_type == "manga" else "TV",
            "status": status,
            "score": max(0, min(10, _import_int(node.find("my_score")))),
            "can_export_xml": True,
            "can_write_anilist": False,   # no AniList id until it is looked up
            "imported": True,
        }
        entry.update(counts)
        for xml_tag, key in (("my_start_date", "start_date"), ("my_finish_date", "finish_date")):
            kept = _import_date(_import_text(node.find(xml_tag)))
            if kept and kept != "0000-00-00":
                entry[key] = kept
        for xml_tag, key in (("my_comments", "comments"), ("my_tags", "tags")):
            text = _import_text(node.find(xml_tag))
            if text:
                entry[key] = text
        entries.append(entry)

    return {"ok": True, "media_type": media_type, "entries": entries, "skipped": skipped}


def _export_state() -> Dict[str, Any]:
    """What the server actually holds, so the client never guesses."""
    state: Dict[str, Any] = {"active_media": EXPORT_MEDIA_TYPE}
    for media in ("anime", "manga"):
        xml_path, gz_path = _export_paths(media)
        xml_name, gz_name = _export_filenames(media)
        info: Dict[str, Any] = {
            "xml": xml_path.is_file(), "gz": gz_path.is_file(),
            "xml_name": xml_name, "gz_name": gz_name,
            "count": None, "updated_at": None,
            "archive": _latest_archive(media),
        }
        if info["xml"]:
            try:
                root = ET.fromstring(xml_path.read_bytes())
                info["count"] = len(root.findall("manga" if media == "manga" else "anime"))
            except Exception:
                pass
            try:
                info["updated_at"] = int(xml_path.stat().st_mtime)
            except Exception:
                pass
        state[media] = info
    return state

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AniExport &mdash; Universal XML Generator for MAL &amp; AniList</title>
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Crect width='24' height='24' rx='5' fill='%233FA796'/%3E%3Cpath d='M12 4 4 9l8 5 8-5-8-5zm-8 8 8 5 8-5' fill='none' stroke='white' stroke-width='2' stroke-linejoin='round'/%3E%3C/svg%3E">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Figtree:wght@400;500;600;700&family=Fira+Code:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {
      color-scheme: dark;
      --bg: #0C0E12;
      --surface-1: #12161B;
      --surface-2: #191E24;
      --surface-3: #22282F;
      --border: rgba(170, 180, 190, 0.10);
      --border-hover: rgba(170, 180, 190, 0.22);
      --border-focus: var(--accent);
      --text: #EAE8E4;
      --text-muted: #A8AFB9;
      --text-subtle: #8A919B;

      /* Filled-accent pairs. Measured: dark ink on the bright accent is 6.4:1;
         the light theme needs the reverse, white on a darker fill. */
      --accent-strong: #3FA796;
      --on-accent: #06211C;
      --anilist-only-text: #8FD6C8;
      --danger-text: #DD9089;
      --accent: #3FA796;
      --accent-hover: #348D7E;
      --accent-active: #2B766A;
      --accent-text: #7CC7B9;
      --accent-bg: rgba(63, 167, 150, 0.14);
      --accent-tint: rgba(63, 167, 150, 0.30);

      --success: #82B489;
      --success-bg: rgba(130, 180, 137, 0.12);
      --warning: #C9A55C;
      --warning-bg: rgba(201, 165, 92, 0.12);
      --danger: #C96F62;
      --danger-bg: rgba(201, 111, 98, 0.12);
      --planned-text: #9AA4B2;
      --planned-bg: rgba(154, 164, 178, 0.12);
      --paused-text: #A8ADB5;
      --paused-bg: rgba(168, 173, 181, 0.12);

      --panel-tint: rgba(18, 22, 27, 0.6);
      --table-tint: rgba(18, 22, 27, 0.4);
      --row-hover: rgba(170, 180, 190, 0.08);
      --row-excluded-bg: rgba(18, 22, 27, 0.35);
      --card-tint: rgba(25, 30, 36, 0.5);

      --font-sans: 'Figtree', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      --font-mono: 'Fira Code', ui-monospace, SFMono-Regular, monospace;
      --radius-sm: 6px;
      --radius-md: 10px;
      --radius-lg: 14px;
      --shadow-sm: 0 1px 2px 0 rgba(6, 8, 12, 0.40);
      --shadow-md: 0 4px 14px 0 rgba(6, 8, 12, 0.45);
      --shadow-lg: 0 12px 30px 0 rgba(6, 8, 12, 0.55);
    }

    body.light {
      color-scheme: light;
      --bg: #EFEBE4;
      --surface-1: #F7F5F0;
      --surface-2: #FFFFFF;
      --surface-3: #E9E5DD;
      --border: rgba(40, 44, 50, 0.10);
      --border-hover: rgba(40, 44, 50, 0.22);
      --text: #1E2126;
      --text-muted: #525963;
      --text-subtle: #6A717B;

      --accent-strong: #1F5B50;
      --on-accent: #FFFFFF;
      --anilist-only-text: #1A6B5C;
      --danger-text: #9A3B30;
      /* Declared on :root as var(--accent), which freezes it to the dark value;
         it has to be restated here or light surfaces use the dark accent. */
      --border-focus: #2E8577;
      --accent: #2E8577;
      --accent-hover: #276F63;
      --accent-active: #1F5B50;
      --accent-text: #23705F;
      --accent-bg: rgba(46, 133, 119, 0.10);
      --accent-tint: rgba(46, 133, 119, 0.24);

      --success: #43764A;
      --success-bg: rgba(67, 118, 74, 0.10);
      --warning: #7A5C1E;
      --warning-bg: rgba(151, 116, 43, 0.10);
      --danger: #A85246;
      --danger-bg: rgba(168, 82, 70, 0.10);
      --planned-text: #5D6572;
      --planned-bg: rgba(93, 101, 114, 0.10);
      --paused-text: #6B727C;
      --paused-bg: rgba(107, 114, 124, 0.12);

      --panel-tint: rgba(247, 245, 240, 0.75);
      --table-tint: rgba(247, 245, 240, 0.6);
      --row-hover: rgba(40, 44, 50, 0.05);
      --row-excluded-bg: rgba(40, 44, 50, 0.05);
      --card-tint: rgba(255, 255, 255, 0.55);

      --shadow-sm: 0 1px 2px 0 rgba(60, 56, 48, 0.10);
      --shadow-md: 0 4px 14px 0 rgba(60, 56, 48, 0.10);
      --shadow-lg: 0 12px 30px 0 rgba(60, 56, 48, 0.14);
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    ::selection {
      background: var(--accent);
      color: #FFFFFF;
    }

    ::-webkit-scrollbar {
      width: 6px;
      height: 6px;
    }
    ::-webkit-scrollbar-track {
      background: var(--surface-1);
    }
    ::-webkit-scrollbar-thumb {
      background: var(--surface-3);
      border-radius: 3px;
    }
    ::-webkit-scrollbar-thumb:hover {
      background: rgba(170, 180, 190, 0.25);
    }

    html {
      scroll-behavior: smooth;
    }

    body {
      font-family: var(--font-sans);
      background-color: var(--bg);
      color: var(--text);
      line-height: 1.5;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
    }

    body::before {
      content: '';
      position: fixed;
      inset: 0;
      pointer-events: none;
      z-index: 60;
      opacity: 0.16;
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2'/%3E%3C/filter%3E%3Crect width='160' height='160' filter='url(%23n)' opacity='0.55'/%3E%3C/svg%3E");
    }

    body.light::before {
      opacity: 0.07;
    }

    /* Header */
    header {
      background: var(--surface-1);
      border-bottom: 1px solid var(--border);
      padding: 0.875rem 1.75rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      position: sticky;
      top: 0;
      z-index: 40;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }

    .brand-logo {
      width: 32px;
      height: 32px;
      background: var(--accent);
      border-radius: var(--radius-sm);
      display: flex;
      align-items: center;
      justify-content: center;
      color: #FFFFFF;
      box-shadow: 0 2px 10px rgba(63, 167, 150, 0.22);
    }

    .brand-logo svg {
      width: 18px;
      height: 18px;
    }

    .brand-title {
      font-size: 1.125rem;
      font-weight: 700;
      letter-spacing: -0.02em;
      color: var(--text);
    }

    .brand-subtitle {
      font-size: 0.75rem;
      color: var(--text-muted);
      font-weight: 500;
    }

    .header-actions {
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }

    .badge {
      display: inline-flex;
      align-items: center;
      gap: 0.375rem;
      padding: 0.25rem 0.625rem;
      font-size: 0.75rem;
      font-weight: 500;
      border-radius: 9999px;
      background: var(--surface-2);
      border: 1px solid var(--border);
      color: var(--text-muted);
    }

    .badge-dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--accent);
    }

    /* Main Container */
    main {
      flex: 1;
      max-width: 1440px;
      width: 100%;
      margin: 0 auto;
      padding: 1.5rem;
      display: grid;
      grid-template-columns: 460px 1fr;
      gap: 1.5rem;
    }

    @media (max-width: 1080px) {
      main {
        grid-template-columns: 1fr;
      }
    }

    /* Panels */
    .panel {
      background: var(--surface-1);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      display: flex;
      flex-direction: column;
      overflow: hidden;
      box-shadow: var(--shadow-sm);
    }

    .panel-header {
      padding: 1rem 1.25rem;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      background: var(--panel-tint);
    }

    .panel-title {
      font-size: 0.9375rem;
      font-weight: 600;
      letter-spacing: -0.01em;
      color: var(--text);
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }

    .panel-title svg {
      width: 16px;
      height: 16px;
      color: var(--text-muted);
    }

    .panel-body {
      padding: 1.25rem;
      flex: 1;
      display: flex;
      flex-direction: column;
      gap: 1.125rem;
    }

    /* Form controls */
    label {
      display: block;
      font-size: 0.8125rem;
      font-weight: 500;
      color: var(--text-muted);
      margin-bottom: 0.375rem;
    }

    .textarea-wrapper {
      position: relative;
      flex: 1;
      min-height: 380px;
      display: flex;
      flex-direction: column;
    }

    textarea {
      width: 100%;
      flex: 1;
      min-height: 380px;
      padding: 0.875rem 1rem;
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      color: var(--text);
      font-size: 0.8125rem;
      line-height: 1.6;
      resize: vertical;
      outline: none;
      transition: border-color 150ms ease, box-shadow 150ms ease;
    }

    textarea:focus {
      border-color: var(--border-focus);
      box-shadow: 0 0 0 2px var(--accent-bg);
    }

    .line-counter {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0.375rem 0.25rem;
      font-size: 0.75rem;
      color: var(--text-subtle);
    }

    .input-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 0.75rem;
    }

    select, input[type="text"], input[type="number"] {
      width: 100%;
      padding: 0.5rem 0.75rem;
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      color: var(--text);
      font-family: var(--font-sans);
      font-size: 0.8125rem;
      outline: none;
      transition: border-color 150ms ease;
    }

    select:focus, input:focus {
      border-color: var(--border-focus);
    }

    /* Buttons */
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 0.5rem;
      padding: 0.5625rem 1rem;
      font-size: 0.8125rem;
      font-weight: 600;
      font-family: var(--font-sans);
      border-radius: var(--radius-sm);
      border: 1px solid transparent;
      cursor: pointer;
      transition: background-color 150ms ease, border-color 150ms ease, opacity 150ms ease;
      text-decoration: none;
      white-space: nowrap;
    }

    .btn:disabled,
    .btn.disabled,
    .btn[aria-disabled="true"] {
      opacity: 0.4;
      pointer-events: none;
      cursor: not-allowed;
    }

    .btn svg {
      width: 15px;
      height: 15px;
    }

    .btn-primary {
      background: var(--accent);
      color: #FFFFFF;
      box-shadow: 0 1px 3px rgba(6, 8, 12, 0.35);
    }
    .btn-primary:hover:not(:disabled) {
      background: var(--accent-hover);
    }
    .btn-primary:active:not(:disabled) {
      background: var(--accent-active);
    }

    .btn-secondary {
      background: var(--surface-2);
      color: var(--text);
      border-color: var(--border);
    }
    .btn-secondary:hover:not(:disabled) {
      background: var(--surface-3);
      border-color: var(--border-hover);
    }

    .btn:active:not(:disabled) {
      transform: translateY(1px);
    }

    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after {
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
        scroll-behavior: auto !important;
      }
    }

    :where(button, a, select, input, textarea):focus-visible {
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }

    /* Toggle switch control */
    .toggle-card {
      margin-top: 1rem;
      padding: 0.75rem 0.875rem;
      background: var(--card-tint);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      transition: border-color 150ms ease;
    }
    .toggle-card:hover {
      border-color: var(--border-hover);
    }
    .toggle-control {
      display: flex;
      align-items: center;
      gap: 0.75rem;
      cursor: pointer;
      user-select: none;
      margin-bottom: 0;
    }
    .toggle-control input {
      position: absolute;
      opacity: 0;
      width: 0;
      height: 0;
    }
    .toggle-switch {
      width: 38px;
      height: 22px;
      background: var(--surface-3);
      border-radius: 9999px;
      position: relative;
      flex-shrink: 0;
      transition: background-color 200ms ease;
      border: 1px solid var(--border);
    }
    .toggle-switch::after {
      content: '';
      position: absolute;
      top: 2px;
      left: 2px;
      width: 16px;
      height: 16px;
      background: #FFFFFF;
      border-radius: 50%;
      transition: transform 200ms cubic-bezier(0.16, 1, 0.3, 1);
      box-shadow: 0 1px 2px rgba(0, 0, 0, 0.3);
    }
    .toggle-control input:checked + .toggle-switch {
      background: var(--accent);
      border-color: var(--accent-hover);
    }
    .toggle-control input:checked + .toggle-switch::after {
      transform: translateX(16px);
    }
    .toggle-control input:focus-visible + .toggle-switch {
      outline: 2px solid var(--border-focus);
      outline-offset: 2px;
    }
    .toggle-info {
      display: flex;
      flex-direction: column;
      gap: 0.125rem;
    }
    .toggle-title {
      font-size: 0.8125rem;
      font-weight: 600;
      color: var(--text);
    }
    .toggle-desc {
      font-size: 0.6875rem;
      color: var(--text-muted);
    }

    /* Action bar */
    .panel-footer {
      padding: 1rem 1.25rem;
      border-top: 1px solid var(--border);
      background: var(--panel-tint);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
    }

    /* Right column: Export Banner & Results Table */
    .export-card {
      background: var(--surface-1);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      padding: 1.25rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1.5rem;
      box-shadow: var(--shadow-sm);
    }

    .export-info h3 {
      font-size: 1rem;
      font-weight: 600;
      color: var(--text);
      letter-spacing: -0.01em;
      margin-bottom: 0.25rem;
    }

    .export-info p {
      font-size: 0.8125rem;
      color: var(--text-muted);
    }

    .export-buttons {
      display: flex;
      align-items: center;
      gap: 0.625rem;
      flex-shrink: 0;
    }

    .text-link {
      color: var(--text-muted);
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      gap: 3px;
      transition: color 120ms ease;
    }

    .text-link:hover {
      color: var(--text);
    }

    /* Stats strip */
    .stats-strip {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 0.75rem;
    }

    @media (max-width: 640px) {
      .stats-strip {
        grid-template-columns: repeat(2, 1fr);
      }
    }

    .stat-box {
      background: var(--surface-1);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      padding: 0.875rem 1rem;
    }

    .stat-label {
      font-size: 0.75rem;
      color: var(--text-subtle);
      font-weight: 500;
    }

    .stat-value {
      font-size: 1.375rem;
      font-weight: 700;
      color: var(--text);
      margin-top: 0.25rem;
      font-variant-numeric: tabular-nums;
    }

    /* Table Container */
    .table-panel {
      flex: 1;
      display: flex;
      flex-direction: column;
      background: var(--surface-1);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      overflow: hidden;
    }

    .table-toolbar {
      padding: 0.875rem 1.25rem;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      background: var(--table-tint);
    }

    .search-input-wrapper {
      position: relative;
      flex: 1;
      max-width: 340px;
    }

    .search-input-wrapper input {
      padding-left: 2.25rem;
    }

    .search-icon {
      position: absolute;
      left: 0.75rem;
      top: 50%;
      transform: translateY(-50%);
      width: 14px;
      height: 14px;
      color: var(--text-subtle);
    }

    .table-wrapper {
      flex: 1;
      overflow-y: auto;
      max-height: 520px;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      text-align: left;
      font-size: 0.8125rem;
    }

    thead {
      position: sticky;
      top: 0;
      background: var(--surface-2);
      z-index: 10;
    }

    th {
      padding: 0.75rem 1rem;
      font-size: 0.6875rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-muted);
      border-bottom: 1px solid var(--border);
    }

    td {
      padding: 0.6875rem 1rem;
      border-bottom: 1px solid var(--border);
      color: var(--text);
    }

    tbody tr:hover {
      background: var(--row-hover);
    }

    .table-checkbox {
      width: 15px;
      height: 15px;
      accent-color: var(--accent);
      cursor: pointer;
      vertical-align: middle;
    }

    tr.row-excluded {
      opacity: 0.45;
      background: var(--row-excluded-bg);
    }

    tr.row-excluded:hover {
      background: var(--row-hover);
    }

    tr.row-excluded .cell-title {
      text-decoration: line-through;
      color: var(--text-muted);
    }

    tr.row-excluded .tag-type,
    tr.row-excluded .status-pill {
      opacity: 0.6;
    }

    .cell-title {
      font-weight: 500;
    }

    .cell-original {
      font-size: 0.75rem;
      color: var(--text-subtle);
      margin-top: 0.125rem;
    }

    .tag-type {
      display: inline-block;
      padding: 0.125rem 0.375rem;
      font-size: 0.6875rem;
      font-weight: 600;
      border-radius: var(--radius-sm);
      background: var(--accent-bg);
      color: var(--accent-text);
    }

    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 0.375rem;
      padding: 0.1875rem 0.5rem;
      font-size: 0.6875rem;
      font-weight: 500;
      border-radius: 9999px;
    }
    .status-completed {
      background: var(--success-bg);
      color: var(--success);
    }
    .status-watching {
      background: var(--accent-bg);
      color: var(--accent-text);
    }
    .status-planned {
      background: var(--planned-bg);
      color: var(--planned-text);
    }
    .status-paused {
      background: var(--paused-bg);
      color: var(--paused-text);
    }
    .status-dropped {
      background: var(--danger-bg);
      color: var(--danger);
    }
    .status-unresolved {
      background: var(--danger-bg);
      color: var(--danger);
    }

    .run-controls {
      display: none;
      gap: 0.5rem;
      margin-top: 0.75rem;
    }
    .run-controls.show { display: flex; }

    .chip-row {
      display: flex;
      flex-wrap: wrap;
      gap: 0.375rem;
      padding: 0 1rem 0.625rem;
    }
    .chip {
      padding: 0.1875rem 0.625rem;
      font-size: 0.6875rem;
      font-weight: 500;
      color: var(--text-muted);
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: 9999px;
      cursor: pointer;
    }
    .chip.active {
      color: #fff;
      background: var(--border-focus);
      border-color: var(--border-focus);
    }

    th.sortable { cursor: pointer; user-select: none; }
    th.sortable:hover { color: var(--text-primary); }
    th.sortable:focus-visible {
      outline: 2px solid var(--accent);
      outline-offset: -2px;
    }
    th .sort-mark { font-size: 0.625rem; color: var(--text-subtle); margin-left: 0.25rem; }

    .cover-thumb {
      width: 32px;
      height: 45px;
      object-fit: cover;
      border-radius: 3px;
      background: var(--surface-2);
      flex-shrink: 0;
    }
    .title-row { display: flex; gap: 0.5rem; align-items: flex-start; }

    .fix-row {
      display: flex;
      gap: 0.3125rem;
      margin-top: 0.3125rem;
    }
    .fix-input {
      flex: 1;
      min-width: 90px;
      padding: 0.1875rem 0.375rem;
      font-size: 0.6875rem;
      font-family: inherit;
      color: var(--text-primary);
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: 4px;
    }
    .fix-input:focus { outline: none; border-color: var(--border-focus); }

    .detail-row td {
      background: var(--surface-2);
      padding: 0.625rem 0.75rem;
    }
    .detail-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
      gap: 0.5rem;
    }
    .detail-grid label {
      display: flex;
      flex-direction: column;
      gap: 0.1875rem;
      margin: 0;
      font-size: 0.6875rem;
      color: var(--text-subtle);
    }
    .detail-grid input {
      padding: 0.25rem 0.375rem;
      font-size: 0.6875rem;
      font-family: inherit;
      color: var(--text-primary);
      background: var(--surface-1);
      border: 1px solid var(--border);
      border-radius: 4px;
    }
    .detail-grid input:focus { outline: none; border-color: var(--border-focus); }
    .btn-detail {
      background: none;
      border: none;
      color: var(--text-subtle);
      cursor: pointer;
      font-size: 0.875rem;
      padding: 0 0.25rem;
    }
    .btn-detail:hover { color: var(--text-primary); }

    .modal-backdrop {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(2, 6, 23, 0.72);
      z-index: 60;
      padding: 2rem;
    }
    .modal-backdrop.show { display: flex; align-items: center; justify-content: center; }
    .modal-card {
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
      width: min(900px, 100%);
      max-height: 100%;
      padding: 1rem;
      background: var(--surface-1);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
    }
    .modal-card pre {
      flex: 1;
      overflow: auto;
      margin: 0;
      padding: 0.75rem;
      font-size: 0.6875rem;
      line-height: 1.5;
      color: var(--text-muted);
      background: var(--surface-2);
      border-radius: 6px;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .modal-head { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; }

    .anilist-panel {
      display: none;
      flex-direction: column;
      gap: 0.375rem;
    }
    .anilist-panel.show { display: flex; }
    .token-row { display: flex; gap: 0.375rem; }
    .token-row input { flex: 1; }
    .token-status {
      font-size: var(--font-xs);
      line-height: 1.45;
      color: var(--text-muted);
    }
    .token-status.ok { color: var(--success); }
    .token-status.bad { color: var(--danger); }

    .write-plan {
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
      font-size: var(--font-sm);
      color: var(--text-muted);
    }
    .write-plan strong { color: var(--text); }
    .write-plan .warn { color: var(--warning); }


    .diff-list {
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
      max-height: 46vh;
      overflow-y: auto;
      padding: 0.5rem;
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      background: var(--table-tint);
    }
    .diff-row {
      display: flex;
      flex-direction: column;
      gap: 0.25rem;
      padding: 0.5rem 0.625rem;
      border-radius: var(--radius-md);
      background: var(--surface-2);
    }
    .diff-head {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      overflow-wrap: anywhere;
      font-size: var(--font-sm);
      color: var(--text);
      font-weight: 600;
    }
    .diff-tag {
      font-size: var(--font-xs);
      font-weight: 500;
      color: var(--text-muted);
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 0 0.4rem;
    }
    .diff-line {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 0.375rem;
      font-size: var(--font-xs);
      line-height: 1.5;
      /* A note read back from AniList can be 60 unbroken characters. */
      overflow-wrap: anywhere;
    }
    .diff-field {
      min-width: 5.5rem;
      color: var(--text-muted);
    }
    .diff-from { color: var(--text-muted); }
    .diff-to { color: var(--text); font-variant-numeric: tabular-nums; }
    .diff-to.overridden { color: var(--text-subtle); text-decoration: line-through; }
    .diff-arrow { color: var(--text-muted); }
    .diff-note { color: var(--text-muted); }
    .diff-note.warn { color: var(--warning); }
    .diff-line.blocked .diff-to { color: var(--warning); }
    .diff-pick {
      display: inline-flex;
      align-items: center;
      gap: 0.3rem;
      cursor: pointer;
      color: var(--text-muted);
    }
    .diff-pick input { cursor: pointer; }

    .fix-counter {
      font-size: var(--font-sm);
      color: var(--text-muted);
      font-variant-numeric: tabular-nums;
    }
    /* This dialog is the one place a person has to read carefully and decide,
       so it is the widest card and the only one that steps the type up. */
    .fix-card { width: min(1100px, 100%); padding: 1.125rem 1.25rem; }
    .fix-body { display: flex; flex-direction: column; gap: 0.875rem; min-height: 0; }
    .fix-original { font-size: var(--font-md); color: var(--text-muted); }
    .fix-original strong { color: var(--text); }
    .fix-search { display: flex; gap: 0.5rem; }
    .fix-search input { flex: 1; font-size: var(--font-md); }
    .fix-search select { width: auto; min-height: var(--control-md); padding-top: 0; padding-bottom: 0; font-size: var(--font-sm); }
    .fix-results {
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
      max-height: 60vh;
      overflow-y: auto;
      padding-right: 0.25rem;
    }
    .fix-hint { font-size: var(--font-sm); color: var(--text-muted); padding: 0.75rem 0; }
    .fix-result {
      display: flex;
      gap: 0.875rem;
      align-items: flex-start;
      width: 100%;
      padding: 0.75rem;
      text-align: left;
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: var(--control-radius);
      color: var(--text);
      cursor: pointer;
      font-family: inherit;
      font-size: var(--font-md);
    }
    .fix-result:hover { border-color: var(--accent); }
    .fix-result:focus-visible { outline: none; border-color: var(--accent); box-shadow: var(--ring); }
    .fix-result .meta {
      display: flex;
      gap: 0.625rem;
      flex-wrap: wrap;
      font-size: var(--font-sm);
      color: var(--text-muted);
    }
    .fix-result .grow { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 0.3125rem; }
    .fix-result .name {
      font-size: 1rem;
      font-weight: 600;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .fix-result .alt {
      font-size: var(--font-sm);
      color: var(--text-muted);
      overflow-wrap: anywhere;
    }
    .fix-result .cover-thumb { width: 72px; height: 101px; border-radius: 5px; }
    .fix-result .synonyms {
      font-size: var(--font-sm);
      color: var(--text-muted);
      overflow-wrap: anywhere;
    }
    .fix-result .synonyms b { color: var(--text-muted); font-weight: 600; }
    .fix-result .desc {
      font-size: var(--font-sm);
      line-height: 1.5;
      color: var(--text-muted);
    }
    .fix-genres { display: flex; flex-wrap: wrap; gap: 0.25rem; }
    .fix-genre {
      padding: 0.0625rem 0.4375rem;
      font-size: var(--font-xs);
      color: var(--text-muted);
      background: var(--surface-1);
      border: 1px solid var(--border);
      border-radius: 999px;
    }
    .fix-flag {
      padding: 0.0625rem 0.4375rem;
      font-size: var(--font-xs);
      font-weight: 600;
      border-radius: 999px;
      background: var(--accent-bg);
      color: var(--accent-text);
    }
    .fix-flag.warn { background: var(--warning-bg); color: var(--warning); }

    .destination-prompt {
      display: none;
      flex-direction: column;
      gap: 0.375rem;
      padding: 0.75rem 0.875rem;
      background: var(--accent-bg);
      border: 1px solid var(--accent-tint);
      border-radius: var(--radius-lg);
      font-size: var(--font-xs);
      color: var(--text-muted);
    }
    .destination-prompt.show { display: flex; }
    .destination-prompt strong { color: var(--text); font-size: var(--font-sm); }
    .destination-choice { display: flex; gap: 0.375rem; margin-top: 0.125rem; }

    .status-anilist-only {
      background: var(--accent-bg);
      color: var(--accent-text);
    }

    .row-status {
      font-family: inherit;
      font-size: 0.6875rem;
      font-weight: 500;
      border: 1px solid transparent;
      border-radius: 9999px;
      padding: 0.1875rem 0.5rem;
      cursor: pointer;
      max-width: 100%;
    }
    .row-status:focus {
      outline: none;
      border-color: var(--border-focus);
    }
    .row-status:disabled {
      opacity: 0.45;
      cursor: not-allowed;
    }

    .unresolved-bar {
      display: none;
      align-items: center;
      gap: 0.75rem;
      margin: 0 1rem 0.75rem;
      padding: 0.625rem 0.875rem;
      background: var(--danger-bg);
      border: 1px solid var(--danger);
      border-radius: var(--radius-lg);
      font-size: 0.75rem;
      color: var(--text-muted);
    }
    .unresolved-bar.show { display: flex; }

    .progress-cell {
      display: flex;
      flex-direction: column;
      gap: 0.25rem;
    }
    .progress-cell label {
      display: flex;
      align-items: center;
      gap: 0.3125rem;
      margin: 0;
      font-size: 0.6875rem;
      color: var(--text-subtle);
    }
    .progress-input {
      width: 58px;
      padding: 0.125rem 0.3125rem;
      font-size: 0.6875rem;
      font-family: inherit;
      color: var(--text);
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: 4px;
    }
    .progress-input:focus {
      outline: none;
      border-color: var(--border-focus);
    }
    .progress-input:disabled {
      opacity: 0.4;
      cursor: not-allowed;
    }
    .match-picker {
      width: 100%;
      margin-top: 0.3125rem;
      padding: 0.1875rem 0.3125rem;
      font-size: 0.6875rem;
      font-family: inherit;
      color: var(--text-muted);
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: 4px;
    }
    .match-picker:focus {
      outline: none;
      border-color: var(--border-focus);
    }
    .unresolved-bar strong { color: var(--danger); }

    /* ---- redesign pass -------------------------------------------------
       The table is the work surface. Everything above it was competing with
       it: four stat cards, a warning band, a chip band and a toolbar band,
       stacked, pushed the rows below the fold. They are one line each now. */

    .stats-strip {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: 0 2rem;
      margin: 0 0 1rem;
      padding: 0.625rem 1rem;
      background: var(--surface-1);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
    }
    .stat-box {
      display: flex;
      align-items: baseline;
      gap: 0.5rem;
      padding: 0;
      background: none;
      border: none;
    }
    .stat-label {
      font-size: 0.6875rem;
      font-weight: 500;
      letter-spacing: 0.02em;
      color: var(--text-subtle);
      text-transform: none;
    }
    .stat-value {
      font-size: 0.9375rem;
      font-weight: 600;
      font-variant-numeric: tabular-nums;
      color: var(--text-primary);
    }

    .table-toolbar {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 0.625rem;
      padding: 0.75rem 1rem;
    }
    .table-toolbar .search-input-wrapper { flex: 0 1 280px; }
    .toolbar-end {
      margin-left: auto;
      display: flex;
      align-items: center;
      flex-wrap: wrap;          /* three buttons now; they must wrap, not overflow */
      gap: 0.625rem;
    }
    .filter-count {
      font-size: 0.75rem;
      font-variant-numeric: tabular-nums;
      color: var(--text-subtle);
    }
    .chip-row { padding: 0; gap: 0.25rem; }
    .chip {
      padding: 0.1875rem 0.5625rem;
      font-size: 0.6875rem;
      font-variant-numeric: tabular-nums;
      transition: background 160ms ease, color 160ms ease, border-color 160ms ease;
    }
    .chip:hover { color: var(--text-primary); border-color: var(--text-subtle); }

    .unresolved-bar {
      margin: 0 1rem 0.75rem;
      padding: 0.5rem 0.75rem;
      font-size: 0.75rem;
      line-height: 1.45;
    }
    .btn-xs { padding: 0.1875rem 0.5rem; font-size: 0.6875rem; white-space: nowrap; }
    .btn-danger.btn-danger {
      color: var(--danger);
      border-color: var(--danger);
      background: var(--danger-bg);
    }

    /* ---- table ---- */
    #animeTextInput { min-height: 232px; }

    #animeTable thead th {
      color: var(--text-muted);
      position: sticky;
      top: 0;
      z-index: 2;
      padding: 0.5rem 0.75rem;
      font-size: 0.6875rem;
      font-weight: 600;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--text-subtle);
      background: var(--surface-2);
      white-space: nowrap;
    }
    #animeTable tbody td {
      padding: 0.5rem 0.75rem;
      vertical-align: middle;
      border-top: 1px solid var(--border);
    }
    #animeTable tbody tr:hover > td { background: rgba(148, 163, 184, 0.05); }

    .title-row { gap: 0.625rem; align-items: center; }
    .title-stack { min-width: 0; flex: 1; display: flex; flex-direction: column; gap: 0.125rem; }
    .cell-title {
      font-size: 0.8125rem;
      font-weight: 500;
      color: var(--text-primary);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .title-meta {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 0 0.625rem;
      font-size: 0.6875rem;
      color: var(--text-subtle);
      min-width: 0;
    }
    .title-meta .id-link {
      font-variant-numeric: tabular-nums;
      color: var(--text-muted);
      text-decoration: none;
    }
    .title-meta .id-link:hover { color: var(--border-focus); text-decoration: underline; }
    .id-missing { color: var(--danger); }
    .cell-original {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      max-width: 22ch;
    }
    .cover-thumb { width: 30px; height: 42px; border-radius: 3px; }
    .cover-empty { display: block; background: rgba(148, 163, 184, 0.07); }

    .tag-type { font-size: 0.6875rem; }
    .tag-year {
      display: block;
      margin-top: 0.1875rem;
      font-size: 0.6875rem;
      font-variant-numeric: tabular-nums;
      color: var(--text-subtle);
    }

    .progress-cell { gap: 0.3125rem; }
    .progress-line {
      display: flex;
      align-items: center;
      gap: 0.375rem;
      margin: 0;
      font-size: 0.6875rem;
      color: var(--text-subtle);
    }
    .progress-input {
      width: 54px;
      text-align: right;
      font-variant-numeric: tabular-nums;
    }
    .progress-total { white-space: nowrap; font-variant-numeric: tabular-nums; }

    /* Status reads as a value you can change, not a button pretending to be a pill. */
    .row-status {
      width: 100%;
      min-width: 112px;
      padding: 0.25rem 0.4375rem;
      border-radius: 6px;
      border: 1px solid transparent;
      background-color: transparent;
      font-size: 0.6875rem;
      font-weight: 500;
    }
    .row-status:hover:not(:disabled) { border-color: var(--border); }

    .fix-row { margin-top: 0.375rem; gap: 0.25rem; }
    .match-picker {
      margin-top: 0.25rem;
      max-width: 100%;
      font-size: 0.6875rem;
      color: var(--text-subtle);
    }

    .btn-detail {
      border-radius: 6px;
      line-height: 1;
      padding: 0.25rem 0.375rem;
      transition: background 160ms ease, color 160ms ease;
    }
    .btn-detail:hover { background: var(--surface-2); }

    .detail-row td { padding: 0.75rem 1rem 0.875rem; border-top: none; }
    .detail-head {
      margin-bottom: 0.5rem;
      font-size: 0.6875rem;
      color: var(--text-subtle);
    }
    .detail-head strong { color: var(--text-muted); font-weight: 600; }
    .detail-grid { grid-template-columns: repeat(auto-fit, minmax(168px, 1fr)); gap: 0.625rem 0.875rem; }
    .detail-wide { grid-column: 1 / -1; }
    .detail-grid input { padding: 0.3125rem 0.4375rem; font-size: 0.75rem; }

    @media (max-width: 1180px) {
      .title-meta .cell-original { display: none; }
      .stats-strip { gap: 0 1.25rem; }
    }

    /* Live Progress Card */
    .progress-card {
      background: var(--surface-1);
      border: 1px solid var(--accent-tint);
      border-radius: var(--radius-lg);
      padding: 1rem 1.25rem;
      box-shadow: var(--shadow-md), 0 0 0 1px var(--accent-bg);
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
      animation: fadeInProgress 200ms ease;
    }

    @keyframes fadeInProgress {
      from { opacity: 0; transform: translateY(-4px); }
      to { opacity: 1; transform: translateY(0); }
    }

    .progress-card-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
    }

    .progress-card-status {
      display: flex;
      align-items: center;
      gap: 0.75rem;
      min-width: 0;
      flex: 1;
    }

    .progress-spinner {
      width: 18px;
      height: 18px;
      border: 2px solid var(--accent-bg);
      border-top-color: var(--accent);
      border-radius: 50%;
      flex-shrink: 0;
      animation: spin-progress 0.7s linear infinite;
    }

    @keyframes spin-progress {
      to { transform: rotate(360deg); }
    }

    .progress-current-title {
      font-size: 0.875rem;
      font-weight: 600;
      color: var(--text);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .progress-current-sub {
      font-size: 0.75rem;
      color: var(--text-muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .progress-badge-group {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      flex-shrink: 0;
    }

    .progress-count-pill {
      font-family: var(--font-mono);
      font-size: 0.75rem;
      font-weight: 600;
      padding: 0.25rem 0.625rem;
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: 9999px;
      color: var(--text);
    }

    .progress-percent-pill {
      font-family: var(--font-mono);
      font-size: 0.75rem;
      font-weight: 700;
      padding: 0.25rem 0.625rem;
      background: var(--accent-bg);
      border-radius: 9999px;
      color: var(--accent-text);
    }

    .progress-track {
      width: 100%;
      height: 8px;
      background: var(--surface-2);
      border-radius: 9999px;
      overflow: hidden;
      position: relative;
    }

    .progress-fill {
      width: 100%;
      height: 100%;
      background: var(--accent);
      border-radius: 9999px;
      transform-origin: left center;
      transform: scaleX(0);
      transition: transform 180ms cubic-bezier(0.16, 1, 0.3, 1);
    }

    /* Empty state */
    .empty-state {
      padding: 3.5rem 1.5rem;
      text-align: center;
      color: var(--text-muted);
    }
    .empty-state svg {
      width: 40px;
      height: 40px;
      color: var(--text-subtle);
      margin-bottom: 0.75rem;
    }
    .empty-state h4 {
      font-size: 0.9375rem;
      font-weight: 600;
      color: var(--text);
      margin-bottom: 0.25rem;
    }
    .empty-state p {
      font-size: 0.8125rem;
      max-width: 360px;
      margin: 0 auto;
    }

    /* Toast notification */
    .toast {
      position: fixed;
      bottom: 1.5rem;
      right: 1.5rem;
      padding: 0.75rem 1rem;
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      box-shadow: var(--shadow-lg);
      font-size: 0.8125rem;
      color: var(--text);
      display: flex;
      align-items: center;
      gap: 0.5rem;
      transform: translateY(100px);
      opacity: 0;
      transition: transform 200ms cubic-bezier(0.16, 1, 0.3, 1), opacity 200ms ease;
      z-index: 50;
    }
    .toast.show {
      transform: translateY(0);
      opacity: 1;
    }
    .toast.toast-success {
      border-color: var(--success);
    }
    .toast.toast-error {
      border-color: var(--danger);
    }

    /* ====================================================================
       Control system. Audited before this existed: buttons came in four
       heights (23/26/30/37px), inputs in three with two radii, selects in
       three with two radii. Every control now resolves to one of three
       sizes, one radius, one border treatment and one focus ring.
       ==================================================================== */
    :root {
      --control-sm: 26px;      /* inside table rows */
      --control-md: 32px;      /* panels, toolbars */
      --control-lg: 38px;      /* primary action */
      --control-radius: 6px;
      --control-pad-sm: 0.4375rem;
      --control-pad-md: 0.625rem;
      --font-xs: 0.75rem;      /* 12px: smallest text anywhere */
      --font-sm: 0.8125rem;    /* 13px */
      --font-md: 0.875rem;     /* 14px */
      --ring: 0 0 0 2px var(--bg), 0 0 0 4px var(--accent-tint);
    }

    .btn,
    input[type="text"],
    input[type="number"],
    input[type="date"],
    select,
    textarea {
      font-family: var(--font-sans);
      border-radius: var(--control-radius);
      transition: border-color 160ms ease, background-color 160ms ease, color 160ms ease, box-shadow 160ms ease;
    }

    /* Keyboard focus is visible everywhere and identical everywhere. */
    .btn:focus-visible,
    input:focus-visible,
    select:focus-visible,
    textarea:focus-visible,
    .chip:focus-visible,
    .btn-detail:focus-visible,
    a:focus-visible {
      outline: none;
      border-color: var(--accent);
      box-shadow: var(--ring);
    }

    /* ---- medium: the default for panel and toolbar controls ---- */
    /* Doubled class names lift these to (0,2,0) so they beat the base
       `select, input[type="text"], input[type="number"]` rule at (0,1,1).
       Without this the whole control system is silently ignored. */
    .btn.btn,
    .input-grid input,
    .input-grid select,
    #mediaType,
    #searchInput {
      min-height: var(--control-md);
      padding-top: 0;
      padding-bottom: 0;
      font-size: var(--font-sm);
    }
    .btn { padding-left: var(--control-pad-md); padding-right: var(--control-pad-md); }
    #searchInput { min-height: var(--control-md); }

    /* ---- large: one primary action per view ---- */
    #btnProcess { min-height: var(--control-lg); font-size: var(--font-md); font-weight: 600; }
    .header-actions .btn.btn, .panel-header .btn.btn { min-height: var(--control-sm); font-size: var(--font-xs); }

    /* ---- small: dense controls that live inside table rows ---- */
    .btn-xs.btn-xs,
    .chip.chip,
    .progress-input.progress-input,
    .row-status.row-status,
    .fix-input.fix-input,
    .btn-fix.btn-fix,
    .match-picker.match-picker,
    .detail-input.detail-input {
      min-height: var(--control-sm);
      padding-top: 0;
      padding-bottom: 0;
      font-size: var(--font-xs);
      border-radius: var(--control-radius);
    }
    .chip.chip { border-radius: 9999px; padding-left: 0.625rem; padding-right: 0.625rem; }
    .row-status.row-status { border-radius: var(--control-radius); }
    /* The base control rule contributes 12px of horizontal padding; with the
       native number spinner on top, 56px clipped "142" to "14". Reclaim both. */
    .progress-input.progress-input {
      width: 62px;
      padding-left: var(--control-pad-sm);
      padding-right: var(--control-pad-sm);
      -moz-appearance: textfield;
    }
    .progress-input::-webkit-outer-spin-button,
    .progress-input::-webkit-inner-spin-button {
      -webkit-appearance: none;
      margin: 0;
    }
    .fix-input.fix-input,
    .match-picker.match-picker,
    .row-status.row-status,
    .detail-input.detail-input {
      padding-left: var(--control-pad-sm);
      padding-right: var(--control-pad-sm);
    }
    .detail-input.detail-input { min-height: var(--control-md); padding-top: 0; padding-bottom: 0; }

    /* ====================================================================
       Contrast. Audited at 11-12px: chips 2.93, detail head 3.6, stat
       labels / meta / counts 3.9 - all under the 4.5 AA floor for small
       text. Secondary text is lifted and nothing sits below 12px.
       ==================================================================== */
    .stat-label,
    .title-meta,
    .filter-count,
    .progress-total,
    .detail-head,
    .line-counter,
    .progress-line,
    .cell-original,
    .tag-year,
    .toggle-desc,
    .detail-grid label {
      font-size: var(--font-xs);
      color: var(--text-muted);
    }
    .chip { color: var(--text-muted); }
    .chip.active { color: var(--bg); }
    .detail-head strong { color: var(--text); }
    .title-meta .id-link { color: var(--text-muted); }

    /* ====================================================================
       Horizontal overflow fix. A grid item defaults to min-width:auto, so the
       1fr column could not shrink below the table's min-content width (1105px)
       and the whole page scrolled sideways under 1920px. Three parts: let the
       column shrink, let the table scroll inside its own wrapper, and make the
       table obey its column widths instead of its content.
       ==================================================================== */
    main > * { min-width: 0; }
    .table-wrapper { overflow-x: auto; }
    #animeTable { table-layout: fixed; }
    .match-picker.match-picker { width: 100%; }
    .title-row { min-width: 0; }
    #animeTable td { overflow: hidden; }
    .detail-row td { overflow: visible; }

    /* ====================================================================
       Audit fixes. Measured in Chrome, both themes:
       - white on the accent was 2.93:1, below the 4.5 AA floor, on the most
         prominent control in the app. Dark ink on the same accent is 6.4:1 and
         keeps the colour vivid.
       - the AniList token field is type=password, which every rule in this
         file missed (they all target text/number/date), so it rendered as a
         white browser-default box in a dark UI.
       - the destination select and the row detail button sat outside the
         three-size control system.
       ==================================================================== */
    .btn-primary.btn-primary {
      background: var(--accent-strong);
      color: var(--on-accent);
      border-color: var(--accent-strong);
      font-weight: 600;
    }
    .btn-primary.btn-primary:hover {
      background: var(--accent-hover);
      color: var(--on-accent);
    }
    .chip.active {
      background: var(--accent-strong);
      border-color: var(--accent-strong);
      color: var(--on-accent);
    }

    input[type="password"].token-input,
    .token-row input {
      width: 100%;
      min-height: var(--control-md);
      padding: 0 var(--control-pad-md);
      font-family: var(--font-sans);
      font-size: var(--font-sm);
      color: var(--text);
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: var(--control-radius);
    }
    .token-row input::placeholder { color: var(--text-subtle); }
    .token-row input:focus-visible {
      outline: none;
      border-color: var(--accent);
      box-shadow: var(--ring);
    }

    /* The base select rule contributes 8px of vertical padding, which pushed
       this to 37px; the other medium controls zero it out the same way. */
    #destination {
      min-height: var(--control-md);
      padding-top: 0;
      padding-bottom: 0;
      font-size: var(--font-sm);
    }

    /* Four action buttons at 544px kept the page wider than the viewport below
       about 1250px, because a flex item defaults to min-width:auto. */
    .export-card { flex-wrap: wrap; }
    .export-buttons {
      flex-wrap: wrap;
      min-width: 0;
      justify-content: flex-end;
    }
    .export-info { min-width: 0; }

    .btn-detail.btn-detail {
      color: var(--text-muted);
      min-height: var(--control-sm);
      min-width: var(--control-sm);
      font-size: var(--font-sm);
    }

    /* These two sat at 4.45:1, just under the floor, once their translucent
       backgrounds were composited properly. */
    .status-anilist-only { color: var(--anilist-only-text); }
    /* The danger red sat at 4.46:1 on its own tint: a hair under the floor. */
    .status-dropped, .status-unresolved, .id-missing,
    .unresolved-bar strong { color: var(--danger-text); }

    /* Narrow overrides sit at the end on purpose. Every rule in here is a
       plain class, the same specificity as the component rule it overrides,
       so the cascade decides on source order alone. Sitting above the
       components - where this block used to be - silently killed 13 of its
       declarations, including the phone sizing for the match picker and for
       the write diff. Found in Chrome, not by reading. Keep it last. */
    /* Below this the right column has no room to lay things out side by side.
       Measured: at 600px the title column was squeezed to 10px, and at 430px it
       disappeared entirely while the export buttons ran off the page. */
    @media (max-width: 760px) {
      .export-card {
        align-items: stretch;
        gap: 1rem;
      }
      .export-buttons {
        justify-content: flex-start;
        width: 100%;
      }
      .export-buttons .btn {
        flex: 1 1 auto;
        justify-content: center;
      }
      /* decorative, and it wraps to three lines down here */
      .badge { display: none; }
      /* 2rem of backdrop padding each side is a quarter of a phone screen */
      .modal-backdrop { padding: 0.75rem; }
      .fix-card { padding: 0.875rem; }
      .fix-result { gap: 0.625rem; padding: 0.625rem; }
      .fix-result .cover-thumb { width: 52px; height: 73px; }
      .fix-search { flex-wrap: wrap; }
      .write-card { padding: 0.875rem; }
      .diff-list { max-height: 50vh; padding: 0.375rem; }
      .diff-field { min-width: 0; font-weight: 600; }
      .fix-search input { flex: 1 1 100%; }
      .modal-head { flex-wrap: wrap; }
      .toolbar-end {
        margin-left: 0;
        width: 100%;
        justify-content: space-between;
      }
      .unresolved-bar {
        flex-direction: column;
        align-items: stretch;
      }
      .unresolved-bar .btn {
        flex: 1 1 auto;
        justify-content: center;
      }
      /* Scroll the table instead of crushing the title out of existence. */
      #animeTable {
        table-layout: auto;
        min-width: 780px;
      }
    }

  </style>
</head>
<body>

  <!-- Top App Navigation -->
  <header>
    <div class="brand">
      <div class="brand-logo" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
          <polygon points="12 2 2 7 12 12 22 7 12 2"></polygon>
          <polyline points="2 17 12 22 22 17"></polyline>
          <polyline points="2 12 12 17 22 12"></polyline>
        </svg>
      </div>
      <div>
        <h1 class="brand-title">AniExport</h1>
        <p class="brand-subtitle">Plain title lists &rarr; your AniList account, or a MAL-compatible XML file</p>
      </div>
    </div>

    <div class="header-actions">
      <button id="btnTheme" class="btn btn-secondary btn-xs" title="Switch color theme">Light mode</button>
      <div class="badge" title="Universal XML format supported natively by MyAnimeList and AniList import">
        <span class="badge-dot" aria-hidden="true"></span>
        <span>MAL &amp; AniList Compatible</span>
      </div>
    </div>
  </header>

  <!-- Main View -->
  <main>
    <!-- Left Column: Plain text editor and parameters -->
    <section class="panel">
      <div class="panel-header">
        <h2 class="panel-title">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"></path>
            <rect x="8" y="2" width="8" height="4" rx="1" ry="1"></rect>
          </svg>
          <span id="listPanelTitle">Paste Anime List</span>
        </h2>
        <div style="display:flex; gap:0.375rem;">
          <button id="btnSample" class="btn btn-secondary btn-xs" title="Load sample list">
            Sample
          </button>
          <button id="btnClear" class="btn btn-secondary btn-xs" title="Clear text area">
            Clear
          </button>
        </div>
      </div>

      <div class="panel-body">
        <div>
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.375rem;">
            <label for="animeTextInput" style="margin-bottom:0;">Paste your list (one title per line):</label>
            <button id="btnImport" class="btn btn-secondary btn-xs" title="Load an existing MyAnimeList XML export">Import XML</button>
          <input type="file" id="importFile" accept=".xml,.gz" style="display:none;">
          <button id="btnPaste" class="btn btn-secondary btn-xs" title="Paste clipboard content">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:12px; height:12px;">
                <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"></path>
                <rect x="8" y="2" width="8" height="4" rx="1" ry="1"></rect>
              </svg>
              Paste
            </button>
          </div>
          <div class="textarea-wrapper">
            <textarea id="animeTextInput" spellcheck="false" placeholder="Paste anime titles here (one per line):&#10;&#10;Sousou no Frieren&#10;Attack on Titan&#10;Solo Leveling&#10;Dandadan&#10;Chainsaw Man&#10;..."></textarea>
            <div class="line-counter">
              <span id="lineCountLabel">0 titles</span>
              <span style="font-size:0.6875rem;">Strips numbering and bullets</span>
            </div>
          </div>
        </div>

        <div id="anilistPanel" class="anilist-panel">
          <label for="anilistToken">AniList access token:</label>
          <div class="token-row">
            <input type="password" id="anilistToken" class="token-input" autocomplete="off"
                   spellcheck="false" placeholder="Paste your token (kept in this tab only)">
            <button id="btnTokenTest" class="btn btn-secondary btn-xs">Check</button>
          </div>
          <span id="tokenStatus" class="token-status">Not connected. The token is held in this
            browser tab only: never saved to disk, never written to the export files.</span>
        </div>

        <div id="destinationPrompt" class="destination-prompt">
          <strong>Where is this list going?</strong>
          <span>AniList can take titles that MyAnimeList has never heard of. Pick once; change it any time.</span>
          <div class="destination-choice">
            <button class="btn btn-primary btn-xs" data-destination="anilist">AniList (direct)</button>
            <button class="btn btn-secondary btn-xs" data-destination="xml">MAL-compatible XML</button>
          </div>
        </div>

        <div id="destinationField">
          <label for="destination">Importing into:</label>
          <select id="destination">
            <option value="">Choose a destination...</option>
            <option value="anilist">AniList (direct)</option>
            <option value="xml">MAL-compatible XML file</option>
          </select>
        </div>

        <div>
          <label for="mediaType">Media Type:</label>
          <select id="mediaType">
            <option value="anime" selected>Anime</option>
            <option value="manga">Manga</option>
          </select>
        </div>

        <div class="input-grid">
          <div>
            <label for="selectStatus">List Status:</label>
            <select id="selectStatus">
              <option value="Watching">Watching</option>
              <option value="Plan to watch">Plan to watch</option>
              <option value="Completed" selected>Completed</option>
              <option value="Rewatching">Rewatching</option>
              <option value="Paused">Paused</option>
              <option value="Dropped">Dropped</option>
            </select>
          </div>
          <div>
            <label for="inputScore">Score (0-10):</label>
            <input type="number" id="inputScore" min="0" max="10" value="0" placeholder="0 = Unscored">
          </div>
        </div>

        <div class="toggle-card">
          <label class="toggle-control" for="addUnmatchedImports">
            <input type="checkbox" id="addUnmatchedImports" checked>
            <span class="toggle-switch" aria-hidden="true"></span>
            <span class="toggle-info">
              <span class="toggle-title">Add imported entries not in my list</span>
              <span class="toggle-desc">Off: an imported XML only updates rows you already have</span>
            </span>
          </label>
        </div>

        <div class="toggle-card">
          <label class="toggle-control" for="toggleFresh">
            <input type="checkbox" id="toggleFresh">
            <span class="toggle-switch" aria-hidden="true"></span>
            <span class="toggle-info">
              <span class="toggle-title">Ignore saved results</span>
              <span class="toggle-desc">Ask AniList again instead of reusing the 7-day cache</span>
            </span>
          </label>
        </div>

        <div class="toggle-card">
          <label class="toggle-control" for="toggleArchive">
            <input type="checkbox" id="toggleArchive">
            <span class="toggle-switch" aria-hidden="true"></span>
            <span class="toggle-info">
              <span class="toggle-title">Keep timestamped copies</span>
              <span class="toggle-desc">Also saves a dated copy on every write</span>
            </span>
          </label>
        </div>

        <div class="toggle-card">
          <label class="toggle-control" for="toggleSequels">
            <input type="checkbox" id="toggleSequels">
            <span class="toggle-switch" aria-hidden="true"></span>
            <span class="toggle-info">
              <span class="toggle-title">Auto-expand sequels &amp; movies</span>
              <span class="toggle-desc">Automatically detects subsequent seasons and sequel movies</span>
            </span>
          </label>
        </div>
      </div>

      <div class="panel-footer" style="justify-content:flex-end;">
        <button id="btnProcess" class="btn btn-primary" style="width:100%;">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polygon points="5 3 19 12 5 21 5 3"></polygon>
          </svg>
          <span id="btnProcessLabel">Normalize &amp; Generate XML</span>
        </button>
        <div id="runControls" class="run-controls">
          <button id="btnPause" class="btn btn-secondary" style="flex:1;">Pause</button>
          <button id="btnCancel" class="btn btn-secondary" style="flex:1;">Cancel run</button>
        </div>
      </div>
    </section>

    <!-- Right Column: Quick Status Banner and Results Preview -->
    <div style="display:flex; flex-direction:column; gap:1.25rem;">
      
      <!-- Top Card: Download Actions & Ready Status -->
      <section class="export-card">
        <div class="export-info">
          <h3 id="exportHeadline">Export Pending</h3>
          <p id="exportSubline">Paste your anime list on the left to begin.</p>
          <div style="margin-top:0.375rem; display:flex; gap:0.875rem; font-size:0.75rem;">
            <a href="https://myanimelist.net/import.php" target="_blank" rel="noopener noreferrer" class="text-link" title="Open MyAnimeList Import page">
              <span>MyAnimeList Import</span>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:11px; height:11px;"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path><polyline points="15 3 21 3 21 9"></polyline><line x1="10" y1="14" x2="21" y2="3"></line></svg>
            </a>
            <a href="https://anilist.co/settings/import" target="_blank" rel="noopener noreferrer" class="text-link" title="Open AniList Import page">
              <span>AniList Import</span>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:11px; height:11px;"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path><polyline points="15 3 21 3 21 9"></polyline><line x1="10" y1="14" x2="21" y2="3"></line></svg>
            </a>
          </div>
        </div>
        <div class="export-buttons">
          <button id="btnWrite" class="btn btn-primary" title="Write the selected rows to your AniList account">Write to AniList...</button>
          <button id="btnUndo" class="btn btn-secondary" style="display:none;"
                  title="Put your AniList account back the way it was before the last write">Undo last write...</button>
          <button id="btnRetry" class="btn btn-secondary" style="display:none;"
                  title="Send the rows that failed again">Retry failed</button>
          <button id="btnPreview" class="btn btn-secondary" title="Show the generated XML">Preview XML</button>
          <a id="btnDownloadXml" href="/api/download/xml" class="btn btn-secondary disabled" aria-disabled="true" download="myanimelist_export.xml">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
              <polyline points="7 10 12 15 17 10"></polyline>
              <line x1="12" y1="15" x2="12" y2="3"></line>
            </svg>
            Download XML
          </a>
          <a id="btnDownloadGz" href="/api/download/gz" class="btn btn-primary disabled" aria-disabled="true" download="myanimelist_export.xml.gz">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
              <polyline points="7 10 12 15 17 10"></polyline>
              <line x1="12" y1="15" x2="12" y2="3"></line>
            </svg>
            Download .XML.GZ
          </a>
        </div>
      </section>

      <!-- Summary line: four numbers do not need four cards -->
      <div class="stats-strip">
        <div class="stat-box">
          <span class="stat-label">Total Entries</span>
          <span id="statTotal" class="stat-value">0</span>
        </div>
        <div class="stat-box">
          <span class="stat-label">Included in Export</span>
          <span id="statNormalized" class="stat-value">0</span>
        </div>
        <div class="stat-box">
          <span class="stat-label">Total Episodes / Chapters</span>
          <span id="statEpisodes" class="stat-value">0</span>
        </div>
        <div class="stat-box">
          <span class="stat-label">XML Status</span>
          <span id="statXmlValid" class="stat-value">Not Generated</span>
        </div>
      </div>

      <!-- Live Progress Card -->
      <section id="progressCard" class="progress-card" style="display: none;">
        <div class="progress-card-header">
          <div class="progress-card-status">
            <span class="progress-spinner" aria-hidden="true"></span>
            <div style="min-width: 0;">
              <div id="progressCurrentTitle" class="progress-current-title">Starting normalization...</div>
              <div id="progressCurrentSub" class="progress-current-sub">Connecting to AniList GraphQL...</div>
            </div>
          </div>
          <div class="progress-badge-group">
            <span id="progressCountLabel" class="progress-count-pill">0 / 0</span>
            <span id="progressPercentLabel" class="progress-percent-pill">0%</span>
            <span id="etaLabel" class="progress-count-pill"></span>
          </div>
        </div>
        <div class="progress-track" role="progressbar" aria-valuenow="0" aria-valuemin="0" aria-valuemax="100">
          <div id="progressBarFill" class="progress-fill"></div>
        </div>
      </section>

      <div id="fixModal" class="modal-backdrop" role="dialog" aria-modal="true"
           aria-labelledby="fixModalTitle">
        <div class="modal-card fix-card">
          <div class="modal-head">
            <strong id="fixModalTitle">Fix unmatched titles</strong>
            <span id="fixCounter" class="fix-counter"></span>
            <span style="flex:1;"></span>
            <button id="btnFixSkip" class="btn btn-secondary">Skip</button>
            <button id="btnFixClose" class="btn btn-secondary">Done</button>
          </div>
          <div class="fix-body">
            <div class="fix-original">You pasted: <strong id="fixOriginal"></strong></div>
            <div class="fix-search">
              <input type="text" id="fixQuery" class="token-input"
                     placeholder="Title, AniList id, MAL id, or a pasted anilist.co / myanimelist.net link"
                     aria-label="Search for a match">
              <select id="fixIdKind" aria-label="What a bare number means">
                <option value="">Number: either id</option>
                <option value="anilist">Number: AniList id</option>
                <option value="mal">Number: MAL id</option>
              </select>
              <button id="btnFixSearch" class="btn btn-primary">Search</button>
            </div>
            <div id="fixResults" class="fix-results"></div>
          </div>
        </div>
      </div>

      <div id="writeModal" class="modal-backdrop" role="dialog" aria-modal="true"
           aria-labelledby="writeModalTitle">
        <div class="modal-card write-card">
          <div class="modal-head">
            <strong id="writeModalTitle">Write to AniList</strong>
            <span style="flex:1;"></span>
            <button id="btnWriteCancel" class="btn btn-secondary">Cancel</button>
            <button id="btnWriteConfirm" class="btn btn-primary">Write now</button>
          </div>
          <div id="writePlan" class="write-plan"></div>
        </div>
      </div>

      <div id="undoModal" class="modal-backdrop" role="dialog" aria-modal="true"
           aria-labelledby="undoModalTitle">
        <div class="modal-card write-card">
          <div class="modal-head">
            <strong id="undoModalTitle">Undo the last AniList write</strong>
            <span style="flex:1;"></span>
            <button id="btnUndoCancel" class="btn btn-secondary">Cancel</button>
            <button id="btnUndoConfirm" class="btn btn-primary">Undo now</button>
          </div>
          <div id="undoPlan" class="write-plan"></div>
        </div>
      </div>

      <div id="previewModal" class="modal-backdrop" role="dialog" aria-modal="true"
           aria-labelledby="previewTitle">
        <div class="modal-card">
          <div class="modal-head">
            <strong id="previewTitle">Export preview</strong>
            <span style="flex:1;"></span>
            <button id="btnPreviewCopy" class="btn btn-secondary">Copy</button>
            <button id="btnPreviewClose" class="btn btn-secondary">Close</button>
          </div>
          <pre id="previewBody"></pre>
        </div>
      </div>

      <!-- Table Panel -->
      <section class="table-panel">
        <div class="table-toolbar">
          <div class="search-input-wrapper">
            <svg class="search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="11" cy="11" r="8"></circle>
              <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
            </svg>
            <input type="text" id="searchInput" aria-label="Filter rows"
                  placeholder="Filter by title, MAL ID, or AniList ID...">
          </div>
          <div id="chipRow" class="chip-row"></div>
          <div class="toolbar-end">
            <span id="filterCountLabel" class="filter-count">Showing 0</span>
            <button id="btnRemember" class="btn btn-secondary btn-xs"
                    title="Skip the rows you have unchecked in future lists too, not just this one">Always skip unchecked</button>
            <button id="btnForgetIgnored" class="btn btn-secondary btn-xs" style="display:none;"
                    title="Stop skipping the remembered titles">Forget 0 skipped</button>
            <button id="btnReset" class="btn btn-secondary btn-xs" title="Clear the saved session and start from an empty list">Start over</button>
          </div>
        </div>
        <div id="unresolvedBar" class="unresolved-bar">
          <span id="unresolvedText"></span>
          <button id="btnFixUnmatched" class="btn btn-primary btn-xs">Fix unmatched</button>
          <button id="btnCopyUnresolved" class="btn btn-secondary btn-xs">Copy unmatched</button>
        </div>

        <div class="table-wrapper">
          <table id="animeTable">
            <thead>
              <tr>
                <th scope="col" style="width: 42px; text-align: center;">
                  <input type="checkbox" id="selectAllCheckbox" class="table-checkbox" checked
                         aria-label="Select or deselect every shown row" title="Select/Deselect all">
                </th>
                <th scope="col" class="sortable" data-sort="title" tabindex="0" aria-sort="none">Title<span class="sort-mark"></span></th>
                <th scope="col" style="width: 104px;" class="sortable" data-sort="type"
                    tabindex="0" aria-sort="none">Type<span class="sort-mark"></span></th>
                <th scope="col" style="width: 158px;" id="countColumnHeading" class="sortable" data-sort="count"
                    tabindex="0" aria-sort="none">Episodes<span class="sort-mark"></span></th>
                <th scope="col" style="width: 132px;" class="sortable" data-sort="status"
                    tabindex="0" aria-sort="none">Status<span class="sort-mark"></span></th>
                <th scope="col" style="width: 40px;" aria-label="More fields"></th>
              </tr>
            </thead>
            <tbody id="tableBody">
              <!-- Dynamically rendered rows -->
            </tbody>
          </table>

          <div id="rowCapNotice" style="display:none; padding:0.625rem 1rem; font-size:0.75rem; color:var(--text-muted); border-top:1px solid var(--border);"></div>
          <div id="emptyTableState" class="empty-state">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="10"></circle>
              <line x1="12" y1="8" x2="12" y2="12"></line>
              <line x1="12" y1="16" x2="12.01" y2="16"></line>
            </svg>
            <h4>No titles processed yet</h4>
            <p>Paste your list of titles on the left and press <strong id="emptyStateAction">Normalize &amp; Generate XML</strong>.</p>
          </div>
        </div>
      </section>

    </div>
  </main>

  <!-- Toast -->
  <div id="toast" class="toast">
    <span id="toastMessage">Action completed</span>
  </div>

  <script>
    let currentAnimes = [];
    let ignoredKeys = new Set();
    let isProcessing = false;

    // Elements
    const txtArea = document.getElementById('animeTextInput');
    const mediaTypeSelect = document.getElementById('mediaType');
    const lineCountLabel = document.getElementById('lineCountLabel');
    const selectStatus = document.getElementById('selectStatus');
    const inputScore = document.getElementById('inputScore');
    const btnProcess = document.getElementById('btnProcess');
    const btnPaste = document.getElementById('btnPaste');
    const btnClear = document.getElementById('btnClear');
    const btnSample = document.getElementById('btnSample');
    const toggleSequels = document.getElementById('toggleSequels');
    const searchInput = document.getElementById('searchInput');
    const tableBody = document.getElementById('tableBody');
    const emptyState = document.getElementById('emptyTableState');
    const selectAllCheckbox = document.getElementById('selectAllCheckbox');
    const progressCard = document.getElementById('progressCard');
    const progressBarFill = document.getElementById('progressBarFill');
    const progressCurrentTitle = document.getElementById('progressCurrentTitle');
    const progressCurrentSub = document.getElementById('progressCurrentSub');
    const progressCountLabel = document.getElementById('progressCountLabel');
    const progressPercentLabel = document.getElementById('progressPercentLabel');
    const etaLabel = document.getElementById('etaLabel');
    const exportHeadline = document.getElementById('exportHeadline');
    const exportSubline = document.getElementById('exportSubline');
    const btnDownloadXml = document.getElementById('btnDownloadXml');
    const btnDownloadGz = document.getElementById('btnDownloadGz');
    const statTotal = document.getElementById('statTotal');
    const statNormalized = document.getElementById('statNormalized');
    const statEpisodes = document.getElementById('statEpisodes');
    const statXmlValid = document.getElementById('statXmlValid');
    const filterCountLabel = document.getElementById('filterCountLabel');
    const toast = document.getElementById('toast');
    const toastMessage = document.getElementById('toastMessage');
    const countColumnHeading = document.getElementById('countColumnHeading');
    const unresolvedBar = document.getElementById('unresolvedBar');
    const unresolvedText = document.getElementById('unresolvedText');
    const btnCopyUnresolved = document.getElementById('btnCopyUnresolved');
    const btnReset = document.getElementById('btnReset');
    const btnImport = document.getElementById('btnImport');
    const fixModal = document.getElementById('fixModal');
    const fixOriginal = document.getElementById('fixOriginal');
    const fixQuery = document.getElementById('fixQuery');
    const fixIdKind = document.getElementById('fixIdKind');
    const fixResults = document.getElementById('fixResults');
    const fixCounter = document.getElementById('fixCounter');
    const btnFixSearch = document.getElementById('btnFixSearch');
    const btnFixSkip = document.getElementById('btnFixSkip');
    const btnFixClose = document.getElementById('btnFixClose');
    const btnFixUnmatched = document.getElementById('btnFixUnmatched');
    const anilistPanel = document.getElementById('anilistPanel');
    const anilistToken = document.getElementById('anilistToken');
    const btnTokenTest = document.getElementById('btnTokenTest');
    const btnRemember = document.getElementById('btnRemember');
    const btnForgetIgnored = document.getElementById('btnForgetIgnored');
    const tokenStatus = document.getElementById('tokenStatus');
    const btnWrite = document.getElementById('btnWrite');
    const writeModal = document.getElementById('writeModal');
    const writePlan = document.getElementById('writePlan');
    const btnWriteConfirm = document.getElementById('btnWriteConfirm');
    const btnWriteCancel = document.getElementById('btnWriteCancel');
    const undoModal = document.getElementById('undoModal');
    const undoPlan = document.getElementById('undoPlan');
    const btnUndo = document.getElementById('btnUndo');
    const btnRetry = document.getElementById('btnRetry');
    const btnUndoConfirm = document.getElementById('btnUndoConfirm');
    const btnUndoCancel = document.getElementById('btnUndoCancel');
    const importFile = document.getElementById('importFile');
    const destinationSelect = document.getElementById('destination');
    const destinationPrompt = document.getElementById('destinationPrompt');
    const destinationField = document.getElementById('destinationField');
    const runControls = document.getElementById('runControls');
    const btnPause = document.getElementById('btnPause');
    const btnCancel = document.getElementById('btnCancel');
    const chipRow = document.getElementById('chipRow');
    const rowCapNotice = document.getElementById('rowCapNotice');
    const toggleArchive = document.getElementById('toggleArchive');
    const toggleFresh = document.getElementById('toggleFresh');
    const addUnmatchedImports = document.getElementById('addUnmatchedImports');
    const btnPreview = document.getElementById('btnPreview');
    const previewModal = document.getElementById('previewModal');
    const previewBody = document.getElementById('previewBody');
    const previewTitle = document.getElementById('previewTitle');
    const btnPreviewCopy = document.getElementById('btnPreviewCopy');
    const btnPreviewClose = document.getElementById('btnPreviewClose');

    const ROW_CAP = 300;
    let showAllRows = false;
    let statusFilter = '';
    let sortKey = '';
    let sortDir = 0;            // 0 = off, 1 = asc, -1 = desc
    let activeRunId = null;
    let isPaused = false;
    let networkFailures = 0;
    let mergedTitles = [];
    // '' until the user picks. Not defaulted on their behalf: the two
    // destinations accept different titles, so guessing hides rows.
    let destination = '';
    let exportFileInfo = null;
    // Memory only, deliberately: not localStorage, not the saved session, never
    // sent anywhere except this machine's own server.
    let anilistUser = null;
    const expandedRows = new Set();

    // AniList's own vocabulary. The server maps these onto MAL's XML values:
    // Paused -> On-Hold, Rereading/Rewatching -> Reading/Watching + the repeat flag.
    const STATUS_SETS = {
      manga: ['Reading', 'Plan to read', 'Completed', 'Rereading', 'Paused', 'Dropped'],
      anime: ['Watching', 'Plan to watch', 'Completed', 'Rewatching', 'Paused', 'Dropped'],
    };

    const STATUS_PILL = {
      'reading': 'status-watching', 'watching': 'status-watching',
      'rereading': 'status-watching', 'rewatching': 'status-watching',
      'completed': 'status-completed',
      'plan to read': 'status-planned', 'plan to watch': 'status-planned',
      'paused': 'status-paused', 'on-hold': 'status-paused',
      'dropped': 'status-dropped',
    };

    // Mirrors the server: a finished entry (or an in-progress repeat) counts as
    // fully read/watched; anything else starts at zero. Shown as a placeholder so
    // an untouched field stays derived rather than frozen at today's value.
    function isRepeatStatus(status) {
      const s = String(status || '').toLowerCase();
      return s === 'rereading' || s === 'rewatching';
    }

    function derivedProgress(item, field) {
      const done = String(item.status || '').toLowerCase() === 'completed' || isRepeatStatus(item.status);
      const total = field === 'my_read_volumes' ? item.volumes
                  : field === 'my_read_chapters' ? item.chapters
                  : item.episodes;
      return done ? (Number(total) || 0) : 0;
    }

    const PROGRESS_TOTAL_FIELD = {
      'my_read_chapters': 'chapters',
      'my_read_volumes': 'volumes',
      'my_watched_episodes': 'episodes',
    };

    function progressFields() {
      return mediaType() === 'manga'
        ? [['my_read_chapters', 'ch'], ['my_read_volumes', 'vol']]
        : [['my_watched_episodes', 'ep']];
    }

    function mediaType() {
      return mediaTypeSelect.value === 'manga' ? 'manga' : 'anime';
    }

    function updateMediaTypeUI(clearResults = false) {
      const isManga = mediaType() === 'manga';
      // Same order in both sets, so a switch carries the equivalent state across
      // by index (Reading <-> Watching, Rereading <-> Rewatching, ...).
      const from = STATUS_SETS[isManga ? 'anime' : 'manga'];
      const to = STATUS_SETS[isManga ? 'manga' : 'anime'];
      const previous = selectStatus.value;
      const sameIdx = from.indexOf(previous);
      const carried = sameIdx >= 0 ? to[sameIdx] : (to.indexOf(previous) >= 0 ? previous : 'Completed');
      selectStatus.innerHTML = to.map(v => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join('');
      selectStatus.value = carried;
      document.getElementById('listPanelTitle').textContent = isManga ? 'Paste Manga List' : 'Paste Anime List';
      document.querySelector('label[for="animeTextInput"]').textContent = `Paste your list (one ${isManga ? 'manga' : 'anime'} per line):`;
      // One column now holds "read / total", so it carries the label and the
      // trailing control column stays unlabelled.
      countColumnHeading.textContent = isManga ? 'Read / Total' : 'Watched / Total';
      const statLabels = document.querySelectorAll('.stat-label');
      if (statLabels[2]) statLabels[2].textContent = isManga ? 'Total Chapters / Volumes' : 'Total Episodes';
      toggleSequels.closest('.toggle-card').style.display = isManga ? 'none' : '';
      // NOTE: this template is a plain Python string, so a backslash-n here would
      // become a real newline and break the whole <script>. Build newlines in JS.
      txtArea.placeholder = (isManga
        ? ['Paste manga titles here (one per line):', '', 'Berserk', 'One Piece', 'Vagabond', 'Fullmetal Alchemist', '...']
        : ['Paste anime titles here (one per line):', '', 'Sousou no Frieren', 'Attack on Titan', 'Solo Leveling', '...']
      ).join(String.fromCharCode(10));
      btnDownloadXml.download = isManga ? 'myanimelist_manga_export.xml' : 'myanimelist_export.xml';
      btnDownloadGz.download = isManga ? 'myanimelist_manga_export.xml.gz' : 'myanimelist_export.xml.gz';
      if (clearResults) {
        currentAnimes = [];
        renderTable([]);
        updateStatsAndExport();
      }
      ignoredKeys = loadIgnored();
      updateIgnoredLabel();
      updateLineCounter();
      refreshExportState();
    }

    mediaTypeSelect.addEventListener('change', () => {
      updateMediaTypeUI(true);
      saveState();
    });

    toggleSequels.addEventListener('change', saveState);

    function showToast(msg, type = 'success') {
      toastMessage.textContent = msg;
      toast.className = 'toast show toast-' + type;
      setTimeout(() => {
        toast.className = 'toast';
      }, 3500);
    }

    function updateLineCounter() {
      const text = txtArea.value;
      const lines = text.split(String.fromCharCode(10)).filter(l => l.trim() && !l.trim().startsWith('#'));
      lineCountLabel.textContent = lines.length + (mediaType() === 'manga' ? ' manga' : ' anime');
    }

    txtArea.addEventListener('input', () => {
      updateLineCounter();
      saveState();
    });

    // Paste from clipboard
    btnPaste.addEventListener('click', async () => {
      try {
        const text = await navigator.clipboard.readText();
        if (text) {
          txtArea.value = text;
          updateLineCounter();
          showToast('Pasted from clipboard');
        } else {
          txtArea.focus();
          showToast('Clipboard is empty', 'warning');
        }
      } catch (e) {
        txtArea.focus();
        showToast('Press Ctrl+V to paste into the text area', 'warning');
      }
    });

    // Clear textarea
    btnClear.addEventListener('click', () => {
      txtArea.value = '';
      updateLineCounter();
      saveState();
      showToast(currentAnimes.length
        ? 'Text cleared. Use Start over to also clear the results table.'
        : 'Text area cleared');
    });

    // Load sample list
    btnSample.addEventListener('click', () => {
      const sampleTitles = mediaType() === 'manga'
        ? ['Berserk', 'One Piece', 'Vagabond', 'Fullmetal Alchemist', 'Dandadan', 'Chainsaw Man', 'Vinland Saga']
        : ['Sousou no Frieren', 'Shingeki no Kyojin', 'Solo Leveling', 'Dandadan', 'Chainsaw Man', 'KonoSuba', 'Jujutsu Kaisen'];
      txtArea.value = sampleTitles.join(String.fromCharCode(10));
      updateLineCounter();
      showToast('Sample list loaded');
    });

    // Start completely empty every time
    async function loadData() {
      renderTable([]);
      statTotal.textContent = '0';
      statNormalized.textContent = '0';
      statEpisodes.textContent = '0';
      statXmlValid.textContent = 'Not Generated';
      statXmlValid.style.color = 'var(--text-subtle)';
    }

    // Process & Normalize
    btnProcess.addEventListener('click', async () => {
      if (isProcessing) return;
      isProcessing = true;
      btnProcess.disabled = true;

      try {
        const payload = {
          content: txtArea.value,
          media_type: mediaType(),
          status: selectStatus.value,
          score: parseInt(inputScore.value, 10) || 0,
          expand_sequels: mediaType() === 'anime' && toggleSequels.checked,
          archive: toggleArchive.checked,
          use_cache: !toggleFresh.checked,
          destination: destination
        };

        currentAnimes = [];
        expandedRows.clear();
        showAllRows = false;
        networkFailures = 0;
        mergedTitles = [];
        renderTable(currentAnimes);
        updateStats();
        activeRunId = null;
        isPaused = false;
        btnPause.textContent = 'Pause';
        btnPause.disabled = false;
        btnCancel.disabled = false;
        runControls.classList.add('show');
        progressCard.style.display = 'flex';
        progressBarFill.style.transform = 'scaleX(0)';
        progressCountLabel.textContent = '0 / 0';
        progressPercentLabel.textContent = '0%';
        progressCurrentTitle.textContent = 'Starting normalization...';
        progressCurrentSub.textContent = 'Connecting to AniList GraphQL...';

        const res = await fetch('/api/process', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });

        if (!res.ok) {
          const errData = await res.json().catch(() => ({}));
          showToast(errData.error || 'Error processing list', res.status === 409 ? 'warning' : 'error');
          progressCard.style.display = 'none';
          runControls.classList.remove('show');
          return;
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';
        let cancelledRun = false;
        // Set when the server has said all it is going to say. Breaking out of the
        // chunk loop alone would leave the outer reader awaiting a read that only
        // resolves when the socket closes.
        let streamDone = false;

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const parts = buffer.split(String.fromCharCode(10) + String.fromCharCode(10));
          buffer = parts.pop();

          for (const chunk of parts) {
            const trimmed = chunk.trim();
            if (!trimmed.startsWith('data: ')) continue;
            try {
              const event = JSON.parse(trimmed.slice(6));
              if (event.type === 'progress') {
                const ratio = Math.min(1, Math.max(0, event.percent / 100));
                progressBarFill.style.transform = 'scaleX(' + ratio + ')';
                progressCountLabel.textContent = event.current + ' / ' + event.total;
                if (event.eta_seconds !== undefined || event.interval) {
                  // Server knows the batch size; per-title maths overestimated by ~8x.
                  const left = event.eta_seconds !== undefined
                    ? event.eta_seconds
                    : Math.max(0, event.total - event.current) * event.interval;
                  const mins = Math.floor(left / 60), secs = Math.round(left % 60);
                  progressCountLabel.title = `~${mins > 0 ? mins + 'm ' : ''}${secs}s remaining at AniList's rate limit`;
                  etaLabel.textContent = left > 0 ? `~${mins > 0 ? mins + 'm ' : ''}${secs}s left` : 'finishing...';
                }
                progressPercentLabel.textContent = Math.round(event.percent) + '%';
                progressCurrentTitle.textContent = `[${event.current}/${event.total}] ${event.title}`;
                if (event.stage === 'retry') {
                  // `done` does not advance during retries; say so rather than
                  // letting the bar sit still and look stuck.
                  progressCurrentSub.textContent = 'Retrying titles that did not match on the first pass...';
                } else if (event.resolved_title) {
                  progressCurrentSub.textContent = '-> ' + event.resolved_title;
                } else {
                  progressCurrentSub.textContent = 'Resolving metadata via AniList & MAL...';
                }
                if (Array.isArray(event.entries) && event.entries.length) {
                  const seen = new Set(currentAnimes.map(entryKey));
                  let added = false;
                  event.entries.forEach(entry => {
                    const key = entryKey(entry);
                    if (seen.has(key)) return;   // same dedupe the final list applies
                    seen.add(key);
                    currentAnimes.push({ ...entry, enabled: enabledFor(entry) });
                    added = true;
                  });
                  if (added) {
                    renderTable(currentAnimes);
                    updateStats();   // display only - the run still owns the files
                  }
                }
              } else if (event.type === 'started') {
                activeRunId = event.run_id;
              } else if (event.type === 'paused') {
                isPaused = true;
                btnPause.textContent = 'Resume';
                progressCurrentSub.textContent = 'Paused - nothing is being requested.';
              } else if (event.type === 'resumed') {
                isPaused = false;
                btnPause.textContent = 'Pause';
              } else if (event.type === 'cancelled') {
                const existing = new Map(currentAnimes.map(i => [entryKey(i), i]));
                currentAnimes = (event.entries || []).map(e => {
                  const row = existing.get(entryKey(e));
                  return row || { ...e, enabled: enabledFor(e) };
                });
                renderTable(currentAnimes);
                showToast('Run cancelled - ' + (event.resolved || 0) + ' titles resolved', 'warning');
                try { reader.cancel(); } catch (err) {}
                cancelledRun = true;
                streamDone = true;
                break;
              } else if (event.type === 'network_error') {
                networkFailures = event.count || 0;
                progressCurrentSub.textContent = 'AniList could not be reached for '
                  + networkFailures + ' titles - they are not necessarily missing.';
              } else if (event.type === 'error') {
                showToast(event.error || 'Could not build the export', 'error');
                progressCard.style.display = 'none';
              } else if (event.type === 'rate_limit') {
                progressCurrentSub.textContent = `AniList rate limit reached (30 requests/min) - waiting ${event.seconds}s, this is normal for long lists...`;
              } else if (event.type === 'complete' && event.ok) {
                progressBarFill.style.transform = 'scaleX(1)';
                progressPercentLabel.textContent = '100%';
                // Keep the row objects already on screen so any status/progress edits
                // made while the run was going are not thrown away; take the server's
                // deduped order and membership as authoritative.
                const existing = new Map(currentAnimes.map(i => [entryKey(i), i]));
                currentAnimes = event.entries.map(e => {
                  const row = existing.get(entryKey(e));
                  return row || { ...e, enabled: enabledFor(e) };
                });
                selectAllCheckbox.checked = true;
                selectAllCheckbox.indeterminate = false;
                renderTable(currentAnimes);
                updateStatsAndExport();
                mergedTitles = event.merged || [];
                refreshExportState();
                showToast('XML generated (' + event.entries.length + ' ' + (mediaType() === 'manga' ? 'manga' : 'anime') + ' rows; unresolved rows are excluded)');
                try { reader.cancel(); } catch(e) {}
                streamDone = true;
                break;
              }
            } catch (errParse) {
              // Ignore partial JSON
            }
          }
          if (streamDone) break;
        }
      } catch (err) {
        showToast('Error communicating with local server', 'error');
      } finally {
        isProcessing = false;
        btnProcess.disabled = false;
        activeRunId = null;
        isPaused = false;
        runControls.classList.remove('show');
        // A cancelled run wrote nothing, so re-sync the files to what is on screen.
        updateStatsAndExport();
        setTimeout(() => {
          progressCard.style.display = 'none';
        }, 1500);
      }
    });

    let exportDebounceTimer = null;

    function filteredCount() {
      return currentAnimes.filter(matchesFilters).length;
    }

    function updateStats() {
      const enabledItems = currentAnimes.filter(i => i.enabled !== false && isActionable(i));
      const totalCount = currentAnimes.length;
      const enabledCount = enabledItems.length;

      statTotal.textContent = totalCount;
      statNormalized.textContent = enabledCount === totalCount ? String(totalCount) : `${enabledCount} of ${totalCount}`;
      if (mediaType() === 'manga') {
        const totalCh = enabledItems.reduce((acc, curr) => acc + (curr.chapters || 0), 0);
        const totalVol = enabledItems.reduce((acc, curr) => acc + (curr.volumes || 0), 0);
        statEpisodes.textContent = totalCh.toLocaleString() + ' ch / ' + totalVol.toLocaleString() + ' vol';
      } else {
        const totalEps = enabledItems.reduce((acc, curr) => acc + (curr.episodes || 0), 0);
        statEpisodes.textContent = totalEps.toLocaleString();
      }

      filterCountLabel.textContent = `Showing ${filteredCount()} of ${totalCount} (${enabledCount} selected)`;

      const missed = currentAnimes.filter(i => !canXml(i) && !canWrite(i));
      const anilistOnly = currentAnimes.filter(i => !canXml(i) && canWrite(i));
      const notes = [];
      const noun = mediaType() === 'manga' ? 'manga' : 'anime';
      if (missed.length > 0) {
        // Blaming the list for a network outage is how a tool loses trust.
        notes.push(networkFailures > 0
          ? `<strong>${missed.length}</strong> ${noun} ${missed.length === 1 ? 'title' : 'titles'} could not be checked: AniList was unreachable for ${networkFailures} of them. Re-run before assuming the titles are wrong.`
          : `<strong>${missed.length}</strong> ${noun} ${missed.length === 1 ? 'title has' : 'titles have'} no MyAnimeList ID on AniList and cannot be exported. Check the spelling, or try the romaji title.`);
      }
      if (anilistOnly.length > 0) {
        notes.push(destination === 'xml'
          ? `<strong>${anilistOnly.length}</strong> ${anilistOnly.length === 1 ? 'title exists' : 'titles exist'} on AniList but ${anilistOnly.length === 1 ? 'has' : 'have'} no MyAnimeList ID, so MAL XML cannot carry ${anilistOnly.length === 1 ? 'it' : 'them'}. Switch the destination to AniList to keep ${anilistOnly.length === 1 ? 'it' : 'them'}.`
          : `<strong>${anilistOnly.length}</strong> ${anilistOnly.length === 1 ? 'title is' : 'titles are'} on AniList without a MyAnimeList ID. ${anilistOnly.length === 1 ? 'It' : 'They'} cannot go into the MAL XML file, but <strong>Write to AniList</strong> sends ${anilistOnly.length === 1 ? 'it' : 'them'} straight to your account.`);
      }
      if (mergedTitles.length > 0) {
        const names = mergedTitles.slice(0, 4).map(m => escapeHtml(m.original || m.title)).join(', ');
        const more = mergedTitles.length > 4 ? ` and ${mergedTitles.length - 4} more` : '';
        notes.push(`<strong>${mergedTitles.length}</strong> pasted ${mergedTitles.length === 1 ? 'line' : 'lines'} matched an entry already in the list and ${mergedTitles.length === 1 ? 'was' : 'were'} merged: ${names}${more}.`);
      }
      unresolvedBar.classList.toggle('show', notes.length > 0);
      if (notes.length > 0) {
        unresolvedText.innerHTML = notes.join('<br>');
      }

      if (enabledCount > 0) {
        const xmlable = enabledItems.filter(canXml).length;
        exportHeadline.textContent = `${enabledCount} ${mediaType() === 'manga' ? 'manga' : 'anime'} selected`;
        const onDisk = exportFileInfo
          ? `${exportFileInfo.count} ${exportFileInfo.count === 1 ? 'entry' : 'entries'} in ${exportFileInfo.name}. `
          : '';
        exportSubline.textContent = onDisk + (xmlable === enabledCount
          ? 'MAL-compatible XML for import through MyAnimeList or AniList.'
          : `Only ${xmlable} of ${enabledCount} can travel in MAL XML; the rest have no MyAnimeList ID.`);
        btnDownloadXml.classList.remove('disabled');
        btnDownloadXml.removeAttribute('aria-disabled');
        btnDownloadGz.classList.remove('disabled');
        btnDownloadGz.removeAttribute('aria-disabled');
        statXmlValid.textContent = 'Valid';
        statXmlValid.style.color = 'var(--success)';
      } else {
        exportHeadline.textContent = `No ${mediaType() === 'manga' ? 'manga' : 'anime'} selected`;
        exportSubline.textContent = `Select at least one ${mediaType() === 'manga' ? 'manga' : 'anime'} row to enable the download.`;
        btnDownloadXml.classList.add('disabled');
        btnDownloadXml.setAttribute('aria-disabled', 'true');
        btnDownloadGz.classList.add('disabled');
        btnDownloadGz.setAttribute('aria-disabled', 'true');
        statXmlValid.textContent = 'Empty';
        statXmlValid.style.color = 'var(--text-muted)';
      }

      // A run owns the export files until it finishes; never offer a stale download.
      if (isProcessing) {
        setDownloadEnabled(btnDownloadXml, false);
        setDownloadEnabled(btnDownloadGz, false);
        exportHeadline.textContent = `Resolving... ${enabledCount} ready so far`;
        exportSubline.textContent = 'Download unlocks when the run finishes.';
      }
    }

    function updateStatsAndExport() {
      updateStats();
      saveState();
      const enabledItems = currentAnimes.filter(i => i.enabled !== false && isActionable(i));
      clearTimeout(exportDebounceTimer);
      exportDebounceTimer = setTimeout(async () => {
        try {
          const res = await fetch('/api/update-export', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              entries: enabledItems.map(({ candidates, ...rest }) => rest),
              media_type: mediaType(),
              status: selectStatus.value,
              score: parseInt(inputScore.value, 10) || 0,
              archive: toggleArchive.checked
            })
          });
          if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            if (err.error) showToast(err.error, 'error');
          }
          await refreshExportState();
        } catch (e) {
          // ignore background sync error
        }
      }, 250);
    }

    function setDownloadEnabled(btn, enabled) {
      btn.classList.toggle('disabled', !enabled);
      if (enabled) {
        btn.removeAttribute('aria-disabled');
      } else {
        btn.setAttribute('aria-disabled', 'true');
      }
    }

    // Ask the server what it actually holds. The client's own guess is used for the
    // instant feedback; this is the authority once a write has landed.
    async function refreshExportState() {
      try {
        const res = await fetch('/api/export-state');
        if (!res.ok) return;
        const state = await res.json();
        const info = state[mediaType()];
        if (!info) return;
        btnDownloadXml.href = '/api/download/xml?media=' + mediaType();
        btnDownloadGz.href = '/api/download/gz?media=' + mediaType();
        btnDownloadXml.download = info.xml_name;
        btnDownloadGz.download = info.gz_name;
        setDownloadEnabled(btnDownloadXml, info.xml);
        setDownloadEnabled(btnDownloadGz, info.gz);
        // Record what the server holds; updateStats owns the wording so the two
        // do not overwrite each other's message.
        exportFileInfo = (info.xml && info.count !== null && info.count !== undefined)
          ? { name: info.xml_name, count: info.count }
          : null;
        updateStats();
      } catch (e) {
        // leave the client-side state as-is
      }
    }

    [btnDownloadXml, btnDownloadGz].forEach(btn => {
      btn.addEventListener('click', (e) => {
        if (btn.classList.contains('disabled')) {
          e.preventDefault();
          showToast('No export generated yet for this media type', 'warning');
        }
      });
    });

    function updateSelectAllState() {
      const visible = currentAnimes.filter(matchesFilters);
      if (visible.length === 0) {
        selectAllCheckbox.checked = false;
        selectAllCheckbox.indeterminate = false;
        return;
      }
      const allChecked = visible.every(i => i.enabled !== false);
      const someChecked = visible.some(i => i.enabled !== false);
      selectAllCheckbox.checked = allChecked;
      selectAllCheckbox.indeterminate = !allChecked && someChecked;
    }

    function pickerHtml(item, idx) {
      const list = Array.isArray(item.candidates) ? item.candidates : [];
      if (list.length < 2) return '';
      const options = list.map((c, i) => {
        const counts = mediaType() === 'manga'
          ? `${c.chapters || '?'} ch`
          : `${c.episodes || '?'} eps`;
        // A candidate with no MAL id is still valid for a direct AniList write;
        // labelling it "MAL 0" would read as broken.
        const ident = Number(c.id) > 0 ? `MAL ${c.id}` : `AniList ${c.anilist_id} (no MAL ID)`;
        const label = `${c.title} - ${ident} - ${c.type} - ${counts}`;
        return `<option value="${i}" ${Number(c.id) === Number(item.id) ? 'selected' : ''}>${escapeHtml(label)}</option>`;
      }).join('');
      return `<select class="match-picker" data-index="${idx}" title="Pick the correct match for this title">${options}</select>`;
    }

    // A row can be exportable by one route and not the other. Older saved rows
    // predate the flags, so fall back to the ids they do carry.
    function canXml(item) {
      return item.can_export_xml !== undefined
        ? !!item.can_export_xml
        : Number(item.id) > 0;
    }

    function canWrite(item) {
      return item.can_write_anilist !== undefined
        ? !!item.can_write_anilist
        : Number(item.anilist_id) > 0;
    }

    function isActionable(item) {
      if (destination === 'xml') return canXml(item);
      if (destination === 'anilist') return canWrite(item);
      return canXml(item) || canWrite(item);   // no destination chosen yet
    }

    function rowBadge(item) {
      if (canXml(item)) return { cls: 'tag-type', text: item.type || 'TV' };
      if (canWrite(item)) return { cls: 'status-pill status-anilist-only', text: 'AniList only' };
      return { cls: 'status-pill status-unresolved', text: 'Not found' };
    }

    function setDestination(value, persist) {
      destination = (value === 'anilist' || value === 'xml') ? value : '';
      destinationSelect.value = destination;
      destinationPrompt.classList.toggle('show', destination === '');
      destinationField.style.display = destination === '' ? 'none' : '';
      anilistPanel.classList.toggle('show', destination === 'anilist');
      btnWrite.style.display = destination === 'anilist' ? '' : 'none';
      updateWriteAftermath();
      // Naming the file format while the user is sending rows to an account
      // is the kind of small lie that makes people distrust the rest.
      const action = destination === 'anilist' ? 'Resolve titles' : 'Normalize & Generate XML';
      document.querySelectorAll('#btnProcessLabel, #emptyStateAction')
        .forEach(el => { el.textContent = action; });
      renderTable(currentAnimes);
      updateStats();
      if (persist !== false) saveState();
    }

    // Mirrors deduplicate_entries() on the server: MAL id when present, else title.
    const STATE_KEY = 'aniexport.state';
    let stateSaveTimer = null;

    function statePayload(includeResults, includeCandidates) {
      const base = {
        v: 1,
        mediaType: mediaType(),
        status: selectStatus.value,
        score: parseInt(inputScore.value, 10) || 0,
        sequels: !!toggleSequels.checked,
        archive: !!toggleArchive.checked,
        fresh: !!toggleFresh.checked,
        destination: destination,
        text: txtArea.value,
        updated: Date.now(),
      };
      if (!includeResults) return base;
      base.results = currentAnimes.map(item => {
        if (includeCandidates) return item;
        const copy = Object.assign({}, item);
        delete copy.candidates;
        return copy;
      });
      return base;
    }

    // A long run is expensive to repeat, so the session survives a refresh.
    // Candidates are the bulky part: drop them before dropping the rows themselves.
    function saveStateNow() {
      const attempts = [[true, true], [true, false], [false, false]];
      for (let i = 0; i < attempts.length; i++) {
        try {
          window.localStorage.setItem(STATE_KEY, JSON.stringify(statePayload(attempts[i][0], attempts[i][1])));
          return;
        } catch (e) {
          // quota or disabled storage - try a smaller payload, then give up quietly
        }
      }
    }

    function saveState() {
      clearTimeout(stateSaveTimer);
      stateSaveTimer = setTimeout(saveStateNow, 300);
    }

    function clearSavedState() {
      try {
        window.localStorage.removeItem(STATE_KEY);
      } catch (e) {
        // storage unavailable: nothing was saved either
      }
    }

    // Everything the session remembers, back to first-run values. Without this
    // a saved session was permanent: Clear only emptied the textarea and the
    // rows came back on every reopen.
    function resetSession() {
      clearTimeout(stateSaveTimer);
      clearSavedState();
      currentAnimes = [];
      expandedRows.clear();
      statusFilter = '';
      sortKey = '';
      sortDir = 0;
      showAllRows = false;
      searchInput.value = '';
      txtArea.value = '';
      inputScore.value = '0';
      toggleSequels.checked = false;
      toggleArchive.checked = false;
      toggleFresh.checked = false;
      setDestination('', false);
      mediaTypeSelect.value = 'anime';
      document.querySelectorAll('th.sortable').forEach(th => {
        const mark = th.querySelector('.sort-mark');
        if (mark) mark.textContent = '';
        th.setAttribute('aria-sort', 'none');
      });
      updateMediaTypeUI(false);
      renderTable(currentAnimes);
      updateStats();
      refreshExportState();
      updateLineCounter();
    }

    let resetArmed = null;
    btnReset.addEventListener('click', () => {
      // Two steps on purpose: a long run is expensive to repeat.
      if (!resetArmed) {
        btnReset.textContent = 'Confirm reset';
        btnReset.classList.add('btn-danger');
        resetArmed = setTimeout(() => {
          resetArmed = null;
          btnReset.textContent = 'Start over';
          btnReset.classList.remove('btn-danger');
        }, 4000);
        return;
      }
      clearTimeout(resetArmed);
      resetArmed = null;
      btnReset.textContent = 'Start over';
      btnReset.classList.remove('btn-danger');
      resetSession();
      showToast('Session cleared. Files already written to disk are untouched.');
    });

    function restoreState() {
      // Loading with #reset discards the saved session before it is applied.
      if (window.location.hash === '#reset') {
        clearSavedState();
        return false;
      }
      let raw = null;
      try {
        raw = window.localStorage.getItem(STATE_KEY);
      } catch (e) {
        return false;
      }
      if (!raw) return false;
      let saved;
      try {
        saved = JSON.parse(raw);
      } catch (e) {
        return false;
      }
      if (!saved || saved.v !== 1) return false;

      if (saved.mediaType === 'manga' || saved.mediaType === 'anime') {
        mediaTypeSelect.value = saved.mediaType;
      }
      // Rebuild the media-specific controls first, then restore the values that
      // depend on them - the status options only exist after this call.
      updateMediaTypeUI(false);
      if (typeof saved.status === 'string') {
        const allowed = STATUS_SETS[mediaType()];
        if (allowed.indexOf(saved.status) >= 0) selectStatus.value = saved.status;
      }
      if (typeof saved.score === 'number') inputScore.value = Math.max(0, Math.min(10, saved.score));
      toggleSequels.checked = !!saved.sequels;
      toggleArchive.checked = !!saved.archive;
      toggleFresh.checked = !!saved.fresh;
      setDestination(saved.destination, false);
      if (typeof saved.text === 'string') txtArea.value = saved.text;
      updateLineCounter();

      if (Array.isArray(saved.results) && saved.results.length) {
        currentAnimes = saved.results;
        renderTable(currentAnimes);
        // Re-sync the server's files so a download matches what is on screen.
        updateStatsAndExport();
        showToast(`Restored ${currentAnimes.length} rows from your last session`);
      } else {
        renderTable([]);
        updateStats();
      }
      return true;
    }

    function sortValue(item) {
      if (sortKey === 'id') return Number(item.id) || 0;
      if (sortKey === 'title') return String(item.title || '').toLowerCase();
      if (sortKey === 'type') return String(item.type || '').toLowerCase();
      if (sortKey === 'status') return String(item.status || '').toLowerCase();
      if (sortKey === 'count') {
        return mediaType() === 'manga' ? (Number(item.chapters) || 0) : (Number(item.episodes) || 0);
      }
      return 0;
    }

    function applySort(list) {
      if (!sortKey || sortDir === 0) return list;
      // decorate-sort-undecorate keeps it stable, so equal rows hold their order
      return list
        .map((item, i) => ({ item, i }))
        .sort((a, b) => {
          const av = sortValue(a.item);
          const bv = sortValue(b.item);
          if (av < bv) return -1 * sortDir;
          if (av > bv) return 1 * sortDir;
          return a.i - b.i;
        })
        .map(pair => pair.item);
    }

    const CHIPS = [
      { id: '', label: 'All' },
      { id: 'reading', label: 'Reading' },
      { id: 'completed', label: 'Completed' },
      { id: 'planned', label: 'Planned' },
      { id: 'paused', label: 'Paused' },
      { id: 'dropped', label: 'Dropped' },
      { id: 'anilistonly', label: 'AniList only' },
      { id: 'unresolved', label: 'Not found' },
    ];

    function chipLabel(chip) {
      // The list is one media type at a time; naming both is noise.
      if (chip.id === 'reading') return mediaType() === 'manga' ? 'Reading' : 'Watching';
      return chip.label;
    }

    function chipBucket(item) {
      if (!canXml(item) && !canWrite(item)) return 'unresolved';
      if (!canXml(item)) return 'anilistonly';
      const status = String(item.status || '').toLowerCase();
      if (status === 'reading' || status === 'watching' || status === 'rereading' || status === 'rewatching') return 'reading';
      if (status === 'completed') return 'completed';
      if (status === 'plan to read' || status === 'plan to watch') return 'planned';
      if (status === 'paused' || status === 'on-hold') return 'paused';
      if (status === 'dropped') return 'dropped';
      return 'other';
    }

    function renderChips() {
      const counts = {};
      currentAnimes.forEach(item => {
        const bucket = chipBucket(item);
        counts[bucket] = (counts[bucket] || 0) + 1;
      });
      chipRow.innerHTML = CHIPS.filter(chip => chip.id === '' || counts[chip.id])
        .map(chip => {
          const n = chip.id === '' ? currentAnimes.length : (counts[chip.id] || 0);
          return `<button class="chip ${statusFilter === chip.id ? 'active' : ''}" data-chip="${chip.id}">${escapeHtml(chipLabel(chip))} ${n}</button>`;
        }).join('');
    }

    function matchesFilters(item) {
      const q = searchInput.value.toLowerCase().trim();
      if (statusFilter && chipBucket(item) !== statusFilter) return false;
      if (!q) return true;
      const title = (item.title || '').toLowerCase();
      const orig = (item.original || '').toLowerCase();
      return title.includes(q) || orig.includes(q)
        || String(item.id || '').includes(q) || String(item.anilist_id || '').includes(q);
    }

    function entryKey(entry) {
      const id = Number(entry.id) || 0;
      return id > 0 ? 'id:' + id : 'title:' + String(entry.title || '').trim().toLowerCase();
    }

    // Titles the user has chosen to skip for good, per media type. Unchecking a
    // box stays per-session as it always has; this list only changes when the
    // button is pressed, so the checkbox keeps its original meaning.
    const IGNORE_KEY = 'aniexport.ignored';

    function readIgnoreStore() {
      try {
        return JSON.parse(window.localStorage.getItem(IGNORE_KEY) || '{}') || {};
      } catch (e) {
        return {};
      }
    }

    function loadIgnored() {
      const all = readIgnoreStore();
      return new Set(Array.isArray(all[mediaType()]) ? all[mediaType()] : []);
    }

    function saveIgnored() {
      const all = readIgnoreStore();
      all[mediaType()] = Array.from(ignoredKeys);
      try {
        window.localStorage.setItem(IGNORE_KEY, JSON.stringify(all));
      } catch (e) {
        showToast('Could not save the skip list - browser storage is full or blocked.', 'error');
      }
    }

    function enabledFor(entry) {
      return isActionable(entry) && !ignoredKeys.has(entryKey(entry));
    }

    function updateIgnoredLabel() {
      const n = ignoredKeys.size;
      btnForgetIgnored.textContent = 'Forget ' + n + ' skipped';
      btnForgetIgnored.style.display = n ? '' : 'none';
    }

    function fixRowHtml(item, idx) {
      if (isActionable(item)) return '';
      return `<div class="fix-row">
        <input class="fix-input" data-index="${idx}" placeholder="Search title or MAL ID..."
          aria-label="Find a match for ${escapeHtml(item.original || item.title)}">
        <button class="btn btn-secondary btn-fix" data-index="${idx}">Find</button>
      </div>`;
    }

    function detailRowHtml(item, idx) {
      if (!expandedRows.has(idx)) return '';
      const val = key => (item[key] === undefined || item[key] === null ? '' : String(item[key]));
      const dateVal = key => {
        const raw = val(key);
        return raw === '0000-00-00' ? '' : raw;
      };
      return `<tr class="detail-row" data-detail-for="${idx}"><td colspan="6">
        <div class="detail-head">Extra fields for <strong>${escapeHtml(item.title)}</strong> - blank means unset</div>
        <div class="detail-grid">
          <label>Score (0-10)
            <input type="number" min="0" max="10" class="detail-input" data-index="${idx}" data-field="score" value="${escapeHtml(val('score'))}">
          </label>
          <label>Start date
            <input type="date" class="detail-input" data-index="${idx}" data-field="start_date" value="${escapeHtml(dateVal('start_date'))}">
          </label>
          <label>Finish date
            <input type="date" class="detail-input" data-index="${idx}" data-field="finish_date" value="${escapeHtml(dateVal('finish_date'))}">
          </label>
          <label>Tags
            <input type="text" class="detail-input" data-index="${idx}" data-field="tags" value="${escapeHtml(val('tags'))}">
          </label>
          <label class="detail-wide">Comments
            <input type="text" class="detail-input" data-index="${idx}" data-field="comments" value="${escapeHtml(val('comments'))}">
          </label>
        </div>
      </td></tr>`;
    }

    function statusPillClass(status) {
      return STATUS_PILL[String(status || '').toLowerCase()] || 'status-completed';
    }

    function statusCellHtml(item, idx) {
      // Per-row status. The list-wide dropdown still stamps every row as before;
      // this only adds the ability to differ, which a real mixed list needs.
      const current = item.status || 'Completed';
      const set = STATUS_SETS[mediaType()];
      const known = set.indexOf(current) >= 0;
      const options = set.map(v =>
        `<option value="${escapeHtml(v)}" ${v === current ? 'selected' : ''}>${escapeHtml(v)}</option>`).join('');
      const orphan = known ? '' : `<option value="${escapeHtml(current)}" selected>${escapeHtml(current)}</option>`;
      return `<select class="row-status status-pill ${statusPillClass(current)}" data-index="${idx}"
        ${isActionable(item) ? '' : 'disabled'}
        aria-label="Status for ${escapeHtml(item.title)}">${orphan}${options}</select>`;
    }

    function progressHtml(item, idx) {
      const exportable = isActionable(item);
      // One column, not two: an editable count against its total reads as progress.
      // Separate "total" and "read" columns forced the eye to join them every row.
      return '<div class="progress-cell">' + progressFields().map(([field, unit]) => {
        const value = item[field] === undefined || item[field] === null ? '' : item[field];
        const total = Number(item[PROGRESS_TOTAL_FIELD[field]] || 0);
        return `<label class="progress-line">
          <input type="number" class="progress-input" min="0" ${total > 0 ? `max="${total}"` : ''}
            data-index="${idx}" data-field="${field}" value="${value}"
            placeholder="${derivedProgress(item, field)}" ${exportable ? '' : 'disabled'}
            aria-label="${escapeHtml(unit)} read for ${escapeHtml(item.title)}">
          <span class="progress-total">/ ${total > 0 ? total : '?'} ${escapeHtml(unit)}</span>
        </label>`;
      }).join('') + '</div>';
    }

    // Render Table
    function renderTable(items) {
      renderChips();
      const matched = applySort(items.filter(matchesFilters));
      const capped = showAllRows ? matched : matched.slice(0, ROW_CAP);

      const enabledCount = currentAnimes.filter(i => i.enabled !== false).length;
      filterCountLabel.textContent = 'Showing ' + capped.length + ' of ' + items.length + ' (' + enabledCount + ' selected)';

      if (matched.length > capped.length) {
        rowCapNotice.style.display = 'block';
        rowCapNotice.innerHTML = `Showing the first ${capped.length} of ${matched.length} matching rows. <button id="btnShowAll" class="btn btn-secondary btn-xs">Show all</button>`;
      } else {
        rowCapNotice.style.display = 'none';
        rowCapNotice.innerHTML = '';
      }

      if (capped.length === 0) {
        tableBody.innerHTML = '';
        emptyState.style.display = 'block';
        updateSelectAllState();
        return;
      }

      emptyState.style.display = 'none';
      tableBody.innerHTML = capped.map(item => {
        const globalIdx = currentAnimes.indexOf(item);
        const malUrl = item.id ? `https://myanimelist.net/${mediaType()}/${item.id}` : '';
        const alUrl = item.anilist_id ? `https://anilist.co/${mediaType()}/${item.anilist_id}` : '';
        const hasRenamed = item.original && item.original.toLowerCase() !== item.title.toLowerCase();
        const isChecked = item.enabled !== false && isActionable(item);

        return `
          <tr class="${isChecked ? '' : 'row-excluded'}">
            <td class="cell-checkbox" style="text-align: center; cursor: pointer;">
              <input type="checkbox" class="table-checkbox row-checkbox" data-index="${globalIdx}" ${isChecked ? 'checked' : ''} ${isActionable(item) ? '' : 'disabled'} aria-label="Include ${escapeHtml(item.title)}">
            </td>
            <td>
              <div class="title-row">
                ${item.cover
                  ? `<img class="cover-thumb" src="${escapeHtml(item.cover)}" alt="" loading="lazy">`
                  : '<span class="cover-thumb cover-empty" aria-hidden="true"></span>'}
                <div class="title-stack">
                  <div class="cell-title" title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</div>
                  <div class="title-meta">
                    ${malUrl ? `<a href="${malUrl}" class="id-link" target="_blank" rel="noopener noreferrer" title="View on MyAnimeList">MAL ${item.id}</a>` : '<span class="id-missing">no MAL ID</span>'}
                    ${alUrl ? `<a href="${alUrl}" class="id-link" target="_blank" rel="noopener noreferrer" title="View on AniList">AL ${item.anilist_id}</a>` : ''}
                    ${hasRenamed ? `<span class="cell-original" title="${escapeHtml(item.original)}">from ${escapeHtml(item.original)}</span>` : ''}
                  </div>
                  ${pickerHtml(item, globalIdx)}
                  ${fixRowHtml(item, globalIdx)}
                </div>
              </div>
            </td>
            <td>
              <span class="${rowBadge(item).cls}">${escapeHtml(rowBadge(item).text)}</span>
              ${item.year ? `<span class="tag-year">${item.year}</span>` : ''}
            </td>
            <td>${progressHtml(item, globalIdx)}</td>
            <td>${statusCellHtml(item, globalIdx)}</td>
            <td style="text-align:center;">
              <button class="btn-detail" data-detail="${globalIdx}"
                aria-label="More fields for ${escapeHtml(item.title)}"
                title="Score, dates, tags, comments">${expandedRows.has(globalIdx) ? '&#9662;' : '&#8943;'}</button>
            </td>
          </tr>
          ${detailRowHtml(item, globalIdx)}
        `;
      }).join('');

      updateSelectAllState();
    }

    selectAllCheckbox.addEventListener('change', () => {
      const isChecked = selectAllCheckbox.checked;
      currentAnimes.forEach(item => {
        if (!isActionable(item)) { item.enabled = false; return; }
        if (matchesFilters(item)) item.enabled = isChecked;
      });
      renderTable(currentAnimes);
      updateStatsAndExport();
    });

    tableBody.addEventListener('change', (e) => {
      if (e.target && e.target.classList.contains('match-picker')) {
        const idx = parseInt(e.target.dataset.index, 10);
        const item = currentAnimes[idx];
        const pick = item && Array.isArray(item.candidates)
          ? item.candidates[parseInt(e.target.value, 10)] : null;
        if (!pick) return;
        // Replace the identity/metadata only; status, score and progress stay.
        Object.assign(item, pick, { enabled: isActionable(pick) });
        renderTable(currentAnimes);
        updateStatsAndExport();
        showToast('Match changed to ' + pick.title);
        return;
      }

      if (e.target && e.target.classList.contains('detail-input')) {
        const idx = parseInt(e.target.dataset.index, 10);
        const field = e.target.dataset.field;
        const item = currentAnimes[idx];
        if (!item || !field) return;
        const raw = e.target.value.trim();
        if (raw === '') {
          delete item[field];
        } else if (field === 'score') {
          item.score = Math.max(0, Math.min(10, Math.floor(Number(raw) || 0)));
          e.target.value = item.score;
        } else {
          item[field] = raw;
        }
        updateStatsAndExport();
        return;
      }

      if (e.target && e.target.classList.contains('row-status')) {
        const idx = parseInt(e.target.dataset.index, 10);
        const item = currentAnimes[idx];
        if (!item) return;
        item.status = e.target.value;
        // Recolour in place rather than re-rendering, so the select keeps focus.
        e.target.className = 'row-status status-pill ' + statusPillClass(item.status);
        updateStatsAndExport();
        return;
      }

      if (e.target && e.target.classList.contains('progress-input')) {
        const idx = parseInt(e.target.dataset.index, 10);
        const field = e.target.dataset.field;
        const item = currentAnimes[idx];
        if (!item || !field) return;
        const raw = e.target.value.trim();
        if (raw === '') {
          delete item[field];           // fall back to the derived value
        } else {
          let value = Math.max(0, Math.floor(Number(raw) || 0));
          const total = Number(item[PROGRESS_TOTAL_FIELD[field]] || 0);
          if (total > 0 && value > total) value = total;  // MAL rejects progress past the total
          item[field] = value;
          e.target.value = value;
        }
        updateStatsAndExport();
        return;
      }

      if (e.target && e.target.classList.contains('row-checkbox')) {
        const idx = parseInt(e.target.dataset.index, 10);
        if (!isNaN(idx) && currentAnimes[idx]) {
          currentAnimes[idx].enabled = e.target.checked;
          const tr = e.target.closest('tr');
          if (tr) {
            tr.classList.toggle('row-excluded', !e.target.checked);
          }
          updateSelectAllState();
          updateStatsAndExport();
        }
      }
    });

    async function repairRow(idx, queryText) {
      const item = currentAnimes[idx];
      const text = String(queryText || '').trim();
      if (!item || !text) return;
      const digitsOnly = text.length > 0 && Array.from(text).every(ch => ch >= '0' && ch <= '9');
      const url = digitsOnly ? '/api/lookup' : '/api/search';
      const body = digitsOnly
        ? { mal_id: text, media_type: mediaType() }
        : { query: text, media_type: mediaType() };
      try {
        const res = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const payload = await res.json().catch(() => ({}));
        if (!res.ok || !payload.ok) {
          showToast(payload.error || 'Nothing found for that search', 'warning');
          return;
        }
        const found = digitsOnly ? payload.entry : (payload.entries || [])[0];
        const list = digitsOnly ? [payload.entry] : (payload.entries || []);
        if (!found) {
          showToast('Nothing found for that search', 'warning');
          return;
        }
        // Replace identity and metadata; the user's own choices stay put.
        Object.assign(item, found, { enabled: isActionable(found) });
        if (list.length > 1) item.candidates = list;
        renderTable(currentAnimes);
        updateStatsAndExport();
        showToast('Matched to ' + found.title);
      } catch (err) {
        showToast('Could not reach the local server', 'error');
      }
    }

    // Allow clicking anywhere inside the checkbox cell to toggle
    tableBody.addEventListener('click', (e) => {
      const detailBtn = e.target.closest('.btn-detail');
      if (detailBtn) {
        const idx = parseInt(detailBtn.dataset.detail, 10);
        if (expandedRows.has(idx)) {
          expandedRows.delete(idx);
        } else {
          expandedRows.add(idx);
        }
        renderTable(currentAnimes);
        return;
      }

      const fixBtn = e.target.closest('.btn-fix');
      if (fixBtn) {
        const idx = parseInt(fixBtn.dataset.index, 10);
        const input = tableBody.querySelector(`.fix-input[data-index="${idx}"]`);
        const typed = input ? input.value.trim() : '';
        if (typed) {
          repairRow(idx, typed);            // they already typed something specific
        } else {
          openFixer(idx);                   // otherwise show them the candidates
        }
        return;
      }

      const cell = e.target.closest('.cell-checkbox');
      if (cell && e.target.tagName !== 'INPUT') {
        const cb = cell.querySelector('.row-checkbox');
        if (cb) {
          cb.checked = !cb.checked;
          cb.dispatchEvent(new Event('change', { bubbles: true }));
        }
      }
    });

    btnCopyUnresolved.addEventListener('click', async () => {
      const missed = currentAnimes.filter(i => Number(i.id) <= 0).map(i => i.original || i.title);
      if (!missed.length) return;
      try {
        await navigator.clipboard.writeText(missed.join(String.fromCharCode(10)));
        showToast(`Copied ${missed.length} unmatched title(s)`);
      } catch (e) {
        showToast('Clipboard blocked - the unmatched rows are listed in the table', 'warning');
      }
    });

    tableBody.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' || !e.target.classList.contains('fix-input')) return;
      e.preventDefault();
      repairRow(parseInt(e.target.dataset.index, 10), e.target.value);
    });

    searchInput.addEventListener('input', () => {
      showAllRows = false;
      renderTable(currentAnimes);
    });

    chipRow.addEventListener('click', (e) => {
      const chip = e.target.closest('.chip');
      if (!chip) return;
      statusFilter = chip.dataset.chip || '';
      showAllRows = false;
      renderTable(currentAnimes);
      updateStats();
    });

    rowCapNotice.addEventListener('click', (e) => {
      if (!e.target.closest('#btnShowAll')) return;
      showAllRows = true;
      renderTable(currentAnimes);
    });

    function toggleSort(th) {
      const key = th.dataset.sort;
      if (sortKey !== key) {
        sortKey = key;
        sortDir = 1;
      } else if (sortDir === 1) {
        sortDir = -1;
      } else {
        sortKey = '';
        sortDir = 0;
      }
      document.querySelectorAll('th.sortable').forEach(other => {
        const mark = other.querySelector('.sort-mark');
        if (mark) mark.textContent = '';
        other.setAttribute('aria-sort', 'none');
      });
      if (sortKey) {
        const mark = th.querySelector('.sort-mark');
        if (mark) mark.textContent = sortDir === 1 ? '^' : 'v';
        th.setAttribute('aria-sort', sortDir === 1 ? 'ascending' : 'descending');
      }
      renderTable(currentAnimes);
    }

    document.querySelectorAll('th.sortable').forEach(th => {
      th.addEventListener('click', () => toggleSort(th));
      th.addEventListener('keydown', event => {
        // A header you can only reach with a mouse is not a control.
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          toggleSort(th);
        }
      });
    });

    selectStatus.addEventListener('change', () => {
      const newStatus = selectStatus.value;
      currentAnimes.forEach(item => { item.status = newStatus; });
      renderTable(currentAnimes);
      if (currentAnimes.length > 0) updateStatsAndExport();
    });

    inputScore.addEventListener('input', () => {
      const newScore = parseInt(inputScore.value, 10) || 0;
      currentAnimes.forEach(item => { item.score = newScore; });
      if (currentAnimes.length > 0) updateStatsAndExport();
    });

    async function sendRunControl(action) {
      try {
        const res = await fetch('/api/run-control', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: action, run_id: activeRunId }),
        });
        const payload = await res.json().catch(() => ({}));
        if (!res.ok || !payload.ok) showToast(payload.error || 'Could not control the run', 'warning');
      } catch (e) {
        showToast('Could not reach the local server', 'error');
      }
    }

    btnCancel.addEventListener('click', () => {
      btnCancel.disabled = true;
      sendRunControl('cancel');
    });

    btnPause.addEventListener('click', () => {
      isPaused = !isPaused;
      btnPause.textContent = isPaused ? 'Resume' : 'Pause';
      sendRunControl(isPaused ? 'pause' : 'resume');
    });

    btnPreview.addEventListener('click', async () => {
      try {
        const res = await fetch('/api/preview-xml?media=' + mediaType());
        const payload = await res.json().catch(() => ({}));
        if (!res.ok || !payload.ok) {
          showToast(payload.error || 'Nothing to preview yet', 'warning');
          return;
        }
        previewTitle.textContent = payload.name + ' (' + payload.bytes.toLocaleString() + ' bytes'
          + (payload.truncated ? ', truncated' : '') + ')';
        previewBody.textContent = payload.xml;
        showModal(previewModal);
      } catch (e) {
        showToast('Could not reach the local server', 'error');
      }
    });

    btnPreviewClose.addEventListener('click', () => hideModal(previewModal));
    previewModal.addEventListener('click', (e) => {
      if (e.target === previewModal) hideModal(previewModal);
    });
    // Opening a dialog without moving focus leaves a keyboard user outside it,
    // still tabbing the page behind the backdrop.
    let modalReturnFocus = null;
    function showModal(modal) {
      if (!modal.classList.contains('show')) {
        modalReturnFocus = document.activeElement;
      }
      modal.classList.add('show');
      const first = modal.querySelector('input:not([type=hidden]):not([disabled]), textarea, select, button');
      if (first) first.focus();
    }

    function hideModal(modal) {
      const wasOpen = modal.classList.contains('show');
      modal.classList.remove('show');
      if (wasOpen && modalReturnFocus && document.contains(modalReturnFocus)) {
        modalReturnFocus.focus();
        modalReturnFocus = null;
      }
    }

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        hideModal(previewModal);
        hideModal(writeModal);
        hideModal(fixModal);
      }
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !isProcessing) {
        e.preventDefault();
        btnProcess.click();
      }
    });
    btnPreviewCopy.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(previewBody.textContent || '');
        showToast('XML copied');
      } catch (e) {
        showToast('Clipboard blocked by the browser', 'warning');
      }
    });

    toggleArchive.addEventListener('change', saveState);
    toggleFresh.addEventListener('change', saveState);

    // ---- fixing unmatched rows -------------------------------------------
    let fixQueue = [];
    let fixPos = 0;
    let fixFixed = 0;

    // Every row that cannot be used for the current destination, which is
    // exactly the set that shows a Find button. Keying this off "no ids at all"
    // instead meant clicking Find on an AniList-only row under the XML
    // destination opened the fixer on a different title.
    function unmatchedIndexes() {
      return currentAnimes.map((item, i) => isActionable(item) ? -1 : i).filter(i => i >= 0);
    }

    function openFixer(startIndex) {
      fixQueue = unmatchedIndexes();
      if (!fixQueue.length) {
        showToast('Nothing left to fix');
        return;
      }
      fixFixed = 0;
      let wanted = fixQueue.indexOf(startIndex);
      if (wanted < 0 && startIndex >= 0 && currentAnimes[startIndex]) {
        fixQueue.unshift(startIndex);      // asked for a specific row: honour it
        wanted = 0;
      }
      fixPos = wanted >= 0 ? wanted : 0;
      showModal(fixModal);
      showFixCurrent();
    }

    function showFixCurrent() {
      if (fixPos >= fixQueue.length) {
        hideModal(fixModal);
        showToast(fixFixed ? `Fixed ${fixFixed} of ${fixQueue.length}` : 'Nothing changed');
        return;
      }
      const item = currentAnimes[fixQueue[fixPos]];
      if (!item) { fixPos += 1; showFixCurrent(); return; }
      const text = item.original || item.title || '';
      fixOriginal.textContent = text;
      fixQuery.value = text;
      fixCounter.textContent = `${fixPos + 1} of ${fixQueue.length}`;
      fixResults.innerHTML = '<div class="fix-hint">Press Search, or paste an AniList link and press Enter.</div>';
      fixQuery.focus();
      fixQuery.select();
    }

    // The years an entry ran, as one field: a single year when it started and
    // finished in the same one, an open range while it is still running.
    function runYears(d) {
      if (!d || !d.start_year) return '';
      if (!d.end_year) return d.start_year + '-';
      return d.end_year === d.start_year ? String(d.start_year) : d.start_year + '-' + d.end_year;
    }

    // Every title AniList knows this entry by, minus the one already shown as
    // the heading. This is usually what tells two near-identical entries apart.
    function otherTitles(entry, d) {
      const shown = String(entry.title || '').toLowerCase();
      const seen = new Set([shown]);
      const out = [];
      [d.romaji, d.english, d.native].concat(d.synonyms || []).forEach(t => {
        const key = String(t || '').toLowerCase();
        if (!key || seen.has(key)) return;
        seen.add(key);
        out.push(t);
      });
      return out;
    }

    function resultRowHtml(entry, i) {
      const d = entry.details || {};
      const counts = mediaType() === 'manga'
        ? `${entry.chapters || '?'} ch / ${entry.volumes || '?'} vol`
        : `${entry.episodes || '?'} eps`;
      const years = runYears(d) || (entry.year ? String(entry.year) : '');
      const bits = [escapeHtml(entry.type || ''), escapeHtml(years), escapeHtml(d.status || ''), counts,
                    d.mean_score ? d.mean_score + '% rated' : '',
                    d.popularity ? Number(d.popularity).toLocaleString() + ' users' : '']
                   .filter(Boolean);
      // The one thing that decides whether a row can travel as MAL XML, so it
      // is a flag rather than another grey item in the list.
      const ident = Number(entry.id) > 0
        ? `<span class="fix-flag">MAL ${entry.id}</span>`
        : '<span class="fix-flag warn">No MAL ID - AniList only</span>';
      const alts = otherTitles(entry, d);
      return `<button class="fix-result" data-pick="${i}">
        ${entry.cover ? `<img class="cover-thumb" src="${escapeHtml(entry.cover)}" alt="" loading="lazy">`
                      : '<span class="cover-thumb cover-empty" aria-hidden="true"></span>'}
        <span class="grow">
          <span class="name">${escapeHtml(entry.title || '')}</span>
          ${alts.length ? `<span class="alt">${escapeHtml(alts[0])}</span>` : ''}
          <span class="meta">
            ${ident}<span>AniList ${entry.anilist_id || '?'}</span>${bits.map(b => `<span>${b}</span>`).join('')}
          </span>
          ${(d.genres || []).length
            ? `<span class="fix-genres">${d.genres.map(g => `<span class="fix-genre">${escapeHtml(g)}</span>`).join('')}</span>`
            : ''}
          ${alts.length > 1
            ? `<span class="synonyms"><b>Also known as:</b> ${escapeHtml(alts.slice(1).join(' | '))}</span>`
            : ''}
          ${d.description ? `<span class="desc">${escapeHtml(d.description)}</span>` : ''}
        </span>
      </button>`;
    }

    let fixCandidates = [];

    async function runFixSearch() {
      const query = fixQuery.value.trim();
      if (!query) return;
      fixResults.innerHTML = '<div class="fix-hint">Searching AniList...</div>';
      try {
        const res = await fetch('/api/resolve-one', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ query: query, media_type: mediaType(), id_kind: fixIdKind.value }),
        });
        const payload = await res.json().catch(() => ({}));
        if (!payload.ok) {
          fixCandidates = [];
          fixResults.innerHTML = `<div class="fix-hint">${escapeHtml(payload.error || 'Nothing found.')}</div>`;
          return;
        }
        fixCandidates = payload.entries || [];
        fixResults.innerHTML = fixCandidates.map(resultRowHtml).join('')
          || '<div class="fix-hint">Nothing found.</div>';
      } catch (e) {
        fixResults.innerHTML = '<div class="fix-hint">Could not reach the local server.</div>';
      }
    }

    function applyFix(entry) {
      const idx = fixQueue[fixPos];
      const item = currentAnimes[idx];
      if (!item || !entry) return;
      // Identity and metadata change; the user's status, score and progress stay.
      Object.assign(item, entry, { enabled: isActionable(entry) });
      if (fixCandidates.length > 1) item.candidates = fixCandidates;
      fixFixed += 1;
      fixPos += 1;
      renderTable(currentAnimes);
      updateStatsAndExport();
      showFixCurrent();
    }

    btnRemember.addEventListener('click', () => {
      const excluded = currentAnimes.filter(item => isActionable(item) && item.enabled === false);
      if (!excluded.length) {
        showToast('Nothing is unchecked, so there is nothing to skip.', 'warning');
        return;
      }
      excluded.forEach(item => ignoredKeys.add(entryKey(item)));
      saveIgnored();
      updateIgnoredLabel();
      showToast(excluded.length + (excluded.length === 1 ? ' title' : ' titles')
        + ' will stay unchecked in future lists.');
    });

    btnForgetIgnored.addEventListener('click', () => {
      const n = ignoredKeys.size;
      ignoredKeys = new Set();
      saveIgnored();
      updateIgnoredLabel();
      showToast('No longer skipping ' + n + (n === 1 ? ' title' : ' titles') + '.');
    });

    btnFixUnmatched.addEventListener('click', () => openFixer(-1));
    btnFixSearch.addEventListener('click', runFixSearch);
    fixIdKind.addEventListener('change', runFixSearch);
    fixQuery.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); runFixSearch(); }
    });
    fixResults.addEventListener('click', (e) => {
      const pick = e.target.closest('[data-pick]');
      if (pick) applyFix(fixCandidates[parseInt(pick.dataset.pick, 10)]);
    });
    btnFixSkip.addEventListener('click', () => { fixPos += 1; showFixCurrent(); });
    btnFixClose.addEventListener('click', () => {
      hideModal(fixModal);
      if (fixFixed) showToast(`Fixed ${fixFixed} title${fixFixed === 1 ? '' : 's'}`);
    });
    fixModal.addEventListener('click', (e) => {
      if (e.target === fixModal) hideModal(fixModal);
    });

    function writableRows() {
      return currentAnimes.filter(i => i.enabled !== false && canWrite(i));
    }

    // What the server said the last preview would change, and the per-row
    // per-field escape hatches the user ticked against it.
    let lastWritePlan = null;
    let writeOverrides = {};
    let lastWriteSnapshot = null;
    let lastWriteRunId = null;
    let lastWriteFailed = 0;
    let lastWriteCreated = 0;

    const AL_STATUS_LABELS = {
      CURRENT: { anime: 'Watching', manga: 'Reading' },
      PLANNING: { anime: 'Plan to watch', manga: 'Plan to read' },
      COMPLETED: { anime: 'Completed', manga: 'Completed' },
      DROPPED: { anime: 'Dropped', manga: 'Dropped' },
      PAUSED: { anime: 'On hold', manga: 'On hold' },
      REPEATING: { anime: 'Rewatching', manga: 'Rereading' },
    };

    function diffFieldLabel(field) {
      const manga = mediaType() === 'manga';
      if (field === 'progress') return manga ? 'chapters' : 'episodes';
      if (field === 'progressVolumes') return 'volumes';
      if (field === 'scoreRaw') return 'score';
      if (field === 'repeat') return manga ? 'rereads' : 'rewatches';
      if (field === 'startedAt') return 'start date';
      if (field === 'completedAt') return 'finish date';
      return field;
    }

    function diffValueText(field, value) {
      if (value === null || value === undefined || value === '') return 'none';
      if (field === 'status') {
        const row = AL_STATUS_LABELS[value];
        return row ? row[mediaType() === 'manga' ? 'manga' : 'anime'] : String(value);
      }
      // The server asks AniList for POINT_100 on both sides of the comparison,
      // so the scale is stated rather than left to look like a 1-10 score.
      if (field === 'scoreRaw') return Number(value) ? Number(value) + '/100' : 'none';
      if (field === 'startedAt' || field === 'completedAt') {
        if (!value.year) return 'none';
        const parts = [String(value.year)];
        if (value.month) parts.push(String(value.month).padStart(2, '0'));
        if (value.month && value.day) parts.push(String(value.day).padStart(2, '0'));
        return parts.join('-');
      }
      if (field === 'notes') {
        const text = String(value);
        return text.length > 60 ? text.slice(0, 60) + '...' : text;
      }
      return String(Number(value) || 0);
    }

    function writeOverrideFor(id, field) {
      const row = writeOverrides[id];
      return row ? row[field] : undefined;
    }

    function setWriteOverride(id, field, value) {
      if (!writeOverrides[id]) writeOverrides[id] = {};
      if (value) writeOverrides[id][field] = value;
      else delete writeOverrides[id][field];
      if (!Object.keys(writeOverrides[id]).length) delete writeOverrides[id];
    }

    function pickBoxHtml(id, field, action, label, on) {
      return '<label class="diff-pick"><input type="checkbox" data-write-override="' + action
        + '" data-id="' + id + '" data-field="' + escapeHtml(field) + '"'
        + (on ? ' checked' : '') + '> ' + escapeHtml(label) + '</label>';
    }

    function diffLineHtml(row, d) {
      const forced = writeOverrideFor(row.anilist_id, d.field) === 'mine';
      // The arrow always shows what the row ASKED for, so a blocked field does
      // not render as "147 -> 147" and read like a no-op. `applied` is what the
      // account will actually hold, which differs when the policy keeps the
      // account's value or when AniList overrides us on a status change.
      // Ticking "keep mine" beats the policy, so the outcome becomes the row's
      // own value - but it does not beat AniList, which overrides us whatever
      // anyone ticks (measured: a forced chapter count on a row that stays
      // completed is still clamped to the total).
      const outcome = (forced && !d.by_anilist) ? d.to : d.applied;
      const outcomeText = diffValueText(d.field, outcome);
      // A field we never send has no asked-for value; show what AniList does.
      const asked = (d.to === null || d.to === undefined) ? outcome : d.to;
      const askedText = diffValueText(d.field, asked);
      const kept = askedText !== outcomeText;
      const bits = [
        '<span class="diff-field">' + escapeHtml(diffFieldLabel(d.field)) + '</span>',
        '<span class="diff-from">' + escapeHtml(diffValueText(d.field, d.from)) + '</span>',
        '<span class="diff-arrow">-&gt;</span>',
        '<span class="diff-to' + (kept ? ' overridden' : '') + '">' + escapeHtml(askedText) + '</span>',
      ];
      if (kept) {
        bits.push('<span class="diff-note warn">' + (d.by_anilist
          ? 'AniList sets ' + escapeHtml(outcomeText) + ' itself'
          : 'stays ' + escapeHtml(outcomeText)) + '</span>');
      } else if (d.by_anilist) {
        bits.push('<span class="diff-note warn">AniList sets this itself</span>');
      } else if (forced) {
        bits.push('<span class="diff-note warn">forced</span>');
      }
      if (d.blocked) {
        bits.push(pickBoxHtml(row.anilist_id, d.field, 'mine', 'keep mine', forced));
      }
      return '<div class="diff-line' + (d.blocked ? ' blocked' : '') + '">' + bits.join('') + '</div>';
    }

    function staleDateLineHtml(row) {
      const clearing = writeOverrideFor(row.anilist_id, 'completedAt') === 'clear';
      return '<div class="diff-line blocked">'
        + '<span class="diff-field">' + escapeHtml(diffFieldLabel('completedAt')) + '</span>'
        + '<span class="diff-note warn">' + (clearing
            ? 'will be cleared, so the entry is no longer both in progress and finished'
            : 'this row stops being completed but keeps its finish date on AniList')
        + '</span>'
        + pickBoxHtml(row.anilist_id, 'completedAt', 'clear', 'clear the finish date', clearing)
        + '</div>';
    }

    function writeRowHtml(row) {
      const lines = (row.diff || []).map(d => diffLineHtml(row, d));
      if (row.stale_completed_at) lines.push(staleDateLineHtml(row));
      return '<div class="diff-row"><div class="diff-head">' + escapeHtml(row.title)
        + '<span class="diff-tag">' + (row.update ? 'update' : 'new') + '</span></div>'
        + lines.join('') + '</div>';
    }

    function renderWritePlan(plan) {
      const changing = (plan.rows || []).filter(r => (r.diff || []).length || r.stale_completed_at);
      const open = writePlan.querySelector('.diff-list');
      const keepTop = open ? open.scrollTop : 0;
      const skippedNote = plan.skipped.length
        ? `<div>${plan.skipped.length} row${plan.skipped.length === 1 ? '' : 's'} will be skipped: ${escapeHtml(plan.skipped.slice(0, 3).map(sk => sk.title).join(', '))}${plan.skipped.length > 3 ? '...' : ''}</div>`
        : '';
      const unchangedNote = plan.unchanged
        ? `<div>${plan.unchanged} already match your account and will not be touched.</div>`
        : '';
      const conflictNote = plan.conflicts
        ? `<div class="warn">${plan.conflicts} row${plan.conflicts === 1 ? '' : 's'} would send a value backwards. Those fields keep what AniList has unless you tick "keep mine".</div>`
        : '';
      const list = changing.length
        ? '<div class="diff-list">' + changing.map(writeRowHtml).join('') + '</div>'
        : '<div>Nothing on your account changes.</div>';
      writePlan.innerHTML = `
        <div>Account: <strong>${escapeHtml(plan.viewer)}</strong></div>
        <div><strong>${plan.creates}</strong> ${plan.creates === 1 ? 'entry' : 'entries'} will be added,
             <strong>${plan.updates}</strong> already on your list will be updated.</div>
        ${skippedNote}${unchangedNote}${conflictNote}
        ${list}
        <div class="warn">This modifies your AniList account. Entries already on your list are
          updated in place, never duplicated, and the state before this write is saved so it
          can be undone.</div>`;
      const again = writePlan.querySelector('.diff-list');
      if (again) again.scrollTop = keepTop;
    }

    writePlan.addEventListener('change', (e) => {
      const box = e.target && e.target.closest ? e.target.closest('input[data-write-override]') : null;
      if (!box) return;
      setWriteOverride(box.dataset.id, box.dataset.field,
                       box.checked ? box.dataset.writeOverride : null);
      if (lastWritePlan) renderWritePlan(lastWritePlan);
    });

    function updateWriteAftermath() {
      const live = destination === 'anilist';
      btnUndo.style.display = (live && lastWriteSnapshot) ? '' : 'none';
      btnRetry.style.display = (live && lastWriteFailed > 0 && lastWriteRunId) ? '' : 'none';
      btnRetry.textContent = 'Retry ' + lastWriteFailed + ' failed';
    }

    // One reader for both streams. Returning false from the handler stops it.
    async function streamSse(res, onEvent) {
      const reader = res.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';
      let done = false;
      while (!done) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        const parts = buffer.split(String.fromCharCode(10) + String.fromCharCode(10));
        buffer = parts.pop();
        for (const part of parts) {
          const trimmed = part.trim();
          if (!trimmed.startsWith('data: ')) continue;
          let event;
          try {
            event = JSON.parse(trimmed.slice(6));
          } catch (errParse) {
            continue;
          }
          if (onEvent(event) === false) { done = true; break; }
        }
      }
      try { reader.cancel(); } catch (e) {}
    }

    function stripForServer(item) {
      const { candidates, ...rest } = item;
      return rest;
    }

    btnTokenTest.addEventListener('click', async () => {
      const token = anilistToken.value.trim();
      if (!token) {
        tokenStatus.textContent = 'Paste a token first.';
        tokenStatus.className = 'token-status bad';
        return;
      }
      tokenStatus.textContent = 'Checking...';
      tokenStatus.className = 'token-status';
      try {
        const res = await fetch('/api/anilist-test', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ token: token }),
        });
        const payload = await res.json().catch(() => ({}));
        if (!res.ok || !payload.ok) {
          anilistUser = null;
          tokenStatus.textContent = payload.error || 'That token did not work.';
          tokenStatus.className = 'token-status bad';
          return;
        }
        anilistUser = { id: payload.id, name: payload.name };
        tokenStatus.textContent = 'Connected as ' + payload.name + '.';
        tokenStatus.className = 'token-status ok';
      } catch (e) {
        tokenStatus.textContent = 'Could not reach the local server.';
        tokenStatus.className = 'token-status bad';
      }
    });

    btnWrite.addEventListener('click', async () => {
      const token = anilistToken.value.trim();
      const rows = writableRows();
      if (!token) {
        showToast('Paste and check an AniList token first', 'warning');
        return;
      }
      if (!rows.length) {
        showToast('No rows with an AniList id are selected', 'warning');
        return;
      }
      writePlan.innerHTML = 'Checking your AniList list...';
      lastWritePlan = null;
      writeOverrides = {};
      showModal(writeModal);
      try {
        const res = await fetch('/api/anilist-preview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            token: token,
            media_type: mediaType(),
            status: selectStatus.value,
            score: parseInt(inputScore.value, 10) || 0,
            entries: rows.map(stripForServer),
          }),
        });
        const plan = await res.json().catch(() => ({}));
        if (!res.ok || !plan.ok) {
          writePlan.innerHTML = escapeHtml(plan.error || 'Could not read your AniList list.');
          return;
        }
        lastWritePlan = plan;
        renderWritePlan(plan);
      } catch (e) {
        writePlan.innerHTML = 'Could not reach the local server.';
      }
    });

    btnWriteCancel.addEventListener('click', () => hideModal(writeModal));
    writeModal.addEventListener('click', (e) => {
      if (e.target === writeModal) hideModal(writeModal);
    });

    btnWriteConfirm.addEventListener('click', async () => {
      const token = anilistToken.value.trim();
      const rows = writableRows();
      if (!token || !rows.length) return;
      hideModal(writeModal);
      await runWrite({
        token: token,
        confirm: true,
        media_type: mediaType(),
        status: selectStatus.value,
        score: parseInt(inputScore.value, 10) || 0,
        overrides: writeOverrides,
        entries: rows.map(stripForServer),
      }, 'Writing to AniList...');
    });

    btnRetry.addEventListener('click', async () => {
      const token = anilistToken.value.trim();
      if (!token) {
        showToast('Paste and check an AniList token first', 'warning');
        return;
      }
      if (!lastWriteRunId || !lastWriteFailed) return;
      await runWrite({
        token: token,
        confirm: true,
        retry: true,
        run_id: lastWriteRunId,
        media_type: mediaType(),
      }, 'Retrying the rows that failed...');
    });

    async function runWrite(body, label) {
      isProcessing = true;
      btnWrite.disabled = true;
      btnUndo.disabled = true;
      btnRetry.disabled = true;
      progressCard.style.display = 'flex';
      progressBarFill.style.transform = 'scaleX(0)';
      progressCurrentTitle.textContent = label;
      progressCurrentSub.textContent = '';
      try {
        const res = await fetch('/api/anilist-write', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          showToast(err.error || 'The write could not start', 'error');
          return;
        }
        await streamSse(res, (event) => {
          if (event.type === 'write_progress') {
            progressBarFill.style.transform = 'scaleX(' + Math.min(1, event.percent / 100) + ')';
            progressCountLabel.textContent = event.current + ' / ' + event.total;
            progressPercentLabel.textContent = Math.round(event.percent) + '%';
            progressCurrentTitle.textContent = `[${event.current}/${event.total}] ${event.title}`;
            progressCurrentSub.textContent = event.ok ? 'Saved' : 'Failed';
          } else if (event.type === 'plan') {
            if (event.snapshot) lastWriteSnapshot = event.snapshot;
            progressCurrentSub.textContent = `${event.creates} to add, ${event.updates} to update`;
          } else if (event.type === 'error') {
            // The existing-entries read failed, so nothing was sent: writing
            // blind would duplicate entries the account already has.
            showToast(event.error || 'AniList could not be read', 'error');
            return false;
          } else if (event.type === 'write_complete') {
            const failedCount = event.failed_count !== undefined
              ? event.failed_count : (event.failed || []).length;
            lastWriteRunId = event.run_id || null;
            lastWriteFailed = failedCount;
            lastWriteCreated = event.created || 0;
            if (event.snapshot) lastWriteSnapshot = event.snapshot;
            updateWriteAftermath();
            showToast(`AniList updated: ${event.created} added, ${event.updated} updated`
              + (failedCount ? `, ${failedCount} failed` : ''), failedCount ? 'warning' : 'success');
            if (failedCount) {
              unresolvedBar.classList.add('show');
              unresolvedText.innerHTML = `<strong>${failedCount}</strong> AniList ${failedCount === 1 ? 'write' : 'writes'} failed: `
                + event.failed.slice(0, 3).map(f => escapeHtml(f.title + ' (' + f.error + ')')).join('; ');
            }
            return false;
          } else if (event.type === 'cancelled') {
            if (event.snapshot) lastWriteSnapshot = event.snapshot;
            updateWriteAftermath();
            showToast('Write cancelled after ' + event.written + ' entries', 'warning');
            return false;
          }
        });
      } catch (e) {
        showToast('Lost contact with the local server', 'error');
      } finally {
        isProcessing = false;
        btnWrite.disabled = false;
        btnUndo.disabled = false;
        btnRetry.disabled = false;
        setTimeout(() => { progressCard.style.display = 'none'; }, 1500);
      }
    }

    btnUndoCancel.addEventListener('click', () => hideModal(undoModal));
    undoModal.addEventListener('click', (e) => {
      if (e.target === undoModal) hideModal(undoModal);
    });

    btnUndo.addEventListener('click', () => {
      if (!lastWriteSnapshot) return;
      const deleteBox = lastWriteCreated
        ? `<div class="warn"><label class="diff-pick"><input type="checkbox" id="undoDeleteCreated">
             also delete the ${lastWriteCreated} ${lastWriteCreated === 1 ? 'entry' : 'entries'} this
             write created</label> Deleting removes them from your account for good; leaving this
             unticked keeps them exactly as the write left them.</div>`
        : '';
      undoPlan.innerHTML = `
        <div>Putting your account back the way it was before the last write.</div>
        <div>Rows that already existed are restored field by field, including notes and dates.</div>
        ${deleteBox}
        <div class="warn">AniList stamps a start date itself when an entry becomes in-progress
          without one, so a row that had no start date before may keep the one AniList added.</div>`;
      showModal(undoModal);
    });

    btnUndoConfirm.addEventListener('click', async () => {
      const token = anilistToken.value.trim();
      if (!token) {
        showToast('Paste and check an AniList token first', 'warning');
        return;
      }
      if (!lastWriteSnapshot) return;
      const deleteBox = document.getElementById('undoDeleteCreated');
      hideModal(undoModal);
      isProcessing = true;
      btnWrite.disabled = true;
      btnUndo.disabled = true;
      progressCard.style.display = 'flex';
      progressBarFill.style.transform = 'scaleX(0)';
      progressCurrentTitle.textContent = 'Undoing the last write...';
      progressCurrentSub.textContent = '';
      try {
        const res = await fetch('/api/anilist-undo', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            token: token,
            confirm: true,
            snapshot: lastWriteSnapshot,
            delete_created: !!(deleteBox && deleteBox.checked),
          }),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          showToast(err.error || 'The undo could not start', 'error');
          return;
        }
        await streamSse(res, (event) => {
          if (event.type === 'write_progress') {
            progressBarFill.style.transform = 'scaleX(' + Math.min(1, event.percent / 100) + ')';
            progressCountLabel.textContent = event.current + ' / ' + event.total;
            progressPercentLabel.textContent = Math.round(event.percent) + '%';
            progressCurrentTitle.textContent = `[${event.current}/${event.total}] ${event.title}`;
            progressCurrentSub.textContent = event.ok ? 'Restored' : 'Failed';
          } else if (event.type === 'plan') {
            progressCurrentSub.textContent = `${event.restores} to restore, ${event.deletes} to delete`;
          } else if (event.type === 'undo_complete') {
            const notes = [];
            if (event.failed_count) notes.push(event.failed_count + ' failed');
            if ((event.unrecoverable || []).length) {
              notes.push((event.unrecoverable || []).length + ' could not be undone');
            }
            showToast(`Undo done: ${event.restored} restored, ${event.deleted} deleted`
              + (notes.length ? ' (' + notes.join(', ') + ')' : ''),
              notes.length ? 'warning' : 'success');
            if (notes.length) {
              unresolvedBar.classList.add('show');
              unresolvedText.innerHTML = '<strong>Undo was partial.</strong> '
                + (event.failed || []).slice(0, 3)
                    .map(f => escapeHtml(f.title + ' (' + f.error + ')')).join('; ')
                + (event.unrecoverable || []).slice(0, 3)
                    .map(u => escapeHtml(' ' + u.title + ' (' + u.reason + ')')).join('; ');
            }
            return false;
          } else if (event.type === 'cancelled') {
            showToast('Undo cancelled after ' + event.written + ' rows', 'warning');
            return false;
          }
        });
      } catch (e) {
        showToast('Lost contact with the local server', 'error');
      } finally {
        isProcessing = false;
        btnWrite.disabled = false;
        btnUndo.disabled = false;
        setTimeout(() => { progressCard.style.display = 'none'; }, 1500);
      }
    });

    btnImport.addEventListener('click', () => importFile.click());

    importFile.addEventListener('change', async () => {
      const file = importFile.files && importFile.files[0];
      importFile.value = '';            // let the same file be picked twice
      if (!file) return;
      let text;
      try {
        if (file.name.toLowerCase().endsWith('.gz')) {
          if (typeof DecompressionStream === 'undefined') {
            showToast('This browser cannot unpack .gz - import the .xml instead', 'warning');
            return;
          }
          const stream = file.stream().pipeThrough(new DecompressionStream('gzip'));
          text = await new Response(stream).text();
        } else {
          text = await file.text();
        }
      } catch (err) {
        showToast('Could not read that file', 'error');
        return;
      }

      let payload;
      try {
        const res = await fetch('/api/parse-mal-xml', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ xml: text }),
        });
        payload = await res.json().catch(() => ({}));
        if (!res.ok || !payload.ok) {
          showToast(payload.error || 'That file could not be imported', 'error');
          return;
        }
      } catch (err) {
        showToast('Could not reach the local server', 'error');
        return;
      }

      if (payload.media_type !== mediaType()) {
        mediaTypeSelect.value = payload.media_type;
        updateMediaTypeUI(false);       // keep the rows; only the labels change
      }

      // Imported values win on the fields the file actually carries; the row's
      // AniList identity and candidate list are kept.
      const byId = new Map(currentAnimes.filter(i => Number(i.id) > 0).map(i => [Number(i.id), i]));
      let matched = 0;
      let added = 0;
      payload.entries.forEach(entry => {
        const existing = byId.get(Number(entry.id));
        if (existing) {
          const keep = {
            anilist_id: existing.anilist_id,
            candidates: existing.candidates,
            cover: existing.cover,
            year: existing.year,
            original: existing.original,
            can_write_anilist: existing.can_write_anilist,
          };
          Object.assign(existing, entry, keep);
          existing.enabled = isActionable(existing);
          matched += 1;
        } else if (addUnmatchedImports.checked) {
          currentAnimes.push({ ...entry, original: entry.title, enabled: enabledFor(entry) });
          added += 1;
        }
      });
      renderTable(currentAnimes);
      updateStatsAndExport();
      showToast(`Imported: ${matched} matched, ${added} added, ${payload.skipped} skipped`);
    });

    destinationSelect.addEventListener('change', () => setDestination(destinationSelect.value));
    destinationPrompt.addEventListener('click', (e) => {
      const choice = e.target.closest('[data-destination]');
      if (choice) setDestination(choice.dataset.destination);
    });

    ['dragenter', 'dragover'].forEach(name => {
      txtArea.addEventListener(name, (e) => {
        e.preventDefault();
        txtArea.style.borderColor = 'var(--border-focus)';
      });
    });
    ['dragleave', 'drop'].forEach(name => {
      txtArea.addEventListener(name, () => { txtArea.style.borderColor = ''; });
    });
    txtArea.addEventListener('drop', async (e) => {
      e.preventDefault();
      const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (!file) return;
      try {
        txtArea.value = await file.text();
        updateLineCounter();
        saveState();
        showToast('Loaded ' + file.name);
      } catch (err) {
        showToast('Could not read that file', 'error');
      }
    });

    function escapeHtml(str) {
      if (!str) return '';
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    }

    const btnTheme = document.getElementById('btnTheme');

    function applyTheme(mode) {
      document.body.classList.toggle('light', mode === 'light');
      btnTheme.textContent = mode === 'light' ? 'Dark mode' : 'Light mode';
    }

    let themeMode = null;
    try {
      themeMode = localStorage.getItem('aniexport.theme');
    } catch (e) {}
    if (themeMode !== 'light' && themeMode !== 'dark') themeMode = 'dark';
    applyTheme(themeMode);

    btnTheme.addEventListener('click', () => {
      themeMode = themeMode === 'light' ? 'dark' : 'light';
      try {
        localStorage.setItem('aniexport.theme', themeMode);
      } catch (e) {}
      applyTheme(themeMode);
    });

    window.addEventListener('beforeunload', (e) => {
      saveStateNow();
      if (!isProcessing) return;
      e.preventDefault();
      e.returnValue = '';
    });

    // A snapshot outlives the page, so the undo stays offered after a reload.
    (async () => {
      try {
        const res = await fetch('/api/anilist-snapshots');
        const payload = await res.json().catch(() => ({}));
        const newest = (payload.snapshots || [])[0];
        if (newest && !lastWriteSnapshot) {
          lastWriteSnapshot = newest.name;
          lastWriteCreated = newest.deletable || 0;
          updateWriteAftermath();
        }
      } catch (e) {}
    })();

    // Initial load: restore the previous session, or start clean.
    if (!restoreState()) {
      updateMediaTypeUI(false);
      loadData();
      setDestination('', false);   // first run: ask rather than assume
    }
  </script>
</body>
</html>
"""


class MalGuiHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler for the local MAL Export Studio Web GUI."""

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress noisy standard request logging to console
        pass

    def send_json(self, data: Any, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            body = HTML_TEMPLATE.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        elif path in ("/api/download/xml", "/api/download/gz"):
            wants_gz = path.endswith("gz")
            requested = urllib.parse.parse_qs(parsed.query).get("media", [None])[0]
            if requested is None:
                requested = EXPORT_MEDIA_TYPE          # omitted = whatever was last built
            if requested not in ("anime", "manga"):
                self.send_json({"ok": False, "error": "media must be anime or manga"}, status=400)
                return
            xml_path, gz_path = _export_paths(requested)
            xml_name, gz_name = _export_filenames(requested)
            target, filename = (gz_path, gz_name) if wants_gz else (xml_path, xml_name)

            archive = urllib.parse.parse_qs(parsed.query).get("archive", [None])[0]
            if archive:
                # Whitelist the name outright: no separators, no traversal, and it
                # must sit in the export directory next to the live files.
                allowed = archive and set(archive) <= ARCHIVE_NAME_CHARS and ".." not in archive
                if not allowed or not archive.endswith(".xml.gz" if wants_gz else ".xml"):
                    self.send_json({"ok": False, "error": "invalid archive name"}, status=400)
                    return
                target = xml_path.parent / archive
                filename = archive
                if not target.is_file():
                    self.send_error(HTTPStatus.NOT_FOUND, "Archive not found")
                    return
            if not target.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, f"No {requested} export has been generated yet")
                return
            content = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/gzip" if wants_gz else "application/xml; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)
            return

        elif path == "/api/export-state":
            self.send_json(_export_state())
            return

        elif path == "/api/anilist-snapshots":
            # What an undo can be run against. Snapshots hold no token, so this
            # is safe to serve, and reading it survives a page reload.
            self.send_json({"ok": True, "snapshots": list_undo_snapshots()})
            return

        elif path == "/api/preview-xml":
            query = urllib.parse.parse_qs(parsed.query)
            requested = query.get("media", [EXPORT_MEDIA_TYPE])[0]
            if requested not in ("anime", "manga"):
                self.send_json({"ok": False, "error": "media must be anime or manga"}, status=400)
                return
            xml_path, _ = _export_paths(requested)
            if not xml_path.is_file():
                self.send_json({"ok": False, "error": f"No {requested} export has been generated yet"}, status=404)
                return
            raw = xml_path.read_bytes()
            truncated = len(raw) > MAX_PREVIEW_BYTES
            text = raw[:MAX_PREVIEW_BYTES].decode("utf-8", errors="replace")
            self.send_json({"ok": True, "xml": text, "truncated": truncated,
                            "name": xml_path.name, "bytes": len(raw)})
            return

        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"

        try:
            data = json.loads(raw_body.decode("utf-8"))
        except Exception:
            data = {}

        if path == "/api/process":
            global EXPORT_MEDIA_TYPE
            media_type = "manga" if data.get("media_type") == "manga" else "anime"
            EXPORT_MEDIA_TYPE = media_type
            content = data.get("content", "")
            default_status = data.get("status", "Completed")
            default_score = int(data.get("score", 0))

            # Parse lines from the pasted content directly (no disk reading)
            raw_lines = content.splitlines()
            cleaned_titles: List[str] = []
            for line in raw_lines:
                sanitized = sanitize_title_line(line)
                if sanitized:
                    cleaned_titles.append(sanitized)

            if not cleaned_titles:
                self.send_json({"ok": False, "error": f"No valid {media_type} titles found in the list."}, status=400)
                return

            if not _run_lock.acquire(blocking=False):
                self.send_json({"ok": False, "error": "A run is already in progress"}, status=409)
                return
            run_id, control = _register_run()

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            def send_event(event_dict: Dict[str, Any]) -> None:
                payload = f"data: {json.dumps(event_dict, ensure_ascii=False)}\n\n".encode("utf-8")
                self.wfile.write(payload)
                self.wfile.flush()

            # Duplicates resolve once, so progress and the final event both count
            # unique titles rather than pasted lines.
            total_count = len(dict.fromkeys(cleaned_titles))
            expand_sequels = bool(data.get("expand_sequels", False))
            session_cache: Dict[str, List[Dict[str, Any]]] = {}
            resolved_list: List[Dict[str, Any]] = []

            def notify_rate_limit(seconds: float) -> None:
                send_event({"type": "rate_limit", "seconds": round(seconds)})

            send_event({"type": "started", "run_id": run_id, "total": total_count})

            def wait_while_paused() -> bool:
                """Block while paused. Returns False if the run was cancelled."""
                announced = False
                while control["pause"].is_set() and not control["cancel"].is_set():
                    if not announced:
                        send_event({"type": "paused"})
                        announced = True
                    time.sleep(0.2)
                if announced and not control["cancel"].is_set():
                    send_event({"type": "resumed"})
                return not control["cancel"].is_set()

            cancelled = False
            set_rate_limit_notifier(notify_rate_limit)

            def should_cancel() -> bool:
                # Checked between batches: pause parks here, cancel unwinds.
                if not wait_while_paused():
                    return True
                return control["cancel"].is_set()

            network_failures: List[str] = []

            def decorate(resolved: Dict[str, Any], raw_title: str) -> Dict[str, Any]:
                item = dict(resolved)
                item["original"] = raw_title
                item["status"] = default_status
                item["score"] = default_score
                return item

            def on_progress(stage: str, done: int, total: int, info=None) -> None:
                """Stream rows for the live table only.

                The authoritative list is rebuilt from the resolver's return value
                after the run; appending here as well dropped sequel rows, which
                are added after a title has already streamed.
                """
                if stage == "network_error":
                    failed_titles = (info or {}).get("titles") or []
                    network_failures.extend(failed_titles)
                    send_event({"type": "network_error", "titles": failed_titles,
                                "count": len(network_failures)})
                    return
                title = (info or {}).get("title") or ""
                batch_items = [decorate(r, title) for r in ((info or {}).get("entries") or [])]
                remaining = max(0, total - done)
                # One request covers a whole batch, so pace the estimate by batches.
                batches_left = (remaining + BATCH_SIZE - 1) // BATCH_SIZE
                send_event({
                    "type": "progress",
                    "current": done,
                    "total": total,
                    "title": title,
                    "resolved_title": batch_items[0]["title"] if batch_items else title,
                    "percent": round((done / total) * 100, 1) if total else 100.0,
                    "interval": current_interval(),
                    "eta_seconds": round(batches_left * current_interval() + remaining * 0.05, 1),
                    "stage": stage,
                    "entries": batch_items,
                })

            try:
                resolved_map = resolve_many_entries(
                    cleaned_titles,
                    media_type=media_type,
                    expand_sequels=expand_sequels,
                    session_cache=session_cache,
                    use_cache=bool(data.get("use_cache", True)),
                    prefer_mal_id=data.get("destination") != "anilist",
                    on_progress=on_progress,
                    should_cancel=should_cancel,
                )
                if control["cancel"].is_set():
                    cancelled = True
                # The resolver's return value is the single source of truth, in
                # the order the titles were pasted. Includes expanded sequels,
                # cache hits and unresolved rows alike.
                handled: set = set()
                for raw_title in cleaned_titles:
                    if raw_title in handled:
                        continue
                    handled.add(raw_title)
                    for resolved in resolved_map.get(raw_title, []):
                        resolved_list.append(decorate(resolved, raw_title))
            finally:
                set_rate_limit_notifier(None)
                _unregister_run(run_id)
                _run_lock.release()

            if cancelled:
                # Write nothing: the files on disk still belong to the last finished
                # run. The client re-syncs from what it shows, if the user keeps it.
                send_event({
                    "type": "cancelled",
                    "entries": deduplicate_entries(resolved_list),
                    "resolved": len(resolved_list),
                })
                self.close_connection = True
                return

            unique_entries = deduplicate_entries(resolved_list)
            # Two different pasted lines can resolve to the same MAL entry. The
            # second one is dropped, and used to vanish without a word, which is
            # how a 185-title list quietly becomes 150 rows.
            kept = {id(entry) for entry in unique_entries}
            merged = [
                {"original": entry.get("original"), "title": entry.get("title"),
                 "id": entry.get("id")}
                for entry in resolved_list if id(entry) not in kept
            ]

            # Build media-specific XML & GZ. Invalid MAL IDs are omitted by the builders.
            try:
                xml_str = (
                    build_mal_manga_xml(unique_entries, default_status=default_status, default_score=default_score)
                    if media_type == "manga"
                    else build_mal_xml(unique_entries, default_status=default_status, default_score=default_score)
                )
            except ValueError as exc:
                # Headers are already sent, so this cannot be a 400 - tell the client
                # over the stream instead of dying with a traceback and no event.
                send_event({"type": "error", "error": str(exc)})
                self.close_connection = True
                return
            xml_bytes = xml_str.encode("utf-8")

            xml_path, gz_path = _export_paths(media_type)
            xml_path.write_bytes(xml_bytes)
            gz_bytes = gzip.compress(xml_bytes, compresslevel=9)
            with open(gz_path, "wb") as f_out:
                f_out.write(gz_bytes)
            if data.get("archive"):
                _write_archive(media_type, xml_bytes, gz_bytes)

            send_event({
                "type": "complete",
                "ok": True,
                "total": total_count,
                "entries": unique_entries,
                "network_failures": len(network_failures),
                "merged": merged,
            })
            self.close_connection = True
            return

        elif path == "/api/run-control":
            action = data.get("action")
            run_id = data.get("run_id")
            if action not in ("cancel", "pause", "resume"):
                self.send_json({"ok": False, "error": "action must be cancel, pause or resume"}, status=400)
                return
            with _runs_guard:
                control = _active_runs.get(run_id) if run_id else (
                    next(iter(_active_runs.values())) if len(_active_runs) == 1 else None)
            if control is None:
                self.send_json({"ok": False, "error": "no matching run in progress"}, status=404)
                return
            if action == "cancel":
                control["cancel"].set()
                control["pause"].clear()      # never leave a cancelled run parked
            elif action == "pause":
                control["pause"].set()
            else:
                control["pause"].clear()
            self.send_json({"ok": True, "action": action})
            return

        elif path == "/api/anilist-test":
            # The token lives in this request only: never logged, never stored,
            # never echoed back in a response body.
            token = str(data.get("token") or "").strip()
            if not token:
                self.send_json({"ok": False, "error": "Paste an AniList access token first."}, status=400)
                return
            self.send_json(anilist_viewer(token))
            return

        elif path == "/api/anilist-preview":
            token = str(data.get("token") or "").strip()
            if not token:
                self.send_json({"ok": False, "error": "Paste an AniList access token first."}, status=400)
                return
            viewer = anilist_viewer(token)
            if not viewer.get("ok"):
                self.send_json(viewer, status=401)
                return
            media_type = "manga" if data.get("media_type") == "manga" else "anime"
            entries = data.get("entries", [])
            # Rows imported from a MAL export carry only a MAL id; without this
            # they can never be written to AniList.
            filled = backfill_anilist_ids(entries, media_type)
            try:
                existing = anilist_existing_entries(
                    token, viewer["id"], media_type,
                    [e.get("anilist_id") for e in entries],
                )
            except AniListReadError as exc:
                # Refusing is correct: without the existing entries these rows
                # would be written as creates and duplicate what is already there.
                self.send_json({"ok": False, "error": _read_failure_message(exc)}, status=502)
                return
            payloads, skipped = iter_write_payloads(
                entries, media_type, existing,
                default_status=data.get("status", "Completed"),
                default_score=int(data.get("score", 0)),
                policy=_write_policy(data), overrides=_row_overrides(data),
            )
            self.send_json({
                "ok": True,
                "viewer": viewer.get("name", ""),
                "backfilled": filled,
                "updates": sum(1 for p in payloads if p["update"]),
                "creates": sum(1 for p in payloads if not p["update"]),
                "unchanged": sum(1 for p in payloads if not p["diff"]),
                "conflicts": sum(1 for p in payloads
                                 if any(d["blocked"] for d in p["diff"])),
                "skipped": skipped,
                "total": len(payloads),
                "rows": [{"title": p["title"], "anilist_id": p["anilist_id"],
                          "update": p["update"], "diff": p["diff"],
                          "stale_completed_at": p["stale_completed_at"]}
                         for p in payloads],
            })
            return

        elif path == "/api/anilist-write":
            token = str(data.get("token") or "").strip()
            if not token:
                self.send_json({"ok": False, "error": "Paste an AniList access token first."}, status=400)
                return
            if not data.get("confirm"):
                self.send_json({"ok": False, "error": "This writes to your AniList account and needs confirmation."}, status=400)
                return
            entries = data.get("entries", [])
            retry_payloads = _take_failures(str(data.get("run_id") or "")) if data.get("retry") else None
            if data.get("retry") and not retry_payloads:
                self.send_json({"ok": False, "error": "Those failed rows are no longer "
                                                      "available to retry; run the write again."},
                               status=404)
                return
            if not entries and not retry_payloads:
                self.send_json({"ok": False, "error": "Nothing selected to write."}, status=400)
                return
            if not _run_lock.acquire(blocking=False):
                self.send_json({"ok": False, "error": "A run is already in progress"}, status=409)
                return
            run_id, control = _register_run()

            media_type = "manga" if data.get("media_type") == "manga" else "anime"
            viewer = anilist_viewer(token)
            if not viewer.get("ok"):
                _unregister_run(run_id)
                _run_lock.release()
                self.send_json(viewer, status=401)
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            def send_event(event_dict: Dict[str, Any]) -> None:
                payload = f"data: {json.dumps(event_dict, ensure_ascii=False)}\n\n".encode("utf-8")
                self.wfile.write(payload)
                self.wfile.flush()

            updated = 0
            created = 0
            failed: List[Dict[str, str]] = []
            failed_payloads: List[Dict[str, Any]] = []
            snapshot_name: Optional[str] = None
            try:
                send_event({"type": "started", "run_id": run_id, "viewer": viewer.get("name", "")})
                skipped: List[Dict[str, str]] = []
                if retry_payloads is not None:
                    # A retry re-sends the exact payloads that failed, so the
                    # conflict policy that produced them is not re-run, and it
                    # keeps writing into the original run's snapshot.
                    payloads = retry_payloads["payloads"]
                    snapshot_name = retry_payloads.get("snapshot")
                    snapshot = load_undo_snapshot(snapshot_name) if snapshot_name else None
                    if snapshot is None:
                        snapshot_name = None
                    existing = {}
                else:
                    backfill_anilist_ids(entries, media_type)
                    try:
                        existing = anilist_existing_entries(
                            token, viewer["id"], media_type,
                            [e.get("anilist_id") for e in entries],
                        )
                    except AniListReadError as exc:
                        send_event({"type": "error", "error": _read_failure_message(exc)})
                        # The stream is the whole response, so it has to be
                        # closed here or the client waits for rows that never come.
                        self.close_connection = True
                        return
                    payloads, skipped = iter_write_payloads(
                        entries, media_type, existing,
                        default_status=data.get("status", "Completed"),
                        default_score=int(data.get("score", 0)),
                        policy=_write_policy(data), overrides=_row_overrides(data),
                    )
                    # Captured before the first mutation: the only record of what
                    # the account held, and it never contains the token.
                    snapshot = build_undo_snapshot(media_type, viewer.get("id", 0),
                                                   payloads, existing)
                    snapshot_name = save_undo_snapshot(snapshot)
                    if snapshot_name:
                        prune_undo_snapshots()
                total = len(payloads)
                send_event({"type": "plan", "total": total,
                            "updates": sum(1 for p in payloads if p["update"]),
                            "creates": sum(1 for p in payloads if not p["update"]),
                            "snapshot": snapshot_name,
                            "skipped": skipped})
                for index, payload in enumerate(payloads, start=1):
                    if control["cancel"].is_set():
                        send_event({"type": "cancelled", "written": updated + created,
                                    "snapshot": snapshot_name})
                        break
                    outcome = anilist_write_entry(token, payload)
                    if outcome.get("ok"):
                        if payload["update"]:
                            updated += 1
                        else:
                            created += 1
                            # A created entry's id exists only in this response,
                            # and it is the only handle an undo has on the row.
                            if snapshot and snapshot_name:
                                record_created_entry(snapshot, payload["anilist_id"], outcome["id"])
                                save_undo_snapshot(snapshot, snapshot_name)
                    else:
                        failed.append({"title": payload["title"], "error": outcome.get("error", "")})
                        failed_payloads.append(payload)
                    send_event({
                        "type": "write_progress",
                        "current": index,
                        "total": total,
                        "title": payload["title"],
                        "ok": bool(outcome.get("ok")),
                        "percent": round((index / total) * 100, 1) if total else 100.0,
                        "interval": current_interval(),
                    })
                else:
                    send_event({
                        "type": "write_complete",
                        "run_id": run_id,
                        "updated": updated,
                        "created": created,
                        "failed": failed,
                        "failed_count": len(failed),
                        "snapshot": snapshot_name,
                        "skipped": skipped,
                    })
            finally:
                token = ""
                _remember_failures(run_id, failed_payloads, snapshot_name)
                _unregister_run(run_id)
                _run_lock.release()
            self.close_connection = True
            return

        elif path == "/api/anilist-undo":
            # Undo is itself a write, so it needs the token re-supplied: the
            # server cleared the one the original run used.
            token = str(data.get("token") or "").strip()
            if not token:
                self.send_json({"ok": False, "error": "Paste an AniList access token first."}, status=400)
                return
            if not data.get("confirm"):
                self.send_json({"ok": False, "error": "Undo writes to your AniList account "
                                                      "and needs confirmation."}, status=400)
                return
            snapshot = load_undo_snapshot(data.get("snapshot"))
            if not snapshot:
                self.send_json({"ok": False, "error": "That undo snapshot could not be read."},
                               status=404)
                return
            delete_created = bool(data.get("delete_created"))
            restores, deletes, unrecoverable = iter_undo_payloads(snapshot, delete_created)
            if not restores and not deletes:
                self.send_json({"ok": False, "error": "Nothing in that snapshot can be undone.",
                                "unrecoverable": unrecoverable}, status=400)
                return
            if not _run_lock.acquire(blocking=False):
                self.send_json({"ok": False, "error": "A run is already in progress"}, status=409)
                return
            run_id, control = _register_run()
            viewer = anilist_viewer(token)
            if not viewer.get("ok"):
                _unregister_run(run_id)
                _run_lock.release()
                self.send_json(viewer, status=401)
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            def send_event(event_dict: Dict[str, Any]) -> None:
                payload = f"data: {json.dumps(event_dict, ensure_ascii=False)}\n\n".encode("utf-8")
                self.wfile.write(payload)
                self.wfile.flush()

            restored = 0
            removed = 0
            failed = []
            try:
                send_event({"type": "started", "run_id": run_id, "viewer": viewer.get("name", "")})
                steps = ([("restore", r) for r in restores]
                         + [("delete", d) for d in deletes])
                total = len(steps)
                send_event({"type": "plan", "total": total, "restores": len(restores),
                            "deletes": len(deletes), "unrecoverable": unrecoverable})
                for index, (kind, item) in enumerate(steps, start=1):
                    if control["cancel"].is_set():
                        send_event({"type": "cancelled", "written": restored + removed})
                        break
                    if kind == "restore":
                        outcome = anilist_write_entry(token, item)
                    else:
                        outcome = anilist_delete_entry(token, item["entry_id"])
                    if outcome.get("ok"):
                        if kind != "restore":
                            removed += 1
                        elif not item.get("repair"):
                            restored += 1
                    else:
                        # A stale snapshot - the entry was deleted on AniList
                        # since the write - fails this row and no other.
                        failed.append({"title": item["title"], "error": outcome.get("error", "")})
                    send_event({
                        "type": "write_progress",
                        "current": index,
                        "total": total,
                        "title": item["title"],
                        "repair": item.get("repair") or "",
                        "ok": bool(outcome.get("ok")),
                        "percent": round((index / total) * 100, 1) if total else 100.0,
                        "interval": current_interval(),
                    })
                else:
                    send_event({"type": "undo_complete", "restored": restored,
                                "deleted": removed, "failed": failed,
                                "failed_count": len(failed),
                                "unrecoverable": unrecoverable})
            finally:
                token = ""
                _unregister_run(run_id)
                _run_lock.release()
            self.close_connection = True
            return

        elif path == "/api/resolve-one":
            # One entry point for fixing a row: a title, a bare id, or a pasted
            # AniList / MyAnimeList link.
            media_type = "manga" if data.get("media_type") == "manga" else "anime"
            query = str(data.get("query", "")).strip()
            if not query:
                self.send_json({"ok": False, "error": "Type a title, an id, or paste a link."}, status=400)
                return
            id_kind = data.get("id_kind")
            try:
                result = resolve_one(query, media_type,
                                     id_kind if id_kind in ("anilist", "mal") else None)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=502)
                return
            self.send_json(result, status=200 if result.get("ok") else 404)
            return

        elif path == "/api/parse-mal-xml":
            raw = data.get("xml", "")
            if len(raw.encode("utf-8", "ignore")) > MAX_IMPORT_BYTES:
                self.send_json({"ok": False, "error": "That file is too large to import."}, status=413)
                return
            try:
                self.send_json(parse_mal_xml(raw))
            except ValueError as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return

        elif path == "/api/search":
            media_type = "manga" if data.get("media_type") == "manga" else "anime"
            query = str(data.get("query", "")).strip()
            if not query:
                self.send_json({"ok": False, "error": "query is required"}, status=400)
                return
            try:
                entries = search_candidates(query, media_type)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=502)
                return
            self.send_json({"ok": True, "entries": entries})
            return

        elif path == "/api/lookup":
            media_type = "manga" if data.get("media_type") == "manga" else "anime"
            raw_id = str(data.get("mal_id", "")).strip()
            if not raw_id.isdigit() or int(raw_id) <= 0:
                self.send_json({"ok": False, "error": "mal_id must be a positive whole number"}, status=400)
                return
            try:
                entry = lookup_by_mal_id(raw_id, media_type)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=502)
                return
            if not entry or int(entry.get("id") or 0) <= 0:
                self.send_json({"ok": False, "error": f"No {media_type} with MAL ID {raw_id}"}, status=404)
                return
            self.send_json({"ok": True, "entry": entry})
            return

        elif path == "/api/update-export":
            media_type = "manga" if data.get("media_type") == "manga" else "anime"
            EXPORT_MEDIA_TYPE = media_type
            entries = data.get("entries", [])
            default_status = data.get("status", "Completed")
            default_score = int(data.get("score", 0))

            xml_path, gz_path = _export_paths(media_type)

            if not entries:
                # Deliberately keep whatever is on disk. Deleting here meant that
                # merely switching media type silently destroyed a finished export.
                self.send_json({"ok": True, "count": 0, "kept_existing": xml_path.is_file()})
                return

            # Two rows can end up on the same entry (for instance after fixing two
            # unmatched titles to the same series). /api/process dedupes; this
            # path did not, and wrote the same id twice into one file.
            entries = deduplicate_entries(entries)
            try:
                xml_str = (
                    build_mal_manga_xml(entries, default_status=default_status, default_score=default_score)
                    if media_type == "manga"
                    else build_mal_xml(entries, default_status=default_status, default_score=default_score)
                )
            except ValueError as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
                return
            xml_bytes = xml_str.encode("utf-8")

            xml_path.write_bytes(xml_bytes)
            gz_bytes = gzip.compress(xml_bytes, compresslevel=9)
            with open(gz_path, "wb") as f_out:
                f_out.write(gz_bytes)
            if data.get("archive"):
                _write_archive(media_type, xml_bytes, gz_bytes)

            self.send_json({"ok": True, "count": len(entries)})
            return

        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")


def find_free_port(preferred_port: int = 5000) -> int:
    """Finds an open port starting from preferred_port."""
    for port in range(preferred_port, preferred_port + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred_port


def start_server(port: int = 5000, open_browser: bool = True) -> None:
    """Starts the local GUI web server and launches the browser."""
    actual_port = find_free_port(port)
    server_address = ("127.0.0.1", actual_port)
    httpd = ThreadingHTTPServer(server_address, MalGuiHandler)
    url = f"http://127.0.0.1:{actual_port}"

    print(f"\n==================================================")
    print(f"  AniExport - Local Server Running")
    print(f"==================================================")
    print(f"  URL: {url}")
    print(f"  Directory: {BASE_DIR}")
    print(f"  Press Ctrl+C in this console to stop.\n")

    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping local server...")
        httpd.server_close()
        print("Server stopped successfully.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Launch the local web GUI for AniExport."
    )
    parser.add_argument(
        "-p", "--port",
        type=int,
        default=5000,
        help="Local port for the web server (default: 5000).",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not automatically open the web browser on startup.",
    )

    args = parser.parse_args()
    start_server(port=args.port, open_browser=not args.no_browser)

if __name__ == "__main__":
    main()
