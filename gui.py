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
import re
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
    AniListReadError,
    anilist_delete_entry,
    anilist_existing_entries,
    anilist_list_collection,
    anilist_viewer,
    anilist_write_batch,
    backfill_anilist_ids,
    build_undo_snapshot,
    REVIEW_RATIO_FLOOR,
    DIFF_FIELDS,
    iter_undo_payloads,
    iter_write_payloads,
    list_undo_snapshots,
    load_undo_snapshot,
    prune_undo_snapshots,
    record_created_entry,
    save_undo_snapshot,
    WRITE_BATCH_SIZE,
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
    parse_title_line,
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


# Every spelling of "this machine" a browser may put in an Origin header.
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

MAX_PREVIEW_BYTES = 5 * 1024 * 1024
ARCHIVE_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.")


# A copy touches both media types, so every stage is described the same way
# and the caller only varies what it does with the payloads.
COPY_MEDIA_TYPES = ("anime", "manga")


def _copy_stage(media_type: str, source_username: str, source_token: str,
                dest_token: str, dest_user_id: int, policy: str) -> Dict[str, Any]:
    """Read one media type from the source and plan the write to the destination.

    Returns {"ok": True, rows, existing, payloads, skipped} or {"ok": False, error}.
    The source is read with its own credentials - the destination's token says
    nothing about what the source account has made public.
    """
    found = anilist_list_collection(media_type, username=source_username,
                                    token=source_token, user_id=0)
    if not found.get("ok"):
        return {"ok": False, "error": found.get("error", "Could not read the source list.")}
    rows = found["entries"]
    if not rows:
        return {"ok": True, "rows": [], "existing": {}, "payloads": [], "skipped": []}
    try:
        existing = anilist_existing_entries(
            dest_token, dest_user_id, media_type, [r.get("anilist_id") for r in rows])
    except AniListReadError as exc:
        return {"ok": False, "error": _read_failure_message(exc)}
    payloads, skipped = iter_write_payloads(rows, media_type, existing, policy=policy)
    return {"ok": True, "rows": rows, "existing": existing,
            "payloads": payloads, "skipped": skipped}


def _copy_request(data: Dict[str, Any]) -> tuple:
    """Pull the three identities a copy needs out of a request body."""
    return (str(data.get("source_username") or "").strip(),
            str(data.get("source_token") or "").strip(),
            str(data.get("token") or "").strip())


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

# Checked before the body is read rather than after it is parsed. It has to
# clear MAX_IMPORT_BYTES, because /api/process legitimately carries a large
# paste and /api/parse-mal-xml a whole MyAnimeList export.
MAX_BODY_BYTES = MAX_IMPORT_BYTES + 5 * 1024 * 1024


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


def _page_html() -> str:
    """The page with the server's own tuning constants substituted in.

    The review floor is calibrated from real data and lives in
    generate_mal_xml; copying the number into the template by hand would drift
    the first time it is retuned.

    Re-read from static/ on every request, so editing the CSS or the script
    shows up on a refresh instead of needing the server restarted. If static/
    has gone missing, the copy built at import still serves.
    """
    try:
        page = _build_page()
    except OSError:
        page = HTML_TEMPLATE
    return page.replace("__REVIEW_RATIO_FLOOR__", repr(float(REVIEW_RATIO_FLOOR)))


def parse_mal_xml(raw: str) -> Dict[str, Any]:
    """Read a MyAnimeList export back into the rows this app works with.

    Returns {ok, media_type, entries, skipped}. Raises ValueError with a
    user-facing message for anything that is not a MAL export.
    """
    # ElementTree expands internal entities, so a DOCTYPE is the doorway to
    # an entity-expansion blow-up. The scan used to cover the first 4KB only,
    # which a long comment prolog walks straight past; an entity cannot be
    # declared without a DOCTYPE, so covering the whole document is enough.
    # re.search beats raw.upper() here - it does not copy the string.
    if re.search(r"<!\s*DOCTYPE", raw, re.IGNORECASE):
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

STATIC_DIR = BASE_DIR / "static"

# index.html refers to the stylesheet and the script as ordinary files, so it
# opens in a browser on its own and an editor treats each as what it is. The
# server inlines both into a single response: one request, one no-store header,
# and no window in which the markup has arrived but the script has not.
#
# This used to be a 5,127-line string literal in this file. Because a Python
# string consumes backslashes, the JavaScript inside it could not contain one -
# no "\n", no regex literals - which is why there are String.fromCharCode(10)
# calls in the script. Those are now only history, not a constraint.
_CSS_LINK = '<link rel="stylesheet" href="app.css">'
_JS_TAG = '<script src="app.js"></script>'

