"""Landscape job -> 1080x1920 vertical clip. Runs on a GitHub Actions runner (Linux, CPU only).

Input is what the local half shipped: <name>.mp4 (landscape) and <name>.job.json (trim plan + word timings).
Steps: find the facecam scene by scene -> pick a layout per scene -> crop/stack to 1080x1920 -> cut the dead air ->
hook title + word-by-word captions + streamer credit -> loudness to -14 LUFS -> self-check -> post kit."""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from . import captions, face, scenes
from .util import read_json, run, say, settings, warn

def loudness(path: Path) -> float:
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "ebur128=peak=true",
                        "-f", "null", "-"], capture_output=True, text=True, errors="replace")
    vals = re.findall(r"I:\s+(-?[\d.]+) LUFS", p.stderr)
    return float(vals[-1]) if vals else -99.0


def probe_video(path: Path) -> dict:
    p = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,r_frame_rate:format=duration", "-of", "json", str(path)])
    d = json.loads(p.stdout)
    st = (d.get("streams") or [{}])[0]
    num, den = (st.get("r_frame_rate") or "30/1").split("/")
    fps = float(num) / max(1.0, float(den))
    return {"w": st.get("width"), "h": st.get("height"), "fps": fps,
            "dur": float((d.get("format") or {}).get("duration") or 0)}


def remap(words: list[dict], keep: list[tuple[float, float]]) -> list[dict]:
    out, offset = [], 0.0
    for x, y in keep:
        for w in words:
            if w["s"] >= x - 0.02 and w["e"] <= y + 0.05 and w["s"] < y:
                out.append({**w, "s": offset + max(0.0, w["s"] - x), "e": offset + min(y, w["e"]) - x})
        offset += y - x
    out.sort(key=lambda w: w["s"])
    return out


def source_link(info: dict, t: float) -> str:
    url = info.get("url") or ""
    t = int(t)
    if info.get("platform") == "youtube":
        return f"https://www.youtube.com/watch?v={info['id']}&t={t}s"
    if info.get("platform") == "twitch":
        h, m, s = t // 3600, (t % 3600) // 60, t % 60
        return f"https://www.twitch.tv/videos/{str(info['id']).lstrip('v')}?t={h}h{m}m{s}s"
    return url


def _measure_keep(sec: Path, keep) -> float:
    """Integrated loudness of exactly the parts we keep, measured on the original (stereo) audio."""
    parts = "".join(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[m{i}];" for i, (a, b) in enumerate(keep))
    fc = parts + "".join(f"[m{i}]" for i in range(len(keep))) + f"concat=n={len(keep)}:v=0:a=1,ebur128[out]"
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(sec), "-filter_complex", fc, "-map", "[out]",
                        "-f", "null", "-"], capture_output=True, text=True, errors="replace")
    vals = re.findall(r"I:\s+(-?[\d.]+) LUFS", p.stderr)
    v = float(vals[-1]) if vals else -99.0
    return v if v > -70 else -23.0


def check_clip(out: Path, L: float, words: list[dict], fa: dict) -> dict:
    pv = probe_video(out)
    lufs = loudness(out)
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(out), "-vf", "blackdetect=d=0.4:pix_th=0.06",
                        "-an", "-f", "null", "-"], capture_output=True, text=True, errors="replace")
    blacks = len(re.findall(r"black_start", p.stderr))
    issues = []
    if not (12 <= pv["dur"] <= 61):
        issues.append(f"length {pv['dur']:.1f} s")
    if abs(lufs + 14) > 1.5:
        issues.append(f"loudness {lufs:.1f} LUFS")
    if blacks:
        issues.append(f"{blacks} black stretch(es)")
    if pv["w"] != 1080 or pv["h"] != 1920:
        issues.append(f"size {pv['w']}x{pv['h']}")
    if len(words) < max(4, L * 0.6):
        issues.append("very few words: check the captions")
    if words and words[0]["s"] > 1.2:
        issues.append(f"first word at {words[0]['s']:.1f} s")
    if any(sc["layout"] == "stack" for sc in fa.get("scenes", [])) and fa.get("cam_edges", 0) <= 1:
        issues.append("facecam box guessed (look at the top panel)")
    return {"ok": not issues, "issues": issues, "lufs": round(lufs, 1), "seconds": round(pv["dur"], 2),
            "fps": round(pv["fps"], 2)}


