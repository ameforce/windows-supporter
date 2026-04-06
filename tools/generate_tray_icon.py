from __future__ import annotations

import math
import struct
import zlib


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    if edge0 == edge1:
        return 0.0
    t = _clamp((x - edge0) / (edge1 - edge0))
    return t * t * (3.0 - 2.0 * t)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _mix_rgb(c1: tuple[int, int, int], c2: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    tt = _clamp(t)
    r = int(_lerp(c1[0], c2[0], tt) + 0.5)
    g = int(_lerp(c1[1], c2[1], tt) + 0.5)
    b = int(_lerp(c1[2], c2[2], tt) + 0.5)
    return r, g, b


def _sd_round_rect(px: float, py: float, hx: float, hy: float, r: float) -> float:
    qx = abs(px) - hx
    qy = abs(py) - hy
    ax = max(qx, 0.0)
    ay = max(qy, 0.0)
    outside = math.hypot(ax, ay)
    inside = min(max(qx, qy), 0.0)
    return outside + inside - r


def _alpha_from_sd(sd: float, aa: float = 1.0) -> float:
    return 1.0 - _smoothstep(0.0, aa, sd)


def _dist_to_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    vx = bx - ax
    vy = by - ay
    wx = px - ax
    wy = py - ay
    vv = vx * vx + vy * vy
    if vv <= 1e-9:
        return math.hypot(px - ax, py - ay)
    t = (wx * vx + wy * vy) / vv
    if t <= 0.0:
        return math.hypot(px - ax, py - ay)
    if t >= 1.0:
        return math.hypot(px - bx, py - by)
    cx = ax + t * vx
    cy = ay + t * vy
    return math.hypot(px - cx, py - cy)


def _blend(dst: tuple[int, int, int, int], src: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    dr, dg, db, da = dst
    sr, sg, sb, sa = src
    if sa <= 0:
        return dst
    if da <= 0:
        return src

    sa_f = sa / 255.0
    da_f = da / 255.0
    out_a = sa_f + da_f * (1.0 - sa_f)
    if out_a <= 1e-9:
        return 0, 0, 0, 0
    out_r = (sr * sa_f + dr * da_f * (1.0 - sa_f)) / out_a
    out_g = (sg * sa_f + dg * da_f * (1.0 - sa_f)) / out_a
    out_b = (sb * sa_f + db * da_f * (1.0 - sa_f)) / out_a
    return (
        int(out_r + 0.5),
        int(out_g + 0.5),
        int(out_b + 0.5),
        int(out_a * 255.0 + 0.5),
    )


def _png_bytes(w: int, h: int, rgba: bytes) -> bytes:
    stride = w * 4
    raw = bytearray()
    off = 0
    for _ in range(h):
        raw.append(0)
        raw.extend(rgba[off : off + stride])
        off += stride

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw), 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _render_rgba(size: int) -> bytes:
    w = h = int(size)
    cx = w / 2.0
    cy = h / 2.0

    radius = max(2.0, size * 0.22)
    half = (size / 2.0) - 0.5
    hx = max(0.0, half - radius)
    hy = max(0.0, half - radius)

    margin = max(3, int(round(size * 0.22)))
    gap = max(1, int(round(size * 0.06)))
    inner = size - (margin * 2)
    pane = max(2, int((inner - gap) / 2))
    pane_r = max(1.0, pane * 0.18)

    pane_color = (255, 255, 255, 220)

    thick = max(2.0, size * 0.12)
    r = thick / 2.0
    off = max(1.0, size * 0.03)

    p1 = (size * 0.40, size * 0.62)
    p2 = (size * 0.50, size * 0.72)
    p3 = (size * 0.74, size * 0.46)

    check_color = (16, 185, 129, 240)
    shadow_color = (0, 0, 0, 90)

    c1 = (37, 99, 235)
    c2 = (124, 58, 237)

    buf = bytearray(w * h * 4)

    for y in range(h):
        py = (y + 0.5)
        for x in range(w):
            px = (x + 0.5)

            sd_bg = _sd_round_rect(px - cx, py - cy, hx, hy, radius)
            a_bg = _alpha_from_sd(sd_bg, aa=1.2)
            if a_bg <= 0.0:
                continue

            t = (x + y) / max(1.0, (2.0 * (size - 1.0)))
            br, bg, bb = _mix_rgb(c1, c2, t)

            hx0 = size * 0.22
            hy0 = size * 0.20
            sigma = size * 0.42
            dist2 = (px - hx0) ** 2 + (py - hy0) ** 2
            highlight = math.exp(-dist2 / (2.0 * sigma * sigma))
            light = 0.18 * highlight

            edge = _smoothstep(-2.0, -0.2, sd_bg)
            dark = 1.0 - (edge * 0.18)

            br = int(br * dark + 0.5)
            bg = int(bg * dark + 0.5)
            bb = int(bb * dark + 0.5)

            br = int(br + (255 - br) * light + 0.5)
            bg = int(bg + (255 - bg) * light + 0.5)
            bb = int(bb + (255 - bb) * light + 0.5)

            dst = (0, 0, 0, 0)
            dst = _blend(dst, (br, bg, bb, int(a_bg * 255.0 + 0.5)))

            for j in (0, 1):
                for i in (0, 1):
                    x0 = margin + i * (pane + gap)
                    y0 = margin + j * (pane + gap)
                    x1 = x0 + pane
                    y1 = y0 + pane
                    pcx = (x0 + x1) / 2.0
                    pcy = (y0 + y1) / 2.0
                    phx = max(0.0, (pane / 2.0) - pane_r)
                    phy = max(0.0, (pane / 2.0) - pane_r)
                    sd_p = _sd_round_rect(px - pcx, py - pcy, phx, phy, pane_r)
                    ap = _alpha_from_sd(sd_p, aa=1.0)
                    if ap > 0.0:
                        sr, sg, sb, sa = pane_color
                        dst = _blend(dst, (sr, sg, sb, int(sa * ap + 0.5)))

            d1s = _dist_to_segment(px, py, p1[0] + off, p1[1] + off, p2[0] + off, p2[1] + off)
            d2s = _dist_to_segment(px, py, p2[0] + off, p2[1] + off, p3[0] + off, p3[1] + off)
            ds = min(d1s, d2s)
            ash = 1.0 - _smoothstep(r, r + 1.2, ds)
            if ash > 0.0:
                sr, sg, sb, sa = shadow_color
                dst = _blend(dst, (sr, sg, sb, int(sa * ash + 0.5)))

            d1 = _dist_to_segment(px, py, p1[0], p1[1], p2[0], p2[1])
            d2 = _dist_to_segment(px, py, p2[0], p2[1], p3[0], p3[1])
            d = min(d1, d2)
            ac = 1.0 - _smoothstep(r, r + 1.2, d)
            if ac > 0.0:
                sr, sg, sb, sa = check_color
                dst = _blend(dst, (sr, sg, sb, int(sa * ac + 0.5)))

            idx = (y * w + x) * 4
            buf[idx + 0] = dst[0]
            buf[idx + 1] = dst[1]
            buf[idx + 2] = dst[2]
            buf[idx + 3] = dst[3]

    return bytes(buf)


def build_ico(sizes: list[int]) -> bytes:
    images: list[tuple[int, int, bytes]] = []
    for s in sizes:
        rgba = _render_rgba(int(s))
        png = _png_bytes(int(s), int(s), rgba)
        images.append((int(s), int(s), png))

    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    dir_size = 16 * count
    offset = 6 + dir_size

    entries = bytearray()
    blobs = bytearray()
    for (w, h, data) in images:
        w_b = 0 if w >= 256 else w
        h_b = 0 if h >= 256 else h
        entries.extend(
            struct.pack(
                "<BBBBHHII",
                w_b,
                h_b,
                0,
                0,
                1,
                32,
                len(data),
                offset,
            )
        )
        blobs.extend(data)
        offset += len(data)

    return bytes(header + entries + blobs)


def main() -> None:
    ico = build_ico([16, 32, 48, 64, 128, 256])
    out = "src/utils/windows_supporter.ico"
    with open(out, "wb") as fp:
        fp.write(ico)
    print(f"wrote {out} ({len(ico)} bytes)")


if __name__ == "__main__":
    main()
