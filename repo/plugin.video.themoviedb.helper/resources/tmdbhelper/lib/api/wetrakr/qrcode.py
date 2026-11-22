""" A small QR Code encoder with no dependencies: byte mode, error correction level M, versions 1 to 6 (up to 106 bytes), the mask chosen by
the standard penalty rules. Used to draw the QR code of the WeTrakr authorization dialog (a short web address). make_matrix() gives the modules,
png_bytes() a PNG picture of them, solid_png() a plain coloured PNG. """
import struct
import zlib

# version: (all codewords, error correction codewords per block, blocks) at level M; every block of these versions holds the same number of data codewords
_VERSIONS = {1: (26, 10, 1), 2: (44, 16, 1), 3: (70, 26, 1), 4: (100, 18, 2), 5: (134, 24, 2), 6: (172, 16, 4)}
_ALIGNMENT = {1: (), 2: (6, 18), 3: (6, 22), 4: (6, 26), 5: (6, 30), 6: (6, 34)}
MAX_BYTES = 106  # what version 6 holds at level M


def _gf_tables():
    exp, log = [0] * 512, [0] * 256
    x = 1
    for i in range(255):
        exp[i], log[x] = x, i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_EXP, _LOG = _gf_tables()


def _gf_mul(a, b):
    return 0 if not a or not b else _EXP[_LOG[a] + _LOG[b]]


def _rs_remainder(data, ec_len):
    """ Reed-Solomon error correction codewords of data """
    gen = [1]
    for i in range(ec_len):
        nxt = [0] * (len(gen) + 1)
        for j, coef in enumerate(gen):
            nxt[j] ^= coef
            nxt[j + 1] ^= _gf_mul(coef, _EXP[i])
        gen = nxt
    rem = [0] * ec_len
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        for i in range(ec_len):
            rem[i] ^= _gf_mul(gen[i + 1], factor)
    return rem


def _pick_version(length):
    for version, (total, ec, blocks) in _VERSIONS.items():
        if length <= total - ec * blocks - 2:  # mode (4 bits) + count (8 bits) take 12 bits, which is 1.5 bytes, rounded up to 2
            return version
    raise ValueError(f'{length} bytes do not fit (at most {MAX_BYTES})')


