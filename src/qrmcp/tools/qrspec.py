# -*- coding: utf-8 -*-
"""Minimal QR Code specification helpers for damaged-code repair.

Supports QR versions 1-4 (single and multi RS blocks), all four EC levels and
all eight mask patterns. Anything larger is reported as unsupported instead
of being guessed.
"""
from __future__ import annotations

import numpy as np

EC_ORDER = ("L", "M", "Q", "H")
EC_FORMAT_CODE = {"L": 1, "M": 0, "Q": 3, "H": 2}

# (version, ec) -> (ecc codewords per block, number of blocks, data codewords per block)
RS_TABLE = {
    (1, "L"): (7, 1, 19), (1, "M"): (10, 1, 16),
    (1, "Q"): (13, 1, 13), (1, "H"): (17, 1, 9),
    (2, "L"): (10, 1, 34), (2, "M"): (16, 1, 28),
    (2, "Q"): (22, 1, 22), (2, "H"): (28, 1, 16),
    (3, "L"): (15, 1, 55), (3, "M"): (26, 1, 44),
    (3, "Q"): (18, 2, 17), (3, "H"): (22, 2, 13),
    (4, "L"): (20, 1, 80), (4, "M"): (18, 2, 32),
    (4, "Q"): (26, 2, 24), (4, "H"): (16, 4, 9),
}

TOTAL_CODEWORDS = {1: 26, 2: 44, 3: 70, 4: 100}
ALIGNMENT_CENTERS = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26]}

FINDER = np.array([
    [1, 1, 1, 1, 1, 1, 1],
    [1, 0, 0, 0, 0, 0, 1],
    [1, 0, 1, 1, 1, 0, 1],
    [1, 0, 1, 1, 1, 0, 1],
    [1, 0, 1, 1, 1, 0, 1],
    [1, 0, 0, 0, 0, 0, 1],
    [1, 1, 1, 1, 1, 1, 1]], dtype=np.uint8)


def modules(version: int) -> int:
    return 17 + 4 * version


