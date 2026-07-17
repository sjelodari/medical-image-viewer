#!/usr/bin/env python3
"""Web backend for the Medical Image Viewer.

Run:  python app.py [optional/path/to/scan]
Then open http://127.0.0.1:5001 (the launcher opens it for you).
"""

import os
import sys
import time
import platform
import subprocess
import threading
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, request, Response, render_template

import viewer_core as core

app = Flask(__name__)

STATE = {
    "root": None,
    "series": [],       # list of series info dicts
    "patient": None,
    "volumes": {},      # index -> (vol, zooms, (level, width))
}


def _load_volume(i):
    if i in STATE["volumes"]:
        return STATE["volumes"][i]
    s = STATE["series"][i]
    vol, zooms = core.load(s["path"])
    level, width = core.default_window(vol)
    STATE["volumes"][i] = (vol, zooms, (level, width))
    return STATE["volumes"][i]


def _open(path):
    path = Path(path).expanduser()
    if not path.exists():
        return {"ok": False, "error": f"Path not found: {path}"}
    series = core.find_series(path)
    if not series:
        return {"ok": False, "error": f"No MRI series under {path}"}
    STATE.update(root=str(path), series=series,
                 patient=core.patient_info(path), volumes={})
    return {
        "ok": True,
        "root": str(path),
        "patient": STATE["patient"],
        "series": [
            {"index": i, "desc": s["desc"], "modality": s["modality"],
             "count": s["count"], "is_image": s["is_image"]}
            for i, s in enumerate(series)],
    }


# ---------------------------------------------------------------- routes

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    """Current study (e.g. one passed on the command line), if any."""
    if not STATE["series"]:
        return jsonify({"ok": False})
    return jsonify({
        "ok": True, "root": STATE["root"], "patient": STATE["patient"],
        "series": [
            {"index": i, "desc": s["desc"], "modality": s["modality"],
             "count": s["count"], "is_image": s["is_image"]}
            for i, s in enumerate(STATE["series"])],
    })


@app.route("/api/open")
def api_open():
    return jsonify(_open(request.args.get("path", "")))


def _native_pick(kind):
    """Show a native folder/file picker. Works on macOS, Windows, Linux."""
    system = platform.system()
    prompt = "Select an MRI folder" if kind == "folder" else "Select an MRI file"
    try:
        if system == "Darwin":
            verb = "choose folder" if kind == "folder" else "choose file"
            out = subprocess.run(
                ["osascript", "-e",
                 f'POSIX path of ({verb} with prompt "{prompt}")'],
                capture_output=True, text=True)
            return out.stdout.strip()
        if system == "Windows":
            if kind == "folder":
                ps = ("Add-Type -AssemblyName System.Windows.Forms;"
                      "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
                      f"$d.Description='{prompt}';"
                      "if($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]"
                      "::OK){[Console]::Out.Write($d.SelectedPath)}")
            else:
                ps = ("Add-Type -AssemblyName System.Windows.Forms;"
                      "$d=New-Object System.Windows.Forms.OpenFileDialog;"
                      f"$d.Title='{prompt}';"
                      "if($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]"
                      "::OK){[Console]::Out.Write($d.FileName)}")
            out = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-Command", ps],
                capture_output=True, text=True)
            return out.stdout.strip()
        # Linux: try zenity, then kdialog
        for cmd in (["zenity", "--file-selection"]
                    + (["--directory"] if kind == "folder" else []),
                    ["kdialog"]
                    + (["--getexistingdirectory", str(Path.home())]
                       if kind == "folder"
                       else ["--getopenfilename", str(Path.home())])):
            try:
                out = subprocess.run(cmd, capture_output=True, text=True)
                if out.returncode == 0:
                    return out.stdout.strip()
            except FileNotFoundError:
                continue
        return ""
    except Exception:
        return ""


@app.route("/api/browse")
def api_browse():
    path = _native_pick(request.args.get("type", "folder"))
    if not path:
        return jsonify({"ok": False, "cancelled": True})
    return jsonify(_open(path))


@app.route("/api/quit", methods=["GET", "POST"])
def api_quit():
    """Stop the server so non-technical users can close it from the browser."""
    def _die():
        time.sleep(0.4)
        os._exit(0)
    threading.Thread(target=_die, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/series/<int:i>/meta")
def api_meta(i):
    if i < 0 or i >= len(STATE["series"]):
        return jsonify({"ok": False, "error": "bad series"}), 404
    vol, zooms, (level, width) = _load_volume(i)
    return jsonify({
        "ok": True,
        "index": i,
        "desc": STATE["series"][i]["desc"],
        "planes": {p: core.n_slices(vol, p) for p in core.PLANES},
        "aspect": {p: core.aspect(zooms, p) for p in core.PLANES},
        "level": level, "width": width,
        "range": [float(vol.min()), float(vol.max())],
    })


@app.route("/api/series/<int:i>/slice")
def api_slice(i):
    if i < 0 or i >= len(STATE["series"]):
        return Response(status=404)
    vol, zooms, (level0, width0) = _load_volume(i)
    plane = request.args.get("plane", "axial")
    n = core.n_slices(vol, plane)
    index = max(0, min(int(request.args.get("index", n // 2)), n - 1))
    filt = request.args.get("filter", "none")
    level = request.args.get("level", type=float)
    width = request.args.get("width", type=float)
    if level is None:
        level = level0
    if width is None:
        width = width0
    scale = request.args.get("scale", default=1, type=int)
    png = core.render_png(vol, zooms, plane, index, filt=filt,
                          level=level, width=width, downscale=max(scale, 1))
    return Response(png, mimetype="image/png",
                    headers={"Cache-Control": "no-store"})


def open_browser(port):
    webbrowser.open(f"http://127.0.0.1:{port}")


def main():
    port = 5001
    if len(sys.argv) > 1:
        result = _open(sys.argv[1])
        if not result["ok"]:
            print(result["error"])
    print(f"Medical Image Viewer running at http://127.0.0.1:{port}  "
          f"(Ctrl+C to stop)")
    if not os.environ.get("MRI_NO_BROWSER"):
        threading.Timer(1.0, open_browser, args=[port]).start()
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
