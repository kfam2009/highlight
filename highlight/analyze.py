"""Analisis completo de un partido grabado."""
import json
import pickle
import time
from pathlib import Path

import numpy as np

from . import events as ev_mod
from .extract import RMS_RATE, extract, find_stingers, load_audio_rms, load_context
from .ffmpeg import hms, probe_duration
from .render import cut, poster, safe_name
from .scorebug import Profile, read_scorebug_video

HALF_NAME = {"T": "alta", "B": "baja"}


def half_label(inning, half):
    return f"{inning}ª {HALF_NAME.get(half, half)}" if inning else "Sin entrada"


def score_text(state):
    a, h = state.get("away"), state.get("home")
    if a is None or h is None:
        return ""
    return f"{a}-{h}"


def bases_text(b):
    if not b:
        return ""
    names = [n for flag, n in zip(b, ["1ª", "2ª", "3ª"]) if flag == "1"]
    return ", ".join(names) if names else "vacias"


def build_clips(result):
    clips = []
    halves = []
    n = 0
    for h in result["story"]:
        key = f"{h['inning']}{h['half']}"
        ids = []
        for e in h["events"]:
            n += 1
            cid = f"{n:02d}"
            b, a = e.get("before") or {}, e.get("after") or {}
            title = e["label"]
            if e["type"] == "tercer_out" and e.get("strikeout"):
                title = "3er out (posible ponche)"
            if e["type"] == "carrera" and e.get("runs") not in (0, None):
                r = e["runs"]
                title = "Carrera" if r == 1 else (f"{r} carreras" if r != "?" else "Carrera(s)")
            detail = []
            if score_text(b) and score_text(a) and score_text(b) != score_text(a):
                detail.append(f"{score_text(b)} → {score_text(a)}")
            elif score_text(b):
                detail.append(score_text(b))
            if e.get("count_before"):
                detail.append(f"conteo {e['count_before']}")
            if b.get("outs") is not None:
                detail.append(f"{b['outs']} out" + ("s" if str(b["outs"]) != "1" else ""))
            if b.get("bases") is not None and a.get("bases") is not None and b.get("bases") != a.get("bases") and not e.get("half_changed"):
                detail.append(f"bases {bases_text(b['bases'])} → {bases_text(a['bases'])}")
            elif b.get("bases") not in (None, "000"):
                detail.append(f"en base: {bases_text(b['bases'])}")
            clips.append({
                "id": cid,
                "half": key,
                "halfLabel": half_label(h["inning"], h["half"]),
                "type": e["type"],
                "title": title,
                "detail": " · ".join(detail),
                "priority": e["priority"],
                "suggested": e["type"] in ("carrera", "hit", "doble_play") or (e["type"] == "tercer_out" and b.get("bases") not in (None, "000")),
                "runs": e.get("runs"),
                "strikeout": bool(e.get("strikeout")),
                "start": e["start"],
                "end": e["end"],
                "duration": round(e["end"] - e["start"], 2),
                "pitchTime": e["pitchTime"],
                "scorebugTime": e["scorebugTime"],
                "replay": e.get("replay"),
                "timing": e["timing"],
                "sourceTime": hms(e["start"]),
                "before": b,
                "after": a,
            })
            ids.append(cid)
        halves.append({"key": key, "label": half_label(h["inning"], h["half"]), "inning": h["inning"], "half": h["half"],
                       "start": h["start"], "end": h["end"], "clips": ids})
    fin = result.get("final")
    if fin:
        n += 1
        cid = f"{n:02d}"
        sc = score_text(fin["after"])
        clips.append({
            "id": cid, "half": "FIN", "halfLabel": "Final", "type": "final",
            "title": f"Final {sc}" if sc else "Final",
            "detail": "última jugada, festejos, saludos y score final"
                      + (" · ojo: la grabación parece cortarse antes del final" if fin.get("recordingCut") else ""),
            "priority": fin["priority"], "suggested": True, "runs": 0,
            "start": fin["start"], "end": fin["end"], "duration": round(fin["end"] - fin["start"], 2),
            "pitchTime": fin["pitchTime"], "scorebugTime": fin["scorebugTime"], "replay": None,
            "timing": "final", "sourceTime": hms(fin["start"]), "before": fin["before"], "after": fin["after"],
        })
        halves.append({"key": "FIN", "label": "Final", "inning": None, "half": None,
                       "start": fin["start"], "end": fin["end"], "clips": [cid]})
    return clips, halves


