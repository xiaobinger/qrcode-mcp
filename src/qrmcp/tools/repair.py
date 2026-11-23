# -*- coding: utf-8 -*-
"""Damaged QR recovery.

Pipeline: finder detection -> module grid fitting -> format information (BCH)
-> data codewords -> Reed-Solomon erasure/Chase decoding -> segment parsing
-> regeneration-based verification. When full correction is impossible the
tool still returns a soft-decoded readable approximation and a rebuilt image.

Scope: QR versions 1-4 (21x21 .. 33x33). Larger versions are reported as
unsupported rather than guessed.
"""
from __future__ import annotations

import base64
import io
import itertools
import math
import os
import string
import time

import numpy as np
from PIL import Image, ImageFilter, ImageOps

from ..errors import UnsupportedError
from ..registry import tool
from ..security import resolve_output_path
from .imaging import load_image
from .qrspec import (ALIGNMENT_CENTERS, FINDER, RS_TABLE, TOTAL_CODEWORDS,
                     best_format, bits_to_text, data_order, format_positions,
                     format_string, function_mask, mask_bit, modules)

try:
    from reedsolo import RSCodec, init_tables, rs_calc_syndromes
    init_tables(prim=0x11d, generator=2)
    _RS_OK = True
except Exception:  # pragma: no cover
    _RS_OK = False

SUPPORTED_VERSIONS = (1, 2, 3, 4)
CHARS = (string.ascii_letters + string.digits + ":/.?=&%+-_~#,;@!$'()* []")


# --------------------------------------------------------------------------
# grid fitting
# --------------------------------------------------------------------------
def _contrast_map(gray: np.ndarray) -> np.ndarray:
    pil = Image.fromarray(gray)
    white = np.asarray(pil.filter(ImageFilter.MaxFilter(9)), dtype=np.float32)
    g = gray.astype(np.float32)
    return np.clip((white - g) / (white + 1.0), 0.0, 1.0)


def _normalise(m: np.ndarray) -> np.ndarray:
    lo, hi = np.percentile(m, [8, 92])
    return np.clip((m - lo) / (hi - lo + 1e-6), 0.0, 1.0)


def _sample(con: np.ndarray, ox: float, oy: float, pitch: float,
            n: int, rad: float = 1.6) -> np.ndarray:
    h, w = con.shape
    out = np.zeros((n, n), dtype=np.float32)
    half = rad / 2.0
    for r in range(n):
        yc = oy + (r + 0.5) * pitch
        y0, y1 = max(0, int(round(yc - half))), min(h, int(round(yc + half)) + 1)
        for c in range(n):
            xc = ox + (c + 0.5) * pitch
            x0, x1 = max(0, int(round(xc - half))), min(w, int(round(xc + half)) + 1)
            if y1 > y0 and x1 > x0:
                out[r, c] = con[y0:y1, x0:x1].mean()
    return out


def _finder_candidates(con: np.ndarray, limit: int = 16):
    """Multi-scale finder detection.

    A finder is 7x7 *modules*, not 7x7 pixels, so the image is resampled to a
    7-pixels-per-module scale for several candidate pitches before matching.
    """
    from numpy.lib.stride_tricks import sliding_window_view
    h, w = con.shape
    if min(h, w) < 8:
        return []
    src = Image.fromarray((con * 255).astype(np.uint8))
    found = []
    for pitch in (2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0):
        f = 7.0 / pitch
        nw, nh = int(w * f), int(h * f)
        if min(nw, nh) < 9:
            continue
        small = np.asarray(src.resize((nw, nh), Image.Resampling.BILINEAR),
                           dtype=np.float32) / 255.0
        win = sliding_window_view(small, (7, 7))
        lo = win.min(axis=(2, 3), keepdims=True)
        hi = win.max(axis=(2, 3), keepdims=True)
        norm = (win - lo) / (hi - lo + 1e-6)
        score = np.abs(norm - FINDER).sum(axis=(2, 3))
        flat = score.ravel()
        for k in np.argsort(flat)[:6]:
            y, x = divmod(int(k), score.shape[1])
            cx, cy = (x + 3.0) / f, (y + 3.0) / f
            if all((cx - p[0]) ** 2 + (cy - p[1]) ** 2 > 25 for p in found):
                found.append((cx, cy, float(flat[k]), pitch))
    found.sort(key=lambda p: p[2])
    return found[:limit]


