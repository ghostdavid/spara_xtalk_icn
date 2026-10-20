"""Touchstone S参数解析与 PowerSum 串扰计算核心。

纯数值模块，不依赖任何 GUI 库：
- SParameter: Touchstone .sNp 文件解析器
- snp2smp / s2sdd / safe_db: S 参数数学工具
- power_sum_db: PSFEXT / PSNEXT / PSXT 功率和计算

从 V3.3 单文件版本重构而来，解析语义与原实现保持一致，
仅修复以下边缘问题：
- 2 端口文件的 repeated-frequency 格式现在能被正确识别（原来按 standard 误读）；
- 文件名无 .sNp 后缀时，端口数推断改为对所有整除候选按"频率序列单调性"
  打分选优（原来取第一个能整除的 n，几乎总会误判成 1 端口）；
- standard / repeated 布局同时整除时（如 2 端口 repeated 恰有 9 的倍数个频点），
  按频率列单调性消歧（原来无条件选 standard，会静默解析出垃圾矩阵）；
- 数据行含非法 token 时整行丢弃，不再把行内前半段数字残留进数据流。

V3.6 起数据重组方式: 阻抗归一化与频点重采样合并为
resample_sparams 一次性预处理 (基本参数变化时重建缓存),
功率和/ICN 计算直接在重组后的等间隔网格上进行;
复数 S 参数的实部/虚部分别做 PCHIP 插值 (保单调不过冲),
不在 dB/功率域插值, 避免对数域过冲经指数放大成虚假尖峰。
"""

import ast
import os
import re

import numpy as np
from scipy.interpolate import PchipInterpolator


# ==================== Touchstone S参数文件解析器 ====================

