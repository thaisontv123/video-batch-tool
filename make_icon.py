"""Sinh icon.ico (không cần thư viện ngoài) — nút Play trắng trên nền teal bo góc."""
import struct, math, os

SIZES = [16, 32, 48, 64, 256]

def make_image(n):
    px = bytearray(n * n * 4)  # BGRA
    r = n * 0.22               # bán kính bo góc
    cx = cy = (n - 1) / 2.0
    # tam giác play
    tri_l = n * 0.36; tri_r = n * 0.68
    tri_t = n * 0.30; tri_b = n * 0.70
    for y in range(n):
        for x in range(n):
            # nền bo góc: kiểm tra 4 góc
            inside = True
            for gx, gy in ((r, r), (n - r, r), (r, n - r), (n - r, n - r)):
                if ((x < r and y < r) or (x > n - r and y < r) or
                        (x < r and y > n - r) or (x > n - r and y > n - r)):
                    pass
            # tính khoảng cách tới góc gần nhất để bo
            ax = min(x, n - 1 - x); ay = min(y, n - 1 - y)
            if ax < r and ay < r:
                d = math.hypot(r - ax, r - ay)
                if d > r:
                    inside = False
            if not inside:
                continue
            # gradient teal dọc theo y
            t = y / (n - 1)
            R = int(20 + 20 * t); G = int(150 - 40 * t); B = int(150 - 20 * t)
            A = 255
            # tam giác play (trỏ phải)
            if tri_l <= x <= tri_r and tri_t <= y <= tri_b:
                frac = (x - tri_l) / (tri_r - tri_l)
                half = (1 - frac) * (tri_b - tri_t) / 2
                if abs(y - cy) <= half:
                    R = G = B = 255
            i = (y * n + x) * 4
            px[i] = B; px[i + 1] = G; px[i + 2] = R; px[i + 3] = A
    return bytes(px)

def bmp_for_ico(n, bgra):
    # BITMAPINFOHEADER, height*2 (XOR+AND), bottom-up
    hdr = struct.pack("<IiiHHIIiiII", 40, n, n * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    rows = []
    for y in range(n - 1, -1, -1):  # bottom-up
        rows.append(bgra[y * n * 4:(y + 1) * n * 4])
    xor = b"".join(rows)
    and_row = ((n + 31) // 32) * 4
    and_mask = b"\x00" * (and_row * n)
    return hdr + xor + and_mask

def main():
    images = [(n, bmp_for_ico(n, make_image(n))) for n in SIZES]
    out = bytearray()
    out += struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = bytearray()
    body = bytearray()
    for n, data in images:
        w = 0 if n >= 256 else n
        entries += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(data), offset)
        body += data
        offset += len(data)
    out += entries + body
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
    with open(path, "wb") as f:
        f.write(out)
    print("wrote", path, len(out), "bytes")

if __name__ == "__main__":
    main()
