"""S参数解析与 PowerSum 计算的回归测试。

对比两种基线:
1. 旧版快照 (tests/snapshot_v3.3.json, 由重构前的 main.py 生成):
   除白名单中"已知修复"的用例外, 新实现输出必须与旧版一致;
2. 解析解 (由 tests/synth_touchstone.py 的确定性公式直接推得):
   验证白名单用例的修复结果以及数学工具函数的正确性。

用法: python tests/test_sparam.py
"""

import sys
import tempfile
import shutil
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(HERE))

import sparam_core as core  # noqa: E402
import synth_touchstone as synth  # noqa: E402

SNAPSHOT_FILE = HERE / 'snapshot_v3.3.json'

# 旧版存在 bug、新版已修复的用例: 跳过快照对比, 改为解析解断言
# s2p_ri_rep9: F=9 时 90 个值同时被 standard(9)/repeated(10) 整除,
# 旧版无条件按 standard 解析出垃圾矩阵 (快照里的负频率即是证据)
EXPECTED_FIX_CASES = {'s2p_ri_rep2', 's2p_ri_rep9', 'noext_ri_4port'}

RTOL = 1e-8
ATOL = 1e-9


def assert_close(actual, expected, msg):
    np.testing.assert_allclose(actual, expected, rtol=RTOL, atol=ATOL,
                               err_msg=msg)


def c2l(arr):
    arr = np.asarray(arr)
    return np.stack([arr.real, arr.imag], axis=-1)


# -------------------- 快照对比 --------------------

def test_parse_matches_old_snapshot(cases, snap):
    for name, data in snap['parse'].items():
        if name in EXPECTED_FIX_CASES:
            continue
        sp = core.SParameter(str(cases[name]))
        assert sp.num_ports == data['num_ports'], f'{name}: num_ports 不一致'
        np.testing.assert_allclose(sp.z0, data['z0'], rtol=0, atol=0)
        assert list(sp.port_names) == data['port_names'], f'{name}: 端口名不一致'
        assert_close(sp.frequencies, np.asarray(data['frequencies']),
                     f'{name}: 频率与旧版不一致')
        assert_close(c2l(sp.parameters), np.asarray(data['parameters']),
                     f'{name}: S参数与旧版不一致')
    print(f'  PASS 解析结果与旧版快照一致 '
          f'({len(snap["parse"]) - len(EXPECTED_FIX_CASES)} 个用例)')


def test_math_matches_old_snapshot(cases, snap):
    sp8 = core.SParameter(str(cases['s8p_ri_z0_75']))

    renorm = core.snp2smp(sp8.parameters, 75.0, [1, 2, 5, 6], 50.0)
    reorder = core.snp2smp(sp8.parameters, 75.0, [1, 2, 5, 6], 75.0)
    sdd = core.s2sdd(renorm)
    assert_close(c2l(renorm), np.asarray(snap['math']['snp2smp_renorm']),
                 'snp2smp 阻抗归一化与旧版不一致')
    assert_close(c2l(reorder), np.asarray(snap['math']['snp2smp_reorder']),
                 'snp2smp 端口重排与旧版不一致')
    assert_close(c2l(sdd), np.asarray(snap['math']['s2sdd_renorm']),
                 's2sdd 与旧版不一致')

    for key, v, f, n, is_diff in (
            ('power_diff', [1, 2], [5, 6, 7, 8], [3, 4], True),
            ('power_se', [1], [5, 6], [7, 8], False)):
        result = core.power_sum_db(sp8, 50.0, v, f, n, is_diff,
                                   0.05, 1.15, 0.01)
        ref = snap['math'][key]
        assert result['has_fext'] == ref['has_fext'], f'{key}: has_fext 不一致'
        assert result['has_next'] == ref['has_next'], f'{key}: has_next 不一致'
        for curve in ('fn_ghz', 'MDFEXT', 'MDNEXT', 'PSXT'):
            assert_close(np.asarray(result[curve]), np.asarray(ref[curve]),
                         f'{key}.{curve} 与旧版不一致')
    print('  PASS snp2smp / s2sdd / 功率和计算与旧版快照一致')


# -------------------- 修复用例的解析解断言 --------------------

def test_fixed_2port_repeated_format(cases):
    """旧版把 2 端口 repeated 格式按 standard 误读, 新版正确解析。

    覆盖两种触发路径: rep2 (仅 repeated 整除) 与 rep9 (standard/repeated
    同时整除的布局歧义, 需按频率单调性消歧)。
    """
    for name, num_freqs in (('s2p_ri_rep2', 2), ('s2p_ri_rep9', 9)):
        sp = core.SParameter(str(cases[name]))
        assert sp.num_ports == 2
        expected_freqs = np.array([1e8 + 1e8 * k for k in range(num_freqs)])
        assert_close(sp.frequencies, expected_freqs, f'{name} 频率错误')
        for f_idx in range(num_freqs):
            expected = np.asarray(synth.gen_matrix(2, 0.1 * (f_idx + 1)))
            assert_close(sp.parameters[:, :, f_idx], expected,
                         f'{name} S 参数错误 (freq #{f_idx})')
    print('  PASS 修复: 2 端口 repeated-frequency 格式正确解析 '
          '(含布局歧义消歧)')


