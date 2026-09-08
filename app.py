"""Part Portal — shareable web page with Search PN / Download BOM / Download PDF.

Designed to be embeddable (iframe or direct REST calls) into other internal
applications. Endpoints are plain GET + JSON/file responses with permissive
CORS for read-only lookups against Teamcenter.

Run:
    python app.py
"""
from __future__ import annotations

import io
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

from tc_client import (
    TCClient,
    bom_to_xlsx_bytes,
    fetch_bom,
    find_pdf_dataset,
    get_pdf_bytes,
    search_part,
)

PORT = int(os.environ.get("PART_PORTAL_PORT", "5057"))


def _resource_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return Path(__file__).parent


app = Flask(
    __name__,
    template_folder=str(_resource_dir() / "templates"),
    static_folder=str(_resource_dir() / "static"),
)

# Writable dir for cached SSO cookies / playwright profile (not under a
# read-only frozen bundle).
def _writable_dir() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PartPortal"
        base.mkdir(parents=True, exist_ok=True)
        return base
    return Path(__file__).parent


_client: TCClient | None = None
_client_lock = threading.Lock()
_cdv_client: TCClient | None = None
_cdv_client_lock = threading.Lock()

# Short-lived cache of search_part() results so clicking both Download BOM
# and Download PDF for the same part number only searches Teamcenter once.
_search_cache: dict[str, tuple[float, dict]] = {}
_search_cache_lock = threading.Lock()
_SEARCH_CACHE_TTL = 300  # seconds


def save_and_open_pdf(content: bytes, filename: str) -> str:
    downloads_dir = Path.home() / "Downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    target = downloads_dir / Path(filename).name
    stem = target.stem
    suffix = target.suffix or ".pdf"
    counter = 1
    while target.exists():
        target = downloads_dir / f"{stem}_{counter}{suffix}"
        counter += 1
    target.write_bytes(content)
    if sys.platform == "win32":
        os.startfile(str(target))
    return target.name


def cached_search_part(client: TCClient, pn: str) -> dict:
    with _search_cache_lock:
        hit = _search_cache.get(pn)
        if hit and (time.monotonic() - hit[0]) < _SEARCH_CACHE_TTL:
            return hit[1]
    resolved = search_part(client, pn)
    with _search_cache_lock:
        _search_cache[pn] = (time.monotonic(), resolved)
    return resolved


def get_client() -> TCClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = TCClient(workdir=_writable_dir())
            _client.connect()
        return _client


def get_cdv_client() -> TCClient:
    global _cdv_client
    with _cdv_client_lock:
        if _cdv_client is None:
            _cdv_client = TCClient(workdir=_writable_dir())
        return _cdv_client


def warm_teamcenter_client() -> None:
    try:
        get_client()
    except Exception:
        pass


@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search")
def api_search():
    pn = (request.args.get("pn") or "").strip().upper()
    if not pn:
        return jsonify(ok=False, error="Part number is required"), 400
    try:
        client = get_client()
        resolved = cached_search_part(client, pn)
        return jsonify(ok=True, part_number=pn, **resolved)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 404


@app.route("/api/download/bom")
def api_download_bom():
    pn = (request.args.get("pn") or "").strip().upper()
    rev_uid = request.args.get("uid") or None
    if not pn:
        return jsonify(ok=False, error="Part number is required"), 400
    try:
        client = get_client()
        if not rev_uid:
            rev_uid = cached_search_part(client, pn)["item_rev_uid"]
        rows = fetch_bom(client, rev_uid)
        if not rows:
            return jsonify(ok=False, error="No BOM rows found for this part"), 404
        xlsx_bytes = bom_to_xlsx_bytes(rows)
        return send_file(
            io.BytesIO(xlsx_bytes),
            as_attachment=True,
            download_name=f"{pn}_BOM.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/download/pdf")
def api_download_pdf():
    pn = (request.args.get("pn") or "").strip().upper()
    rev_uid = request.args.get("uid") or None
    rev_type = request.args.get("type") or None
    if not pn:
        return jsonify(ok=False, error="Part number is required"), 400
    try:
        client = get_client()
        if not rev_uid or not rev_type:
            resolved = cached_search_part(client, pn)
            rev_uid = rev_uid or resolved["item_rev_uid"]
            rev_type = rev_type or resolved["item_rev_type"]
        dataset_info = find_pdf_dataset(client, rev_uid, rev_type)
        if not dataset_info:
            return jsonify(ok=False, error="No PDF attachment found for this part"), 404
        pdf_bytes, fname = get_pdf_bytes(client, dataset_info)
        if not fname.lower().endswith(".pdf"):
            fname += ".pdf"
        return send_file(
            io.BytesIO(pdf_bytes),
            as_attachment=True,
            download_name=fname,
            mimetype="application/pdf",
        )
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/download/km-pdf")
def api_download_km_pdf():
    pn = (request.args.get("pn") or "").strip().upper()
    if not pn:
        return jsonify(ok=False, error="Part number is required"), 400
    try:
        content, filename = get_cdv_client().km_download(pn)
        saved_filename = save_and_open_pdf(content, filename)
        return jsonify(ok=True, filename=saved_filename, opened=True)
    except Exception as e:
        status = 401 if "KM sign-in" in str(e) else 500
        return jsonify(ok=False, error=str(e)), status


@app.route("/api/download/cdv-pdf")
def api_download_cdv_pdf():
    pn = (request.args.get("pn") or "").strip().upper()
    revision = (request.args.get("revision") or "").strip().upper() or None
    if not pn:
        return jsonify(ok=False, error="Part number is required"), 400
    try:
        content, filename = get_cdv_client().cdv_download(pn, revision)
        saved_filename = save_and_open_pdf(content, filename)
        return jsonify(ok=True, filename=saved_filename, opened=True)
    except Exception as e:
        status = 401 if "CDV sign-in" in str(e) else 500
        return jsonify(ok=False, error=str(e)), status


if __name__ == "__main__":
    threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    threading.Thread(target=warm_teamcenter_client, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, debug=False)
