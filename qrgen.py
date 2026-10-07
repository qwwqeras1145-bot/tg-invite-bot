"""
qrgen.py —— 纯 Python 标准库实现的 QR Code(二维码)生成器

用法::

    from qrgen import make_matrix, svg

    m = make_matrix("https://t.me/+AbCdEf123")   # list[list[bool]],True = 黑块
    html_fragment = svg("https://t.me/+AbCdEf123")  # 可直接嵌进 HTML 的 <svg> 字符串

实现范围(满足 Telegram 邀请链接的渲染需求):

* 数据模式:Byte(8 bit),字符串按 UTF-8 编码后逐字节写入;
* 纠错等级:M;
* 版本:1..10 自动选择(数据超长抛 ``ValueError``);
* 掩码:0..7 全部实现,按 ISO/IEC 18004 的 4 条惩罚规则打分后自动选最低分;
* 完整实现定位图形 / 分隔符 / 校正图形 / 定时图形 / 暗模块 /
  格式信息(BCH 15,5 + 0x5412)/ 版本信息(BCH 18,6 + 0x1F25)/
  Reed-Solomon 纠错 / 分块交织 / zigzag 数据填充。

整个模块只依赖 Python 标准库,不含任何第三方 import。
"""

from __future__ import annotations

__all__ = ["make_matrix", "svg", "MAX_VERSION"]

# --------------------------------------------------------------------------
# 常量表
# --------------------------------------------------------------------------

MAX_VERSION = 10

# 各版本在纠错等级 M 下的数据码字总数(不含纠错码字)
_DATA_CODEWORDS = {
    1: 16, 2: 28, 3: 44, 4: 64, 5: 86,
    6: 108, 7: 124, 8: 154, 9: 182, 10: 216,
}

# 版本 -> (每块纠错码字个数, [(块数, 每块数据码字数), ...])
_RS_BLOCKS = {
    1: (10, [(1, 16)]),
    2: (16, [(1, 28)]),
    3: (26, [(1, 44)]),
    4: (18, [(2, 32)]),
    5: (24, [(2, 43)]),
    6: (16, [(4, 27)]),
    7: (18, [(4, 31)]),
    8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
}

# 校正图形中心坐标(按版本)
_ALIGN_CENTERS = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
    7: [6, 22, 38],
    8: [6, 24, 42],
    9: [6, 26, 46],
    10: [6, 28, 50],
}

_ECC_BITS_M = 0b00  # 纠错等级 M 在格式信息中的 2 bit 编码


# --------------------------------------------------------------------------
# GF(256) 有限域运算(a ^ 8 + a ^ 4 + a ^ 3 + a ^ 2 + 1,即 0x11D)
# --------------------------------------------------------------------------

_GF_EXP = [0] * 512
_GF_LOG = [0] * 256
_v = 1
for _i in range(255):
    _GF_EXP[_i] = _v
    _GF_LOG[_v] = _i
    _v <<= 1
    if _v & 0x100:
        _v ^= 0x11D
for _i in range(255, 512):
    _GF_EXP[_i] = _GF_EXP[_i - 255]


def _gf_mul(a: int, b: int) -> int:
    """GF(256) 乘法。"""
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_generator_poly(degree: int) -> list[int]:
    """生成 RS 生成多项式 g(x) = (x-a^0)(x-a^1)...(x-a^(degree-1))。

    返回长度为 degree+1 的系数列表,索引 0 为最高次项(首项恒为 1)。
    """
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for j, coef in enumerate(poly):
            nxt[j] ^= coef                       # 乘 x
            nxt[j + 1] ^= _gf_mul(coef, _GF_EXP[i])  # 乘 a^i
        poly = nxt
    return poly


def _rs_encode(data: list[int], ec_len: int) -> list[int]:
    """对一块数据码字做 Reed-Solomon 纠错编码,返回 ec_len 个纠错码字。"""
    gen = _rs_generator_poly(ec_len)
    rem = [0] * ec_len
    for byte in data:
        factor = byte ^ rem[0]
        del rem[0]
        rem.append(0)
        if factor:
            for i in range(ec_len):
                rem[i] ^= _gf_mul(gen[i + 1], factor)
    return rem


# --------------------------------------------------------------------------
# BCH 校验:格式信息(15,5)与版本信息(18,6)
# --------------------------------------------------------------------------

def _format_bits(mask: int) -> int:
    """格式信息:5 bit 数据(2 bit 纠错等级 + 3 bit 掩码)+ BCH(15,5),再异或 0x5412。"""
    data = (_ECC_BITS_M << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)   # 生成多项式 0x537
    return ((data << 10) | rem) ^ 0x5412