class SParameter:
    """解析 Touchstone 格式的 S 参数文件 (.sNp)"""

    # 三级端口名匹配正则，按优先级排序
    # 1. Cadence格式:   ! Port1_DIE_sbump_12052::DDR0_A0D0_DQ0
    #    注意: 名字中的数字不等于矩阵序号，需按出现顺序计数
    # 2. 标准格式:      ! Port 1 = DIE_SBUMP-12052 DDR0_A0D0_DQ0
    # 3. 简单格式:      ! 1: name  或  ! 1 = name
    _PAT_CADENCE = re.compile(r'!\s*[Pp]ort(\d+)_(.+)')
    _PAT_STANDARD = re.compile(r'!\s*[Pp]ort\s*[\[\(]?\s*(\d+)\s*[\]\)]?\s*[:=]\s*(.*)')
    _PAT_SIMPLE = re.compile(r'!\s*(\d+)\s*[:=]\s*(.*)')
    _FREQ_FACTORS = {'hz': 1.0, 'khz': 1e3, 'mhz': 1e6, 'ghz': 1e9}

    def __init__(self, filepath):
        self.filepath = filepath
        self.num_ports = 0
        self.frequencies = np.zeros(0)          # Hz, 升序去重
        self.parameters = np.zeros((0, 0, 0), dtype=complex)  # (N, N, F)
        self.z0 = 50.0
        self.port_names = []
        self._parse()

    # -------------------- 主流程 --------------------

    def _parse(self):
        with open(self.filepath, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()

        freq_factor, data_format, z0, port_names_raw, all_values = \
            self._scan_lines(lines)

        m = re.match(r'.*\.s(\d+)p', os.path.basename(self.filepath), re.IGNORECASE)
        num_ports = int(m.group(1)) if m else self._infer_port_count(all_values)

        values_per_freq, use_repeated = self._resolve_layout(num_ports, all_values)
        num_freqs = len(all_values) // values_per_freq
        if num_freqs == 0:
            raise ValueError('文件中未找到有效的频率数据')

        values = np.asarray(all_values[:num_freqs * values_per_freq], dtype=float)
        frequencies, s_params = self._fill_matrices(
            values, num_ports, num_freqs, values_per_freq,
            use_repeated, freq_factor, data_format)

        sort_idx = np.argsort(frequencies)
        frequencies = frequencies[sort_idx]
        s_params = s_params[:, :, sort_idx]
        unique_mask = np.concatenate(([True], np.diff(frequencies) > 1e-6))

        self.num_ports = num_ports
        self.frequencies = frequencies[unique_mask]
        self.parameters = s_params[:, :, unique_mask]
        self.z0 = z0

        names = {}
        for pnum, pname in port_names_raw:
            if 1 <= pnum <= num_ports:
                names[pnum] = pname
        self.port_names = [names.get(i, '-') for i in range(1, num_ports + 1)]

    # -------------------- 文本扫描 --------------------

    def _scan_lines(self, lines):
        """扫描选项行 / 注释行 / 数据行。"""
        freq_factor = 1e9
        data_format = 'RI'
        z0 = 50.0
        port_names_raw = []
        all_values = []

        # TouchstoneFormatFlag 标记，检测到后停止端口名解析
        # 部分 EDA 工具 (如 Cadence) 会在该标记后重复输出端口注释
        touchstone_flag_seen = False

        # Cadence 格式专用顺序计数器: 端口号按出现顺序编号，
        # 而非端口名中的数字 (如 Port103 实际是矩阵中第 17 个端口)
        cadence_port_counter = 0

        for line in lines:
            line = line.strip()
            if not line:
                continue
            if line.startswith('#'):
                for tok in line[1:].split():
                    tl = tok.lower()
                    if tl in self._FREQ_FACTORS:
                        freq_factor = self._FREQ_FACTORS[tl]
                    elif tl in ('ri', 'ma', 'db'):
                        data_format = tl.upper()
                    else:
                        try:
                            z0 = float(tok)
                        except ValueError:
                            pass
                continue
            if line.startswith('!'):
                if 'TouchstoneFormatFlag' in line:
                    touchstone_flag_seen = True
                    continue
                if not touchstone_flag_seen:
                    pm = self._PAT_CADENCE.match(line)
                    if pm:
                        cadence_port_counter += 1
                        pname = pm.group(2).strip()
                        if pname:
                            port_names_raw.append((cadence_port_counter, pname))
                        continue

                    pm = self._PAT_STANDARD.match(line)
                    if pm:
                        pname = pm.group(2).strip()
                        if pname:
                            port_names_raw.append((int(pm.group(1)), pname))
                        continue

                    pm = self._PAT_SIMPLE.match(line)
                    if pm:
                        pname = pm.group(2).strip()
                        if pname:
                            port_names_raw.append((int(pm.group(1)), pname))
                    continue
                continue
            try:
                tokens = [float(t) for t in line.split()]
            except ValueError:
                # 整行丢弃: 不能用 extend(generator), 否则行内前半段
                # 合法数字已被追加, 数据流被污染
                continue
            all_values.extend(tokens)

        return freq_factor, data_format, z0, port_names_raw, all_values

    # -------------------- 布局判定 --------------------

    @staticmethod
    def _resolve_layout(num_ports, all_values):
        """判定每个频点的数值个数与是否为重复频率格式。

        standard: 1 + 2N²  (1 个频率 + 全部 N² 个复数对)
        repeated: N + 2N²  (N 行, 每行 1 个频率 + N 个复数对)

        两种布局都能整除时 (如 2 端口 repeated 恰有 9 的倍数个频点),
        按取出的"频率列"单调性打分消歧, 平局取 standard。
        """
        total = len(all_values)
        vpf_std = 1 + 2 * num_ports * num_ports
        vpf_rep = num_ports + 2 * num_ports * num_ports
        std_ok = total % vpf_std == 0
        rep_ok = total % vpf_rep == 0

        if std_ok and not rep_ok:
            return vpf_std, False
        if rep_ok and not std_ok:
            return vpf_rep, True
        if std_ok and rep_ok:
            def monotonic_score(vpf):
                freqs = np.asarray(all_values[::vpf], dtype=float)
                diffs = np.diff(freqs)
                return float(np.mean(diffs > 0)) if diffs.size else 0.5
            if monotonic_score(vpf_rep) > monotonic_score(vpf_std):
                return vpf_rep, True
            return vpf_std, False

        n_std = total // vpf_std
        n_rep = total // vpf_rep
        if n_rep > 0 and n_rep > n_std:
            return vpf_rep, True
        return vpf_std, False

    @classmethod
    def _infer_port_count(cls, all_values):
        """文件名无 .sNp 后缀时推断端口数。

        枚举所有 (端口数, 格式) 整除候选，按频率序列单调递增的比例打分：
        频率列永远单调递增，误判布局下取出的"频率"几乎必然乱序。
        平局时优先更大端口数与 standard 格式。
        """
        total = len(all_values)
        best_key, best_n = None, None
        for n in range(1, 65):
            for use_rep, vpf in ((False, 1 + 2 * n * n), (True, n + 2 * n * n)):
                if vpf > total or total % vpf != 0:
                    continue
                freqs = np.asarray(all_values[::vpf], dtype=float)
                diffs = np.diff(freqs)
                score = float(np.mean(diffs > 0)) if diffs.size else 1.0
                key = (score, n, 0 if use_rep else 1)
                if best_key is None or key > best_key:
                    best_key, best_n = key, n
        if best_n is None or best_key[0] < 0.5:
            raise ValueError('无法从文件确定端口数量'
                             '（请将文件重命名为标准 .sNp 格式后重试）')
        return best_n

    # -------------------- 矩阵填充 (向量化) --------------------

    @staticmethod
    def _fill_matrices(values, num_ports, num_freqs, values_per_freq,
                       use_repeated, freq_factor, data_format):
        """按 Touchstone 数据顺序填充频率向量与 S 参数矩阵。

        2 端口 (standard 与 repeated) 数据顺序均为 S11 S21 S12 S22 (列优先),
        N>2 端口为行优先；repeated 格式每个频率重复 N 行。
        """
        if num_ports == 2:
            s_order = [(0, 0), (1, 0), (0, 1), (1, 1)]
        else:
            s_order = [(i, j) for i in range(num_ports) for j in range(num_ports)]

        def to_complex(pairs):
            v1, v2 = pairs[..., 0], pairs[..., 1]
            if data_format == 'RI':
                return v1 + 1j * v2
            if data_format == 'MA':
                return v1 * np.exp(1j * np.radians(v2))
            return (10.0 ** (v1 / 20.0)) * np.exp(1j * np.radians(v2))  # DB

        frequencies = np.zeros(num_freqs)
        s_params = np.zeros((num_ports, num_ports, num_freqs), dtype=complex)

        if not use_repeated:
            mat = values.reshape(num_freqs, values_per_freq)
            frequencies = mat[:, 0] * freq_factor
            block = to_complex(mat[:, 1:].reshape(num_freqs, num_ports * num_ports, 2))
            for k, (i, j) in enumerate(s_order):
                s_params[i, j, :] = block[:, k]
        else:
            mat = values.reshape(num_freqs, num_ports, 1 + 2 * num_ports)
            frequencies = mat[:, 0, 0] * freq_factor
            blocks = to_complex(mat[:, :, 1:].reshape(
                num_freqs, num_ports, num_ports, 2))
            for row in range(num_ports):
                for k in range(num_ports):
                    i, j = s_order[row * num_ports + k]
                    s_params[i, j, :] = blocks[:, row, k]
        return frequencies, s_params


# ==================== S 参数工具函数 ====================

def _extract_ports(s_params, ports):
    """按 1 起始端口号抽取子矩阵 (返回新数组, 不改原矩阵)。"""
    port_indices = np.asarray(ports, dtype=int) - 1
    return s_params[port_indices][:, port_indices]


def snp2smp(s_params, z0_old, ports, z0_new):
    """
    端口重排与阻抗归一化 (对应 MATLAB snp2smp)

    公式: S_new = (S - Γ·I) · (I - Γ·S)^(-1)
    其中 Γ = (Z0_new - Z0_old) / (Z0_new + Z0_old)

    ports 为 1 起始端口号列表; 全部频点一次性批量处理。
    """
    s_reordered = _extract_ports(s_params, ports)

    if z0_old == z0_new:
        return s_reordered

    n = len(ports)
    gamma = (z0_new - z0_old) / (z0_new + z0_old)
    eye = np.eye(n)
    S = np.transpose(s_reordered, (2, 0, 1))                     # (F, n, n)
    S_new = (S - gamma * eye) @ np.linalg.inv(eye - gamma * S)
    return np.transpose(S_new, (1, 2, 0))


def s2sdd(s_params):
    """
    单端 S 参数转差分 S 参数 (Mixed-Mode 转换)

    公式: S_dd[i,j] = 0.5 * (S[2i,2j] - S[2i,2j+1] - S[2i+1,2j] + S[2i+1,2j+1])
    """
    s = s_params
    return 0.5 * (s[0::2, 0::2, :] - s[0::2, 1::2, :]
                  - s[1::2, 0::2, :] + s[1::2, 1::2, :])


# ==================== 差分端口配对 ====================
#
# 配对是被测件 (DUT) 封装引脚排列的固有属性, 与勾选顺序无关。
# power_sum_db 按"列表中相邻两个为一对"解释攻击端口, 因此界面层
# 必须先用 order_ports_by_pairs 把勾选端口重排成对子相邻的顺序,
# 否则交错编号的对子 (如排列二的 (1,3)/(2,4)) 会被静默错配成
# (1,2)/(3,4), 直接算错 PSNEXT/PSFEXT。

def diff_pair(port, num_ports, arrange):
    """返回 port 所属的差分对 (p, n) 元组 (1 起始, 升序)。

    arrange='Type1': 同侧连续编号, 对子为 (1,2) (3,4) ...;
    arrange='Type2': 同侧奇偶交错编号, 对子为 (1,3) (2,4) (5,7) (6,8) ...。
    结果可能超出 num_ports (调用方负责边界检查)。
    """
    if arrange == 'Type1':
        return (port, port + 1) if port % 2 == 1 else (port - 1, port)
    if port % 2 == 1:  # Type2 同侧 (奇数端口)
        idx = (port - 1) // 4
        return (4 * idx + 1, 4 * idx + 3)
    idx = (port - 2) // 4
    return (4 * idx + 2, 4 * idx + 4)


def order_ports_by_pairs(ports, num_ports, arrange):
    """把端口列表重排成"对子相邻"的顺序, 供 power_sum_db 两两配对。

    按 (对子最小编号, 对内位置) 稳定排序; 对子不完整 (只勾选了一个)
    时顺序仍是确定的, 由调用方另行校验。
    """
    def key(p):
        pair = diff_pair(p, num_ports, arrange)
        return (min(pair), 0 if p == pair[0] else 1)
    return sorted(ports, key=key)


def find_broken_pairs(ports, num_ports, arrange):
    """返回对子不完整 (配对端口未同列勾选) 的端口列表。"""
    selected = set(ports)
    return [p for p in ports
            if any(q not in selected for q in diff_pair(p, num_ports, arrange))]


def safe_db(s_complex):
    """安全地将复数 S 参数转换为 dB 值，零值用 eps 替换"""
    mag = np.abs(s_complex)
    mag = np.where(mag == 0, np.finfo(float).eps, mag)
    return 20.0 * np.log10(mag)


def resample_sparams(spara, z0, fmin_ghz, fmax_ghz, fstep_ghz):
    """阻抗归一化 + 频点重采样一次性预处理, 返回 (params, freqs_hz)。

    按 fmin/fmax/fstep 把 S 参数重组到等间隔网格; 对每个 S[i,j]
    的实部/虚部分别做 PCHIP 插值 (保单调不过冲, 不在 dB/功率域
    插值, 避免对数域过冲经指数放大成虚假尖峰); 网格超出文件频率
    范围的部分取边界值 (平坦延拓)。全程不修改 spara.parameters。
    调用方应在基本参数变化时重建缓存, 计算时复用。
    """
    spara_param = spara.parameters
    if abs(spara.z0 - z0) > 1e-10:
        all_ports = list(range(1, spara_param.shape[0] + 1))
        spara_param = snp2smp(spara_param, spara.z0, all_ports, z0)

    fmin, fmax, fstep = fmin_ghz * 1e9, fmax_ghz * 1e9, fstep_ghz * 1e9
    num_points = max(2, int(round((fmax - fmin) / fstep)) + 1)
    fn = np.linspace(fmin, fmax, num_points)

    x = spara.frequencies
    n_ports = spara_param.shape[0]
    out = np.zeros((n_ports, n_ports, num_points), dtype=complex)
    for i in range(n_ports):
        for j in range(n_ports):
            re = PchipInterpolator(x, spara_param[i, j, :].real,
                                   extrapolate=False)(fn)
            im = PchipInterpolator(x, spara_param[i, j, :].imag,
                                   extrapolate=False)(fn)
            # 量程外平坦延拓到边界值
            re = np.where(np.isfinite(re), re,
                          np.interp(fn, x, spara_param[i, j, :].real))
            im = np.where(np.isfinite(im), im,
                          np.interp(fn, x, spara_param[i, j, :].imag))
            out[i, j, :] = re + 1j * im
    return out, fn


# ==================== PowerSum 功率和计算 ====================

def power_sum_db(spara, z0, v_ports, f_ports, n_ports, is_diff,
                 fmin_ghz, fmax_ghz, formulas=None):
    """计算 PSFEXT / PSNEXT / PSXT 功率和曲线。

    spara: SParameter 实例; 端口号均为 1 起始。
    差分模式: v_ports 恰好 2 个, f/n_ports 按"列表中相邻两个为一对"
      处理 (s2sdd 混合模转换); 对子顺序由调用方按 DUT 引脚排列
      整理好 (见 order_ports_by_pairs), 本函数不猜测配对关系;
    单端模式: v_ports 1 个, f/n_ports 逐端口取 S[v, k]。
    formulas: dict, 含 FEXT_dB/NEXT_dB/PSFEXT/PSNEXT/PSXT 五条公式
      (缺省用 DEFAULT_FORMULAS); 公式环境提供二维 dB 矩阵
      RAW_FEXT_dB / RAW_NEXT_dB (行=攻击路径, 列=频点),
      PSXT 公式可引用已求出的 PSFEXT/PSNEXT。
    返回 dict: fn_ghz / fn_hz / deltaf_hz / MDFEXT / MDNEXT / PSXT /
      has_fext / has_next / fext_power_lin / next_power_lin。

    不做任何插值: 计算直接在 spara.frequencies 给定的频点网格上进行,
    fmin/fmax 仅作范围筛选; 插值重组由调用方先用 resample_sparams
    完成 (app 的重采样缓存), 本函数对原始网格同样适用, 结果与
    逐点手工计算严格一致。
    不保留任何计算缓存: 每次调用从 spara.parameters 完整重算;
    全程不修改 spara.parameters (归一化/抽取均产生新数组)。
    """
    formulas = dict(DEFAULT_FORMULAS, **(formulas or {}))
    spara_freq = spara.frequencies
    spara_param = spara.parameters

    # Z0 阻抗归一化 (全端口一次完成, 不缓存, 每次重算)
    if abs(spara.z0 - z0) > 1e-10:
        all_ports = list(range(1, spara_param.shape[0] + 1))
        spara_param = snp2smp(spara_param, spara.z0, all_ports, z0)

    # 原始频点网格: fmin/fmax 只筛选范围, 不生成新频点
    fmin, fmax = fmin_ghz * 1e9, fmax_ghz * 1e9
    mask = (spara_freq >= fmin) & (spara_freq <= fmax)
    if not np.any(mask):
        raise ValueError('所选频率范围内没有原始数据频点')
    fn = spara_freq[mask]
    fn_ghz = fn / 1e9

    # ICN 积分步长取原始频点的实际间隔 (非均匀网格取中位数)
    diffs = np.diff(fn) if len(fn) > 1 else np.diff(spara_freq)
    deltaf = float(np.median(diffs)) if diffs.size else 0.0

    def coupled_db(attack_ports):
        """攻击端口列表 -> 各耦合路径在原始频点上的 dB 曲线列表。"""
        if is_diff:
            curves = []
            for i in range(len(attack_ports) // 2):
                idx = 2 * i
                ports = [v_ports[0], v_ports[1],
                         attack_ports[idx], attack_ports[idx + 1]]
                # 全矩阵已归一化到目标 z0, 这里只需抽取 4 端口子矩阵
                s_diff = s2sdd(_extract_ports(spara_param, ports))
                curves.append(safe_db(s_diff[1, 0, mask]))
        else:
            curves = [safe_db(spara_param[v_ports[0] - 1, p - 1, mask])
                      for p in attack_ports]
        return curves

    def stack_paths(attack_ports):
        """耦合路径 dB 曲线堆叠成 (路径数, 频点数) 二维矩阵。"""
        curves = coupled_db(attack_ports)
        if not curves:
            return np.zeros((0, len(fn)))
        return np.array(curves)

    # 从 S 参数直接提取的原始耦合 dB 矩阵 (不经任何修饰)
    env = {'RAW_FEXT_dB': stack_paths(f_ports),
           'RAW_NEXT_dB': stack_paths(n_ports)}

    def eval_matrix(name):
        """FEXT_dB / NEXT_dB 公式求值, 结果形状须与 RAW 矩阵一致。"""
        raw = env['RAW_' + name]
        try:
            arr = np.asarray(evaluate_expr(formulas[name], env), dtype=float)
        except ValueError as e:
            raise ValueError(f'公式 {name}: {e}') from e
        if arr.shape != raw.shape:
            raise ValueError(
                f'公式 {name}: 结果形状 {arr.shape} 与 '
                f'RAW_{name} {raw.shape} 不一致')
        env[name] = arr

    def eval_curve(name):
        try:
            # 功率和为 0 (无耦合路径/空勾选) 时 log10(0) 产生 -inf,
            # 属预期中间态, 抑制告警后在下方统一钳位
            with np.errstate(divide='ignore', invalid='ignore'):
                arr = np.asarray(evaluate_expr(formulas[name], env),
                                 dtype=float)
        except ValueError as e:
            raise ValueError(f'公式 {name}: {e}') from e
        if arr.shape != fn.shape:
            raise ValueError(
                f'公式 {name}: 结果形状 {arr.shape} 与频点 {fn.shape} 不一致')
        # 非有限值 (空路径的 -inf 等) 钳位 -100 dB
        return np.where(np.isfinite(arr), arr, -100.0)

    eval_matrix('FEXT_dB')
    eval_matrix('NEXT_dB')
    mdfext = eval_curve('PSFEXT')
    env['PSFEXT'] = mdfext
    mdnext = eval_curve('PSNEXT')
    env['PSNEXT'] = mdnext
    psxt = eval_curve('PSXT')

    return {
        'fn_ghz': fn_ghz,
        'fn_hz': fn,
        'deltaf_hz': deltaf,
        'MDFEXT': mdfext,
        'MDNEXT': mdnext,
        'PSXT': psxt,
        'has_fext': len(f_ports) > 0,
        'has_next': len(n_ports) > 0,
        # ICN 积分所需的线性功率和, 由公式求值结果直接还原,
        # 与界面公式 pow(10,PSxEXT/10) 口径一致
        'fext_power_lin': 10.0 ** (mdfext / 10.0),
        'next_power_lin': 10.0 ** (mdnext / 10.0),
    }


# ==================== ICN / ccICN 集成串扰噪声 ====================
#
# 公式驱动的计算框架: 默认公式见 DEFAULT_FORMULAS, 与界面展示一致;
# 每个公式都是可在界面编辑的字符串, 经 evaluate_expr 安全求值。
# 求值环境 (env) 中的名称:
#   标量: Ant, Aft, fb, Tnt, Tft, PAM, ILn, ILf, fstep (输入变量),
#         deltaf, fr, ft_n, ft_f, sf (由公式得出, deltaf 默认 = fstep)
#   矢量: fn (Hz), PSNEXT, PSFEXT (线性功率和取 dB)
#   常量: pi, e
# 求值顺序: 标量中间量 -> 矢量 (Wnt/Wft/ChannelLoss) -> 标量结果。
#
# PSFEXT/PSNEXT/PSXT 与 FEXT_dB/NEXT_dB 五条公式在 power_sum_db 中
# 求值, 环境为:
#   RAW_FEXT_dB / RAW_NEXT_dB: (路径数, 频点数) 二维 dB 矩阵,
#     即从 S 参数直接提取的各攻击路径耦合 (单端: 20log10|S(v,k)|;
#     差分: 20log10|Sdd21(受害对, 攻击对k)|), 不经任何修饰;
#   FEXT_dB / NEXT_dB 公式默认恒等引用 RAW 矩阵, 可编辑
#     (如对数域加权), 结果形状必须与 RAW 矩阵一致;
#   psum: 按路径维 (axis=0) 求和的专用函数;
#   PSXT 公式可引用已求出的 PSFEXT / PSNEXT。
# 求值顺序: FEXT_dB -> NEXT_dB -> PSFEXT -> PSNEXT -> PSXT。

DEFAULT_FORMULAS = {
    'FEXT_dB': 'RAW_FEXT_dB',
    'NEXT_dB': 'RAW_NEXT_dB',
    'PSFEXT': '10*log10(psum(pow(10,FEXT_dB/10)))',
    'PSNEXT': '10*log10(psum(pow(10,NEXT_dB/10)))',
    'PSXT': '10*log10(pow(10,PSFEXT/10)+pow(10,PSNEXT/10))',
    'deltaf': 'fstep',
    'fr': '1.5*(fb/2)',
    'ft_n': '0.2365/Tnt',
    'ft_f': '0.2365/Tft',
    'sf': '(PAM+1)/(3*(PAM-1))',
    'Wnt': ('sf*(Ant*Ant/(fb))'
            '*pow(sin(pi*(fn/fb))/(pi*(fn/fb)),2)'
            '*(1/(1+pow(fn/ft_n,4)))*(1/(1+pow(fn/fr,8)))'),
    'Wft': ('sf*(Aft*Aft/(fb))'
            '*pow(sin(pi*(fn/fb))/(pi*(fn/fb)),2)'
            '*(1/(1+pow(fn/ft_f,4)))*(1/(1+pow(fn/fr,8)))'),
    'ChannelLoss_n': 'pow(10,(-2*ILn*fn/fb)/10)',
    'ChannelLoss_f': 'pow(10,(-2*ILf*fn/fb)/10)',
    'ICN_n': 'sqrt(2*deltaf*sum(Wnt*pow(10,PSNEXT/10)))',
    'ICN_f': 'sqrt(2*deltaf*sum(Wft*pow(10,PSFEXT/10)))',
    'ICN': 'sqrt(ICN_n*ICN_n+ICN_f*ICN_f)',
    'ccICN_n': 'sqrt(2*deltaf*sum(Wnt*pow(10,PSNEXT/10)*ChannelLoss_n))',
    'ccICN_f': 'sqrt(2*deltaf*sum(Wft*pow(10,PSFEXT/10)*ChannelLoss_f))',
    'ccICN': 'sqrt(ccICN_n*ccICN_n+ccICN_f*ccICN_f)',
}

# 公式中允许调用的函数 (白名单)
_EXPR_FUNCS = {
    'sin': np.sin, 'cos': np.cos, 'tan': np.tan,
    'asin': np.arcsin, 'acos': np.arccos, 'atan': np.arctan,
    'sqrt': np.sqrt, 'exp': np.exp, 'log': np.log, 'log10': np.log10,
    'abs': np.abs, 'pow': np.power,
    'sum': np.sum, 'min': np.min, 'max': np.max,
    # psum: 沿路径维 (axis=0) 求和, 专供 PSFEXT/PSNEXT 公式
    # 对 (路径数, 频点数) 二维 dB/功率矩阵逐频点聚合
    'psum': lambda a: np.sum(a, axis=0),
}
_EXPR_CONSTS = {'pi': np.pi, 'e': np.e}
_EXPR_BINOPS = {
    ast.Add: np.add, ast.Sub: np.subtract, ast.Mult: np.multiply,
    ast.Div: np.true_divide, ast.Pow: np.power,
}


def _eval_node(node, env):
    """递归求值白名单 AST 节点; 任何越界构造都抛 ValueError。"""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise ValueError(f'不允许的常量: {node.value!r}')
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        if node.id in _EXPR_CONSTS:
            return _EXPR_CONSTS[node.id]
        raise ValueError(f'未知变量: {node.id}')
    if isinstance(node, ast.BinOp) and type(node.op) in _EXPR_BINOPS:
        left = _eval_node(node.left, env)
        right = _eval_node(node.right, env)
        try:
            with np.errstate(divide='ignore', invalid='ignore',
                             over='ignore'):
                return _EXPR_BINOPS[type(node.op)](left, right)
        except (ZeroDivisionError, OverflowError, FloatingPointError) as e:
            raise ValueError(f'运算失败: {e}') from e
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        operand = _eval_node(node.operand, env)
        return operand if isinstance(node.op, ast.UAdd) else -operand
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        func = _EXPR_FUNCS.get(node.func.id)
        if func is None:
            raise ValueError(f'不允许的函数: {node.func.id}')
        if node.keywords:
            raise ValueError('不允许关键字参数')
        args = [_eval_node(a, env) for a in node.args]
        try:
            return func(*args)
        except (TypeError, ValueError, ZeroDivisionError,
                OverflowError, FloatingPointError) as e:
            raise ValueError(f'函数 {node.func.id} 调用失败: {e}') from e
    raise ValueError(f'不允许的表达式元素: {type(node).__name__}')


def evaluate_expr(expr, env=None):
    """安全求值数学表达式字符串。

    支持: 数字 (含科学计数法), env 中的变量, pi/e,
    + - * / ** (与 pow), 括号, 白名单函数 (sin/cos/sqrt/pow/sum/...)。
    禁止属性访问、下标、lambda、任意名称调用等一切其他构造。
    """
    env = {} if env is None else env
    expr = str(expr).strip()
    if not expr:
        raise ValueError('表达式为空')
    if len(expr) > 5000:
        raise ValueError(f'表达式过长 ({len(expr)} 字符, 上限 5000)')
    try:
        tree = ast.parse(expr, mode='eval')
    except SyntaxError as e:
        raise ValueError(f'语法错误: {e.msg}') from e
    except RecursionError:
        raise ValueError('表达式嵌套过深') from None
    try:
        return _eval_node(tree.body, env)
    except RecursionError:
        # 深层嵌套表达式 (如 ((((...1...))))x数千层) 会耗尽递归栈,
        # RecursionError 不在调用方的 ValueError 捕获范围内,
        # 统一转成 ValueError, 避免经 trace 回调向上传播导致崩溃
        raise ValueError('表达式嵌套过深') from None


def _as_scalar(value, name):
    """公式结果必须是有限标量。"""
    arr = np.asarray(value, dtype=float)
    if arr.size != 1:
        raise ValueError(f'{name} 的结果必须是标量, 实际 {arr.size} 个元素')
    result = float(arr.ravel()[0])
    if not np.isfinite(result):
        raise ValueError(f'{name} 的结果不是有限数: {result}')
    return result


def icn_calculate(fn_hz, fext_power_lin, next_power_lin,
                  scalars, formulas=None):
    """按公式计算 ICN / ccICN (结果单位 V)。

    参数:
        fn_hz:           频率网格 (Hz), 来自 power_sum_db 的 fn 网格
        fext_power_lin:  FEXT 线性功率和 (power_sum_db 的 fext_power_lin)
        next_power_lin:  NEXT 线性功率和 (power_sum_db 的 next_power_lin)
        scalars:         dict, 已求值为 float 的标量输入:
                         Ant/Aft (V, 中轴到 peak), fb (Hz),
                         Tnt/Tft (s), PAM, ILn/ILf (dB, 允许由表达式求出),
                         fstep (Hz, 重采样网格间隔; deltaf 默认引用它)
        formulas:        dict, 键为 DEFAULT_FORMULAS 的 20 个名称;
                         缺省用默认公式; deltaf 默认公式为 'fstep',
                         可在公式区改为任意表达式

    返回 dict (单位 V): icn_n / icn_f / icn / ccicn_n / ccicn_f / ccicn。
    """
    formulas = dict(DEFAULT_FORMULAS, **(formulas or {}))

    fn = np.asarray(fn_hz, dtype=float)
    env = dict(scalars)
    env['fn'] = fn
    # 规范口径: PSNEXT/PSFEXT 是 dB 值 (= 10*log10(线性功率和)),
    # 公式中的 pow(10,PSNEXT/10) 据此还原线性功率
    env['PSFEXT'] = 10.0 * np.log10(np.asarray(fext_power_lin, dtype=float))
    env['PSNEXT'] = 10.0 * np.log10(np.asarray(next_power_lin, dtype=float))

    # 1) 标量中间量: deltaf (默认=fstep), fr, ft_n, ft_f, sf
    for name in ('deltaf', 'fr', 'ft_n', 'ft_f', 'sf'):
        try:
            env[name] = _as_scalar(evaluate_expr(formulas[name], env), name)
        except ValueError as e:
            raise ValueError(f'公式 {name}: {e}') from e
    if env['deltaf'] <= 0:
        raise ValueError(f'deltaf 必须大于 0, 实际 {env["deltaf"]}')

    # 2) 矢量: Wnt, Wft, ChannelLoss_n, ChannelLoss_f
    for name in ('Wnt', 'Wft', 'ChannelLoss_n', 'ChannelLoss_f'):
        try:
            arr = np.asarray(evaluate_expr(formulas[name], env), dtype=float)
        except ValueError as e:
            raise ValueError(f'公式 {name}: {e}') from e
        if arr.shape != fn.shape:
            raise ValueError(
                f'公式 {name}: 结果形状 {arr.shape} 与 fn {fn.shape} 不一致')
        if not np.all(np.isfinite(arr)):
            raise ValueError(f'公式 {name}: 结果含非有限值')
        env[name] = arr

    # 3) 标量结果 (逐个写回 env, 后续公式可引用, 如 ICN 依赖 ICN_n/ICN_f)
    out = {}
    for name in ('ICN_n', 'ICN_f', 'ICN', 'ccICN_n', 'ccICN_f', 'ccICN'):
        try:
            out[name.lower()] = _as_scalar(evaluate_expr(formulas[name], env),
                                           name)
        except ValueError as e:
            raise ValueError(f'公式 {name}: {e}') from e
        env[name] = out[name.lower()]
    return out