# The script is kept as numbered sections under static/app/ so it can be read a
# few hundred lines at a time instead of three thousand. They are joined in
# name order, which is why the names are zero-padded: the join has to be
# deterministic, and adding a section must not be able to reorder the others.
#
# Sections rather than ES modules, deliberately. The script has no wrapper
# function, so every cut lands at a top-level statement and the pieces rejoin
# byte for byte - which is what makes the split unable to change behaviour.
# Real modules would mean moving the mutable top-level state behind accessors:
# `currentAnimes` alone is read 79 times and reassigned in several, and an
# imported binding cannot be reassigned. That is a change to the write path,
# not a move, and it belongs in its own piece of work.
SCRIPT_DIR = STATIC_DIR / "app"


def _script_source() -> str:
    parts = sorted(SCRIPT_DIR.glob("*.js"), key=lambda p: p.name)
    if not parts:
        raise OSError("no script sections found in %s" % SCRIPT_DIR)
    return "\n".join(part.read_text(encoding="utf-8") for part in parts)


def _build_page() -> str:
    """Assemble the page from static/, byte for byte as the literal was."""
    def read(name: str) -> str:
        return (STATIC_DIR / name).read_text(encoding="utf-8")

    return (read("index.html")
            .replace(_CSS_LINK, "<style>" + read("app.css") + "</style>", 1)
            .replace(_JS_TAG, "<script>" + _script_source() + "</script>", 1))