def analyze(video, runs_dir, profile_name="on_softball", on_progress=None, render_previews=True, reuse_work=None):
    video = Path(video).resolve()
    if not video.exists():
        raise FileNotFoundError(f"No existe {video}")

    def prog(stage, frac, msg=""):
        if on_progress:
            on_progress(stage, float(frac), msg)

    profile = Profile(profile_name)
    run_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{safe_name(video.stem)}"
    run_dir = Path(runs_dir) / run_id
    work = run_dir / "trabajo"
    clip_dir = run_dir / "clips"
    clip_dir.mkdir(parents=True, exist_ok=True)

    duration = probe_duration(video) or 0
    t0 = time.time()
    if reuse_work:
        # re-analizar sin volver a leer el video (por ejemplo con una version
        # nueva de la logica): se reutiliza lo extraido antes
        import shutil
        shutil.copytree(reuse_work, work, dirs_exist_ok=True)
    else:
        prog("extraer", 0, "Leyendo el partido (scorebug, audio, imagen)")
        extract(video, work, profile, duration, lambda f: prog("extraer", f * 0.6))
    t_extract = time.time() - t0

    prog("scorebug", 0.6, "Leyendo el scorebug")
    raw = read_scorebug_video(work / "scorebug.mp4", profile, lambda f: prog("scorebug", 0.6 + f * 0.15))
    with open(work / "scorebug_raw.pkl", "wb") as fh:
        pickle.dump(raw, fh)

    prog("historia", 0.76, "Armando la historia del partido")
    ctx = load_context(work)
    stingers = find_stingers(ctx, profile.stinger)
    rms = load_audio_rms(work)
    result = ev_mod.build_story(raw, stingers, rms, RMS_RATE)
    clips, halves = build_clips(result)

    if render_previews:
        for k, c in enumerate(clips):
            prog("clips", 0.78 + 0.22 * k / max(1, len(clips)), f"Cortando vista previa {k + 1}/{len(clips)}")
            out = clip_dir / f"{c['id']}_{c['half']}_{c['type']}.mp4"
            if cut(video, out, c["start"], c["duration"], mode="preview"):
                c["file"] = f"clips/{out.name}"
                jpg = clip_dir / f"{out.stem}.jpg"
                poster(out, jpg, min(c["duration"] - 0.5, max(0.5, c["pitchTime"] - c["start"] + 1.5)))
                if jpg.exists():
                    c["poster"] = f"clips/{jpg.name}"

    manifest = {
        "runId": run_id,
        "video": str(video),
        "videoName": video.name,
        "profile": profile_name,
        "duration": duration,
        "created": time.time(),
        "timings": {"extraer_s": round(t_extract, 1), "total_s": round(time.time() - t0, 1)},
        "stats": {
            "halves": len(halves),
            "clips": len(clips),
            "carreras": sum(1 for c in clips if c["type"] == "carrera"),
            "pitchesMph": len(result["mph"]),
            "replays": len(result["replays"]),
            "finalScore": score_text(result["events"][-1]["after"]) if result["events"] else "",
        },
        "halves": halves,
        "clips": clips,
        "logic": [
            "La historia sale del scorebug: cada cambio de outs, bases, carreras o entrada es una jugada.",
            "Ninguna media entrada queda afuera; si no se detecto nada aparece como 'revisar a mano'.",
            "El scorebug se actualiza tarde: el lanzamiento se ubica con el MPH previo, el replay y el audio.",
            "Correcciones del operador (un out que aparece y se va) se descartan.",
            "Los clips de jugadas con replay incluyen el replay; se puede recortar o extender antes de exportar.",
        ],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=_np), encoding="utf-8")
    prog("listo", 1.0, "Listo")
    return manifest


def _np(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(type(o))