def _pick_triangle(pts):
    """Find the finder-corner right isosceles triangle (TL, TR, BL)."""
    best = None
    for a, b, c in itertools.combinations(pts, 3):
        pairs = [((a[0]-b[0])**2 + (a[1]-b[1])**2, a, b),
                 ((a[0]-c[0])**2 + (a[1]-c[1])**2, a, c),
                 ((b[0]-c[0])**2 + (b[1]-c[1])**2, b, c)]
        pairs.sort(key=lambda t: t[0])
        s1, s2, s3 = pairs[0][0], pairs[1][0], pairs[2][0]
        if s1 < 100 or s2 < 100:
            continue
        err = 2.0 * abs(s2 / s1 - 1.0) + abs(s3 / s1 - 2.0)
        if best is None or err < best[0]:
            # right-angle vertex = the point shared by the two short edges
            common = set(pairs[0][1:]) & set(pairs[1][1:])
            if len(common) != 1:
                continue
            tl = next(iter(common))
            others = [p for p in (a, b, c) if p is not tl]
            tr = max(others, key=lambda p: p[0])
            bl = min(others, key=lambda p: p[0])
            best = (err, tl, tr, bl, math.sqrt(s1))
    return best


def _expected_function(version: int) -> dict:
    n = modules(version)
    known: dict[tuple[int, int], int] = {}
    for r0, c0 in ((0, 0), (0, n - 7), (n - 7, 0)):
        for i in range(7):
            for j in range(7):
                known[(r0 + i, c0 + j)] = int(FINDER[i, j])
        for k in range(-1, 8):
            for rr, cc in ((r0 + k, c0 - 1), (r0 + k, c0 + 7),
                           (r0 - 1, c0 + k), (r0 + 7, c0 + k)):
                if 0 <= rr < n and 0 <= cc < n and (rr, cc) not in known:
                    known[(rr, cc)] = 0
    for k in range(8, n - 8):
        known[(6, k)] = int(k % 2 == 0)
        known[(k, 6)] = int(k % 2 == 0)
    centers = ALIGNMENT_CENTERS.get(version, [])
    for i, r0 in enumerate(centers):
        for j, c0 in enumerate(centers):
            if (i == 0 and j == 0) or (i == 0 and j == len(centers) - 1) \
                    or (i == len(centers) - 1 and j == 0):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    v = 1 if (dr in (-2, 2) or dc in (-2, 2)
                              or (dr == 0 and dc == 0)) else 0
                    known[(r0 + dr, c0 + dc)] = v
    known[(8, n - 8)] = 1
    return known


def _function_agreement(m: np.ndarray, version: int) -> float:
    known = _expected_function(version)
    if not known:
        return 0.0
    hard = (m > 0.5).astype(int)
    return float(np.mean([hard[r, c] == v for (r, c), v in known.items()]))


def _zxing_quads(gray: np.ndarray):
    """Ask the decoder for symbol quadrilaterals even when decoding fails."""
    import zxingcpp
    from .detect import position_points
    h, w = gray.shape
    quads = []
    for scale in (1, 2, 4):
        arr = gray if scale == 1 else np.asarray(
            Image.fromarray(gray).resize((w * scale, h * scale),
                                         Image.Resampling.LANCZOS))
        try:
            results = zxingcpp.read_barcodes(np.ascontiguousarray(arr),
                                             return_errors=True)
        except Exception:
            continue
        for r in results:
            pts = position_points(r)
            if not pts or len(pts) != 4:
                continue
            p = [[x / scale, y / scale] for x, y in pts]
            xs, ys = [q[0] for q in p], [q[1] for q in p]
            area = (max(xs) - min(xs)) * (max(ys) - min(ys))
            if area >= 100:
                quads.append((area, p))
        if quads:
            break
    quads.sort(key=lambda t: -t[0])
    return [p for _, p in quads[:3]]


