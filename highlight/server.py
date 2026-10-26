"""Servidor local del panel de highlights.

    python -m highlight.server            (puerto 8790 por defecto)

Todo corre en la PC: lee los videos del disco, analiza en una cola (uno
detras de otro) y exporta los clips aprobados a una carpeta.
"""
import json
import os
import queue
import re
import shutil
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .analyze import analyze
from .render import adjusted_window, cut, safe_name
from .scorebug import list_profiles

APP_DIR = Path(__file__).resolve().parent
REPO = APP_DIR.parent
STATIC = APP_DIR / "static"
CONFIG_PATH = REPO / "config.json"
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm", ".ts", ".flv"}

DEFAULT_CONFIG = {
    "video_folders": [str(Path.home() / "Videos" / "Highlight Videos")],
    "data_dir": str(REPO / "datos"),
    "export_dir": "",
    "default_profile": "auto",
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def runs_dir():
    p = Path(load_config()["data_dir"]) / "analisis"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------- videos

def scan_videos():
    cfg = load_config()
    seen, out = set(), []
    data_dir = Path(cfg["data_dir"]).resolve()
    for folder in cfg["video_folders"]:
        base = Path(folder).expanduser()
        if not base.exists():
            continue
        for path in base.iterdir():  # solo esa carpeta, sin subcarpetas
            try:
                if not path.is_file() or path.suffix.lower() not in VIDEO_EXTS:
                    continue
                rp = path.resolve()
                if rp in seen or data_dir in rp.parents:
                    continue
                st = path.stat()
                if st.st_size < 50 * 1024 * 1024:  # clips chicos no son partidos
                    continue
                seen.add(rp)
                recording = (time.time() - st.st_mtime) < 20
                out.append({"name": path.name, "path": str(rp), "sizeMb": round(st.st_size / 1048576),
                            "modified": st.st_mtime, "recording": recording})
            except OSError:
                continue
    out.sort(key=lambda v: v["modified"], reverse=True)
    analyzed = {}
    for run in scan_runs():
        analyzed.setdefault(run["video"], run["runId"])
    for v in out:
        v["runId"] = analyzed.get(v["path"])
    return out


def scan_runs():
    out = []
    for mp in runs_dir().glob("*/manifest.json"):
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        dec = load_decisions(m["runId"])
        out.append({"runId": m["runId"], "videoName": m.get("videoName"), "video": m.get("video"),
                    "clips": len(m.get("clips", [])), "created": m.get("created", mp.stat().st_mtime),
                    "finalScore": m.get("stats", {}).get("finalScore", ""),
                    "approved": sum(1 for d in dec.values() if d.get("decision") == "approved"),
                    "reviewed": sum(1 for d in dec.values() if d.get("decision"))})
    out.sort(key=lambda r: r["created"], reverse=True)
    return out


def run_path(run_id):
    # el id ya es seguro (lo genera el panel); solo se valida, sin recortarlo
    if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id or ""):
        raise ValueError("runId invalido")
    p = (runs_dir() / run_id).resolve()
    if runs_dir().resolve() not in p.parents:
        raise ValueError("runId invalido")
    return p


def load_run(run_id):
    p = run_path(run_id) / "manifest.json"
    m = json.loads(p.read_text(encoding="utf-8"))
    m["decisions"] = load_decisions(run_id)
    return m