def post_kit(info: dict, clip: dict, s: float, L: float, out: Path) -> str:
    who = clip.get("streamer") or info.get("streamer", "")
    plat = (info.get("platform") or "").title()
    tags = clip.get("hashtags") or settings()["hashtags"]
    tags = [t if t.startswith("#") else f"#{t}" for t in tags][:6]
    cap = (clip.get("caption") or clip.get("hook") or "").strip()
    src = source_link(info, s)
    lines = [
        f"HOOK (on screen): {clip.get('hook', '')}",
        "",
        "— TikTok / Instagram Reels —",
        f"{cap}",
        f"🎥 {who} on {plat}",
        " ".join(tags),
        "",
        "— YouTube Shorts —",
        f"Title: {(clip.get('hook') or cap)[:95]}",
        f"Description: {cap}\n🎥 {who} on {plat}: {src}\n" + " ".join(tags[:3]),
        "",
        f"Source: {src}",
        f"Length: {L:.1f} s",
        "Before you post: watch it once, and make sure this streamer is fine with clips.",
    ]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ manual facecam box (job["cam"])

def manual_scene(job: dict, t0: float, t1: float, W: int, H: int) -> dict | None:
    """The user told us where the webcam is: [x, y, w, h] in source pixels. Skips detection."""
    cam = job.get("cam")
    if not cam:
        return None
    x, y, w, h = [float(v) for v in cam]
    fc = job.get("face") or [x + 0.3 * w, y + 0.08 * h, 0.4 * w, 0.62 * h]
    return {"W": W, "H": H, "cam_edges": 4, "why": "manual cam box", "layout": "stack",
            "scenes": [{"start": t0, "end": t1, "layout": "stack", "face": [float(v) for v in fc], "cam": [x, y, w, h]}]}


def _vid_id(job: dict) -> str:
    k = str(job.get("stream_key", ""))
    return k.split("-", 1)[1] if "-" in k else k