def _candidate_grids(con: np.ndarray, quads, versions) -> dict | None:
    best = None
    for quad in quads:
        xs, ys = [p[0] for p in quad], [p[1] for p in quad]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        for v in versions:
            n = modules(v)
            p0 = ((x1 - x0) + (y1 - y0)) / (2.0 * n)
            if p0 < 1.0:
                continue
            for dp in (0.0, -0.025, 0.025, -0.05, 0.05):
                pitch = p0 * (1.0 + dp)
                for dox in (0.0, -0.7, 0.7, -1.4, 1.4):
                    for doy in (0.0, -0.7, 0.7, -1.4, 1.4):
                        m = _normalise(_sample(con, x0 + dox, y0 + doy, pitch, n))
                        agree = _function_agreement(m, v)
                        if best is None or agree > best["agreement"]:
                            best = {"ok": True, "version": v, "modules": n,
                                    "origin": [round(x0 + dox, 3), round(y0 + doy, 3)],
                                    "pitch": round(pitch, 4), "matrix": m,
                                    "agreement": agree,
                                    "quad": [[round(q[0], 1), round(q[1], 1)]
                                             for q in quad]}
    return best


def fit_grid(gray: np.ndarray, version_hint: int | None = None) -> dict:
    con = _contrast_map(gray)
    versions = [version_hint] if version_hint else list(SUPPORTED_VERSIONS)
    quads = _zxing_quads(gray)
    best = _candidate_grids(con, quads, versions)
    source = "decoder-quadrilateral"

    if best is None or best["agreement"] < 0.82:
        pts = _finder_candidates(con)
        tri = _pick_triangle(pts)
        if tri is not None:
            _, tl, tr, bl, side = tri
            fb = _candidate_grids(con, [[list(tl[:2]), list(tr[:2]), list(bl[:2])]],
                                  versions)
            if fb is not None and (best is None or fb["agreement"] > best["agreement"]):
                best, source = fb, "multiscale-finder-detection"
    if best is None:
        return {"ok": False,
                "reason": "could not fit a QR module grid to this image",
                "quad_candidates": len(quads),
                "hint": "crop tightly with qr_detect, or pass version= explicitly"}
    best["source"] = source
    return best


# --------------------------------------------------------------------------
# codewords / Reed-Solomon
# --------------------------------------------------------------------------
def _read_codewords(m: np.ndarray, version: int, mask: int, thr: float):
    order = data_order(version)
    total = TOTAL_CODEWORDS[version]
    bits = []
    conf = []
    for r, c in order[:total * 8]:
        dark = int(m[r, c] > thr)
        bits.append(dark ^ (1 if mask_bit(mask, r, c) else 0))
        conf.append(abs(float(m[r, c]) - 0.5) * 2.0)
    cw = [int("".join(map(str, bits[k * 8:k * 8 + 8])), 2) for k in range(total)]
    return cw, bits, conf, order


def _interleave_map(version: int, ec: str):
    ecc_len, blocks, data_len = RS_TABLE[(version, ec)]
    mapping = []
    for i in range(data_len):
        for b in range(blocks):
            mapping.append((b, i, "data"))
    for i in range(ecc_len):
        for b in range(blocks):
            mapping.append((b, i, "ecc"))
    return mapping, ecc_len, blocks, data_len


