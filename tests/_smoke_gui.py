r"""GUI 端到端冒烟测试 —— 驱动真实 ttkbootstrap GUI 全链路。

用法 (PowerShell):
    & "c:\david\zcode_david\project2\.venv\Scripts\python.exe" "c:\david\zcode_david\project2\tests\_smoke_gui.py"

覆盖步骤:
  a) 启动: 窗口存在、端口表列齐全;
  b) 导入 sample_data/demo.s8p: file_loaded / 8 行端口表 / fmin 与文件频率一致;
  c) 自动勾选推演 (差分 Type1): Victim[1,2] / NEXT[3,4] / FEXT[7,8] + auto_calculate;
  d) 防抖重算: 修改 fmin 后 plot_data 按新频率重算;
  e) Mark Freq 峰值标注 (验证 numpy 导入修复): ax 上出现标注;
  f) 模式切换: 差分 <-> 单端推演并恢复;
  g) 保存配置 -> 清空勾选 -> 重导入自动恢复 (写 demo.s8p.json);
  h) 清理测试产物 (删除 sample_data/demo.s8p.json)。

实现说明:
- 创建 ttkbootstrap (cosmo) Window + SParamPowerSumApp, 窗口置顶保证截图可见;
- 所有测试步骤经 root.after 链式调度, 在 mainloop 事件循环内顺序执行;
- 启动前把 ttkbootstrap Messagebox 各弹窗方法替换为记录函数, 防止模态阻塞;
- 每阶段截图保存到 %TEMP%\\sparam_smoke\\<NN>_<名称>.png 并打印路径;
  优先用 PIL.ImageGrab.grab(bbox=窗口矩形) 截屏, 若截屏与窗口渲染不符
  (锁屏/被遮挡) 则回退 PrintWindow 窗口渲染, 保证截到 GUI;
- 全部通过: 打印 "SMOKE ALL PASS", 退出码 0;
  任一步失败: 打印明确失败步骤与原因, 退出码 1。
"""

import ctypes
import ctypes.wintypes as wt
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SAMPLE_PATH = PROJECT_ROOT / 'sample_data' / 'demo.s8p'
SHOT_DIR = Path(tempfile.gettempdir()) / 'sparam_smoke'

import ttkbootstrap as ttk  # noqa: E402
from PIL import Image, ImageChops, ImageGrab, ImageStat  # noqa: E402

import app as app_mod  # noqa: E402

MSGBOX_CALLS = []
PATCHED_MSGBOX = set()


def _patch_messagebox():
    """把 ttkbootstrap Messagebox 弹窗替换为记录函数, 避免模态框阻塞测试。"""
    mb = app_mod.ttk.Messagebox

    def make_recorder(name):
        def recorder(*args, **kwargs):
            MSGBOX_CALLS.append((name, ' | '.join(str(a) for a in args)))
            print(f'[msgbox-blocked] {name}: {args}', flush=True)
            return True
        return recorder

    for name in ('show_info', 'show_warning', 'show_error', 'show_question',
                 'ok', 'okcancel', 'yesno', 'yesnocancel', 'retrycancel'):
        if hasattr(mb, name):
            setattr(mb, name, make_recorder(name))
            PATCHED_MSGBOX.add(name)