def _version_bits(version: int) -> int:
    """版本信息(版本 7+):6 bit 版本号 + BCH(18,6),生成多项式 0x1F25。"""
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return (version << 12) | rem


# --------------------------------------------------------------------------
# 数据编码:Byte 模式 -> 码字 -> 分块交织
# --------------------------------------------------------------------------

def _choose_version(nbytes: int) -> int:
    """选择能装下 nbytes 个字节的最小版本(1..10),装不下则抛 ValueError。"""
    for version in range(1, MAX_VERSION + 1):
        count_bits = 8 if version < 10 else 16   # 字符计数指示符长度
        needed = 4 + count_bits + 8 * nbytes
        if needed <= _DATA_CODEWORDS[version] * 8:
            return version
    raise ValueError(
        "数据过长:%d 字节,纠错等级 M 下最大支持 %d 字节(版本 10)"
        % (nbytes, (216 * 8 - 20) // 8)
    )


def _encode_data(data: bytes, version: int) -> list[int]:
    """Byte 模式编码 + 填充,得到该版本的数据码字(尚未加纠错)。"""
    count_bits = 8 if version < 10 else 16
    bits: list[int] = []

    def put(value: int, length: int) -> None:
        for i in range(length - 1, -1, -1):
            bits.append((value >> i) & 1)

    put(0b0100, 4)                 # 模式指示符:Byte
    put(len(data), count_bits)     # 字符计数指示符
    for byte in data:
        put(byte, 8)

    capacity = _DATA_CODEWORDS[version] * 8
    if len(bits) > capacity:       # 理论上 _choose_version 已排除,这里兜底
        raise ValueError("数据超出所选版本容量")
    bits.extend([0] * min(4, capacity - len(bits)))     # 终止符(最多 4 个 0)
    if len(bits) % 8:                                   # 补齐到字节边界
        bits.extend([0] * (8 - len(bits) % 8))

    codewords: list[int] = []
    for i in range(0, len(bits), 8):
        value = 0
        for b in bits[i:i + 8]:
            value = (value << 1) | b
        codewords.append(value)

    pad = (0xEC, 0x11)                                  # 交替填充码字
    for i in range(capacity // 8 - len(codewords)):
        codewords.append(pad[i % 2])
    return codewords


def _interleave(data_codewords: list[int], version: int) -> list[int]:
    """按 RS 分块做纠错,再按 ISO/IEC 18004 规则交织成一个码字序列。"""
    ec_len, groups = _RS_BLOCKS[version]
    blocks: list[tuple[list[int], list[int]]] = []
    pos = 0
    for count, data_len in groups:
        for _ in range(count):
            block = data_codewords[pos:pos + data_len]
            pos += data_len
            blocks.append((block, _rs_encode(block, ec_len)))
    if pos != len(data_codewords):
        raise AssertionError("分块长度与数据码字数不匹配")

    out: list[int] = []
    max_data = max(len(b[0]) for b in blocks)
    for i in range(max_data):                    # 逐列交织数据码字
        for block, _ in blocks:
            if i < len(block):
                out.append(block[i])
    for i in range(ec_len):                      # 逐列交织纠错码字
        for _, ec in blocks:
            out.append(ec[i])
    return out


# --------------------------------------------------------------------------
# 矩阵布局
# --------------------------------------------------------------------------

def _draw_finder_and_separators(mat, func, size: int) -> None:
    """三个 7x7 定位图形 + 一圈白色的分隔符(整体是 8x8 的方块)。"""
    for r0, c0 in ((0, 0), (0, size - 7), (size - 7, 0)):
        for dr in range(-1, 8):
            r = r0 + dr
            if not 0 <= r < size:
                continue
            for dc in range(-1, 8):
                c = c0 + dc
                if not 0 <= c < size:
                    continue
                func[r][c] = 1
                dark = (
                    (0 <= dr <= 6 and dc in (0, 6))
                    or (0 <= dc <= 6 and dr in (0, 6))
                    or (2 <= dr <= 4 and 2 <= dc <= 4)
                )
                mat[r][c] = 1 if dark else 0


def _draw_timing(mat, func, size: int) -> None:
    """定时图形:第 6 行与第 6 列,黑白相间。"""
    for r in range(8, size - 8):
        if not func[r][6]:
            mat[r][6] = 1 if r % 2 == 0 else 0
            func[r][6] = 1
    for c in range(8, size - 8):
        if not func[6][c]:
            mat[6][c] = 1 if c % 2 == 0 else 0
            func[6][c] = 1


def _draw_alignment(mat, func, version: int, size: int) -> None:
    """校正图形(版本 2+):5x5,中心与最外圈为黑。与定位图形重叠的跳过。"""
    centers = _ALIGN_CENTERS[version]
    for row in centers:
        for col in centers:
            if func[row][col]:          # 已属于定位图形/分隔符/定时图形
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    r, c = row + dr, col + dc
                    func[r][c] = 1
                    dark = dr in (-2, 2) or dc in (-2, 2) or (dr == 0 and dc == 0)
                    mat[r][c] = 1 if dark else 0


def _format_positions(size: int):
    """格式信息的 30 个模块坐标(含暗模块),给出 (row, col)。"""
    positions = []
    for i in range(15):                 # 第一份:第 8 列 + 左下
        if i < 6:
            positions.append((i, 8))
        elif i < 8:
            positions.append((i + 1, 8))
        else:
            positions.append((size - 15 + i, 8))
    for i in range(15):                 # 第二份:第 8 行 + 右上
        if i < 8:
            positions.append((8, size - i - 1))
        elif i < 9:
            positions.append((8, 7))
        else:
            positions.append((8, 14 - i))
    positions.append((size - 8, 8))     # 固定的暗模块
    return positions


def _reserve_format(mat, func, size: int) -> None:
    """把格式信息区域预留出来(初值全白),避免被数据填充占用。"""
    for r, c in _format_positions(size):
        func[r][c] = 1
        mat[r][c] = 0


def _write_format(mat, func, mask: int) -> None:
    """写入格式信息 + 暗模块(函数模块,不受掩码影响)。"""
    size = len(mat)
    bits = _format_bits(mask)
    for i in range(15):
        b = (bits >> i) & 1
        if i < 6:
            mat[i][8] = b
        elif i < 8:
            mat[i + 1][8] = b
        else:
            mat[size - 15 + i][8] = b
    for i in range(15):
        b = (bits >> i) & 1
        if i < 8:
            mat[8][size - i - 1] = b
        elif i < 9:
            mat[8][7] = b
        else:
            mat[8][14 - i] = b
    mat[size - 8][8] = 1                # 暗模块恒定黑


def _reserve_version(mat, func, version: int, size: int) -> None:
    """版本 7+ 的版本信息区域预留(初值全白)。"""
    if version < 7:
        return
    for i in range(18):
        for r, c in ((i // 3, i % 3 + size - 11), (i % 3 + size - 11, i // 3)):
            func[r][c] = 1
            mat[r][c] = 0


def _write_version(mat, func, version: int) -> None:
    """写入版本信息(版本 7+)。"""
    if version < 7:
        return
    size = len(mat)
    bits = _version_bits(version)
    for i in range(18):
        b = (bits >> i) & 1
        mat[i // 3][i % 3 + size - 11] = b
        mat[i % 3 + size - 11][i // 3] = b


def _fill_data(mat, func, codewords: list[int]) -> None:
    """从右下角开始按两列一组、上下往复(zigzag)填充数据位。"""
    size = len(mat)
    total_bits = len(codewords) * 8
    index = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:                    # 跳过竖向定时图形所在的第 6 列
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in (col, col - 1):
                if func[row][c]:
                    continue
                bit = 0
                if index < total_bits:
                    bit = (codewords[index >> 3] >> (7 - (index & 7))) & 1
                mat[row][c] = bit
                index += 1
        col -= 2
        upward = not upward


# --------------------------------------------------------------------------
# 掩码与惩罚打分(ISO/IEC 18004 第 4 条规则)
# --------------------------------------------------------------------------

def _mask_bit(mask: int, r: int, c: int) -> bool:
    """8 种掩码图案,返回 (r, c) 处是否需要取反。"""
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
    raise ValueError("非法掩码编号:%r" % (mask,))


def _apply_mask(mat, func, mask: int) -> None:
    """只对数据模块(非函数模块)取反。"""
    size = len(mat)
    for r in range(size):
        frow = func[r]
        mrow = mat[r]
        for c in range(size):
            if not frow[c] and _mask_bit(mask, r, c):
                mrow[c] ^= 1


def _penalty_1(mods, n: int) -> int:
    """规则 1:行/列中连续 5 个及以上同色模块,罚 3 + (长度 - 5)。"""
    counts = [0] * (n + 1)

    def scan(getter) -> None:
        prev = getter(0)
        length = 0
        for i in range(n):
            cur = getter(i)
            if cur == prev:
                length += 1
            else:
                if length >= 5:
                    counts[length] += 1
                length = 1
                prev = cur
        if length >= 5:
            counts[length] += 1

    for r in range(n):
        scan(lambda c, r=r: mods[r][c])
    for c in range(n):
        scan(lambda r, c=c: mods[r][c])
    return sum(counts[length] * (length - 2) for length in range(5, n + 1))


def _penalty_2(mods, n: int) -> int:
    """规则 2:每个 2x2 的同色方块罚 3 分。"""
    score = 0
    for r in range(n - 1):
        row = mods[r]
        nxt = mods[r + 1]
        for c in range(n - 1):
            v = row[c]
            if v == row[c + 1] == nxt[c] == nxt[c + 1]:
                score += 3
    return score


_PAT_A = (1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0)   # 10111010000
_PAT_B = (0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1)   # 00001011101


def _penalty_3(mods, n: int) -> int:
    """规则 3:出现 1:1:3:1:1(带一侧 4 个浅色)图案,每次罚 40 分。"""
    score = 0
    for r in range(n):
        row = mods[r]
        for c in range(n - 10):
            seg = tuple(row[c:c + 11])
            if seg == _PAT_A or seg == _PAT_B:
                score += 40
    for c in range(n):
        col = [mods[r][c] for r in range(n)]
        for r in range(n - 10):
            seg = tuple(col[r:r + 11])
            if seg == _PAT_A or seg == _PAT_B:
                score += 40
    return score


def _penalty_4(mods, n: int) -> int:
    """规则 4:黑色模块占比每偏离 50% 达 5%,罚 10 分。"""
    dark = sum(sum(row) for row in mods)
    percent = float(dark) / (n ** 2)
    rating = int(abs(percent * 100 - 50) / 5)
    return rating * 10


def _penalty(mods) -> int:
    n = len(mods)
    return (
        _penalty_1(mods, n)
        + _penalty_2(mods, n)
        + _penalty_3(mods, n)
        + _penalty_4(mods, n)
    )


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------

def _build_symbol(version: int, codewords: list[int]) -> list[list[int]]:
    """生成最终模块矩阵(0/1)。"""
    size = version * 4 + 17
    mat = [[0] * size for _ in range(size)]
    func = [[0] * size for _ in range(size)]

    _draw_finder_and_separators(mat, func, size)
    # 校正图形必须先于定时图形绘制:与定时图形重叠的校正图形(如版本 7+ 的
    # (6, 22))按规范要画出来,之后定时图形跳过已被占用的模块。
    _draw_alignment(mat, func, version, size)
    _draw_timing(mat, func, size)
    _reserve_format(mat, func, size)
    _reserve_version(mat, func, version, size)
    _fill_data(mat, func, codewords)

    # 8 种掩码逐一试算,取惩罚分最低者(同分时保留编号小的,与参考实现一致)。
    # 注意:打分阶段格式信息/版本信息/暗模块均为空白,与 ISO 参考实现的做法保持一致。
    best_mask = 0
    best_score = None
    for mask in range(8):
        trial = [row[:] for row in mat]
        _apply_mask(trial, func, mask)
        score = _penalty(trial)
        if best_score is None or score < best_score:
            best_score = score
            best_mask = mask

    _apply_mask(mat, func, best_mask)
    _write_format(mat, func, best_mask)
    _write_version(mat, func, version)
    return mat


def make_matrix(data: str) -> list[list[bool]]:
    """返回 QR 模块矩阵,True 表示黑块。不含白边。

    :param data: 任意字符串,内部按 UTF-8 编码后以 Byte 模式写入。
    :raises ValueError: 数据超过版本 10 + 纠错等级 M 的容量。
    """
    if not isinstance(data, str):
        raise TypeError("data 必须是 str")
    raw = data.encode("utf-8")
    version = _choose_version(len(raw))
    codewords = _interleave(_encode_data(raw, version), version)
    return [[bool(v) for v in row] for row in _build_symbol(version, codewords)]


def svg(data: str, scale: int = 4, border: int = 4) -> str:
    """返回可以直接嵌进 HTML 的 ``<svg ...>`` 字符串(白底黑块,无 XML 声明)。

    :param scale: 每个模块的边长(像素)。
    :param border: 四周静区宽度(模块数)。
    """
    if scale <= 0:
        raise ValueError("scale 必须为正整数")
    if border < 0:
        raise ValueError("border 不能为负数")
    matrix = make_matrix(data)
    n = len(matrix)
    dim = (n + 2 * border) * scale

    segments = []
    for r in range(n):
        row = matrix[r]
        c = 0
        while c < n:
            if row[c]:                      # 把每行连续的黑色模块合并成一段
                start = c
                while c < n and row[c]:
                    c += 1
                x = (start + border) * scale
                y = (r + border) * scale
                w = (c - start) * scale
                segments.append("M%d %dh%dv%dh-%dz" % (x, y, w, scale, w))
            else:
                c += 1

    return (
        '<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
        'width="{d}" height="{d}" viewBox="0 0 {d} {d}" '
        'shape-rendering="crispEdges">'
        '<rect width="{d}" height="{d}" fill="#ffffff"/>'
        '<path fill="#000000" d="{p}"/>'
        "</svg>"
    ).format(d=dim, p="".join(segments))