def _rs_solve(cw: list[int], conf: list[float], version: int, ec: str,
              max_attempts: int = 600):
    if not _RS_OK:
        return {"ok": False, "reason": "reedsolo is not installed"}
    mapping, ecc_len, blocks, data_len = _interleave_map(version, ec)
    total = TOTAL_CODEWORDS[version]
    rsc = RSCodec(ecc_len, fcr=0, prim=0x11d, generator=2)
    conf_cw = [min(conf[k * 8:k * 8 + 8]) if conf[k * 8:k * 8 + 8] else 0.0
               for k in range(total)]
    block_cw = [[0] * (data_len + ecc_len) for _ in range(blocks)]
    for k, v in enumerate(cw[:total]):
        b, i, part = mapping[k]
        block_cw[b][i if part == "data" else data_len + i] = v

    attempts = 0
    tried: set = set()
    for ct in (0.0, 0.04, 0.10, 0.18, 0.30, 0.45, 0.65):
        base = [set() for _ in range(blocks)]
        for k, c in enumerate(conf_cw):
            if c < ct and k < len(mapping):
                b, i, part = mapping[k]
                base[b].add(i if part == "data" else data_len + i)
        extras = [()] + [(data_len + i,) for i in range(ecc_len)]
        if ecc_len <= 20:
            extras += list(itertools.combinations(range(data_len, data_len + ecc_len), 2))
        for extra in extras:
            for only in (True, False):
                corrected = []
                good = True
                for b in range(blocks):
                    er = sorted(base[b] | set(extra))
                    if len(er) > ecc_len:
                        good = False
                        break
                    key = (b, tuple(er), only)
                    if key in tried:
                        good = False
                        break
                    tried.add(key)
                    attempts += 1
                    if attempts > max_attempts:
                        return {"ok": False, "reason": "attempt limit reached",
                                "attempts": attempts}
                    try:
                        d, e, _ = rsc.decode(bytes(block_cw[b]), erase_pos=er,
                                             only_erasures=only)
                    except Exception:
                        good = False
                        break
                    full = bytearray(list(d) + list(e))
                    if any(x != 0 for x in rs_calc_syndromes(
                            full, ecc_len, fcr=0, generator=2)):
                        good = False
                        break
                    corrected.append(list(d))
                if not good or len(corrected) != blocks:
                    continue
                data_bits = []
                for i in range(data_len):
                    for b in range(blocks):
                        v = corrected[b][i]
                        data_bits += [(v >> (7 - j)) & 1 for j in range(8)]
                parsed = bits_to_text(data_bits)
                if parsed["ok"] and parsed["text"]:
                    return {"ok": True, "text": parsed["text"],
                            "segments": parsed["segments"],
                            "attempts": attempts, "confidence_threshold": ct,
                            "erased": [len(base[b] | set(extra)) for b in range(blocks)],
                            "only_erasures": only}
    return {"ok": False, "reason": "Reed-Solomon correction did not converge",
            "attempts": attempts}


# --------------------------------------------------------------------------
# structure-guided Reed-Solomon solving
# --------------------------------------------------------------------------
def _char_scores(m, order, idx, mask, limit=8):
    if idx + 8 > len(order):
        return []
    obs = []
    for r, c in order[idx:idx + 8]:
        p = float(np.clip(m[r, c], 0.03, 0.97))
        obs.append(1.0 - p if mask_bit(mask, r, c) else p)
    out = []
    for ch in CHARS:
        v = ord(ch)
        ll = sum(math.log(p if (v >> (7 - j)) & 1 else 1.0 - p)
                 for j, p in enumerate(obs))
        out.append((ll, ch))
    out.sort(reverse=True)
    return out[:limit]


def _group_scores(m, order, idx, nbits, maxval, mask, limit=6):
    if idx + nbits > len(order):
        return []
    obs = []
    for r, c in order[idx:idx + nbits]:
        p = float(np.clip(m[r, c], 0.03, 0.97))
        obs.append(1.0 - p if mask_bit(mask, r, c) else p)
    out = []
    for v in range(maxval + 1):
        ll = sum(math.log(p if (v >> (nbits - 1 - j)) & 1 else 1.0 - p)
                 for j, p in enumerate(obs))
        out.append((ll, v))
    out.sort(reverse=True)
    return out[:limit]


