"""Touchstone S参数解析与 PowerSum 串扰计算核心。

纯数值模块，不依赖任何 GUI 库：
- SParameter: Touchstone .sNp 文件解析器
- snp2smp / s2sdd / safe_db / interp_curve: S 参数数学工具
- power_sum_db: PSFEXT / PSNEXT / PSXT 功率和计算

从 V3.3 单文件版本重构而来，解析语义与原实现保持一致，
仅修复以下边缘问题：
- 2 端口文件的 repeated-frequency 格式现在能被正确识别（原来按 standard 误读）；
- 文件名无 .sNp 后缀时，端口数推断改为对所有整除候选按"频率序列单调性"
  打分选优（原来取第一个能整除的 n，几乎总会误判成 1 端口）；
- standard / repeated 布局同时整除时（如 2 端口 repeated 恰有 9 的倍数个频点），
  按频率列单调性消歧（原来无条件选 standard，会静默解析出垃圾矩阵）；
- 插值改到线性功率域并采用单调 Hermite (PCHIP)，原 dB 域样条的过冲
  经 10**(dB/10) 还原后会被指数放大成虚假尖峰（见 power_sum_db）；
- interp_curve 忽略 NaN/-inf 点而不是先替换成 -100，避免振铃过冲；
- 数据行含非法 token 时整行丢弃，不再把行内前半段数字残留进数据流。
"""

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

def snp2smp(s_params, z0_old, ports, z0_new):
    """
    端口重排与阻抗归一化 (对应 MATLAB snp2smp)

    公式: S_new = (S - Γ·I) · (I - Γ·S)^(-1)
    其中 Γ = (Z0_new - Z0_old) / (Z0_new + Z0_old)

    ports 为 1 起始端口号列表; 全部频点一次性批量处理。
    """
    port_indices = np.asarray(ports, dtype=int) - 1
    s_reordered = s_params[port_indices][:, port_indices]

    if z0_old == z0_new:
        return s_reordered

    n = len(port_indices)
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


def safe_db(s_complex):
    """安全地将复数 S 参数转换为 dB 值，零值用 eps 替换"""
    mag = np.abs(s_complex)
    mag = np.where(mag == 0, np.finfo(float).eps, mag)
    return 20.0 * np.log10(mag)


def interp_curve(x_src, y_src, x_dst, fill_value=-100.0):
    """单调分段三次 Hermite 插值 (PCHIP)，忽略 -inf / NaN 点

    选用 PCHIP 而非普通三次样条的原因：PCHIP 保单调、不过冲，
    插值结果严格介于相邻节点值之间。功率包络在节点间不应出现
    超出邻域的峰/谷，普通样条的振铃过冲会把假峰引入功率和。
    忽略非有限点（而非用常数填充后插值），有限点不足 2 个时
    退化为常量填充。
    fill_value: 量程外与非有限结果的钳位值（缺省 -100 为 dB 域约定）。
    """
    y_src = np.asarray(y_src, dtype=float)
    x_src = np.asarray(x_src, dtype=float)
    x_dst = np.asarray(x_dst, dtype=float)

    good = np.isfinite(y_src)
    if np.count_nonzero(good) >= 2:
        cs = PchipInterpolator(x_src[good], y_src[good], extrapolate=False)
        result = cs(x_dst)
        return np.where(np.isfinite(result), result, fill_value)

    fill = float(np.mean(y_src[good])) if np.any(good) else fill_value
    return np.full_like(x_dst, fill)


# ==================== PowerSum 功率和计算 ====================

def power_sum_db(spara, z0, v_ports, f_ports, n_ports, is_diff,
                 fmin_ghz, fmax_ghz, fstep_ghz):
    """计算 PSFEXT / PSNEXT / PSXT 功率和曲线。

    spara: SParameter 实例; 端口号均为 1 起始。
    差分模式: v_ports 恰好 2 个, f/n_ports 按对处理 (s2sdd 混合模转换);
    单端模式: v_ports 1 个, f/n_ports 逐端口取 S[v, k]。
    返回 dict: fn_ghz / MDFEXT / MDNEXT / PSXT / has_fext / has_next。
    """
    spara_freq = spara.frequencies
    spara_param = spara.parameters

    # Z0 阻抗归一化 (全端口一次完成)。
    # 归一化结果按 (z0, 矩阵) 缓存在 spara 实例上:
    # 输入防抖下连续重算时避免重复 O(F·N³) 求逆。
    cache = getattr(spara, '_renorm_cache', None)
    if cache is not None and cache[0] == z0:
        spara_param = cache[1]
    elif abs(spara.z0 - z0) > 1e-10:
        all_ports = list(range(1, spara_param.shape[0] + 1))
        spara_param = snp2smp(spara_param, spara.z0, all_ports, z0)
        spara._renorm_cache = (z0, spara_param)

    fmin, fmax, fstep = fmin_ghz * 1e9, fmax_ghz * 1e9, fstep_ghz * 1e9
    num_points = max(2, int(round((fmax - fmin) / fstep)) + 1)
    fn = np.linspace(fmin, fmax, num_points)
    fn_ghz = fn / 1e9

    def coupled_db(attack_ports):
        """攻击端口列表 -> 各耦合路径的 dB 曲线列表。"""
        if is_diff:
            curves = []
            for i in range(len(attack_ports) // 2):
                idx = 2 * i
                ports = [v_ports[0], v_ports[1],
                         attack_ports[idx], attack_ports[idx + 1]]
                s_diff = s2sdd(snp2smp(spara_param, z0, ports, z0))
                curves.append(safe_db(s_diff[1, 0, :]))
        else:
            curves = [safe_db(spara_param[v_ports[0] - 1, p - 1, :])
                      for p in attack_ports]
        return curves

    def power_sum(attack_ports):
        if not attack_ports:
            return np.zeros(len(fn))
        curves = coupled_db(attack_ports)
        # 不能在 dB 域插值: 样条在 dB 域的过冲, 经 10**(dB/10) 还原到
        # 线性功率后会被指数放大成虚假尖峰 (深谷旁的 -80dB 点可使过冲
        # 在线性域膨胀多个数量级)。因此先转线性功率域再插值;
        # PCHIP 保单调不过冲, 负值截断为 0 (功率非负)。
        # 量程外钳位 1e-10 (= -100 dB 的线性功率), 保持既有显示约定。
        powers = [10.0 ** (c / 10.0) for c in curves]
        interp = np.array([interp_curve(spara_freq, p, fn, fill_value=1e-10)
                           for p in powers])
        interp = np.clip(interp, 0.0, None)
        return np.sum(interp, axis=0)

    fext_power = power_sum(f_ports)
    next_power = power_sum(n_ports)
    total_power = fext_power + next_power

    eps_val = np.finfo(float).eps
    fext_power = np.where(fext_power == 0, eps_val, fext_power)
    next_power = np.where(next_power == 0, eps_val, next_power)
    total_power = np.where(total_power == 0, eps_val, total_power)

    return {
        'fn_ghz': fn_ghz,
        'MDFEXT': 10 * np.log10(fext_power),
        'MDNEXT': 10 * np.log10(next_power),
        'PSXT': 10 * np.log10(total_power),
        'has_fext': len(f_ports) > 0,
        'has_next': len(n_ports) > 0,
    }
