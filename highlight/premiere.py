"""Secuencia para Premiere (XML de Final Cut 7, que Premiere importa).

Cada jugada aprobada es un clip que apunta al video ORIGINAL con su punto de
entrada y salida: no se recorta ni se re-renderiza nada, y en Premiere se
puede estirar cualquier corte porque el material completo esta ahi.
En Premiere: Archivo > Importar > el .xml  -> aparece la secuencia armada.
"""
import re
import subprocess
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape

from .ffmpeg import ffmpeg_exe, probe_duration


def probe_info(video):
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(video)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="ignore")
    err = proc.stderr
    info = {"width": 1920, "height": 1080, "fps": 30.0, "sample_rate": 48000, "channels": 2}
    m = re.search(r"Video:.*?(\d{3,5})x(\d{3,5})", err)
    if m:
        info["width"], info["height"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"Video:.*?([\d.]+) fps", err) or re.search(r"Video:.*?([\d.]+) tbr", err)
    if m:
        info["fps"] = float(m.group(1))
    m = re.search(r"Audio:.*?(\d+) Hz,\s*([^,]+)", err)
    if m:
        info["sample_rate"] = int(m.group(1))
        info["channels"] = 1 if "mono" in m.group(2) else 2
    info["duration"] = probe_duration(video) or 0
    return info


def _rate(fps):
    """timebase entero + bandera NTSC como espera el XML."""
    for nominal, real in ((24, 23.976), (30, 29.97), (60, 59.94)):
        if abs(fps - real) < 0.02:
            return nominal, True, real
    tb = int(round(fps)) or 30
    return tb, False, float(tb)


def path_url(p):
    p = str(Path(p).resolve()).replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", p):
        return "file://localhost/" + quote(p, safe="/:")
    return "file://localhost" + quote(p, safe="/")


def build_sequence(name, video, items, info=None):
    """items: lista de dicts con start, length (segundos), title, marker."""
    info = info or probe_info(video)
    tb, ntsc, real = _rate(info["fps"])
    ntsc_s = "TRUE" if ntsc else "FALSE"
    fr = lambda s: int(round(float(s) * real))
    file_frames = fr(info["duration"]) if info["duration"] else 10 ** 7
    vname = escape(Path(video).name)
    url = escape(path_url(video))
    rate = f"<rate><timebase>{tb}</timebase><ntsc>{ntsc_s}</ntsc></rate>"

    file_full = f"""<file id="file-1"><name>{vname}</name><pathurl>{url}</pathurl>{rate}<duration>{file_frames}</duration>
<media><video><samplecharacteristics>{rate}<width>{info['width']}</width><height>{info['height']}</height></samplecharacteristics></video>
<audio><samplecharacteristics><depth>16</depth><samplerate>{info['sample_rate']}</samplerate></samplecharacteristics><channelcount>{info['channels']}</channelcount></audio></media></file>"""

    vclips, a1clips, a2clips = [], [], []
    pos = 0
    for k, it in enumerate(items, start=1):
        cin = fr(it["start"])
        cout = cin + max(1, fr(it["length"]))
        end = pos + (cout - cin)
        f = file_full if k == 1 else '<file id="file-1"/>'
        title = escape(it["title"])
        marker = f"<marker><name>{escape(it.get('marker') or it['title'])}</name><comment></comment><in>{cin}</in><out>-1</out></marker>"
        links = (f"<link><linkclipref>v{k}</linkclipref><mediatype>video</mediatype><trackindex>1</trackindex><clipindex>{k}</clipindex></link>"
                 f"<link><linkclipref>a{k}</linkclipref><mediatype>audio</mediatype><trackindex>1</trackindex><clipindex>{k}</clipindex><groupindex>1</groupindex></link>")
        if info["channels"] == 2:
            links += f"<link><linkclipref>b{k}</linkclipref><mediatype>audio</mediatype><trackindex>2</trackindex><clipindex>{k}</clipindex><groupindex>1</groupindex></link>"
        common = f"<name>{title}</name><enabled>TRUE</enabled><duration>{file_frames}</duration>{rate}<start>{pos}</start><end>{end}</end><in>{cin}</in><out>{cout}</out>"
        vclips.append(f'<clipitem id="v{k}">{common}{f}{marker}{links}</clipitem>')
        a1clips.append(f'<clipitem id="a{k}">{common}<file id="file-1"/><sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex></sourcetrack>{links}</clipitem>')
        if info["channels"] == 2:
            a2clips.append(f'<clipitem id="b{k}">{common}<file id="file-1"/><sourcetrack><mediatype>audio</mediatype><trackindex>2</trackindex></sourcetrack>{links}</clipitem>')
        pos = end

    atracks = f"<track>{''.join(a1clips)}</track>" + (f"<track>{''.join(a2clips)}</track>" if a2clips else "")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="4">
<sequence id="seq-1"><name>{escape(name)}</name><duration>{pos}</duration>{rate}
<media>
<video><format><samplecharacteristics>{rate}<width>{info['width']}</width><height>{info['height']}</height><pixelaspectratio>square</pixelaspectratio></samplecharacteristics></format>
<track>{''.join(vclips)}</track></video>
<audio><numOutputChannels>2</numOutputChannels><format><samplecharacteristics><depth>16</depth><samplerate>{info['sample_rate']}</samplerate></samplecharacteristics></format>
{atracks}</audio>
</media>
</sequence>
</xmeml>
"""