def load_decisions(run_id):
    p = run_path(run_id) / "decisiones.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_decisions(run_id, decisions):
    p = run_path(run_id) / "decisiones.json"
    p.write_text(json.dumps(decisions, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True}


def delete_run(run_id):
    p = run_path(run_id)
    if p.exists():
        shutil.rmtree(p)
    return {"ok": True}


# ---------------------------------------------------------------- cola

JOBS = []
JOB_Q = queue.Queue()
JOB_LOCK = threading.Lock()


def enqueue(path, profile, reuse_run=None):
    path = str(Path(path).resolve())
    with JOB_LOCK:
        for j in JOBS:
            if j["path"] == path and j["status"] in ("en cola", "analizando"):
                return j
        job = {"id": f"j{int(time.time() * 1000)}", "path": path, "name": Path(path).name, "profile": profile,
               "status": "en cola", "progress": 0.0, "message": "", "runId": None, "error": None,
               "queued": time.time(), "reuse": reuse_run}
        JOBS.append(job)
    JOB_Q.put(job)
    return job


def worker():
    while True:
        job = JOB_Q.get()
        job["status"] = "analizando"
        job["started"] = time.time()

        def prog(stage, frac, msg):
            job["progress"] = frac
            if msg:
                job["message"] = msg

        try:
            reuse = None
            if job.get("reuse"):
                reuse = run_path(job["reuse"]) / "trabajo"
            m = analyze(job["path"], runs_dir(), job["profile"], on_progress=prog, reuse_work=reuse)
            if job.get("reuse"):
                # conservar las decisiones del analisis anterior cuando coinciden los clips
                old = load_decisions(job["reuse"])
                if old:
                    old_clips = {c["id"]: c for c in load_run(job["reuse"])["clips"]}
                    moved = {}
                    for oid, d in old.items():
                        oc = old_clips.get(oid)
                        if not oc:
                            continue
                        for c in m["clips"]:
                            if abs(c["scorebugTime"] - oc["scorebugTime"]) < 3 and c["half"] == oc["half"]:
                                moved[c["id"]] = d
                                break
                    save_decisions(m["runId"], moved)
            job["runId"] = m["runId"]
            job["status"] = "listo"
            job["progress"] = 1.0
            job["message"] = f"{m['stats']['clips']} jugadas en {m['stats']['halves']} medias entradas"
        except Exception as exc:
            job["status"] = "error"
            job["error"] = str(exc)
            traceback.print_exc()
        job["finished"] = time.time()


# ---------------------------------------------------------------- exportar

def export_approved(run_id, folder=""):
    m = load_run(run_id)
    dec = m["decisions"]
    video = Path(m["video"])
    if not video.exists():
        raise FileNotFoundError(f"No encuentro el video original: {video}")
    cfg = load_config()
    if folder:
        out_dir = Path(folder).expanduser()
    elif cfg.get("export_dir"):
        out_dir = Path(cfg["export_dir"]).expanduser() / safe_name(Path(m["videoName"]).stem)
    else:
        out_dir = Path(cfg["data_dir"]) / "exportados" / safe_name(Path(m["videoName"]).stem)
    out_dir.mkdir(parents=True, exist_ok=True)
    done, failed = [], []
    order = 0
    for c in m["clips"]:
        d = dec.get(c["id"]) or {}
        if d.get("decision") != "approved":
            continue
        order += 1
        start, length = adjusted_window(c, d)
        name = f"{order:02d}_{c['half']}_{safe_name(c['title'])}_{int(start // 60):03d}m{int(start % 60):02d}s.mp4"
        dest = out_dir / name
        if cut(video, dest, start, length, mode="export"):
            done.append({"order": order, "id": c["id"], "half": c["halfLabel"], "title": c["title"],
                         "file": dest.name, "start": round(start, 2), "length": round(length, 2)})
        else:
            failed.append(c["id"])
    lines = [f"{x['order']:02d}\t{x['half']}\t{x['title']}\t{x['file']}" for x in done]
    (out_dir / "orden_para_premiere.txt").write_text("\n".join(lines), encoding="utf-8")
    (out_dir / "decisiones_panel.json").write_text(
        json.dumps({"runId": run_id, "video": str(video), "clips": done, "decisions": dec}, indent=2, ensure_ascii=False),
        encoding="utf-8")
    return {"folder": str(out_dir), "count": len(done), "failed": failed}


def export_dir_for(m, folder=""):
    cfg = load_config()
    if folder:
        out_dir = Path(folder).expanduser()
    elif cfg.get("export_dir"):
        out_dir = Path(cfg["export_dir"]).expanduser() / safe_name(Path(m["videoName"]).stem)
    else:
        out_dir = Path(cfg["data_dir"]) / "exportados" / safe_name(Path(m["videoName"]).stem)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def export_sequence(run_id, folder=""):
    """Secuencia XML para Premiere con los aprobados, apuntando al original."""
    from .premiere import build_sequence

    m = load_run(run_id)
    dec = m["decisions"]
    video = Path(m["video"])
    if not video.exists():
        raise FileNotFoundError(f"No encuentro el video original: {video}")
    items = []
    for c in m["clips"]:
        d = dec.get(c["id"]) or {}
        if d.get("decision") != "approved":
            continue
        start, length = adjusted_window(c, d)
        items.append({"start": start, "length": length,
                      "title": f"{c['id']} {c['halfLabel']} - {c['title']}",
                      "marker": " · ".join(x for x in [c["halfLabel"], c["title"], c.get("detail", "")] if x)})
    if not items:
        raise ValueError("No hay jugadas aprobadas")
    out_dir = export_dir_for(m, folder)
    name = f"Highlights {Path(m['videoName']).stem}"
    xml = build_sequence(name, video, items)
    dest = out_dir / f"{safe_name(Path(m['videoName']).stem)}_highlights.xml"
    dest.write_text(xml, encoding="utf-8")
    total = sum(i["length"] for i in items)
    return {"file": str(dest), "folder": str(out_dir), "count": len(items), "seconds": round(total, 1)}


def render_adjusted(run_id, clip_id, decision):
    m = load_run(run_id)
    c = next(c for c in m["clips"] if c["id"] == clip_id)
    start, length = adjusted_window(c, decision)
    out_dir = run_path(run_id) / "ajustados"
    out_dir.mkdir(exist_ok=True)
    dest = out_dir / f"{clip_id}_{int(time.time())}.mp4"
    if not cut(m["video"], dest, start, length, mode="preview"):
        raise RuntimeError("No se pudo cortar el clip ajustado (¿esta el video original?)")
    return {"file": f"/media/{run_id}/ajustados/{dest.name}", "start": start, "length": length}


def browse_folder(title="Elegir carpeta"):
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder = filedialog.askdirectory(title=title)
        root.destroy()
        return {"folder": folder or ""}
    except Exception as exc:
        return {"folder": "", "error": str(exc)}


# ---------------------------------------------------------------- http

CTYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".json": "application/json", ".mp4": "video/mp4",
          ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".svg": "image/svg+xml"}


