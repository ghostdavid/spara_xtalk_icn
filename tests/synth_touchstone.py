"""确定性合成 Touchstone 文件生成器。

供旧版参考快照与回归测试共用：不使用随机数，同一套公式在
快照阶段和测试阶段生成逐字节一致的文件，保证对比基线可靠。

数据顺序遵循 Touchstone 规范：
- 2 端口 (standard 与 repeated): S11 S21 S12 S22 (列优先)
- N>2 端口: 行优先 S11 S12 ... S1N | S21 ...
- repeated 格式: 每个频率重复 N 次，每行一个频率块
"""

import math
from pathlib import Path

FREQ_START_GHZ = 0.1
FREQ_STEP_GHZ = 0.1


def s_value(n, i, j, f_ghz):
    """确定性合成 S 参数: 平滑幅度 + 随频率/端口变化的相位。"""
    mag = 0.05 + 0.02 * ((3 * i + 5 * j) % 11) / 11 + 0.1 * math.exp(-f_ghz / 0.8)
    phase = 0.7 * i + 1.3 * j + 2.0 * f_ghz
    return mag * complex(math.cos(phase), math.sin(phase))


def gen_matrix(n, f_ghz):
    return [[s_value(n, i, j, f_ghz) for j in range(n)] for i in range(n)]


def _pair_order(n, row, col):
    """返回 (row, col) 在文件数据流中的序号，遵循 Touchstone 顺序约定。"""
    if n == 2:
        order = [(0, 0), (1, 0), (0, 1), (1, 1)]  # S11 S21 S12 S22
        return order.index((row, col))
    return row * n + col


def _fmt_pair(s, fmt):
    mag = abs(s)
    ang = math.degrees(math.atan2(s.imag, s.real))
    if fmt == 'RI':
        return f'{s.real:.10e} {s.imag:.10e}'
    if fmt == 'MA':
        return f'{mag:.10e} {ang:.10e}'
    # DB
    db = 20 * math.log10(mag) if mag > 0 else -300.0
    return f'{db:.10e} {ang:.10e}'


def serialize_touchstone(n, fmt='RI', repeated=False, z0=50, num_freqs=11,
                         port_comment_style='standard', flag_line=False):
    freqs = [FREQ_START_GHZ + FREQ_STEP_GHZ * k for k in range(num_freqs)]
    lines = [f'# {"HZ" if n == 1 else "GHZ"} S {fmt} R {z0:g}']

    def port_comment(k):
        name = f'DDR0_DQ{k}'
        if port_comment_style == 'cadence':
            return f'! Port{k + 1}_{name}'
        if port_comment_style == 'simple':
            return f'! {k + 1}: {name}'
        return f'! Port {k + 1} = {name}'

    for k in range(n):
        lines.append(port_comment(k))
    if flag_line:
        lines.append('! TouchstoneFormatFlag')
        for k in range(n):  # 标记后的重复端口注释必须被忽略
            lines.append(f'! Port{k + 1}_IGNORED_AFTER_FLAG')

    pairs_per_line = 4
    if not repeated:
        for f in freqs:
            mat = gen_matrix(n, f)
            pairs = []
            for i in range(n):
                for j in range(n):
                    pairs.append((i, j, mat[i][j]))
            ordered = sorted(pairs, key=lambda p: _pair_order(n, p[0], p[1]))
            chunks = [ordered[x:x + pairs_per_line]
                      for x in range(0, len(ordered), pairs_per_line)]
            for c_idx, chunk in enumerate(chunks):
                prefix = f'{f:.10e} ' if c_idx == 0 else ''
                lines.append(prefix + ' '.join(_fmt_pair(s, fmt) for _, _, s in chunk))
    else:
        # 平铺的参数对序列 (遵循 2 端口列优先约定) 按 N 对一组分块,
        # 每组配一次重复的频率
        ordered_pairs = sorted(((i, j) for i in range(n) for j in range(n)),
                               key=lambda p: _pair_order(n, p[0], p[1]))
        for f in freqs:
            mat = gen_matrix(n, f)
            for r in range(n):
                chunk = ordered_pairs[r * n:(r + 1) * n]
                lines.append(f'{f:.10e} ' +
                             ' '.join(_fmt_pair(mat[i][j], fmt) for i, j in chunk))
    return '\n'.join(lines) + '\n'


def write_case(directory, filename, n, fmt='RI', repeated=False, z0=50,
               num_freqs=11, port_comment_style='standard', flag_line=False):
    path = Path(directory) / filename
    path.write_text(
        serialize_touchstone(n, fmt=fmt, repeated=repeated, z0=z0,
                             num_freqs=num_freqs,
                             port_comment_style=port_comment_style,
                             flag_line=flag_line),
        encoding='ascii')
    return path


def generate_all(directory):
    """生成全部快照用例，返回 {用例名: 文件路径}。

    s2p_ri_rep9: F=9 时 90 个数值同时被 standard(9) 与 repeated(10) 整除，
    属于格式歧义用例；旧版无条件按 standard 解析出垃圾矩阵 (已修复 bug)，
    新版按频率列单调性消歧后按 repeated 解析；
    s2p_ri_rep2: 仅 repeated(10) 整除，旧版错误按 standard 解析（已修复 bug）。
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    cases = {
        's4p_ri_std': dict(filename='case4.s4p', n=4, fmt='RI'),
        's4p_ma_std': dict(filename='case4m.s4p', n=4, fmt='MA'),
        's4p_db_std': dict(filename='case4d.s4p', n=4, fmt='DB'),
        's4p_ri_simple': dict(filename='case4s.s4p', n=4, fmt='RI',
                              port_comment_style='simple'),
        's8p_ri_cadence': dict(filename='case8.s8p', n=8, fmt='RI',
                               port_comment_style='cadence', flag_line=True),
        's8p_ri_z0_75': dict(filename='case8z.s8p', n=8, fmt='RI', z0=75),
        's2p_ri_std': dict(filename='case2.s2p', n=2, fmt='RI'),
        's2p_ri_std10': dict(filename='case2s10.s2p', n=2, fmt='RI',
                             num_freqs=10),
        's2p_ri_rep2': dict(filename='case2r.s2p', n=2, fmt='RI',
                            repeated=True, num_freqs=2),
        's2p_ri_rep9': dict(filename='case2r9.s2p', n=2, fmt='RI',
                            repeated=True, num_freqs=9),
        's1p_ri': dict(filename='case1.s1p', n=1, fmt='RI'),
        'noext_ri_4port': dict(filename='case_noext.dat', n=4, fmt='RI'),
    }
    return {name: write_case(directory, **kwargs) for name, kwargs in cases.items()}


if __name__ == '__main__':
    import sys
    out = generate_all(sys.argv[1] if len(sys.argv) > 1 else 'fixtures')
    for name, path in out.items():
        print(f'{name}: {path}')