# Built once at import so the module always holds a page it can serve, and so
# anything reading gui.HTML_TEMPLATE still sees the whole document.
HTML_TEMPLATE = _build_page()


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
            body = _page_html().encode("utf-8")
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

    def _same_origin(self) -> bool:
        """Refuse a POST that another website sent on the user's behalf.

        The server is local and has no login, so any page the user happens to
        have open can POST here. It cannot write to AniList - the token lives
        in this page and is never stored - but it can spend the AniList rate
        limit through /api/process and overwrite the export through
        /api/update-export. A JSON body with Content-Type: text/plain is a
        "simple request", so there is no preflight to stop it either.

        An absent Origin is allowed: curl and the test harness send none, and a
        browser that omits it is not acting for a third-party page.
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            sent = urllib.parse.urlparse(origin)
        except ValueError:
            return False
        if sent.scheme not in ("http", "https"):
            return False
        # The app opens 127.0.0.1, but people type localhost - and that is a
        # different origin to a browser, so both loopback spellings have to
        # pass or the page breaks for anyone who types the other one.
        port = sent.port or (443 if sent.scheme == "https" else 80)
        return (str(sent.hostname or "").lower() in LOOPBACK_HOSTS
                and port == self.server.server_address[1])

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if not self._same_origin():
            self.send_json({"ok": False,
                            "error": "This request came from another website and was refused."},
                           status=403)
            return

        # Bounded before the read: the size checks further down run on an
        # already-parsed body, so the whole thing had to be pulled into memory
        # first. A malformed header raised ValueError and became a 500.
        try:
            content_length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            content_length = -1
        if content_length < 0 or content_length > MAX_BODY_BYTES:
            self.send_json({"ok": False, "error": "That request body is too large."},
                           status=413)
            # The body was never read, so this connection cannot be reused.
            self.close_connection = True
            return
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
            # What the user actually typed, keyed by what we searched for. The
            # row only ever carried the cleaned text, so when the clean-up got
            # it wrong the table said "from gatsu no Lion" and there was no way
            # to see that the pasted line had said "3-gatsu no Lion".
            pasted_lines: Dict[str, str] = {}
            # Anything the line itself said about this title: a status, a score,
            # how far through it the user is. Keyed by the title that was left
            # once those were taken off, which is what gets searched.
            line_fields: Dict[str, Dict[str, Any]] = {}
            for line in raw_lines:
                parsed = parse_title_line(line, media_type)
                if not parsed:
                    continue
                title = parsed.pop("title")
                cleaned_titles.append(title)
                pasted_lines.setdefault(title, line.strip())
                if parsed:
                    line_fields.setdefault(title, parsed)

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
                try:
                    self.wfile.write(payload)
                    self.wfile.flush()
                except OSError:
                    # The tab is gone. resolve_many_entries deliberately
                    # swallows anything its progress callback raises, so
                    # without this the run carries on to the end against a
                    # dead socket and holds the run lock for the rest of it -
                    # long enough that reopening the page and pressing Process
                    # is met with "a run is already in progress". Writes are
                    # buffered, so a short run can finish before one fails:
                    # this is best effort, not a guarantee.
                    control["cancel"].set()

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
                # Only when the two differ: an untouched line would just be the
                # same string twice in every row and in the saved session.
                pasted = pasted_lines.get(raw_title, raw_title)
                if pasted != raw_title:
                    item["pasted"] = pasted
                item["status"] = default_status
                item["score"] = default_score
                # What the line said beats the list-wide defaults: it is the
                # only place the user has said anything about this one title.
                # The row stays editable either way, and `pasted` above shows
                # the line it came from, so a misread is visible rather than
                # silent.
                said = line_fields.get(raw_title) or {}
                if "status" in said:
                    item["status"] = said["status"]
                if "score" in said:
                    item["score"] = said["score"]
                if "progress" in said:
                    item["my_read_chapters" if media_type == "manga"
                         else "my_watched_episodes"] = said["progress"]
                if "volumes" in said and media_type == "manga":
                    item["my_read_volumes"] = said["volumes"]
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
                "from_lines": len(line_fields),
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
                # One request per batch, one event per row: the batch is a
                # transport detail and the progress bar should not jump 8 at a
                # time because of it.
                index = 0
                cancelled = False
                for start in range(0, total, WRITE_BATCH_SIZE):
                    if control["cancel"].is_set():
                        send_event({"type": "cancelled", "written": updated + created,
                                    "snapshot": snapshot_name})
                        cancelled = True
                        break
                    batch = payloads[start:start + WRITE_BATCH_SIZE]
                    outcomes = anilist_write_batch(token, batch)
                    for payload, outcome in zip(batch, outcomes):
                        index += 1
                        if outcome.get("ok"):
                            if payload["update"]:
                                updated += 1
                            else:
                                created += 1
                                # A created entry's id exists only in this
                                # response, and it is the only handle undo has.
                                if snapshot and snapshot_name:
                                    record_created_entry(snapshot, payload["anilist_id"],
                                                         outcome["id"])
                                    # Saved per create, not per batch: a crash
                                    # between a batch returning and the file
                                    # being written would otherwise strand up
                                    # to eight created entries with no id, and
                                    # undo cannot delete what it cannot name.
                                    # The file is small and the write is atomic.
                                    save_undo_snapshot(snapshot, snapshot_name)
                        else:
                            failed.append({"title": payload["title"],
                                           "error": outcome.get("error", "")})
                            failed_payloads.append(payload)
                        batches_left = max(0, (total - index + WRITE_BATCH_SIZE - 1) // WRITE_BATCH_SIZE)
                        send_event({
                            "type": "write_progress",
                            "current": index,
                            "total": total,
                            "title": payload["title"],
                            "ok": bool(outcome.get("ok")),
                            "percent": round((index / total) * 100, 1) if total else 100.0,
                            "interval": current_interval(),
                            "eta_seconds": round(batches_left * current_interval(), 1),
                            "batch_size": len(batch),
                        })
                if not cancelled:
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
                index = 0
                cancelled = False

                def report(item, outcome, left):
                    send_event({
                        "type": "write_progress",
                        "current": index,
                        "total": total,
                        "title": item["title"],
                        "repair": item.get("repair") or "",
                        "ok": bool(outcome.get("ok")),
                        "percent": round((index / total) * 100, 1) if total else 100.0,
                        "interval": current_interval(),
                        "eta_seconds": round(left * current_interval(), 1),
                    })

                # Restores are all SaveMediaListEntry, so they batch exactly
                # like the write does. Order is preserved inside a document and
                # across documents, which matters: a repeat repair has to land
                # after the restore it is repairing.
                for start in range(0, len(restores), WRITE_BATCH_SIZE):
                    if control["cancel"].is_set():
                        send_event({"type": "cancelled", "written": restored + removed})
                        cancelled = True
                        break
                    batch = restores[start:start + WRITE_BATCH_SIZE]
                    outcomes = anilist_write_batch(token, batch)
                    for item, outcome in zip(batch, outcomes):
                        index += 1
                        if outcome.get("ok"):
                            # A repair is a second write for a row already
                            # counted, so it must not count again.
                            if not item.get("repair"):
                                restored += 1
                        else:
                            # A stale snapshot - the entry was deleted on
                            # AniList since the write - fails this row alone.
                            failed.append({"title": item["title"],
                                           "error": outcome.get("error", "")})
                        done_rows = index
                        left = ((len(restores) - done_rows + WRITE_BATCH_SIZE - 1)
                                // WRITE_BATCH_SIZE) + len(deletes)
                        report(item, outcome, max(0, left))

                # Deletes are a different mutation, so they cannot share a
                # document with the restores and go one at a time.
                if not cancelled:
                    for position, item in enumerate(deletes):
                        if control["cancel"].is_set():
                            send_event({"type": "cancelled", "written": restored + removed})
                            cancelled = True
                            break
                        outcome = anilist_delete_entry(token, item["entry_id"])
                        index += 1
                        if outcome.get("ok"):
                            removed += 1
                        else:
                            failed.append({"title": item["title"],
                                           "error": outcome.get("error", "")})
                        report(item, outcome, len(deletes) - position - 1)

                if not cancelled:
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

        elif path == "/api/anilist-copy-preview":
            source_username, source_token, token = _copy_request(data)
            if not token:
                self.send_json({"ok": False,
                                "error": "Paste the destination account's token."}, status=400)
                return
            if not source_username and not source_token:
                self.send_json({"ok": False,
                                "error": "Name the account to copy from."}, status=400)
                return
            viewer = anilist_viewer(token)
            if not viewer.get("ok"):
                self.send_json(viewer, status=401)
                return
            policy = _write_policy(data)
            stages = []
            for media_type in COPY_MEDIA_TYPES:
                stage = _copy_stage(media_type, source_username, source_token,
                                    token, viewer["id"], policy)
                if not stage.get("ok"):
                    self.send_json({"ok": False, "error": stage["error"],
                                    "media_type": media_type}, status=502)
                    return
                payloads = stage["payloads"]
                # What the destination holds that the source does not. A copy
                # only ever adds and updates, so these stay behind and the
                # destination ends up a superset - say so rather than letting
                # "exact copy" imply they were removed.
                source_ids = {int(r.get("anilist_id") or 0) for r in stage["rows"]}
                destination = anilist_list_collection(media_type, token=token,
                                                      user_id=viewer["id"])
                extra = sorted(
                    {int(r.get("anilist_id") or 0) for r in destination.get("entries") or []}
                    - source_ids) if destination.get("ok") else []
                stages.append({
                    "media_type": media_type,
                    "extra_on_destination": len(extra),
                    "found": len(stage["rows"]),
                    "total": len(payloads),
                    "creates": sum(1 for p in payloads if not p["update"]),
                    "updates": sum(1 for p in payloads if p["update"]),
                    "unchanged": sum(1 for p in payloads if not p["diff"]),
                    "conflicts": sum(1 for p in payloads
                                     if any(d["blocked"] for d in p["diff"])),
                    "skipped": stage["skipped"],
                })
            self.send_json({"ok": True, "destination": viewer.get("name", ""),
                            "source": source_username or "(the source token's own account)",
                            "policy": policy, "stages": stages})
            return

        elif path == "/api/anilist-copy":
            source_username, source_token, token = _copy_request(data)
            if not token:
                self.send_json({"ok": False,
                                "error": "Paste the destination account's token."}, status=400)
                return
            if not source_username and not source_token:
                self.send_json({"ok": False, "error": "Name the account to copy from."}, status=400)
                return
            if not data.get("confirm"):
                self.send_json({"ok": False,
                                "error": "Copying writes to the destination account "
                                         "and needs confirmation."}, status=400)
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

            policy = _write_policy(data)
            summary: Dict[str, Any] = {}
            snapshots: List[str] = []
            cancelled = False
            try:
                send_event({"type": "started", "run_id": run_id,
                            "destination": viewer.get("name", ""), "policy": policy})
                for media_type in COPY_MEDIA_TYPES:
                    if cancelled:
                        break
                    send_event({"type": "stage_started", "media_type": media_type})
                    stage = _copy_stage(media_type, source_username, source_token,
                                        token, viewer["id"], policy)
                    if not stage.get("ok"):
                        send_event({"type": "error", "media_type": media_type,
                                    "error": stage["error"]})
                        self.close_connection = True
                        return
                    payloads = stage["payloads"]
                    total = len(payloads)
                    # One snapshot per media type: the format records a single
                    # media type, and an undo has to know which it is.
                    snapshot = build_undo_snapshot(media_type, viewer.get("id", 0),
                                                   payloads, stage["existing"])
                    snapshot_name = save_undo_snapshot(snapshot) if payloads else None
                    if snapshot_name:
                        prune_undo_snapshots()
                        snapshots.append(snapshot_name)
                    send_event({"type": "stage", "media_type": media_type,
                                "found": len(stage["rows"]), "total": total,
                                "creates": sum(1 for p in payloads if not p["update"]),
                                "updates": sum(1 for p in payloads if p["update"]),
                                "skipped": stage["skipped"], "snapshot": snapshot_name})
                    created = updated = 0
                    failed: List[Dict[str, str]] = []
                    index = 0
                    for start in range(0, total, WRITE_BATCH_SIZE):
                        if control["cancel"].is_set():
                            send_event({"type": "cancelled",
                                        "written": created + updated,
                                        "media_type": media_type})
                            cancelled = True
                            break
                        batch = payloads[start:start + WRITE_BATCH_SIZE]
                        for payload, outcome in zip(batch, anilist_write_batch(token, batch)):
                            index += 1
                            if outcome.get("ok"):
                                if payload["update"]:
                                    updated += 1
                                else:
                                    created += 1
                                    if snapshot and snapshot_name:
                                        record_created_entry(snapshot, payload["anilist_id"],
                                                             outcome["id"])
                                        save_undo_snapshot(snapshot, snapshot_name)
                            else:
                                failed.append({"title": payload["title"],
                                               "error": outcome.get("error", "")})
                            left = (total - index + WRITE_BATCH_SIZE - 1) // WRITE_BATCH_SIZE
                            send_event({
                                "type": "write_progress",
                                "media_type": media_type,
                                "current": index, "total": total,
                                "title": payload["title"],
                                "ok": bool(outcome.get("ok")),
                                "percent": round((index / total) * 100, 1) if total else 100.0,
                                "interval": current_interval(),
                                "eta_seconds": round(max(0, left) * current_interval(), 1),
                            })
                    summary[media_type] = {"found": len(stage["rows"]), "total": total,
                                           "created": created, "updated": updated,
                                           "failed": failed, "failed_count": len(failed),
                                           "skipped": stage["skipped"],
                                           "snapshot": snapshot_name}
                if not cancelled:
                    send_event({"type": "copy_complete", "run_id": run_id,
                                "destination": viewer.get("name", ""),
                                "snapshots": snapshots, "stages": summary})
            finally:
                token = ""
                source_token = ""
                _unregister_run(run_id)
                _run_lock.release()
            self.close_connection = True
            return

        elif path == "/api/anilist-import":
            # Read-only, so a public list needs no token at all. A token is
            # accepted because it is the only way to read a private list.
            media_type = "manga" if data.get("media_type") == "manga" else "anime"
            username = str(data.get("username") or "").strip()
            token = str(data.get("token") or "").strip()
            if not username and not token:
                self.send_json({"ok": False,
                                "error": "Enter an AniList username, or check a token to read "
                                         "your own list."}, status=400)
                return
            user_id = 0
            if token and not username:
                viewer = anilist_viewer(token)
                if not viewer.get("ok"):
                    self.send_json(viewer, status=401)
                    return
                user_id = viewer["id"]
            # An import landing rows into currentAnimes while a write iterates
            # them is a real race, so it takes the same lock the write does.
            if not _run_lock.acquire(blocking=False):
                self.send_json({"ok": False, "error": "A run is already in progress"}, status=409)
                return
            try:
                # The token travels even when a username is given: that is
                # the only way to read your own private list by name, and
                # visibility is decided by who the request authenticates as.
                result = anilist_list_collection(
                    media_type, username=username, token=token, user_id=user_id)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=502)
                return
            finally:
                _run_lock.release()
            if not result.get("ok"):
                self.send_json(result, status=502)
                return
            rows = result["entries"]
            # The same shape /api/parse-mal-xml returns, so the page reuses one
            # merge rather than growing a second.
            self.send_json({
                "ok": True,
                "media_type": result["media_type"],
                "entries": rows,
                "skipped": result.get("skipped", 0),
                "source": "anilist",
                "anilist_only": sum(1 for r in rows if not r.get("can_export_xml")),
            })
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