def render_job(job: dict, video: Path, out_dir: Path, work_root: Path, layout: str | None = None) -> dict:
    cfg = settings()
    t_start = time.time()
    cid = job["id"]
    work = work_root / cid
    work.mkdir(parents=True, exist_ok=True)
    pv = probe_video(video)
    keep = [(max(0.0, float(a)), min(pv["dur"] - 0.02, float(b))) for a, b in job["keep"]]
    keep = [(a, b) for a, b in keep if b > a]
    if not keep:
        raise RuntimeError("job has no usable keep range")
    L = sum(y - x for x, y in keep)
    out_words = remap(job.get("words") or [], keep)

    # 1. where is the facecam, scene by scene
    sa = manual_scene(job, keep[0][0], keep[-1][1], pv["w"], pv["h"]) or \
        scenes.analyze(video, keep[0][0], keep[-1][1], work / "facecam.jpg")
    want = layout if layout and layout != "auto" else job.get("layout")
    if want in (None, "", "auto"):
        want = None
    if want:
        faces = [sc for sc in sa["scenes"] if sc.get("face")]
        if want in ("stack", "fullcam") and not faces:
            warn(f"{cid}: no face found, so {want} isn't possible; using gameplay")
            want = "gameplay"
        base = dict(faces[0]) if faces else {}
        base.update({"start": keep[0][0], "end": keep[-1][1], "layout": want})
        if want == "stack":
            base["cam"] = next((sc["cam"] for sc in sa["scenes"] if sc.get("cam")), None)
        sa["scenes"] = [base]
        sa["why"] = f"{want} (asked for)"
    W, H = sa["W"], sa["H"]
    pieces = []
    for x, y in keep:
        for sc in sa["scenes"]:
            lo = max(x, sc["start"] if sc is not sa["scenes"][0] else x)
            hi = min(y, sc["end"] if sc is not sa["scenes"][-1] else y)
            if hi - lo > 0.04:
                pieces.append((lo, hi, sc))
    lays = [p[2]["layout"] for p in pieces]
    lay = lays[0] if len(set(lays)) == 1 else "mixed"

    # 2. the graphics
    caplist = captions.render_captions(out_words, L, work / "caps")
    hook = (job.get("hook") or "").strip()
    hook_png = work / "hook.png"
    hw, hh = captions.hook_card(hook, hook_png) if hook else (0, 0)
    credit_png = work / "credit.png"
    who = job.get("streamer") or ""
    plat_name = {"twitch": "Twitch", "youtube": "YouTube", "kick": "Kick"}.get(job.get("platform") or "")
    crw = 0
    if job.get("credit", True) and who:
        crw, _ = captions.credit_tag(f"{who} on {plat_name}" if plat_name else who, credit_png)
    CAP_Y = {"stack": face.CAM_H - captions.CAP_H // 2, "fullcam": 1180, "gameplay": 656 + 608 + 10}
    HOOK_Y = {"stack": face.CAM_H + 70, "fullcam": 250, "gameplay": max(160, 656 - hh - 30)}
    hook_y = HOOK_Y[lays[0]]
    spans, t = [], 0.0
    for lo, hi, sc in pieces:
        y_ = CAP_Y[sc["layout"]]
        if spans and spans[-1][2] == y_:
            spans[-1][1] = t + hi - lo
        else:
            spans.append([t, t + hi - lo, y_])
        t += hi - lo
    cap_expr = str(spans[-1][2])
    for _t0, t1_, y_ in reversed(spans[:-1]):
        cap_expr = f"if(lt(t\\,{t1_:.3f})\\,{y_}\\,{cap_expr})"

    # 3. one ffmpeg pass
    fps = min(float(cfg["fps_cap"]), pv["fps"] if pv["fps"] > 1 else 30.0)
    K = len(pieces)
    fc = [f"[0:v]split={K}" + "".join(f"[vs{i}]" for i in range(K)) if K > 1 else "[0:v]null[vs0]",
          f"[0:a]asplit={K}" + "".join(f"[as{i}]" for i in range(K)) if K > 1 else "[0:a]anull[as0]"]
    for i, (x, y, sc) in enumerate(pieces):
        cr = scenes.crops_for(sc, W, H)
        head = f"[vs{i}]trim=start={x:.3f}:end={y:.3f},setpts=PTS-STARTPTS"
        if sc["layout"] == "stack" and cr.get("cam"):
            cx, cy, cw_, ch_ = cr["cam"]
            gx, gy, gw, gh = cr["game"]
            fc.append(f"{head},split=2[pa{i}][pb{i}]")
            fc.append(f"[pa{i}]crop={cw_}:{ch_}:{cx}:{cy},scale=1080:{face.CAM_H}:flags=lanczos,setsar=1[pt{i}]")
            fc.append(f"[pb{i}]crop={gw}:{gh}:{gx}:{gy},scale=1080:{face.GAME_H}:flags=lanczos,setsar=1[pu{i}]")
            fc.append(f"[pt{i}][pu{i}]vstack=inputs=2,format=yuv420p,setsar=1[v{i}]")
        elif sc["layout"] == "fullcam" and cr.get("full"):
            x_, y_, w_, h_ = cr["full"]
            fc.append(f"{head},crop={w_}:{h_}:{x_}:{y_},scale=1080:1920:flags=lanczos,format=yuv420p,setsar=1[v{i}]")
        else:   # gameplay alone: whole landscape frame centred over a blurred, darkened copy of itself
            fc.append(f"{head},split=2[pa{i}][pb{i}]")
            fc.append(f"[pa{i}]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=40,"
                      f"eq=brightness=-0.12:saturation=0.85[pg{i}]")
            fc.append(f"[pb{i}]scale=1080:-2:flags=lanczos,setsar=1[pf{i}]")
            fc.append(f"[pg{i}][pf{i}]overlay=0:(H-h)/2,format=yuv420p,setsar=1[v{i}]")
        fc.append(f"[as{i}]atrim=start={x:.3f}:end={y:.3f},asetpts=PTS-STARTPTS,"
                  f"afade=t=in:d=0.012,afade=t=out:st={max(0, (y - x) - 0.012):.3f}:d=0.012[a{i}]")
    fc.append("".join(f"[v{i}][a{i}]" for i in range(K)) + f"concat=n={K}:v=1:a=1[base][ac]")
    fc.append(f"[1:v]format=rgba[cap];[base][cap]overlay=x=0:y='{cap_expr}':eval=frame:eof_action=pass[o1]")
    last = "o1"
    inputs = ["-i", str(video), "-f", "concat", "-safe", "0", "-i", str(caplist)]
    n_in = 2
    if hook:
        inputs += ["-loop", "1", "-t", f"{L + 0.5:.2f}", "-i", str(hook_png)]
        fc.append(f"[{n_in}:v]format=rgba[hk];[{last}][hk]overlay=(W-w)/2:{hook_y}:enable='lt(t,{min(3.2, L):.2f})'[o2]")
        last, n_in = "o2", n_in + 1
    if crw:
        inputs += ["-loop", "1", "-t", f"{L + 0.5:.2f}", "-i", str(credit_png)]
        fc.append(f"[{n_in}:v]format=rgba[cr];[{last}][cr]overlay=24:150[o3]")
        last, n_in = "o3", n_in + 1
    gain = max(-12.0, min(18.0, -14.0 - _measure_keep(video, keep)))
    fc.append(f"[ac]volume={gain:.2f}dB,alimiter=limit=0.89:level=false,aresample=48000[aout]")
    name = job.get("name") or cid
    out = out_dir / f"{name}.vertical.mp4"
    out_dir.mkdir(parents=True, exist_ok=True)
    script = work / "filter.txt"
    script.write_text(";\n".join(fc))
    base_cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + inputs
    tail = ["-map", f"[{last}]", "-map", "[aout]", "-r", f"{fps:.3f}", "-c:v", "libx264", "-preset", "medium",
            "-crf", "18", "-maxrate", "20M", "-bufsize", "28M", "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-t", f"{L:.3f}", str(out)]
    p = run(base_cmd + ["-filter_complex_script", str(script)] + tail, check=False, timeout=1500)
    if p.returncode != 0:          # ffmpeg 7 renamed the option
        p2 = run(base_cmd + ["-/filter_complex", str(script)] + tail, check=False, timeout=1500)
        if p2.returncode != 0:
            raise RuntimeError("ffmpeg failed:\n" + (p.stderr or "")[-700:] + (p2.stderr or "")[-700:])

    # 4. self-check, cover frame, post kit
    qc = check_clip(out, L, out_words, sa)
    if -40 < qc["lufs"] and abs(qc["lufs"] + 14) > 0.8:
        fix = out.with_suffix(".fix.mp4")
        p3 = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(out), "-c:v", "copy", "-af",
                  f"volume={-14 - qc['lufs']:.2f}dB,alimiter=limit=0.89:level=false", "-c:a", "aac", "-b:a", "192k",
                  "-movflags", "+faststart", str(fix)], check=False)
        if p3.returncode == 0 and fix.exists():
            fix.replace(out)
            qc = check_clip(out, L, out_words, sa)
    cover_t = max(0.2, min(L - 0.2, L * 0.35))
    if job.get("peak") is not None:
        acc = 0.0
        for x, y in keep:
            if x <= job["peak"] <= y:
                cover_t = max(0.2, min(L - 0.2, acc + job["peak"] - x))
                break
            acc += y - x
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{cover_t:.2f}", "-i", str(out),
         "-frames:v", "1", "-q:v", "3", str(out_dir / f"{name}.cover.jpg")], check=False)
    s_stream = float(job.get("source_t0", 0)) + keep[0][0]
    vinfo = {"url": job.get("source_url"), "platform": job.get("platform"), "id": _vid_id(job), "streamer": who}
    (out_dir / f"{name}.post.txt").write_text(post_kit(vinfo, job, s_stream, L, out))
    dbg = work / "facecam.jpg"
    if dbg.exists():
        (out_dir / f"{name}.facecam.jpg").write_bytes(dbg.read_bytes())
    return {"id": cid, "file": out.name, "layout": lay, "layout_why": sa.get("why"), "length": round(L, 2),
            "cuts": len(keep) - 1, "words": len(out_words), "ok": qc["ok"], "issues": qc["issues"], "lufs": qc["lufs"],
            "seconds_to_render": round(time.time() - t_start, 1), "source": source_link(vinfo, s_stream)}


def render_dir(in_dir: Path, out_dir: Path, layout: str | None = None, only: str | None = None) -> list[dict]:
    jobs = sorted(in_dir.glob("*.job.json"))
    if not jobs:
        raise SystemExit(f"no *.job.json in {in_dir}")
    report, work_root = [], out_dir / "_work"
    for jp in jobs:
        job = read_json(jp)
        if not job or (only and job.get("id") not in only.split(",")):
            continue
        say(f"→ {job['id']}: {job.get('hook') or job.get('name')}")
        try:
            r = render_job(job, in_dir / job["video"], out_dir, work_root, layout)
        except Exception as ex:
            warn(f"{job['id']} failed: {ex}")
            report.append({"id": job["id"], "file": None, "ok": False, "issues": [str(ex)[:300]]})
            continue
        report.append(r)
        say(f"  {'✓' if r['ok'] else '⚠ ' + '; '.join(r['issues'])}  {r['file']}  {r['length']:.1f} s · "
            f"{r['layout']} ({r['layout_why']}) · {r['cuts']} dead-air cuts · {r['lufs']} LUFS · {r['seconds_to_render']:.0f} s")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "render-report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))
    return report