def _codewords(payload, version):
    total, ec_len, blocks = _VERSIONS[version]
    capacity = total - ec_len * blocks
    bits = '0100' + format(len(payload), '08b') + ''.join(format(b, '08b') for b in payload)
    bits += '0' * min(4, capacity * 8 - len(bits))
    bits += '0' * (-len(bits) % 8)
    data = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    pad = (0xEC, 0x11)
    while len(data) < capacity:
        data.append(pad[(len(data) - len(bits) // 8) % 2])
    size = capacity // blocks
    parts = [data[i * size:(i + 1) * size] for i in range(blocks)]
    ecs = [_rs_remainder(p, ec_len) for p in parts]
    out = [p[i] for i in range(size) for p in parts]
    out += [e[i] for i in range(ec_len) for e in ecs]
    return out


def _finder(modules, function, cx, cy, size):
    for dy in range(-4, 5):
        for dx in range(-4, 5):
            x, y = cx + dx, cy + dy
            if 0 <= x < size and 0 <= y < size:
                dist = max(abs(dx), abs(dy))
                modules[y][x] = dist not in (2, 4)
                function[y][x] = True


def _alignment(modules, function, cx, cy):
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            modules[cy + dy][cx + dx] = max(abs(dx), abs(dy)) != 1
            function[cy + dy][cx + dx] = True


def _mask_hit(mask, x, y):
    if mask == 0:
        return (x + y) % 2 == 0
    if mask == 1:
        return y % 2 == 0
    if mask == 2:
        return x % 3 == 0
    if mask == 3:
        return (x + y) % 3 == 0
    if mask == 4:
        return (x // 3 + y // 2) % 2 == 0
    if mask == 5:
        return x * y % 2 + x * y % 3 == 0
    if mask == 6:
        return (x * y % 2 + x * y % 3) % 2 == 0
    return ((x + y) % 2 + x * y % 3) % 2 == 0


def _format_bits(mask):
    data = mask  # error correction level M is 00
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


def _set_format(modules, size, mask):
    bits = _format_bits(mask)

    def bit(i):
        return (bits >> i) & 1 == 1
    for i in range(6):
        modules[i][8] = bit(i)
    modules[7][8], modules[8][8], modules[8][7] = bit(6), bit(7), bit(8)
    for i in range(9, 15):
        modules[8][14 - i] = bit(i)
    for i in range(8):
        modules[8][size - 1 - i] = bit(i)
    for i in range(8, 15):
        modules[size - 15 + i][8] = bit(i)
    modules[size - 8][8] = True  # always dark


def _penalty(m):
    size = len(m)
    score = 0
    lines = [row for row in m] + [[m[y][x] for y in range(size)] for x in range(size)]
    for line in lines:
        run = 1
        for i in range(1, size):
            if line[i] == line[i - 1]:
                run += 1
            else:
                score += 3 + run - 5 if run >= 5 else 0
                run = 1
        score += 3 + run - 5 if run >= 5 else 0
        text = ''.join('1' if v else '0' for v in line)
        for pattern in ('10111010000', '00001011101'):
            start = 0
            while True:
                found = text.find(pattern, start)
                if found < 0:
                    break
                score += 40
                start = found + 1
    for y in range(size - 1):
        for x in range(size - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(sum(1 for v in row if v) for row in m)
    score += (abs(dark * 20 - size * size * 10) // (size * size)) * 10
    return score


def make_matrix(text, mask=None):
    """ The modules of the QR code for text (str, ASCII or UTF-8): a list of rows of booleans (True = dark), without the quiet zone.
    mask (0-7) forces a mask, otherwise the one with the lowest penalty is used. """
    payload = text.encode('utf-8')
    version = _pick_version(len(payload))
    size = 17 + 4 * version
    modules = [[False] * size for _ in range(size)]
    function = [[False] * size for _ in range(size)]
    for i in range(size):  # timing patterns (their ends are overwritten by the finders and separators below)
        modules[6][i] = modules[i][6] = i % 2 == 0
        function[6][i] = function[i][6] = True
    _finder(modules, function, 3, 3, size)
    _finder(modules, function, size - 4, 3, size)
    _finder(modules, function, 3, size - 4, size)
    positions = _ALIGNMENT[version]
    if positions:
        _alignment(modules, function, positions[-1], positions[-1])  # versions 2-6 have this one pattern only
    for i in range(9):  # the format areas are function modules too (filled in by _set_format)
        function[8][i] = function[i][8] = True
    for i in range(8):
        function[8][size - 1 - i] = function[size - 1 - i][8] = True
    function[size - 8][8] = True
    data = _codewords(payload, version)
    i = 0
    for right in range(size - 1, 0, -2):
        if right <= 6:  # the vertical timing column is skipped: the pairs left of it are (5,4), (3,2), (1,0)
            right -= 1
        upward = ((right + 1) & 2) == 0
        for vert in range(size):
            y = size - 1 - vert if upward else vert
            for j in range(2):
                x = right - j
                if not function[y][x] and i < len(data) * 8:
                    modules[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 == 1
                    i += 1
    best = None
    for candidate in ([mask] if mask is not None else range(8)):
        m = [row[:] for row in modules]
        for y in range(size):
            for x in range(size):
                if not function[y][x] and _mask_hit(candidate, x, y):
                    m[y][x] = not m[y][x]
        _set_format(m, size, candidate)
        score = _penalty(m)
        if best is None or score < best[0]:
            best = (score, m)
    return best[1]


def _chunk(kind, data):
    body = kind + data
    return struct.pack('>I', len(data)) + body + struct.pack('>I', zlib.crc32(body) & 0xFFFFFFFF)


def _png(width, height, color_type, rows):
    raw = b''.join(b'\x00' + row for row in rows)
    header = struct.pack('>IIBBBBB', width, height, 8, color_type, 0, 0, 0)
    return b'\x89PNG\r\n\x1a\n' + _chunk(b'IHDR', header) + _chunk(b'IDAT', zlib.compress(raw, 9)) + _chunk(b'IEND', b'')


def png_bytes(matrix, scale=8, border=4):
    """ A grayscale PNG of the matrix: dark modules black, the rest and a quiet zone of `border` modules white """
    size = len(matrix) + 2 * border
    blank = b'\xff' * (size * scale)
    rows = [blank] * (border * scale)
    for line in matrix:
        row = b''.join((b'\x00' if v else b'\xff') * scale for v in ([False] * border + list(line) + [False] * border))
        rows.extend([row] * scale)
    rows.extend([blank] * (border * scale))
    return _png(size * scale, size * scale, 0, rows)


def solid_png(rgba, size=4):
    """ A plain RGBA PNG of one colour (used as the dialog's background) """
    row = bytes(rgba) * size
    return _png(size, size, 6, [row] * size)