class Handler(BaseHTTPRequestHandler):
    def send_json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        q = parse_qs(u.query)
        try:
            if path == "/":
                return self.serve_file(STATIC / "index.html")
            if path == "/api/config":
                return self.send_json(load_config())
            if path == "/api/profiles":
                return self.send_json({"profiles": list_profiles()})
            if path == "/api/videos":
                return self.send_json({"videos": scan_videos()})
            if path == "/api/runs":
                return self.send_json({"runs": scan_runs()})
            if path == "/api/run":
                return self.send_json(load_run(q.get("id", [""])[0]))
            if path == "/api/jobs":
                return self.send_json({"jobs": JOBS[-30:]})
            if path == "/api/browse-folder":
                return self.send_json(browse_folder(q.get("title", ["Elegir carpeta"])[0]))
            if path.startswith("/media/"):
                rest = path[len("/media/"):]
                run_id, _, rel = rest.partition("/")
                base = run_path(run_id)
                f = (base / rel).resolve()
                if base not in f.parents:
                    return self.send_error(403)
                return self.serve_file(f)
            f = (STATIC / path.lstrip("/")).resolve()
            if STATIC.resolve() not in f.parents:
                return self.send_error(403)
            return self.serve_file(f)
        except Exception as exc:
            return self.send_json({"error": str(exc)}, status=500)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length", "0") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        try:
            if u.path == "/api/config":
                cfg = load_config()
                for k in ("video_folders", "data_dir", "export_dir", "default_profile"):
                    if k in body:
                        cfg[k] = body[k]
                save_config(cfg)
                return self.send_json(cfg)
            if u.path == "/api/queue":
                profile = body.get("profile") or load_config()["default_profile"]
                jobs = [enqueue(p, profile) for p in body.get("paths", [])]
                return self.send_json({"jobs": jobs})
            if u.path == "/api/reanalyze":
                m = load_run(body["runId"])
                job = enqueue(m["video"], body.get("profile") or m.get("profile"), reuse_run=body["runId"])
                return self.send_json({"jobs": [job]})
            if u.path == "/api/decisions":
                return self.send_json(save_decisions(body["runId"], body.get("decisions") or {}))
            if u.path == "/api/export":
                return self.send_json(export_approved(body["runId"], body.get("folder", "")))
            if u.path == "/api/export-xml":
                return self.send_json(export_sequence(body["runId"], body.get("folder", "")))
            if u.path == "/api/render-adjusted":
                return self.send_json(render_adjusted(body["runId"], body["clipId"], body.get("decision") or {}))
            if u.path == "/api/delete-run":
                return self.send_json(delete_run(body["runId"]))
        except Exception as exc:
            traceback.print_exc()
            return self.send_json({"error": str(exc)}, status=500)
        return self.send_error(404)

    def serve_file(self, path):
        path = Path(path)
        if not path.exists() or not path.is_file():
            return self.send_error(404)
        ctype = CTYPES.get(path.suffix.lower(), "application/octet-stream")
        size = path.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                a, b = m.groups()
                if a:
                    start = int(a)
                    end = int(b) if b else size - 1
                elif b:
                    start = max(0, size - int(b))
                end = min(end, size - 1)
                status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with path.open("rb") as fh:
                fh.seek(start)
                left = length
                while left > 0:
                    chunk = fh.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    def log_message(self, fmt, *args):
        if "/api/jobs" in (args[0] if args else ""):
            return
        print(fmt % args)


def main():
    port = int(os.environ.get("HIGHLIGHT_PORT", "8790"))
    threading.Thread(target=worker, daemon=True).start()
    host = os.environ.get("HIGHLIGHT_HOST", "127.0.0.1")
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"Panel de highlights: http://127.0.0.1:{port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
