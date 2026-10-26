"""Corte "listo para mandar": desde el pitcher hasta que se resuelve la jugada.

La transmision muestra cada lanzamiento con la misma camara (la toma del
pitcheo). Se aprende esa toma mirando la imagen en el momento de cada MPH
(que aparece en cada lanzamiento) y despues, para cada jugada:
  - inicio: el corte que sale de la toma del pitcheo es el batazo; se
    arranca unos segundos antes para ver al pitcher lanzar;
  - final: cuando empieza el replay, o la camara vuelve a la toma del
    pitcheo para el siguiente bateador, o un maximo segun el tipo de jugada.
El clip amplio (con replay) queda guardado: "Extender" lo recupera.
"""
import cv2
import numpy as np

CTX_FPS = 2


def pitchcam_distance(ctx, mph, max_templates=4, merge=14.0):
    """Distancia de cada cuadro de contexto a la toma del pitcheo (menor = mas parecido)."""
    if ctx is None or not len(ctx) or not mph:
        return None
    G = np.array([cv2.resize(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), (32, 18), interpolation=cv2.INTER_AREA)
                  for f in ctx]).astype(np.float32).reshape(len(ctx), -1)
    idx = [int((m - 2.5) * CTX_FPS) for m in mph if 0 <= (m - 2.5) * CTX_FPS < len(G)]
    if len(idx) < 8:
        return None
    cents, cnt = [], []
    for v in G[idx]:
        if cents:
            d = [np.abs(c / n - v).mean() for c, n in zip(cents, cnt)]
            j = int(np.argmin(d))
            if d[j] < merge:
                cents[j] += v
                cnt[j] += 1
                continue
        cents.append(v.copy())
        cnt.append(1)
    order = np.argsort(cnt)[::-1]
    T = [cents[i] / cnt[i] for i in order[:max_templates] if cnt[i] >= max(5, 0.08 * len(idx))]
    if not T:
        return None
    return np.min([np.abs(G - t).mean(1) for t in T], 0)


def _segments(flags):
    segs, i, n = [], 0, len(flags)
    while i < n:
        if flags[i]:
            j = i
            while j < n and (flags[j] or (j + 1 < n and flags[j + 1])):  # tolera 1 cuadro suelto
                j += 1
            segs.append((i / CTX_FPS, j / CTX_FPS))
            i = j
        else:
            i += 1
    return segs


MAX_LEN = {"carrera": 24.0, "hit": 18.0, "doble_play": 16.0, "final": None}


def tight_window(ev, dist, replays, absent_from=None, thr=12.0):
    """(inicio, fin) del corte ajustado, o None si no se puede estimar."""
    if dist is None or ev.get("type") in ("revisar", "final"):
        return None
    P = float(ev["pitchTime"])  # lanzamiento estimado
    search_end = float(ev.get("searchEnd") or ev.get("scorebugTime") or P + 10)
    w0 = float(ev.get("windowStart") or P - 40)
    reliable = ev.get("timing") in ("mph", "audio")
    lo = int(max(0, (P - 20 if reliable else max(w0, search_end - 90)) * CTX_FPS))
    hi = int(min(len(dist), (min(P + 12, search_end) if reliable else search_end) * CTX_FPS))
    segs = [(a + lo / CTX_FPS, b + lo / CTX_FPS) for a, b in _segments(dist[lo:hi] < thr)]
    segs = [s for s in segs if s[1] - s[0] >= 1.0]
    if reliable:
        # la toma del pitcheo cuyo final (corte a la jugada) cae cerca del lanzamiento
        cand = [s for s in segs if P - 4.0 <= s[1] <= P + 8.0]
        pick = (lambda c: min(c, key=lambda s: abs(s[1] - (P + 1.5)))) if cand else None
    else:
        # sin MPH: el ultimo lanzamiento antes del replay / del cambio del marcador
        cand = [s for s in segs if s[1] <= search_end - 1.0]
        pick = (lambda c: c[-1]) if cand else None
    if cand:
        seg = pick(cand)
        start = max(seg[0], seg[1] - 5.5)
        contact = seg[1] - 0.5
    else:
        start = P - 3.5
        contact = P + 0.5
    start = max(0.0, start)
    longest = MAX_LEN.get(ev["type"], 11.0) or 11.0
    end = contact + longest
    # termina antes si empieza el replay
    for a, b in replays or []:
        if contact + 4.0 <= a < end:
            end = a - 0.5
            break
    # o si vuelve la toma del pitcheo (siguiente bateador / siguiente lanzamiento)
    j0 = int((contact + 5.0) * CTX_FPS)
    j1 = int(min(len(dist), end * CTX_FPS))
    run = 0
    for j in range(j0, j1):
        if dist[j] < thr:
            run += 1
            if run >= 3:  # 1.5 s en la toma del pitcheo
                end = min(end, (j - run + 1) / CTX_FPS)
                break
        else:
            run = 0
    end = max(end, contact + 5.0)
    return round(start, 2), round(end, 2)