def test_fixed_port_count_inference(cases):
    """旧版对无后缀文件总会误判为 1 端口, 新版按频率单调性选优。"""
    sp = core.SParameter(str(cases['noext_ri_4port']))
    assert sp.num_ports == 4, f'应推断为 4 端口, 实际 {sp.num_ports}'
    assert list(sp.port_names) == [f'DDR0_DQ{k}' for k in range(4)]
    expected_freqs = np.array([1e8 + 1e8 * k for k in range(11)])
    assert_close(sp.frequencies, expected_freqs, '无后缀文件频率错误')
    for f_idx, f_ghz in enumerate([0.1 + 0.1 * k for k in range(11)]):
        expected = np.asarray(synth.gen_matrix(4, f_ghz))
        assert_close(sp.parameters[:, :, f_idx], expected,
                     f'无后缀文件 S 参数错误 (freq #{f_idx})')
    print('  PASS 修复: 无 .sNp 后缀文件端口数推断正确 (4 端口)')


def test_standard_layout_wins_ambiguity(cases):
    """standard 2 端口 F=10 (90 值) 同时被两种布局整除时仍应判为 standard。"""
    sp = core.SParameter(str(cases['s2p_ri_std10']))
    assert sp.num_ports == 2
    expected_freqs = np.array([1e8 + 1e8 * k for k in range(10)])
    assert_close(sp.frequencies, expected_freqs, 'standard F=10 频率错误')
    for f_idx in range(10):
        expected = np.asarray(synth.gen_matrix(2, 0.1 * (f_idx + 1)))
        assert_close(sp.parameters[:, :, f_idx], expected,
                     f'standard F=10 S 参数错误 (freq #{f_idx})')
    print('  PASS 消歧: standard 布局在两种布局可整除时仍被正确识别')


def test_port_count_inference_rejects_garbage(tmp):
    """无任何单调频率候选时应报错而非误判。"""
    garbage = tmp / 'garbage.dat'
    garbage.write_text('# GHZ S RI R 50\n' + ('5 5 5 5 5 5 5 5 5\n' * 10),
                       encoding='ascii')
    try:
        core.SParameter(str(garbage))
    except ValueError as e:
        assert '端口数量' in str(e)
    else:
        raise AssertionError('垃圾数据应抛出"无法确定端口数量"')
    print('  PASS 无候选布局时给出明确错误')


# -------------------- 数学工具解析解断言 --------------------

def test_s2sdd_formula():
    n, m, num_freqs = 4, 2, 3
    s = (np.arange(n * n * num_freqs).reshape(n, n, num_freqs)
         + 1j * np.arange(n * n * num_freqs).reshape(n, n, num_freqs)[::-1])
    sdd = core.s2sdd(s)
    for i in range(m):
        for j in range(m):
            expected = 0.5 * (s[2 * i, 2 * j, :] - s[2 * i, 2 * j + 1, :]
                              - s[2 * i + 1, 2 * j, :] + s[2 * i + 1, 2 * j + 1, :])
            assert_close(sdd[i, j, :], expected, f's2sdd 公式错误 [{i},{j}]')
    print('  PASS s2sdd 混合模转换公式正确')


def test_snp2smp_renorm_formula():
    rng = np.random.default_rng(42)
    s = (rng.uniform(-0.5, 0.5, (2, 2, 5))
         + 1j * rng.uniform(-0.5, 0.5, (2, 2, 5)))
    out = core.snp2smp(s, 50.0, [1, 2], 75.0)
    gamma = (75.0 - 50.0) / (75.0 + 50.0)
    eye = np.eye(2)
    for f in range(5):
        S = s[:, :, f]
        expected = (S - gamma * eye) @ np.linalg.inv(eye - gamma * S)
        assert_close(out[:, :, f], expected, 'snp2smp 归一化公式错误')
    # 端口重排与 z0 不变时应原样返回
    s3 = rng.uniform(-0.5, 0.5, (3, 3, 2)) + 0j
    reordered = core.snp2smp(s3, 50.0, [3, 1], 50.0)
    assert_close(reordered, s3[[2, 0]][:, [2, 0]], 'snp2smp 重排错误')
    print('  PASS snp2smp 归一化与端口重排正确')


def test_safe_db_and_interp():
    assert core.safe_db(np.array([0.0]))[0] == 20 * np.log10(np.finfo(float).eps)
    assert_close(core.safe_db(np.array([0.1])), np.array([20 * np.log10(0.1)]),
                 'safe_db 错误')

    x = np.array([1.0, 2.0, 3.0])
    y = np.array([np.nan, -1.0, np.inf])
    out = core.interp_spline(x, y, np.array([1.0, 2.0, 3.0]))
    assert np.all(np.isfinite(out)), 'interp_spline 应兜底非有限值'
    assert out[1] == -1.0
    const = core.interp_spline(np.array([1.0]), np.array([-3.0]),
                               np.array([1.5, 2.5]))
    assert_close(const, np.array([-3.0, -3.0]), '单点插值应为常量')
    print('  PASS safe_db / interp_spline 兜底逻辑正确')


# -------------------- 主入口 --------------------

def main():
    snap = None
    if SNAPSHOT_FILE.exists():
        import json
        snap = json.loads(SNAPSHOT_FILE.read_text(encoding='utf-8'))

    tmp = Path(tempfile.mkdtemp(prefix='snp_test_'))
    try:
        cases = synth.generate_all(tmp)
        print('运行回归测试...')
        if snap:
            test_parse_matches_old_snapshot(cases, snap)
            test_math_matches_old_snapshot(cases, snap)
        else:
            print('  SKIP 未找到快照文件, 仅运行解析解断言')
        test_fixed_2port_repeated_format(cases)
        test_standard_layout_wins_ambiguity(cases)
        test_fixed_port_count_inference(cases)
        test_port_count_inference_rejects_garbage(tmp)
        test_s2sdd_formula()
        test_snp2smp_renorm_formula()
        test_safe_db_and_interp()
        print('全部测试通过 ✓')
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
