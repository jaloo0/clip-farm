"""Finds the facecam by itself: the face that stays put while the game moves, and the box around it."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

try:
    cv2.setLogLevel(2)
except Exception:
    pass

from .util import MODELS

YUNET = MODELS / "face_detection_yunet_2023mar.onnx"
CAM_H = 760          # output: facecam panel 1080 x 760 on top
GAME_H = 1160        # gameplay panel 1080 x 1160 below
OUT_W, OUT_H = 1080, 1920


def _detector(w: int, h: int):
    if YUNET.exists():
        d = cv2.FaceDetectorYN.create(str(YUNET), "", (w, h), 0.65, 0.3, 5000)
        return ("yunet", d)
    casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    return ("haar", casc)


def _detect(kind_det, img):
    kind, det = kind_det
    if kind == "yunet":
        _, faces = det.detect(img)
        if faces is None:
            return []
        return [(float(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[14])) for f in faces]
    if det.empty():          # no cascade file and no YuNet model: behave as "no face found"
        return []
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    faces = det.detectMultiScale(g, 1.1, 6, minSize=(24, 24))
    return [(float(x), float(y), float(w), float(h), 0.8) for (x, y, w, h) in faces]


def sample_frames(video: Path, t0: float, t1: float, n: int = 16):
    cap = cv2.VideoCapture(str(video))
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frames = []
    for k in range(n):
        t = t0 + (t1 - t0) * (k + 0.5) / n
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, fr = cap.read()
        if ok and fr is not None:
            frames.append(fr)
    cap.release()
    return frames, W, H


def analyze(video: Path, t0: float, t1: float, debug_jpg: Path | None = None) -> dict:
    frames, W, H = sample_frames(video, t0, t1)
    if not frames:
        return {"layout": "gameplay", "W": W, "H": H, "why": "couldn't read frames"}
    sw = 960
    s = sw / W
    sh = int(round(H * s))
    det = _detector(sw, sh)
    small = [cv2.resize(f, (sw, sh), interpolation=cv2.INTER_AREA) for f in frames]
    dets = []
    for i, im in enumerate(small):
        for (x, y, w, h, sc) in _detect(det, im):
            if w < 14 or h < 14:
                continue
            dets.append((i, x, y, w, h, sc))

    # cluster by centre: the streamer's face sits in the same place in most frames
    clusters: list[dict] = []
    for (i, x, y, w, h, sc) in sorted(dets, key=lambda d: -d[5]):
        cx, cy = x + w / 2, y + h / 2
        for c in clusters:
            if abs(cx - c["cx"]) < 0.6 * max(w, c["w"]) and abs(cy - c["cy"]) < 0.6 * max(h, c["h"]):
                c["items"].append((i, x, y, w, h))
                c["frames"].add(i)
                break
        else:
            clusters.append({"cx": cx, "cy": cy, "w": w, "h": h, "items": [(i, x, y, w, h)], "frames": {i}})
    n = len(small)
    best = None
    for c in clusters:
        arr = np.array([it[1:] for it in c["items"]])
        cov = len(c["frames"]) / n
        centers = arr[:, :2] + arr[:, 2:] / 2
        jitter = float(np.mean(np.std(centers, axis=0))) / sw if len(arr) > 1 else 0.0
        c.update({"cov": cov, "jitter": jitter, "box": np.median(arr, axis=0)})
        score = cov - 2.0 * jitter + 0.15 * min(1.0, c["box"][3] / sh * 4)
        c["score"] = score
        if cov >= 0.4 and (best is None or score > best["score"]):
            best = c
    out = {"W": W, "H": H, "frames_checked": n, "detector": det[0]}
    if best is None:
        out.update({"layout": "gameplay", "why": "no face that stays on screen"})
        _debug(debug_jpg, small, None, None, s)
        return out
    fx, fy, fw, fh = [float(v) for v in best["box"]]
    out["face"] = [round(v / s) for v in (fx, fy, fw, fh)]
    out["face_cover"] = round(best["cov"], 2)
    if fh / sh >= 0.2 or (fw * fh) / (sw * sh) > 0.035:
        out["layout"] = "fullcam"
        out["why"] = "the camera fills the screen"
        _debug(debug_jpg, small, (fx, fy, fw, fh), None, s)
        return out
    cam = _cam_box(small, fx, fy, fw, fh, sw, sh)
    out["cam"] = [round(v / s) for v in cam["box"]]
    out["cam_edges"] = cam["edges"]
    out["layout"] = "stack"
    out["why"] = f"facecam found ({cam['edges']} of 4 edges seen)"
    _debug(debug_jpg, small, (fx, fy, fw, fh), cam["box"], s)
    return out


def _cam_box(small, fx, fy, fw, fh, sw, sh) -> dict:
    grays = [cv2.cvtColor(im, cv2.COLOR_BGR2GRAY).astype(np.float32) for im in small]
    ex = np.mean([np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)) for g in grays], axis=0)
    ey = np.mean([np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)) for g in grays], axis=0)
    # an edge that is there in every frame is an overlay border; game edges move and average out
    ya, yb = int(max(0, fy - 0.8 * fh)), int(min(sh, fy + 2.0 * fh))
    xa, xb = int(max(0, fx - 1.2 * fw)), int(min(sw, fx + 2.2 * fw))

    def best_line(prof, lo, hi):
        lo, hi = int(max(0, lo)), int(min(len(prof), hi))
        if hi - lo < 3:
            return None
        seg = prof[lo:hi]
        k = int(np.argmax(seg))
        med = float(np.median(seg)) + 1e-3
        if seg[k] > 2.4 * med and seg[k] > 18:
            return lo + k
        return None

    colprof = ex[ya:yb, :].mean(axis=0)
    # the face box often pokes past the webcam's border (a chin at the bottom edge), so search from inside it
    left = best_line(colprof, fx - 4.0 * fw, fx + 0.12 * fw)
    right = best_line(colprof, fx + 0.88 * fw, fx + 5.0 * fw)
    l_ = left if left is not None else (0 if fx < 2.5 * fw else fx - 1.4 * fw)
    r_ = right if right is not None else (sw if sw - (fx + fw) < 2.5 * fw else fx + 2.4 * fw)
    rowprof = ey[:, int(max(0, l_)):int(min(sw, r_))].mean(axis=1)
    top = best_line(rowprof, fy - 3.0 * fh, fy + 0.12 * fh)
    bot = best_line(rowprof, fy + 0.8 * fh, fy + 4.0 * fh)
    t_ = top if top is not None else (0 if fy < 1.8 * fh else fy - 1.0 * fh)
    b_ = bot if bot is not None else (sh if sh - (fy + fh) < 2.2 * fh else fy + 2.4 * fh)
    w, h = r_ - l_, b_ - t_
    edges = sum(v is not None for v in (left, right, top, bot))
    # sanity: a webcam box is wider than tall-ish and well under half the screen
    if not (0.7 < w / max(1, h) < 2.6) or w * h > 0.6 * sw * sh or w < 1.6 * fw:
        l_, r_ = fx - 1.4 * fw, fx + 2.4 * fw
        t_, b_ = fy - 0.9 * fh, fy + 2.2 * fh
        edges = 0
    l_, t_ = max(0, l_), max(0, t_)
    r_, b_ = min(sw, r_), min(sh, b_)
    return {"box": (l_, t_, r_ - l_, b_ - t_), "edges": edges}


def _debug(path, small, face, cam, s):
    if not path:
        return
    im = small[len(small) // 2].copy()
    if face:
        x, y, w, h = [int(v) for v in face]
        cv2.rectangle(im, (x, y), (x + w, y + h), (0, 230, 255), 2)
    if cam:
        x, y, w, h = [int(v) for v in cam]
        cv2.rectangle(im, (x, y), (x + w, y + h), (80, 255, 80), 3)
    cv2.imwrite(str(path), im, [cv2.IMWRITE_JPEG_QUALITY, 82])


# ------------------------------------------------------------------ crops for each layout

def _even(v):
    return int(v) // 2 * 2


def crops(fa: dict) -> dict:
    """Source-pixel crop boxes for each panel of the chosen layout."""
    W, H = fa["W"], fa["H"]
    lay = fa["layout"]
    if lay == "stack":
        cx, cy, cw, ch = fa["cam"]
        fx, fy, fw, fh = fa["face"]
        want = OUT_W / CAM_H
        if cw / ch > want:      # too wide: keep the face centred
            nw = ch * want
            x = min(max(cx, fx + fw / 2 - nw / 2), cx + cw - nw)
            cam = (x, cy, nw, ch)
        else:                   # too tall: keep the face in the upper part
            nh = cw / want
            y = min(max(cy, fy + fh * 0.45 - nh * 0.42), cy + ch - nh)
            cam = (cx, y, cw, nh)
        gw = H * OUT_W / GAME_H
        cands = [W / 2, W / 2 - 0.12 * W, W / 2 + 0.12 * W]

        def overlap(xc):
            a, b = xc - gw / 2, xc + gw / 2
            return max(0.0, min(b, cx + cw) - max(a, cx)) * max(0.0, min(H, cy + ch) - max(0, cy))

        gx = min(cands, key=lambda xc: (overlap(xc), abs(xc - W / 2)))
        if overlap(gx) > 0.25 * cw * ch and cy > 0.35 * H:
            gh = cy                                   # the game is the part above the webcam
            gw2 = gh * OUT_W / GAME_H
            gx2 = min(max(W / 2 - gw2 / 2, 0), W - gw2)
            return {"cam": tuple(_even(v) for v in cam), "game": tuple(_even(v) for v in (gx2, 0, gw2, gh))}
        gx = min(max(gx - gw / 2, 0), W - gw)
        return {"cam": tuple(_even(v) for v in cam), "game": tuple(_even(v) for v in (gx, 0, gw, H))}
    if lay == "fullcam":
        fx, fy, fw, fh = fa["face"]
        cw = H * OUT_W / OUT_H
        x = min(max(fx + fw / 2 - cw / 2, 0), W - cw)
        return {"full": tuple(_even(v) for v in (x, 0, cw, H))}
    return {}