def _guided_solve(m, cw_recv, order, version: int, ec: str, mask: int,
                  max_attempts: int = 400):
    """Use the reliably-read parts of the bitstream as known RS constraints.

    When glare destroys more codewords than the EC budget, plain erasure
    decoding cannot converge. Fixing mode/count/padding bits plus the
    high-confidence characters shrinks the unknown set enough for the
    Reed-Solomon decoder to solve the remaining bytes exactly.
    """
    if not _RS_OK:
        return None
    ecc_len, blocks, data_len = RS_TABLE[(version, ec)]
    if blocks != 1:
        return None  # interleaved multi-block layouts need a different mapper
    cap = data_len * 8
    ideal: list[int | None] = [None] * cap

    def put(pos, val, n):
        for k in range(n):
            if pos + k < cap:
                ideal[pos + k] = (val >> (n - 1 - k)) & 1

    raw_bits = [(v >> (7 - j)) & 1 for v in cw_recv[:data_len] for j in range(8)]
    parsed = bits_to_text(raw_bits)
    pos = 0
    ok_segments = 0
    for seg in parsed["segments"]:
        mode = seg["mode"]
        if mode == "terminator":
            put(pos, 0, 4)
            pos += 4
            break
        if mode == "byte":
            put(pos, 0b0100, 4)
            put(pos + 4, seg["count"], 8)
            pos += 12
            for k in range(min(seg["count"], 200)):
                sc = _char_scores(m, order, seg["bit_start"] + 12 + 8 * k, mask)
                if len(sc) >= 2 and sc[0][0] - sc[1][0] >= 1.5:
                    put(pos, ord(sc[0][1]), 8)
                pos += 8
            ok_segments += 1
        elif mode == "numeric":
            put(pos, 0b0001, 4)
            put(pos + 4, seg["count"], 10)
            pos += 14
            k, count = 0, seg["count"]
            while k + 3 <= count:
                sc = _group_scores(m, order, seg["bit_start"] + 14 + 10 * (k // 3),
                                   10, 999, mask)
                if len(sc) >= 2 and sc[0][0] - sc[1][0] >= 1.5:
                    put(pos, sc[0][1], 10)
                pos += 10
                k += 3
            rem = count - k
            if rem == 1:
                sc = _group_scores(m, order, seg["bit_start"] + 14 + 10 * (k // 3),
                                   4, 9, mask)
                if len(sc) >= 2 and sc[0][0] - sc[1][0] >= 1.5:
                    put(pos, sc[0][1], 4)
                pos += 4
            elif rem == 2:
                sc = _group_scores(m, order, seg["bit_start"] + 14 + 10 * (k // 3),
                                   7, 99, mask)
                if len(sc) >= 2 and sc[0][0] - sc[1][0] >= 1.5:
                    put(pos, sc[0][1], 7)
                pos += 7
            ok_segments += 1
        else:
            return None
    if ok_segments == 0 or pos >= cap:
        return None
    # terminator + byte alignment + standard pad codewords
    put(pos, 0, 4)
    pos += 4
    while pos % 8:
        put(pos, 0, 1)
        pos += 1
    pad = (0xEC, 0x11)
    while pos < cap:
        put(pos, pad[(pos // 8) % 2], 8)
        pos += 8

    known = [(i, b) for i, b in enumerate(ideal) if b is not None]
    er = sorted({i // 8 for i, b in enumerate(ideal) if b is None})
    data_cw = [int("".join(str(b if b is not None else 0)
                           for b in ideal[k * 8:k * 8 + 8]), 2)
               for k in range(data_len)]
    ecc_recv = cw_recv[data_len:data_len + ecc_len]
    rsc = RSCodec(ecc_len, fcr=0, prim=0x11d, generator=2)
    extras = [()] + [(data_len + i,) for i in range(ecc_len)]
    extras += list(itertools.combinations(range(data_len, data_len + ecc_len), 2))
    attempts = 0
    for extra in extras:
        if attempts > max_attempts:
            break
        er_all = sorted(set(er) | set(extra))
        if len(er_all) > ecc_len:
            continue
        for only in (True, False):
            attempts += 1
            try:
                d, e, _ = rsc.decode(bytes(data_cw + ecc_recv),
                                     erase_pos=er_all, only_erasures=only)
            except Exception:
                continue
            full = bytearray(list(d) + list(e))
            if any(x != 0 for x in rs_calc_syndromes(
                    full, ecc_len, fcr=0, generator=2)):
                continue
            solved = [(v >> (7 - j)) & 1 for v in d for j in range(8)]
            if any(solved[i] != b for i, b in known):
                continue
            text = bits_to_text(solved)
            if text["ok"] and text["text"]:
                return {"text": text["text"], "segments": text["segments"],
                        "attempts": attempts, "erased": len(er_all),
                        "known_bits": len(known)}
    return None


# --------------------------------------------------------------------------
# verification / soft text / clean image
# --------------------------------------------------------------------------
def _candidate_matrices(text: str, version: int, ec: str, mask: int):
    """Both explicit multi-segment encoding and segno's single-segment encoding.

    A real-world QR may have been produced by either, so verification must try
    both instead of assuming one segmentation strategy.
    """
    from .qrspec import encode_payload
    out = []
    try:
        b, _ = encode_payload(text, version, ec, mask)
        out.append(("multi-segment", b))
    except Exception:
        pass
    try:
        import segno
        q = segno.make(text, version=version, error=ec.lower(), mask=mask,
                       boost_error=False)
        out.append(("single-segment", np.array(
            [[1 if v else 0 for v in row] for row in q.matrix])))
    except Exception:
        pass
    return out


def _rebuild_matrix(text: str, version: int, ec: str, mask: int):
    candidates = _candidate_matrices(text, version, ec, mask)
    if not candidates:
        raise ValueError("payload cannot be encoded at the detected version/EC")
    return candidates[0][1]


def _verify(text: str, m: np.ndarray, version: int, ec: str, mask: int) -> dict:
    best = None
    reliable = np.abs(m - 0.5) > 0.32
    hard = (m > 0.5).astype(int)
    for label, b in _candidate_matrices(text, version, ec, mask):
        if b.shape != m.shape:
            continue
        mismatch = int(np.sum(reliable & (b != hard)))
        total = int(reliable.sum())
        agreement = 1.0 - mismatch / max(total, 1)
        item = {"valid": agreement >= 0.92, "agreement": round(agreement, 4),
                "reliable_modules": total, "mismatches": mismatch,
                "encoding": label}
        if best is None or agreement > best["agreement"]:
            best = item
    return best or {"valid": False,
                    "reason": "payload cannot be encoded at the detected version/EC"}


def _soft_text(m: np.ndarray, bits: list[int], order, version: int,
               mask: int) -> dict:
    """Best-effort readable text from per-module soft values."""
    parsed = bits_to_text(bits)
    soft_chars: list[str] = []
    alternatives = []
    for seg in parsed["segments"]:
        if seg["mode"] != "byte":
            continue
        for k in range(min(seg["count"], 200)):
            idx = seg["bit_start"] + 12 + 8 * k
            if idx + 8 > len(order):
                break
            mods = order[idx:idx + 8]
            obs = []
            for r, c in mods:
                p = float(np.clip(m[r, c], 0.03, 0.97))
                if mask_bit(mask, r, c):
                    p = 1.0 - p
                obs.append(p)
            scores = []
            for ch in CHARS:
                v = ord(ch)
                ll = 0.0
                for j, p in enumerate(obs):
                    bit = (v >> (7 - j)) & 1
                    ll += math.log(p if bit else 1.0 - p)
                scores.append((ll, ch))
            scores.sort(reverse=True)
            soft_chars.append(scores[0][1])
            alternatives.append({"index": k, "best": scores[0][1],
                                 "top": [c for _, c in scores[:4]],
                                 "margin": round(scores[0][0] - scores[1][0], 2)})
    return {"raw_text": parsed["text"], "soft_text": "".join(soft_chars),
            "segments": parsed["segments"], "char_alternatives": alternatives,
            "parse_ok": parsed["ok"], "parse_errors": parsed["errors"]}


def _clean_image(b: np.ndarray, save: bool) -> dict:
    img = (1 - b).astype(np.uint8) * 255
    scale, qz = 12, 4
    pil = Image.fromarray(img).resize((b.shape[0] * scale, b.shape[1] * scale),
                                      Image.Resampling.NEAREST)
    pil = ImageOps.expand(pil, border=qz * scale, fill=255)
    buf = io.BytesIO()
    pil.save(buf, format="PNG")
    data = buf.getvalue()
    out = {"data_uri": "data:image/png;base64," + base64.b64encode(data).decode("ascii"),
           "width": pil.width, "height": pil.height, "path": None}
    if save:
        path = resolve_output_path("qr_repair.png")
        with open(path, "wb") as fh:
            fh.write(data)
        out["path"] = path
    return out


# --------------------------------------------------------------------------
# tool
# --------------------------------------------------------------------------
@tool(
    "qr_repair",
    "Recover a damaged QR code (glare, blur, low resolution, partial occlusion). "
    "Fits the module grid from the finder patterns, decodes format information via BCH, "
    "then applies Reed-Solomon erasure/Chase correction. Returns the recovered text, "
    "a verification score and a rebuilt clean QR image. Supports QR versions 1-4; "
    "larger versions are reported as unsupported. hint= accepts a suspected full text "
    "to verify against the damaged modules.",
    {
        "image": {"type": "string", "description": "Local path, base64, data URI or http(s) URL"},
        "version": {"type": ["integer", "null"], "minimum": 1, "maximum": 4,
                    "default": None, "description": "Force QR version 1-4, or auto-detect"},
        "ec": {"type": ["string", "null"], "enum": ["L", "M", "Q", "H", None],
               "default": None, "description": "Force error-correction level"},
        "mask": {"type": ["integer", "null"], "minimum": 0, "maximum": 7,
                 "default": None, "description": "Force mask 0-7"},
        "hint": {"type": ["string", "null"], "default": None,
                 "description": "Suspected complete payload; verified by regenerating the QR"},
        "max_attempts": {"type": "integer", "minimum": 50, "maximum": 5000,
                         "default": 700, "description": "Cap on Reed-Solomon attempts"},
        "save_clean": {"type": "boolean", "default": True,
                       "description": "Write the rebuilt QR PNG to the output directory"},
    },
    ("image",),
)
def qr_repair(image: str, version: int | None = None, ec: str | None = None,
              mask: int | None = None, hint: str | None = None,
              max_attempts: int = 700, save_clean: bool = True) -> dict:
    started = time.time()
    loaded = load_image(image)
    fit = fit_grid(loaded.gray, version)
    if not fit.get("ok"):
        return {"ok": False, "stage": "grid", **fit,
                "warnings": ["try cropping the QR tightly with qr_detect first"]}
    v = fit["version"]
    if v not in SUPPORTED_VERSIONS or (v, ec or "L") not in RS_TABLE:
        raise UnsupportedError(
            "repair supports QR versions 1-4 only",
            {"detected_version": v, "modules": fit["modules"]})

    m = fit["matrix"]
    fmt = best_format(m, v)
    ec_final = ec or fmt["ec"]
    mask_final = mask if mask is not None else fmt["mask"]
    if (v, ec_final) not in RS_TABLE:
        raise UnsupportedError("unsupported version/EC combination",
                               {"version": v, "ec": ec_final})

    out: dict = {
        "ok": False,
        "stage": None,
        "grid": {k: fit[k] for k in ("version", "modules", "origin", "pitch",
                                     "agreement", "quad", "source") if k in fit},
        "format": {"ec": ec_final, "mask": mask_final,
                   "detected_ec": fmt["ec"], "detected_mask": fmt["mask"],
                   "method": fmt.get("method"),
                   "format_bit_distance": fmt["distance"],
                   "copy1_errors": fmt.get("copy1_errors"),
                   "copy2_errors": fmt.get("copy2_errors"),
                   "format_copy1": fmt["observed_copy1"],
                   "format_copy2": fmt["observed_copy2"]},
        "warnings": [],
        "elapsed_ms": None,
    }
    if fmt["distance"] > 3:
        out["warnings"].append(
            "format information is unreliable; EC level and mask are best-effort")

    final_text = None
    rebuilt = None

    # 1. hint verification (most reliable when the caller has a candidate)
    if hint:
        chk = _verify(hint, m, v, ec_final, mask_final)
        out["hint_verification"] = chk
        if chk.get("valid"):
            final_text = hint
            out["source"] = "hint-verified"
            rebuilt = _rebuild_matrix(hint, v, ec_final, mask_final)

    # 2. automatic Reed-Solomon correction
    if final_text is None:
        solutions = []
        for thr in (0.40, 0.45, 0.50, 0.35, 0.55):
            cw, bits, conf, order = _read_codewords(m, v, mask_final, thr)
            rs = _rs_solve(cw, conf, v, ec_final, max_attempts=max_attempts)
            if rs.get("ok"):
                chk = _verify(rs["text"], m, v, ec_final, mask_final)
                if chk.get("valid"):
                    solutions.append((rs, chk, thr))
        out["rs_diagnostics"] = {"solutions": len(solutions)}
        if solutions:
            rs, chk, thr = solutions[0]
            final_text = rs["text"]
            out["source"] = "rs-correction"
            out["rs_diagnostics"].update({
                "threshold": thr, "attempts": rs["attempts"],
                "erased_per_block": rs["erased"],
                "verification": chk, "segments": rs["segments"]})
            rebuilt = _rebuild_matrix(final_text, v, ec_final, mask_final)
        else:
            # 3. structure-guided RS solve (fixes mode/count/padding + confident chars)
            cw, bits, conf, order = _read_codewords(m, v, mask_final, 0.45)
            guided = None
            try:
                guided = _guided_solve(m, cw, order, v, ec_final, mask_final,
                                       max_attempts=min(max_attempts, 600))
            except Exception as exc:
                out["warnings"].append(f"guided solve skipped: {type(exc).__name__}: {exc}")
            out["guided_diagnostics"] = {
                "solved": bool(guided),
                "scope": "single RS block (V1-V2 all EC levels, V3-L/M, V4-L)",
                "note": ("if damaged codewords exceed the EC budget, automatic solving "
                         "is mathematically impossible; pass hint= with a suspected "
                         "payload to verify it and obtain a scannable rebuild"),
            }
            candidates: list[str] = []
            if guided:
                chk = _verify(guided["text"], m, v, ec_final, mask_final)
                out["guided_diagnostics"].update({
                    "attempts": guided["attempts"], "erased": guided["erased"],
                    "known_bits": guided["known_bits"],
                    "verification": chk, "candidate": guided["text"]})
                if chk.get("valid"):
                    final_text = guided["text"]
                    out["source"] = "structure-guided"
                    out["rs_diagnostics"]["solutions"] = 1
                    out["rs_diagnostics"]["verification"] = chk
                    rebuilt = _rebuild_matrix(final_text, v, ec_final, mask_final)
                else:
                    candidates.append(guided["text"])

            # 4. soft-decode fallback: readable approximation + candidates
            if final_text is None:
                soft = _soft_text(m, bits, order, v, mask_final)
                out["soft_decode"] = soft
                out["candidates"] = candidates
                out["stage"] = "soft-fallback"
                out["warnings"].append(
                    "full error correction failed; soft_decode contains a best-effort "
                    "reading. Pass a corrected candidate via hint= to verify it and "
                    "obtain a scannable rebuilt QR.")
                out["ambiguous_modules"] = int(np.sum(np.abs(m - 0.5) < 0.25))

    if final_text is not None:
        out["ok"] = True
        out["stage"] = "recovered"
        out["text"] = final_text
        from .parse import parse_content
        out["parse"] = parse_content(final_text)
        chk = out.get("hint_verification") or out.get("rs_diagnostics", {}).get("verification", {})
        out["verification"] = chk
        if rebuilt is None:
            rebuilt = _rebuild_matrix(final_text, v, ec_final, mask_final)
    else:
        rebuilt = (m > 0.45).astype(int)
        try:
            ref = _rebuild_matrix("A", v, ec_final, mask_final)
            fmask = function_mask(v)
            rebuilt[fmask] = ref[fmask]
        except Exception:
            for (r, c), val in _expected_function(v).items():
                rebuilt[r, c] = val

    out["clean_image"] = _clean_image(rebuilt, save_clean)
    out["elapsed_ms"] = int((time.time() - started) * 1000)
    out["scope"] = "QR versions 1-4 (21x21 .. 33x33), all EC levels and masks"
    return out