class SmokeTest:
    """用 root.after 链式调度, 顺序执行所有测试步骤。"""

    def __init__(self, root, app):
        self.root = root
        self.app = app
        self.failures = []          # [(步骤名, 错误摘要)]
        self.queue = []             # [(前置延时ms, 步骤名, 函数)]
        self.shot_no = 0
        self.old_plot_ref = None
        self.new_fmin = None

    # -------------------- 调度 --------------------

    def add(self, delay_ms, name, func):
        self.queue.append((delay_ms, name, func))

    def start(self):
        self.root.after(0, self._next)

    def _next(self):
        if not self.queue:
            self._finish()
            return
        delay_ms, name, func = self.queue.pop(0)

        def runner():
            try:
                func()
                print(f'[ok] {name}', flush=True)
            except Exception:
                err = traceback.format_exc(limit=4)
                self.failures.append((name, err))
                print(f'[FAIL] {name}\n{err}', flush=True)
            self.root.after(1, self._next)

        self.root.after(max(0, int(delay_ms)), runner)

    def _finish(self):
        # h) 清理: 删除测试产生的同名配置文件, 避免污染仓库
        cfg_path = Path(str(SAMPLE_PATH) + '.json')
        try:
            if cfg_path.exists():
                cfg_path.unlink()
                print(f'[cleanup] 已删除测试产物: {cfg_path}', flush=True)
        except OSError as exc:
            self.failures.append(('h_cleanup', repr(exc)))

        print('=' * 64, flush=True)
        if self.failures:
            print(f'SMOKE FAILED —— {len(self.failures)} 个步骤失败:', flush=True)
            for name, err in self.failures:
                lines = err.strip().splitlines()
                print(f'  - {name}: {lines[-1] if lines else "(无详情)"}',
                      flush=True)
        else:
            print('SMOKE ALL PASS', flush=True)
        print(f'截图目录: {SHOT_DIR}', flush=True)
        self.root.after(50, self.root.destroy)

    # -------------------- 工具 --------------------

    def _printwindow_image(self):
        """用 PrintWindow 直接渲染窗口客户区 (锁屏/被遮挡时依然有效)。"""
        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        hwnd = user32.GetAncestor(self.root.winfo_id(), 2)  # GA_ROOT
        rc, cli, pt = wt.RECT(), wt.RECT(), wt.POINT()
        user32.GetWindowRect(hwnd, ctypes.byref(rc))
        user32.GetClientRect(hwnd, ctypes.byref(cli))
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
        w, h = rc.right - rc.left, rc.bottom - rc.top
        if w <= 0 or h <= 0:
            return None
        class BMIH(ctypes.Structure):
            _fields_ = [('biSize', wt.DWORD), ('biWidth', wt.LONG),
                        ('biHeight', wt.LONG), ('biPlanes', wt.WORD),
                        ('biBitCount', wt.WORD), ('biCompression', wt.DWORD),
                        ('biSizeImage', wt.DWORD), ('biXPelsPerMeter', wt.LONG),
                        ('biYPelsPerMeter', wt.LONG), ('biClrUsed', wt.DWORD),
                        ('biClrImportant', wt.DWORD)]

        hdc = user32.GetWindowDC(hwnd)
        mem_dc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        old_bmp = gdi32.SelectObject(mem_dc, bmp)
        try:
            if not user32.PrintWindow(hwnd, mem_dc, 2):  # PW_RENDERFULLCONTENT
                return None
            bmi = BMIH(ctypes.sizeof(BMIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
            buf = ctypes.create_string_buffer(w * h * 4)
            # GetDIBits 必须在 bitmap/DC 仍有效时调用
            if not gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(bmi), 0):
                return None
        finally:
            gdi32.SelectObject(mem_dc, old_bmp)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(hwnd, hdc)
        img = Image.frombuffer('RGB', (w, h), buf.raw, 'raw', 'BGRX', 0, 1)
        # 裁掉 DWM 阴影边框, 对齐客户区 (与窗口 winfo_rootx/rooty 一致)
        left, top = pt.x - rc.left, pt.y - rc.top
        return img.crop((left, top, left + cli.right, top + cli.bottom))

    def snap(self, name):
        """截图当前窗口内容并保存, 打印路径。

        优先按任务约定用 PIL.ImageGrab.grab(bbox=窗口矩形) 截取屏幕;
        若屏幕截取内容与窗口自身渲染不符 (如锁屏/被遮挡导致截到桌面),
        则回退为 PrintWindow 直接渲染的窗口内容, 确保截图可见 GUI。
        """
        self.root.update_idletasks()
        self.root.update()
        bbox = (self.root.winfo_rootx(), self.root.winfo_rooty(),
                self.root.winfo_rootx() + self.root.winfo_width(),
                self.root.winfo_rooty() + self.root.winfo_height())
        img = ImageGrab.grab(bbox=bbox)
        try:
            win_img = self._printwindow_image()
            if (win_img is not None
                    and win_img.size == img.size):
                diff = ImageChops.difference(win_img.convert('RGB'),
                                             img.convert('RGB'))
                mean = sum(ImageStat.Stat(diff).mean) / len(
                    ImageStat.Stat(diff).mean)
                if mean > 8.0:
                    print(f'[snap] 屏幕截取与窗口渲染不符 (mean diff={mean:.1f},'
                          f' 疑似锁屏/遮挡), 改用 PrintWindow 窗口渲染', flush=True)
                    img = win_img
        except Exception as exc:
            print(f'[snap] PrintWindow 备用截取失败: {exc!r}', flush=True)
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SHOT_DIR / f'{self.shot_no:02d}_{name}.png'
        img.save(path)
        self.shot_no += 1
        print(f'[shot] {path}', flush=True)

    # -------------------- a) 启动 --------------------

    def step_a_startup(self):
        root, app = self.root, self.app
        assert root.winfo_exists(), 'Tk 窗口不存在'
        cols = list(app.tree['columns'])
        for col in ('Port', 'Name', 'Victim', 'FEXT', 'NEXT'):
            assert col in cols, f'端口表缺少列 {col}, 实际: {cols}'
        # 归一化初始状态, 屏蔽历史默认配置 (spara_ui_default.json) 的干扰
        app.mode_var.set(app.DIFF_MODE)
        app.arrange_var.set('Type1')
        app.auto_select_var.set(False)
        app.z0_var.set(50.0)
        app.mark_freq_var.set('')
        app.chk_fext_var.set(True)
        app.chk_next_var.set(True)
        app.chk_psxt_var.set(True)
        self.snap('startup')

    # -------------------- b) 导入 --------------------

    def step_b_import(self):
        assert SAMPLE_PATH.exists(), f'示例文件不存在: {SAMPLE_PATH}'
        self.app.import_s_params(str(SAMPLE_PATH))
        self.root.update()

    def step_b_check(self):
        app = self.app
        assert app.file_loaded, \
            f'导入失败: file_loaded=False, status={app.status_var.get()!r}'
        assert not any(n == 'show_error' for n, _ in MSGBOX_CALLS), \
            '导入过程弹出了错误对话框 (show_error)'
        assert len(app.port_vars) == 8, \
            f'port_vars 应为 8 个端口, 实际 {len(app.port_vars)}'
        rows = app.tree.get_children()
        assert len(rows) == 8, f'端口表应为 8 行, 实际 {len(rows)}'
        fmin_file = float(app.spara_obj.frequencies[0]) / 1e9
        assert abs(app.fmin_var.get() - fmin_file) < 1e-6, \
            f'fmin_var={app.fmin_var.get()} 应≈文件起始频率 {fmin_file} GHz'
        self.snap('import')

    # -------------------- c) 自动勾选推演 (差分 Type1) --------------------

    def step_c_set(self):
        app = self.app
        app.auto_select_var.set(True)
        app.on_auto_select_toggle()
        app.port_vars[0][0].set(True)
        app.on_victim_changed(1)
        self.root.update()

    def step_c_check(self):
        app = self.app
        v, f, n = app._get_selected_ports()
        assert v == [1, 2], f'差分 Victim 应为 [1, 2], 实际 {v}'
        assert n == [3, 4], f'同侧 NEXT 应为 [3, 4], 实际 {n}'
        assert f == [7, 8], f'对侧 FEXT 应为 [7, 8], 实际 {f}'
        assert 5 not in (v + f + n) and 6 not in (v + f + n), \
            f'伙伴端口 5/6 不应被勾选: v={v}, f={f}, n={n}'
        app.auto_calculate()
        pd = app.plot_data
        assert pd, 'plot_data 为空, auto_calculate 未产出结果'
        assert len(pd['fn_ghz']) > 2, \
            f"fn_ghz 点数 {len(pd['fn_ghz'])} 应 > 2"
        assert pd['has_fext'] and pd['has_next'], \
            f"has_fext/has_next 应为 True, 实际 {pd['has_fext']}/{pd['has_next']}"
        self.snap('autoselect_diff')

    # -------------------- d) 防抖重算 --------------------

    def step_d_set(self):
        app = self.app
        self.old_plot_ref = app.plot_data
        self.new_fmin = round(app.fmin_var.get() + 0.01, 6)
        app.fmin_var.set(self.new_fmin)

    def step_d_check(self):
        app = self.app
        pd = app.plot_data
        assert pd is not self.old_plot_ref, \
            'plot_data 引用未变, 修改 fmin 后未触发重算'
        assert len(pd['fn_ghz']) > 0, 'fn_ghz 为空'
        assert abs(float(pd['fn_ghz'][0]) - self.new_fmin) < 1e-6, \
            f"防抖重算未生效: fn_ghz[0]={pd['fn_ghz'][0]} 应为 {self.new_fmin}"

    # -------------------- e) Mark Freq 峰值标注 --------------------

    def step_e_set(self):
        self.app.mark_freq_var.set('0.5')

    def step_e_check(self):
        app = self.app
        assert app.ax is not None, 'matplotlib axes 未初始化'
        n_texts = len(app.ax.texts)
        assert n_texts > 0, \
            f'峰值标注缺失: len(ax.texts)={n_texts} (numpy 导入修复未生效?)'
        self.snap('mark_peak')

    # -------------------- f) 模式切换 --------------------

    def step_f_se(self):
        app = self.app
        app.mode_var.set(app.SE_MODE)
        app.mode_changed()
        self.root.update()

    def step_f_se_check(self):
        app = self.app
        msg = app.table_msg_var.get()
        assert '单端' in msg, f'提示语应含「单端」, 实际: {msg!r}'
        v, f, n = app._get_selected_ports()
        assert v == [1], f'单端 Victim 应为 [1], 实际 {v}'
        assert f and n, f'单端推演 f/n 应非空: f={f}, n={n}'
        self.snap('se_mode')
        # 切回差分模式并恢复
        app.mode_var.set(app.DIFF_MODE)
        app.mode_changed()
        self.root.update()

    def step_f_back_check(self):
        app = self.app
        msg = app.table_msg_var.get()
        assert '差分' in msg, f'切回后提示语应含「差分」, 实际: {msg!r}'
        v, f, n = app._get_selected_ports()
        assert v == [1, 2] and f == [7, 8] and n == [3, 4], \
            f'切回差分后推演未恢复: v={v}, f={f}, n={n}'
        self.snap('diff_back')

    # -------------------- g) 保存配置并重导入恢复 --------------------

    def step_g_save(self):
        app = self.app
        assert 'show_info' in PATCHED_MSGBOX, 'Messagebox monkeypatch 未生效'
        app.save_config()
        cfg_path = Path(str(SAMPLE_PATH) + '.json')
        assert cfg_path.exists(), f'保存后配置文件不存在: {cfg_path}'
        assert any(n == 'show_info' for n, _ in MSGBOX_CALLS), \
            'save_config 应触发 show_info (被 monkeypatch 记录)'

    def step_g_clear(self):
        app = self.app
        for var_tuple in app.port_vars:
            for var in var_tuple:
                var.set(False)
        app.sync_table_ui()
        v, f, n = app._get_selected_ports()
        assert (v, f, n) == ([], [], []), f'清空后仍有勾选: v={v}, f={f}, n={n}'

    def step_g_reimport(self):
        self.app.import_s_params(str(SAMPLE_PATH))
        self.root.update()

    def step_g_check(self):
        app = self.app
        v, f, n = app._get_selected_ports()
        assert v == [1, 2], f'恢复后 Victim 应为 [1, 2], 实际 {v}'
        assert f == [7, 8], f'恢复后 FEXT 应为 [7, 8], 实际 {f}'
        assert n == [3, 4], f'恢复后 NEXT 应为 [3, 4], 实际 {n}'
        status = app.status_var.get()
        assert ('恢复' in status) or ('识别到同名配置' in status), \
            f'状态栏应含「恢复/识别到同名配置」, 实际: {status!r}'
        self.snap('restored')
    # -------------------- h) 独立图表窗口 --------------------

    def step_h_window_exists(self):
        app = self.app
        assert app.plot_window is not None, '启动后图表窗口未创建'
        assert app.plot_window.winfo_exists(), '图表窗口不存在'
        assert app.plot_window is not self.root, \
            '图表窗口与主窗口必须是不同窗口'

    def step_h_close_keeps_main(self):
        app = self.app
        app._on_plot_window_close()
        assert app.plot_window is None, '关闭后 plot_window 引用未清空'
        assert self.root.winfo_exists(), \
            '用户关闭图表窗口后主窗口不应被关闭'

    def step_h_reopen_single(self):
        app = self.app
        app._open_plot_window()
        assert app.plot_window is not None and app.plot_window.winfo_exists(), \
            '重新打开图表窗口失败'
        app._open_plot_window()  # 重复调用不得新建第二个窗口
        tops = [w for w in app.root.winfo_children()
                if isinstance(w, app_mod.tk.Toplevel)]
        assert len(tops) == 1, \
            f'主窗口应只搭配 1 个图表窗口, 实际 {len(tops)} 个'
        assert app.plot_window in tops, '图表窗口未挂接在主窗口下'


def main():
    if SHOT_DIR.exists():
        shutil.rmtree(SHOT_DIR, ignore_errors=True)
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    print(f'[env] 截图目录: {SHOT_DIR}', flush=True)
    print(f'[env] 示例文件: {SAMPLE_PATH}', flush=True)

    _patch_messagebox()

    root = ttk.Window(themename='cosmo')
    root.attributes('-topmost', True)
    root.lift()
    root.update()

    app = app_mod.SParamPowerSumApp(root)
    root.update()

    t = SmokeTest(root, app)
    t.add(200, 'a_startup',          t.step_a_startup)
    t.add(350, 'b_import',           t.step_b_import)
    t.add(300, 'b_import_check',     t.step_b_check)
    t.add(100, 'c_autoselect_set',   t.step_c_set)
    t.add(500, 'c_autoselect_check', t.step_c_check)
    t.add(100, 'd_debounce_set',     t.step_d_set)
    t.add(900, 'd_debounce_check',   t.step_d_check)
    t.add(100, 'e_mark_set',         t.step_e_set)
    t.add(900, 'e_mark_check',       t.step_e_check)
    t.add(100, 'f_se_mode_set',      t.step_f_se)
    t.add(600, 'f_se_mode_check',    t.step_f_se_check)
    t.add(400, 'f_diff_back_check',  t.step_f_back_check)
    t.add(100, 'g_save_config',      t.step_g_save)
    t.add(100, 'g_clear_checks',     t.step_g_clear)
    t.add(100, 'g_reimport',         t.step_g_reimport)
    t.add(600, 'g_restore_check',    t.step_g_check)
    t.add(200, 'h_plot_window',        t.step_h_window_exists)
    t.add(200, 'h_close_keeps_main',   t.step_h_close_keeps_main)
    t.add(200, 'h_reopen_single',      t.step_h_reopen_single)
    t.start()

    root.mainloop()
    sys.exit(1 if t.failures else 0)


if __name__ == '__main__':
    main()