def mask_bit(mask: int, r: int, c: int) -> bool:
    if mask == 0:
        return (r + c) % 2 == 0
    if mask == 1:
        return r % 2 == 0
    if mask == 2:
        return c % 3 == 0
    if mask == 3:
        return (r + c) % 3 == 0
    if mask == 4:
        return (r // 2 + c // 3) % 2 == 0
    if mask == 5:
        return (r * c) % 2 + (r * c) % 3 == 0
    if mask == 6:
        return ((r * c) % 2 + (r * c) % 3) % 2 == 0
    if mask == 7:
        return ((r + c) % 2 + (r * c) % 3) % 2 == 0
    raise ValueError("invalid mask %r" % mask)


def function_mask(version: int) -> np.ndarray:
    n = modules(version)
    f = np.zeros((n, n), dtype=bool)
    finder_centers = ((3, 3), (3, n - 4), (n - 4, 3))

    # finder patterns + one-module separators
    for r0, c0 in ((0, 0), (0, n - 7), (n - 7, 0)):
        for i in range(-1, 8):
            for j in range(-1, 8):
                rr, cc = r0 + i, c0 + j
                if 0 <= rr < n and 0 <= cc < n:
                    f[rr, cc] = True

    # timing patterns
    for k in range(8, n - 8):
        f[6, k] = True
        f[k, 6] = True

    # alignment patterns (skip the three overlapping finder corners)
    centers = ALIGNMENT_CENTERS.get(version, [])
    for i, r0 in enumerate(centers):
        for j, c0 in enumerate(centers):
            if (i == 0 and j == 0) or (i == 0 and j == len(centers) - 1) \
                    or (i == len(centers) - 1 and j == 0):
                continue
            if (r0, c0) in finder_centers:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    f[r0 + dr, c0 + dc] = True

    # format information areas
    for i in range(9):
        f[8, i] = True
        f[i, 8] = True
    for i in range(8):
        f[8, n - 1 - i] = True
        f[n - 1 - i, 8] = True
    f[8, n - 8] = True  # fixed dark module
    return f


def data_order(version: int) -> list[tuple[int, int]]:
    n = modules(version)
    f = function_mask(version)
    order: list[tuple[int, int]] = []
    col = n - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(n - 1, -1, -1) if upward else range(n)
        for r in rows:
            for c in (col, col - 1):
                if not f[r, c]:
                    order.append((r, c))
        upward = not upward
        col -= 2
    return order


def format_string(ec: str, mask: int) -> int:
    """15-bit format information (BCH(15,5) + 0x5412 mask)."""
    data = (EC_FORMAT_CODE[ec] << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ (((rem >> 9) & 1) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


def format_positions(version: int):
    n = modules(version)
    p1 = ([(8, i) for i in range(6)] + [(8, 7), (8, 8), (7, 8)]
          + [(5 - i, 8) for i in range(6)])
    p2 = [(n - 1 - i, 8) for i in range(8)] + [(8, n - 7 + i) for i in range(7)]
    return p1, p2


def best_format(bits_matrix: np.ndarray, version: int):
    """Decode EC level and mask by comparing with regenerated reference matrices.

    A damaged photograph often corrupts several format bits, which defeats a
    plain BCH Hamming-distance search. Comparing against all 32 legal
    (EC, mask) reference QR matrices is far more robust; the two format copies
    are then reported for diagnostics.
    """
    import segno

    p1, p2 = format_positions(version)
    refs = {}
    for ec in EC_ORDER:
        for mask in range(8):
            try:
                q = segno.make("A", version=version, error=ec.lower(),
                               mask=mask, boost_error=False)
            except Exception:
                continue
            refs[(ec, mask)] = np.array(
                [[1 if v else 0 for v in row] for row in q.matrix])

    candidates = []
    for thr in (0.35, 0.40, 0.45, 0.50, 0.55, 0.60):
        hard = (np.asarray(bits_matrix) > thr).astype(int)
        for (ec, mask), q in refs.items():
            d1 = sum(int(q[r, c]) != hard[r, c] for r, c in p1)
            d2 = sum(int(q[r, c]) != hard[r, c] for r, c in p2)
            candidates.append((d1 + d2, ec, mask, d1, d2, thr))
    candidates.sort(key=lambda x: (x[0], x[5]))
    if not candidates:
        return {"ec": "L", "mask": 0, "distance": 99, "method": "fallback",
                "observed_copy1": "", "observed_copy2": ""}
    dist, ec, mask, d1, d2, thr = candidates[0]
    hard = (np.asarray(bits_matrix) > 0.5).astype(int)
    return {
        "ec": ec, "mask": mask, "distance": int(dist),
        "copy1_errors": int(d1), "copy2_errors": int(d2),
        "threshold": thr, "method": "reference-matrix",
        "runner_up": candidates[1][:3],
        "bch_string": "".join(str((format_string(ec, mask) >> (14 - i)) & 1)
                              for i in range(15)),
        "observed_copy1": "".join(str(int(hard[r, c])) for r, c in p1),
        "observed_copy2": "".join(str(int(hard[r, c])) for r, c in p2),
    }


def deinterleave(codewords: list[int], version: int, ec: str):
    """Split interleaved codewords into (data, ecc) per RS block."""
    ecc_len, blocks, data_len = RS_TABLE[(version, ec)]
    data: list[list[int]] = [[] for _ in range(blocks)]
    ecc: list[list[int]] = [[] for _ in range(blocks)]
    idx = 0
    for i in range(data_len):
        for b in range(blocks):
            if idx < len(codewords):
                data[b].append(codewords[idx])
            idx += 1
    for i in range(ecc_len):
        for b in range(blocks):
            if idx < len(codewords):
                ecc[b].append(codewords[idx])
            idx += 1
    return data, ecc


def bits_to_text(bits) -> dict:
    """Parse a QR data bitstream into segments and decoded text."""
    pos = 0
    n = len(bits)
    segments = []
    parts: list[str] = []
    alnum = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:"
    errors = []

    def take(k):
        nonlocal pos
        if pos + k > n:
            raise ValueError("bitstream truncated")
        v = "".join(str(int(x)) for x in bits[pos:pos + k])
        pos += k
        return int(v, 2) if k else 0

    try:
        for _ in range(16):
            start = pos
            mode = take(4)
            if mode == 0:
                segments.append({"mode": "terminator", "bit_start": start})
                break
            if mode == 1:  # numeric
                count = take(10)
                text = ""
                for _ in range(count // 3):
                    text += str(take(10)).zfill(3)
                if count % 3 == 1:
                    text += str(take(4))
                elif count % 3 == 2:
                    text += str(take(7)).zfill(2)
                segments.append({"mode": "numeric", "count": count,
                                 "text": text, "bit_start": start})
                parts.append(text)
            elif mode == 2:  # alphanumeric
                count = take(9)
                text = ""
                for _ in range(count // 2):
                    v = take(11)
                    text += alnum[v // 45] + alnum[v % 45]
                if count % 2:
                    text += alnum[take(6)]
                segments.append({"mode": "alnum", "count": count, "text": text,
                                 "bit_start": start})
                parts.append(text)
            elif mode == 4:  # byte
                count = take(8)
                raw = bytearray()
                for _ in range(count):
                    raw.append(take(8))
                text = raw.decode("utf-8", errors="replace")
                segments.append({"mode": "byte", "count": count, "text": text,
                                 "hex": raw.hex(), "bit_start": start})
                parts.append(text)
            elif mode == 7:  # ECI
                take(8)
                segments.append({"mode": "eci"})
            elif mode == 8:  # kanji
                count = take(8)
                take(13 * count)
                segments.append({"mode": "kanji", "count": count, "text": ""})
            else:
                errors.append(f"unknown mode {mode} at bit {pos - 4}")
                break
        else:
            errors.append("too many segments")
    except ValueError as exc:
        errors.append(str(exc))
    return {"text": "".join(parts), "segments": segments,
            "ok": not errors, "errors": errors, "bits_consumed": pos}


def segment_text(text: str) -> list[tuple[str, str]]:
    """Heuristic segmentation: long digit runs become numeric segments.

    This mirrors how real-world QR generators encode URLs containing serial
    numbers (e.g. ``?sn=00005702880412383550``), which a single-byte-segment
    encoder cannot fit into the same version.
    """
    out: list[tuple[str, str]] = []
    buf = ""
    i = 0
    while i < len(text):
        if "0" <= text[i] <= "9":
            j = i
            while j < len(text) and "0" <= text[j] <= "9":
                j += 1
            digits = text[i:j]
            if len(digits) >= 4:
                if buf:
                    out.append(("byte", buf))
                    buf = ""
                out.append(("numeric", digits))
            else:
                buf += digits
            i = j
        else:
            buf += text[i]
            i += 1
    if buf:
        out.append(("byte", buf))
    return out


def _bits(v: int, n: int) -> list[int]:
    return [(v >> (n - 1 - k)) & 1 for k in range(n)]


def encode_payload(text: str, version: int, ec: str, mask: int):
    """Full QR encoding with explicit multi-segment support.

    Returns the NxN module matrix and the interleaved codeword list. Used for
    hint verification and for rebuilding a clean, scannable QR image.
    """
    from reedsolo import RSCodec, init_tables
    init_tables(prim=0x11d, generator=2)
    ecc_len, blocks, data_len = RS_TABLE[(version, ec)]

    bits: list[int] = []
    for kind, val in segment_text(text):
        if kind == "byte":
            raw = val.encode("utf-8")
            if len(raw) > 255:
                raise ValueError("byte segment exceeds 255 bytes (versions 1-4)")
            bits += _bits(0b0100, 4) + _bits(len(raw), 8)
            for b in raw:
                bits += _bits(b, 8)
        else:
            bits += _bits(0b0001, 4) + _bits(len(val), 10)
            k = 0
            while k + 3 <= len(val):
                bits += _bits(int(val[k:k + 3]), 10)
                k += 3
            rem = len(val) - k
            if rem == 1:
                bits += _bits(int(val[k]), 4)
            elif rem == 2:
                bits += _bits(int(val[k:]), 7)
    bits += [0] * 4
    while len(bits) % 8:
        bits.append(0)
    cap = data_len * blocks * 8
    if len(bits) > cap:
        raise ValueError("payload does not fit version %d / EC %s" % (version, ec))
    pad = (0xEC, 0x11)
    while len(bits) < cap:
        bits += _bits(pad[(len(bits) // 8) % 2], 8)
    data_cw = [int("".join(map(str, bits[k * 8:k * 8 + 8])), 2)
               for k in range(data_len * blocks)]
    rsc = RSCodec(ecc_len, fcr=0, prim=0x11d, generator=2)
    blocks_data = [data_cw[b * data_len:(b + 1) * data_len] for b in range(blocks)]
    blocks_ecc = [list(bytearray(rsc.encode(bytes(d)))[-ecc_len:])
                  for d in blocks_data]
    cw: list[int] = []
    for i in range(data_len):
        for b in range(blocks):
            cw.append(blocks_data[b][i])
    for i in range(ecc_len):
        for b in range(blocks):
            cw.append(blocks_ecc[b][i])

    n = modules(version)
    f = function_mask(version)
    import segno
    q = segno.make("A", version=version, error=ec.lower(), mask=mask,
                   boost_error=False)
    ref = np.array([[1 if v else 0 for v in row] for row in q.matrix])
    b = np.zeros((n, n), dtype=int)
    b[f] = ref[f]
    order = data_order(version)
    stream = [(v >> (7 - j)) & 1 for v in cw for j in range(8)]
    for k, (r, c) in enumerate(order):
        bit = stream[k] if k < len(stream) else 0
        b[r, c] = bit ^ (1 if mask_bit(mask, r, c) else 0)
    return b, cw


def text_to_bits(text: str, version: int) -> list[int] | None:
    """Re-encode a byte-mode payload with the same segment layout segno would use.

    Only used for hint verification; returns None when the payload cannot be
    represented as a single byte segment.
    """
    raw = text.encode("utf-8")
    if len(raw) > 255:
        return None
    bits = [(0b0100 >> (3 - i)) & 1 for i in range(4)]
    bits += [(len(raw) >> (7 - i)) & 1 for i in range(8)]
    for byte in raw:
        bits += [(byte >> (7 - i)) & 1 for i in range(8)]
    bits += [0] * 4
    while len(bits) % 8:
        bits.append(0)
    cap = TOTAL_CODEWORDS[version] * 8
    pad = [0xEC, 0x11]
    while len(bits) < cap:
        v = pad[(len(bits) // 8) % 2]
        bits += [(v >> (7 - i)) & 1 for i in range(8)]
    return bits[:cap]
