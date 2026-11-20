"""Scene-aware layouts: streams switch between a full-screen camera, gameplay with a facecam box, and no face at all.
Every half second of the clip gets a state, short blips are smoothed away, and each scene gets its own layout."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from . import face

STEP = 0.5          # seconds between looks
MIN_SCENE = 1.6     # shorter scenes merge into their neighbours


def _timeline(video: Path, t0: float, t1: float):
    cap = cv2.VideoCapture(str(video))
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    sw = 640
    s = sw / max(1, W)
    sh = int(round(H * s))
    det = face._detector(sw, sh)
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t0) * 1000)
    samples = []
    t = t0
    next_t = t0
    frame_dt = 1.0 / fps
    pos = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
    while pos <= t1 + 0.01:
        ok = cap.grab()
        if not ok:
            break
        pos += frame_dt
        if pos + 1e-6 < next_t:
            continue
        ok, fr = cap.retrieve()
        if not ok:
            break
        small = cv2.resize(fr, (sw, sh), interpolation=cv2.INTER_AREA)
        faces = [f for f in face._detect(det, small) if f[2] >= 10]
        samples.append({"t": round(next_t, 2), "faces": [(x / s, y / s, w / s, h / s, sc) for x, y, w, h, sc in faces],
                        "img": small})
        next_t += STEP
    cap.release()
    return samples, W, H, s


def analyze(video: Path, t0: float, t1: float, debug_jpg: Path | None = None) -> dict:
    """Returns {"W","H","scenes": [{"start","end","layout", "face", "cam"}], "why"} in source seconds."""
    samples, W, H, s = _timeline(video, t0, t1)
    if not samples:
        return {"W": W, "H": H, "scenes": [{"start": t0, "end": t1, "layout": "gameplay"}], "why": "no frames"}
    # 1. big faces mean a full-screen camera
    big = []
    small_faces = []
    for k, sm in enumerate(samples):
        b = [f for f in sm["faces"] if f[3] / H >= 0.2 or (f[2] * f[3]) / (W * H) > 0.035]
        big.append(max(b, key=lambda f: f[2] * f[3]) if b else None)
        for f in sm["faces"]:
            if f not in b:
                small_faces.append((k, f))
    # 2. the facecam box: a small face that sits in the same place across the clip
    clusters = []
    for k, (x, y, w, h, sc) in small_faces:
        cx, cy = x + w / 2, y + h / 2
        for c in clusters:
            if abs(cx - c["cx"]) < 0.6 * max(w, c["w"]) and abs(cy - c["cy"]) < 0.6 * max(h, c["h"]):
                c["items"].append((k, x, y, w, h))
                c["ks"].add(k)
                break
        else:
            clusters.append({"cx": cx, "cy": cy, "w": w, "h": h, "items": [(k, x, y, w, h)], "ks": {k}})
    n = len(samples)
    cam_cluster = None
    for c in clusters:
        arr = np.array([it[1:] for it in c["items"]])
        centers = arr[:, :2] + arr[:, 2:] / 2
        jitter = float(np.mean(np.std(centers, axis=0))) / W if len(arr) > 1 else 0.0
        c["jitter"], c["box"] = jitter, np.median(arr, axis=0)
        c["cov"] = len(c["ks"]) / n
        if c["cov"] >= 0.2 and jitter < 0.03 and (cam_cluster is None or c["cov"] > cam_cluster["cov"]):
            cam_cluster = c
    # a big face in the middle of the picture is a full-screen camera; a big face low or to the side is a large
    # webcam sitting over the game (common with Jynxzi-style layouts), which gets the facecam-on-top layout
    def centred(f):
        cx, cy = (f[0] + f[2] / 2) / W, (f[1] + f[3] / 2) / H
        return 0.22 <= cx <= 0.78 and 0.18 <= cy <= 0.62 and f[3] / H >= 0.2
    big_box = None
    bk = [k for k in range(n) if big[k] is not None and not centred(big[k])]
    if bk:
        bx = [float(np.median([big[k][i] for k in bk])) * s for i in range(4)]
        cb = face._cam_box([samples[k]["img"] for k in bk][:16], *bx, samples[0]["img"].shape[1],
                           samples[0]["img"].shape[0])
        big_box = {"cam": [v / s for v in cb["box"]], "face": [v / s for v in bx], "edges": cb["edges"]}
    states = []
    for k in range(n):
        if big[k] is not None:
            states.append("fullcam" if centred(big[k]) else "bigcam")
        elif cam_cluster and k in cam_cluster["ks"]:
            states.append("stack")
        else:
            states.append("none")
    # a moment without a detected face inside a facecam stream is usually the streamer looking away: keep the box
    if cam_cluster or big_box:
        states = [("stack" if cam_cluster else "bigcam") if st == "none" else st for st in states]
    else:
        states = ["gameplay" if st == "none" else st for st in states]
    states = _smooth(states)
    if big_box and not cam_cluster:
        states = ["stack" if st == "bigcam" else st for st in states]
        cam_cluster_box = big_box
    else:
        cam_cluster_box = None
        if big_box:
            states = ["bigcam" if st == "bigcam" else st for st in states]
    # 3. scenes
    scenes = []
    for k, st in enumerate(states):
        ts = samples[k]["t"]
        if scenes and scenes[-1]["layout"] == st:
            scenes[-1]["ks"].append(k)
        else:
            scenes.append({"layout": st, "ks": [k], "start": ts})
    for i, sc in enumerate(scenes):
        sc["start"] = t0 if i == 0 else samples[sc["ks"][0]]["t"] - STEP / 2
        sc["end"] = t1 if i == len(scenes) - 1 else samples[scenes[i + 1]["ks"][0]]["t"] - STEP / 2
    # merge scenes that are too short
    merged = []
    for sc in scenes:
        if merged and sc["end"] - sc["start"] < MIN_SCENE:
            merged[-1]["end"] = sc["end"]
            merged[-1]["ks"] += sc["ks"]
        else:
            merged.append(sc)
    if len(merged) > 1 and merged[0]["end"] - merged[0]["start"] < MIN_SCENE:
        merged[1]["start"] = merged[0]["start"]
        merged[1]["ks"] = merged[0]["ks"] + merged[1]["ks"]
        merged.pop(0)
    tidy = []
    for sc in merged:
        if tidy and tidy[-1]["layout"] == sc["layout"]:
            tidy[-1]["end"] = sc["end"]
            tidy[-1]["ks"] += sc["ks"]
        else:
            tidy.append(sc)
    merged = tidy
    # 4. crop boxes per scene
    cam_box, edges = None, 0
    if cam_cluster:
        fx, fy, fw, fh = [float(v) * s for v in cam_cluster["box"]]
        imgs = [samples[k]["img"] for k in sorted(cam_cluster["ks"])][:16]
        cb = face._cam_box(imgs, fx, fy, fw, fh, samples[0]["img"].shape[1], samples[0]["img"].shape[0])
        cam_box = [v / s for v in cb["box"]]
        edges = cb["edges"]
    out_scenes = []
    for sc in merged:
        d = {"start": round(sc["start"], 2), "end": round(sc["end"], 2), "layout": sc["layout"]}
        if sc["layout"] == "fullcam":
            bs = [big[k] for k in sc["ks"] if big[k] is not None]
            d["face"] = [float(np.median([b[i] for b in bs])) for i in range(4)]
        elif sc["layout"] == "stack" and cam_cluster is not None:
            d["face"] = [float(v) for v in cam_cluster["box"]]
            d["cam"] = cam_box
        elif sc["layout"] in ("stack", "bigcam") and (cam_cluster_box or big_box):
            bb = cam_cluster_box or big_box
            d["layout"] = "stack"
            d["face"], d["cam"] = bb["face"], bb["cam"]
            edges = max(edges, bb["edges"])
        out_scenes.append(d)
    why = " → ".join(f"{d['layout']} {d['end'] - d['start']:.0f}s" for d in out_scenes)
    if debug_jpg:
        _debug(debug_jpg, samples, cam_cluster, cam_box, big, s)
    return {"W": W, "H": H, "scenes": out_scenes, "why": why, "cam_edges": edges,
            "layout": out_scenes[0]["layout"] if len(out_scenes) == 1 else "mixed"}


def _smooth(states: list[str], win: int = 2) -> list[str]:
    out = []
    for i in range(len(states)):
        seg = states[max(0, i - win): i + win + 1]
        out.append(max(set(seg), key=seg.count))
    return out


def _debug(path, samples, cam_cluster, cam_box, big, s):
    k = sorted(cam_cluster["ks"])[len(cam_cluster["ks"]) // 2] if cam_cluster else len(samples) // 2
    im = samples[k]["img"].copy()
    if cam_cluster:
        x, y, w, h = [int(v * s) for v in cam_cluster["box"]]
        cv2.rectangle(im, (x, y), (x + w, y + h), (0, 230, 255), 2)
    if cam_box:
        x, y, w, h = [int(v * s) for v in cam_box]
        cv2.rectangle(im, (x, y), (x + w, y + h), (80, 255, 80), 3)
    if big[k] is not None:
        x, y, w, h = [int(v * s) for v in big[k][:4]]
        cv2.rectangle(im, (x, y), (x + w, y + h), (255, 120, 60), 2)
    cv2.imwrite(str(path), im, [cv2.IMWRITE_JPEG_QUALITY, 82])


def crops_for(scene: dict, W: int, H: int) -> dict:
    fa = {"W": W, "H": H, "layout": scene["layout"]}
    if "face" in scene:
        fa["face"] = scene["face"]
    if "cam" in scene and scene["cam"]:
        fa["cam"] = scene["cam"]
    elif scene["layout"] == "stack" and "face" in scene:
        fx, fy, fw, fh = scene["face"]
        fa["cam"] = [max(0, fx - 1.4 * fw), max(0, fy - 0.9 * fh), min(W, 3.8 * fw), min(H, 3.1 * fh)]
    return face.crops(fa)
