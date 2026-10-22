"""S参数串扰计算工具 —— GUI 主程序 (ttkbootstrap)。

布局 (单窗口三栏, 启动即最大化 (非全屏)):
- 左栏「设置与操作」: 「S参数导入」「基本设置」「ICN计算」「界面设置」
  四个分组, 宽度随内容自适应, 可拖动分割条调整;
- 中栏「计算结果」: 曲线操作栏 (曲线开关 + Mark Freq + 曲线字体滑块)
  在上; 曲线图与「计算数据」表格垂直分割, 分割线可上下拖动
  (计算数据区默认高度占整个窗口 1/3);
  曲线图始终保持 16:10 比例, 区域大小变化时在可用空间内居中自适应;
- 右栏「端口勾选」: 「端口自动勾选」分组 + 端口勾选表 (真实勾选框,
  列序 Port/端口名/Victim/FEXT/NEXT, 端口名默认右对齐,
  栏宽随端口名文字长度自适应; 勾选行按角色变色: Victim 蓝 /
  FEXT 红 / NEXT 绿, 只改颜色不改字体), 可拖动分割条调整;
- 全局字体体系: 全部控件使用全局/命名字体, 左栏「界面设置」的
  全局字体下拉框即时生效; 字号基准以 2K 27 英寸屏为准;
- 底部: 全宽状态栏 (info/success/warn/error 四色语义)。

「批量导出」独立窗口: 逐次缓存主界面当前计算数据
(单端端口一行; 差分端口也是一行, 两个端口在 Port/端口名列
以 / 合并), 可导出 CSV (utf-8-sig, 默认保存路径为 S 参数所在目录)
或整体清空; 随主窗口关闭而关闭, 自身关闭不影响主窗口。

行为与 V3.3 保持一致, 重构点:
- 输入 (Z0/频率/Mark) 采用 300ms 级防抖, 避免每次按键全量重算;
- 配置恢复/保存统一走 _SCALAR_FIELDS/_PORT_FIELDS 映射表,
  旧配置中 PSXT 改名前的遗留键经 _LEGACY_SCALAR_ALIAS 兼容读取;
- 默认配置文件固定在 exe (打包) 或脚本 (源码) 所在目录, 不再依赖 CWD。
"""

import contextlib
import csv
import json
import sys
import tkinter as tk
import tkinter.font as tkfont
import types
from pathlib import Path

import numpy as np
import ttkbootstrap as ttk

from sparam_core import (DEFAULT_FORMULAS, SParameter, diff_pair,
                         evaluate_expr, find_broken_pairs, icn_calculate,
                         order_ports_by_pairs, power_sum_db, resample_sparams)


class SParamPowerSumApp:
    DIFF_MODE = '差分模式 (Differential)'
    SE_MODE = '单端模式 (Single-Ended)'

    # ICN / ccICN 标量输入默认值 (字符串形式, 支持小数/科学计数法/表达式)
    _ICN_SCALAR_DEFAULTS = (
        ('ant_v', '0.5'),      # 近端攻击幅度 Ant (V), 中轴到 peak
        ('aft_v', '0.4'),      # 远端攻击幅度 Aft (V), 中轴到 peak
        ('fb_hz', '32e9'),     # 信号波特率 fb (Hz)
        ('tnt_s', '7.5e-12'),  # 近端攻击上升时间 Tnt (s)
        ('tft_s', '7.5e-12'),  # 远端攻击上升时间 Tft (s)
        ('pam', '4'),          # PAM 电平数
        ('il_n', '2*8.5'),     # 近端攻击路径损耗 ILn (dB)
        ('il_f', '22.75+8.5'),  # 远端攻击路径损耗 ILf (dB)
    )
    # 输入框键 -> 公式环境中的变量名
    _ICN_SCALAR_ENV = {
        'ant_v': 'Ant', 'aft_v': 'Aft', 'fb_hz': 'fb',
        'tnt_s': 'Tnt', 'tft_s': 'Tft', 'pam': 'PAM',
        'il_n': 'ILn', 'il_f': 'ILf',
    }

    # 配置字段映射: (JSON 键, 变量属性名)
    _SCALAR_FIELDS = (
        ('mode_val', 'mode_var'), ('Z0', 'z0_var'), ('fmin', 'fmin_var'),
        ('fmax', 'fmax_var'), ('fstep', 'fstep_var'),
        ('name_align', 'name_align_var'),
        ('mark_freq', 'mark_freq_var'), ('chk_fext', 'chk_fext_var'),
        ('chk_next', 'chk_next_var'), ('chk_psxtlk', 'chk_psxtlk_var'),
        ('auto_select', 'auto_select_var'), ('arrange', 'arrange_var'),
        ('icn_enabled', 'icn_enabled_var'),
        ('font_size', 'font_size_var'), ('plot_font', 'plot_font_var'),
    ) + tuple((key, key + '_var') for key, _ in _ICN_SCALAR_DEFAULTS) \
      + tuple(('formula_' + key, 'formula_var_' + key)
              for key in DEFAULT_FORMULAS)
    # 旧版本配置遗留键 (PSXT 改名 PSXTLK 之前) -> 新变量属性名,
    # 仅在新键缺失时补读, 保证旧配置文件不丢失勾选/公式设置
    _LEGACY_SCALAR_ALIAS = {
        'chk_psxt': 'chk_psxtlk_var',
        'formula_PSXT': 'formula_var_PSXTLK',
    }
    # 端口勾选: (JSON 键, port_vars 元组中的下标)
    _PORT_FIELDS = (
        ('v_ports', 0), ('f_ports', 1), ('n_ports', 2),
    )

    _STATUS_COLORS = {
        'info': '#0b5ed7',
        'success': '#198754',
        'warn': '#D95319',
        'error': '#dc3545',
    }
    _RECALC_DELAY_MS = 350
    _REDRAW_DELAY_MS = 250

    # ICN 显示单位 → 缩放倍数 (与 _icn_display 口径一致, 纯 10 的幂次换算)
    _BATCH_UNIT_SCALE = {'V': 1.0, 'mV': 1e3, 'µV': 1e6, 'nV': 1e9,
                         'pV': 1e12}

    def __init__(self, root):
        self.root = root
        self.root.title('S参数串扰计算')

        self._setup_scaling()

        self.file_loaded = False
        self.spara_obj = None
        self.plot_data = {}
        self.filepath = ''
        self.port_vars = []
        self._port_rows = []        # [((v_cb, f_cb, n_cb), port_label, name_label)]
        self._suppress_auto = 0
        self._recalc_job = None
        self._redraw_job = None
        # S 参数重采样缓存: (基本参数 key, 归一化+重采样后的数据)。
        # 仅在基本参数 (Z0/fmin/fmax/fstep) 或文件变化时重建,
        # 端口勾选/ICN 变量等修改直接复用, 保证快速重算
        self._sparam_cache = None

        self.fig = None
        self.ax = None
        self.canvas_mpl = None

        # 「批量导出」窗口状态: 窗口引用、列表 Treeview、缓存行
        # (行 = {'Port': str, '端口名': str, '_vals': {列名: float|None}},
        #  _vals 存原始计算值, 展示时按主表当前单位实时换算)
        self.batch_window = None
        self._batch_tree = None
        self._batch_rows = []

        # ICN / ccICN 状态: 启用开关、最近一次计算结果、当前正在编辑的
        # 公式键 (同一时间只允许编辑一条)
        self.icn_enabled_var = ttk.BooleanVar(value=True)
        self._last_icn_results = None
        self._formula_editing = None
        # 「计算数据」表格状态: 单元格变量、当前列签名、表格外框容器、
        # 最近一次 Mark 峰值 (曲线名 -> (频率 GHz, 峰值 dB))
        self.data_table_vars = {}
        self._data_table_cols = None
        self._table_outer = None
        self._last_mark_peaks = {}

        # 曲线开关与 Mark Freq 变量在主界面构建前创建 (配置恢复依赖它们);
        # mark_freq 的防抖重绘 trace 只注册一次
        self.chk_fext_var = ttk.BooleanVar(value=True)
        self.chk_next_var = ttk.BooleanVar(value=True)
        self.chk_psxtlk_var = ttk.BooleanVar(value=True)
        self.mark_freq_var = ttk.StringVar(value='')
        self.mark_freq_var.trace_add('write', lambda *_: self._schedule_redraw())
        # 端口名对齐方式: 左 / 居中 / 右 (默认右对齐)
        self.name_align_var = ttk.StringVar(value='右')
        # 曲线图 16:10 比例自适应的防抖任务
        self._plot_fit_job = None

        self._build_ui()
        self._load_last_config()

        # 主窗口关闭时连带关闭「批量导出」窗口
        self.root.protocol('WM_DELETE_WINDOW', self._on_main_window_close)
        # 窗口映射后按内容自适应左右栏默认宽度
        self.root.after(150, self._layout_initial_sashes)

    # -------------------- 窗口与样式 --------------------

    def _setup_scaling(self):
        """启动即最大化 (非全屏), 并建立全局字体体系。

        字号基准以 2K 27 英寸屏 (2560 宽) 为准: 默认 11, 小屏降到 10;
        全部控件使用命名/全局字体, 「界面设置」下拉框调整时即时全局生效。
        曲线图字体独立由工具栏滑块控制 (plot_font_var)。
        """
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()

        try:
            self.root.state('zoomed')      # Windows: 最大化 (保留标题栏/任务栏)
        except tk.TclError:
            # 不支持 zoomed 的平台用接近满屏的 geometry 兜底
            self.root.geometry(
                f'{int(sw * 0.94)}x{int(sh * 0.92)}+{int(sw * 0.03)}+10')
        self.root.minsize(1100, 700)

        base_size = 10 if sw < 2000 else 11
        self.default_font = tkfont.nametofont('TkDefaultFont')
        self.default_font.configure(size=base_size)
        for name in ('TkTextFont', 'TkHeadingFont', 'TkMenuFont'):
            try:
                tkfont.nametofont(name).configure(size=base_size)
            except tk.TclError:
                pass

        # 自定义命名字体: 控件按用途引用, 全局调整时统一 reconfigure
        fam = self.default_font.actual('family')
        self._font_bold = self._named_font(
            'AppBold', family=fam, size=base_size, weight='bold')
        self._font_small = self._named_font(
            'AppSmall', family=fam, size=base_size - 1)
        self._font_mono = self._named_font(
            'AppMono', family='Consolas', size=base_size - 1)
        self._font_mono_lg = self._named_font(
            'AppMonoLg', family='Consolas', size=base_size)
        self._font_mono_small = self._named_font(
            'AppMonoSmall', family='Consolas', size=base_size - 2)

        # 全局字体大小 (下拉框, 见左栏「界面设置」) 与曲线图字号 (工具栏滑块)
        self.font_size_var = ttk.StringVar(value=str(base_size))
        self.font_size_var.trace_add('write', self._apply_font_sizes)
        self.plot_font_var = ttk.IntVar(value=13)
        self.plot_font_var.trace_add('write', self._on_plot_font_changed)

    @staticmethod
    def _named_font(name, **kw):
        """创建命名全局字体; 同名已存在 (重复实例化) 时改为重配置。"""
        try:
            return tkfont.Font(name=name, **kw)
        except tk.TclError:
            font = tkfont.nametofont(name)
            font.configure(**kw)
            return font

    def _apply_font_sizes(self, *_):
        """全局字体下拉框变化: 重配置全局/命名字体并重自适应布局。"""
        try:
            size = int(float(self.font_size_var.get()))
        except (ValueError, tk.TclError):
            return
        size = max(8, min(16, size))
        self.default_font.configure(size=size)
        for name in ('TkTextFont', 'TkHeadingFont', 'TkMenuFont'):
            try:
                tkfont.nametofont(name).configure(size=size)
            except tk.TclError:
                pass
        self._font_bold.configure(size=size)
        self._font_small.configure(size=max(7, size - 1))
        self._font_mono.configure(size=max(7, size - 1))
        self._font_mono_lg.configure(size=size)
        self._font_mono_small.configure(size=max(7, size - 2))
        ttk.Style().configure('Treeview', rowheight=max(22, int(size * 2.2)))
        # 字号影响内容宽高, 重新自适应栏宽与计算数据区高度
        if hasattr(self, '_main_paned'):
            self.root.after_idle(self._layout_after_font_change)

    def _layout_after_font_change(self):
        self._sync_port_columns()
        self._align_port_header()
        self._layout_initial_sashes()
        self._adapt_data_area()

    def _on_plot_font_changed(self, *_):
        """曲线图字号滑块: 更新数值标签并防抖重绘曲线。"""
        try:
            value = self.plot_font_var.get()
        except tk.TclError:
            return
        if hasattr(self, '_plot_font_label'):
            self._plot_font_label.configure(text=str(value))
        self._schedule_redraw()

    def _plot_fontsize(self):
        """曲线图基准字号 (坐标轴标题); 刻度/图例/标注按比例递减。"""
        try:
            return max(8, min(22, self.plot_font_var.get()))
        except (tk.TclError, ValueError):
            return 13

    def _build_ui(self):
        # 底部状态栏先布局, 主区域占据其余全部空间
        status_bar = ttk.Frame(self.root, padding=(12, 4, 12, 8))
        status_bar.pack(side='bottom', fill='x')
        self.status_var = ttk.StringVar(value='状态: 请点击左侧按钮导入 S 参数文件')
        self.status_label = ttk.Label(status_bar, textvariable=self.status_var,
                                      font='AppBold')
        self.status_label.pack(side='left')
        ttk.Sizegrip(status_bar).pack(side='right')

        # 端口行角色高亮: 只改颜色, 不改字体/字号
        # (Victim 蓝 / FEXT 红 / NEXT 绿, 优先级同此顺序)
        style = ttk.Style()
        style.configure('Victim.TLabel', foreground='#0b5ed7')
        style.configure('Fext.TLabel', foreground='#c21f3d')
        style.configure('Next.TLabel', foreground='#1e7e34')
        style.configure('Treeview',
                        rowheight=max(22, int(self.default_font.cget('size')
                                              * 2.2)))

        self._main_paned = ttk.Panedwindow(self.root, orient='horizontal')
        self._main_paned.pack(fill='both', expand=True, padx=12, pady=(10, 0))

        self._build_left_panel()
        self._build_center_panel()
        self._build_right_panel()

    # ==================== 左栏: 设置与操作 ====================

    def _build_left_panel(self):
        left_frame = ttk.LabelFrame(self._main_paned, text='设置与操作',
                                    padding=10)
        self._main_paned.add(left_frame, weight=0)
        left_frame.columnconfigure(0, weight=1)
        left_frame.rowconfigure(0, weight=1)
        panel = ttk.ScrolledFrame(left_frame, autohide=False)
        panel.grid(row=0, column=0, sticky='nsew')
        self._left_scrolled = panel

        # ---- S参数导入: 文件选择 + 保存当前文件配置 ----
        file_group = ttk.LabelFrame(panel, text='S参数导入', padding=10)
        file_group.grid(row=0, column=0, sticky='ew', pady=(0, 10))
        file_group.columnconfigure(0, weight=1)
        self.path_btn = ttk.Button(file_group, text='点击此处选择 .sNp 文件...',
                                   command=self.import_s_params,
                                   bootstyle='info-outline')
        self.path_btn.grid(row=0, column=0, sticky='ew')
        self.file_z0_var = ttk.StringVar(value='文件原始阻抗: --')
        ttk.Label(file_group, textvariable=self.file_z0_var,
                  font='AppSmall').grid(row=1, column=0, sticky='w',
                                        pady=(6, 0))
        ttk.Button(file_group, text='保存当前文件配置', command=self.save_config,
                   bootstyle='success-outline').grid(row=2, column=0,
                                                     sticky='ew', pady=(10, 0))

        # ---- 基本设置 ----
        param_group = ttk.LabelFrame(panel, text='基本设置', padding=10)
        param_group.grid(row=1, column=0, sticky='ew', pady=(0, 10))
        param_group.columnconfigure(0, weight=1)

        ttk.Label(param_group, text='计算模式:').grid(row=0, column=0, sticky='w')
        self.mode_var = ttk.StringVar(value=self.DIFF_MODE)
        mode_combo = ttk.Combobox(param_group, textvariable=self.mode_var,
                                  state='readonly',
                                  values=[self.DIFF_MODE, self.SE_MODE])
        mode_combo.grid(row=1, column=0, sticky='ew', pady=(3, 8))
        mode_combo.bind('<<ComboboxSelected>>', lambda e: self.mode_changed())

        z0_frame = ttk.Frame(param_group)
        z0_frame.grid(row=2, column=0, sticky='ew', pady=(0, 8))
        ttk.Label(z0_frame, text='Z0 (参考阻抗 Ω):').pack(side='left')
        self.z0_var = ttk.DoubleVar(value=50.0)
        ttk.Entry(z0_frame, textvariable=self.z0_var, width=8).pack(side='right')
        self.z0_var.trace_add('write', lambda *_: self._schedule_recalculate())

        ttk.Label(param_group,
                  text='Freq (Hz) [ min / max / step ]:').grid(row=3, column=0,
                                                               sticky='w')
        freq_frame = ttk.Frame(param_group)
        freq_frame.grid(row=4, column=0, sticky='ew', pady=(3, 0))
        freq_frame.columnconfigure((0, 1, 2), weight=1, uniform='freq')
        # StringVar + 科学计数法显示 (如 1e+07), 支持表达式输入,
        # 重算时经 evaluate_expr 求值; 修改任一基本参数都会触发
        # S 参数重采样缓存重建 (见 auto_calculate)
        self.fmin_var = ttk.StringVar(value='1e+07')
        self.fmax_var = ttk.StringVar(value='8e+10')
        self.fstep_var = ttk.StringVar(value='1e+07')
        for col, var in enumerate((self.fmin_var, self.fmax_var, self.fstep_var)):
            ttk.Entry(freq_frame, textvariable=var, width=8,
                      font='AppMono').grid(
                row=0, column=col, sticky='ew', padx=(0 if col == 0 else 3, 0))
            var.trace_add('write', lambda *_: self._schedule_recalculate())

        # ---- ICN计算: 启用开关 + 变量输入 + 公式编辑区 ----
        icn_group = ttk.LabelFrame(panel, text='ICN计算', padding=10)
        icn_group.grid(row=2, column=0, sticky='ew', pady=(0, 10))
        icn_group.columnconfigure(1, weight=1)

        # 启用开关: 关闭时不计算 ICN/ccICN, 「计算数据」表格也不显示对应列
        ttk.Checkbutton(icn_group, text='启用 ICN / ccICN 计算',
                        variable=self.icn_enabled_var,
                        bootstyle='round-toggle',
                        command=self._on_icn_toggle).grid(
            row=0, column=0, columnspan=3, sticky='w', pady=(0, 6))

        icn_labels = {
            'ant_v': 'Ant 近端幅度 (V)（非pk-pk）',
            'aft_v': 'Aft 远端幅度 (V)（非pk-pk）',
            'fb_hz': 'fb 波特率 (Hz)',
            'tnt_s': 'Tnt 近端上升时间 (s)',
            'tft_s': 'Tft 远端上升时间 (s)',
            'pam': 'PAM 电平数',
            'il_n': 'ILn 近端路径损耗 (dB)',
            'il_f': 'ILf 远端路径损耗 (dB)',
        }
        # 变量输入: StringVar 保存原始文本, 支持高精度小数/科学计数法/表达式,
        # 不做任何四舍五入, 重算时才求值
        self.icn_scalar_vars = {}
        for row, (key, default) in enumerate(self._ICN_SCALAR_DEFAULTS,
                                             start=1):
            ttk.Label(icn_group, text=icn_labels[key] + ':').grid(
                row=row, column=0, sticky='w', pady=2)
            var = ttk.StringVar(value=default)
            self.icn_scalar_vars[key] = var
            setattr(self, key + '_var', var)
            ttk.Entry(icn_group, textvariable=var, width=16,
                      font='AppMono').grid(
                row=row, column=1, sticky='e', pady=2)
            var.trace_add('write', lambda *_: self._schedule_recalculate())

        # ---- 公式区: 只读展示; 点「编辑」方可修改, 点「完成」校验并生效 ----
        ttk.Separator(icn_group, orient='horizontal').grid(
            row=9, column=0, columnspan=3, sticky='ew', pady=(8, 6))
        ttk.Label(icn_group,
                  text='计算公式 (点击「编辑」修改, 「完成」生效):',
                  font='AppBold').grid(
            row=10, column=0, columnspan=3, sticky='w', pady=(0, 2))

        self.formula_vars = {}
        self.formula_widgets = {}
        for row, (key, default) in enumerate(DEFAULT_FORMULAS.items(),
                                             start=11):
            ttk.Label(icn_group, text=key,
                      font='AppSmall').grid(
                row=row, column=0, sticky='w', pady=2)
            var = ttk.StringVar(value=default)
            self.formula_vars[key] = var
            setattr(self, 'formula_var_' + key, var)
            # 输入框固定宽度展示 (公式可点选全量复制查看)
            entry = ttk.Entry(icn_group, textvariable=var, width=30,
                              font='AppMonoSmall', state='readonly')
            entry.grid(row=row, column=1, sticky='ew', pady=2, padx=(4, 2))
            btn = ttk.Button(icn_group, text='编辑', width=4,
                             bootstyle='secondary-outline',
                             command=lambda k=key: self._toggle_formula_edit(k))
            btn.grid(row=row, column=2, sticky='e', pady=2)
            self.formula_widgets[key] = (entry, btn)

        # ---- 界面设置: 全局字体大小 ----
        ui_group = ttk.LabelFrame(panel, text='界面设置', padding=10)
        ui_group.grid(row=3, column=0, sticky='ew')
        ttk.Label(ui_group, text='全局字体大小:').pack(side='left')
        font_combo = ttk.Combobox(ui_group, textvariable=self.font_size_var,
                                  state='readonly', width=4,
                                  values=[str(i) for i in range(9, 15)])
        font_combo.pack(side='right')

    # ==================== 中栏: 计算结果 ====================

    def _build_center_panel(self):
        mid_frame = ttk.LabelFrame(self._main_paned, text='计算结果', padding=8)
        self._main_paned.add(mid_frame, weight=1)

        # ---- 曲线操作栏 (曲线图上方): 曲线开关 + Mark Freq + 字号滑块 ----
        toolbar = ttk.Frame(mid_frame, padding=(2, 0, 2, 8))
        toolbar.pack(fill='x')
        ttk.Label(toolbar, text='曲线:').pack(side='left', padx=(0, 4))
        ttk.Checkbutton(toolbar, text='PSFEXT', variable=self.chk_fext_var,
                        command=self._on_curve_toggle).pack(side='left', padx=3)
        ttk.Checkbutton(toolbar, text='PSNEXT', variable=self.chk_next_var,
                        command=self._on_curve_toggle).pack(side='left', padx=3)
        ttk.Checkbutton(toolbar, text='PSXTLK', variable=self.chk_psxtlk_var,
                        command=self._on_curve_toggle).pack(side='left', padx=3)
        ttk.Separator(toolbar, orient='vertical').pack(side='left', fill='y',
                                                       padx=10)
        ttk.Label(toolbar, text='Mark Freq (上限 GHz):').pack(side='left')
        ttk.Entry(toolbar, textvariable=self.mark_freq_var, width=10).pack(
            side='left', padx=(4, 0))
        ttk.Separator(toolbar, orient='vertical').pack(side='left', fill='y',
                                                       padx=10)
        ttk.Label(toolbar, text='曲线字体:').pack(side='left')
        ttk.Scale(toolbar, from_=8, to=22, variable=self.plot_font_var,
                  length=120).pack(side='left', padx=(6, 2))
        self._plot_font_label = ttk.Label(
            toolbar, text=str(self.plot_font_var.get()), width=2)
        self._plot_font_label.pack(side='left')

        # ---- 上下垂直分割: 曲线图 (上, 自适应) / 计算数据 (下, 可拖动调高) ----
        self._mid_split = ttk.Panedwindow(mid_frame, orient='vertical')
        self._mid_split.pack(fill='both', expand=True)

        # 曲线图容器: 画布按 16:10 比例在其中居中 (letterbox),
        # 任何区域拖动引发的大小变化都会重适配, 比例保持不变
        self.plot_frame = ttk.Frame(self._mid_split)
        self._mid_split.add(self.plot_frame, weight=1)
        self.plot_frame.bind('<Configure>', self._schedule_plot_fit)

        try:
            import matplotlib
            matplotlib.use('TkAgg')
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

            self.fig = Figure(figsize=(8, 5), dpi=100)
            self.ax = self.fig.add_subplot(111)
            self.canvas_mpl = FigureCanvasTkAgg(self.fig, master=self.plot_frame)
            fs = self._plot_fontsize()
            self.ax.set_xlabel('Frequency (GHz)', fontsize=fs,
                               fontweight='bold')
            self.ax.set_ylabel('Magnitude (dB)', fontsize=fs, fontweight='bold')
            self.ax.tick_params(labelsize=fs - 1)
            self.ax.grid(True, alpha=0.4)
            self.fig.tight_layout()
            self.canvas_mpl.draw()
            self.root.after_idle(self._fit_plot_canvas)
        except Exception as e:
            self.fig = None
            self.ax = None
            self.canvas_mpl = None
            ttk.Label(self.plot_frame, text=f'图表初始化失败: {e}',
                      bootstyle='danger').pack(expand=True)

        # ---- 计算数据区: 垂直分割下栏, 1 行表格 ----
        # 列 = 勾选曲线的 Mark 峰值 (PSFEXT/PSNEXT/PSXTLK, dB)
        #       + ICN 开关打开时的 6 个 ICN/ccICN 结果 (mV);
        # 按钮在表格上方左侧; 单元格用只读 Entry (tk): 文本可自由框选复制
        self._res_frame = ttk.LabelFrame(self._mid_split, text='计算数据',
                                         padding=(10, 8))
        self._mid_split.add(self._res_frame, weight=0)
        btn_row = ttk.Frame(self._res_frame)
        btn_row.pack(fill='x', pady=(0, 8))
        ttk.Button(btn_row, text='复制表格', width=10,
                   bootstyle='secondary-outline',
                   command=self._copy_data_table).pack(side='left')
        ttk.Button(btn_row, text='批量导出', width=10,
                   bootstyle='info-outline',
                   command=self._open_batch_export).pack(side='left',
                                                         padx=(8, 0))
        # 表格外框容器: 不再带外框线 (单元格 relief='solid' 已构成完整表框)
        self._table_outer = tk.Frame(self._res_frame, bg='white', bd=0,
                                     highlightthickness=0)
        self._table_outer.pack(fill='x', pady=(0, 2))
        self._refresh_data_table()

    # -------------------- 曲线图 16:10 比例自适应 --------------------

    _PLOT_RATIO = 16 / 10     # 曲线图默认宽高比 16:10
    _PLOT_FIT_DELAY_MS = 120

    def _schedule_plot_fit(self, _event=None):
        """容器大小变化时防抖重适配画布 (保持 16:10)。"""
        if self.canvas_mpl is None:
            return
        if self._plot_fit_job is not None:
            self.root.after_cancel(self._plot_fit_job)
        self._plot_fit_job = self.root.after(self._PLOT_FIT_DELAY_MS,
                                             self._fit_plot_canvas)

    def _fit_plot_canvas(self):
        """在容器内放入最大 16:10 画布并居中 (letterbox), 比例不随拖动改变。"""
        self._plot_fit_job = None
        if self.canvas_mpl is None or self.fig is None:
            return
        try:
            cw = self.plot_frame.winfo_width()
            ch = self.plot_frame.winfo_height()
        except tk.TclError:
            return
        if cw < 60 or ch < 60:
            return
        w, h = cw, int(cw / self._PLOT_RATIO)
        if h > ch:
            h = ch
            w = int(h * self._PLOT_RATIO)
        widget = self.canvas_mpl.get_tk_widget()
        widget.place(x=(cw - w) // 2, y=(ch - h) // 2, width=w, height=h)
        self.fig.tight_layout()
        self.canvas_mpl.draw_idle()

    def _adapt_data_area(self):
        """「计算数据」区默认高度取整个窗口的 1/3 (为曲线图保留最小高度),
        用户可拖动水平分割条上下调整, 曲线图区随动自适应。"""
        try:
            total = self._mid_split.winfo_height()
            if total <= 1:
                self.root.after(120, self._adapt_data_area)
                return
            target = self.root.winfo_height() // 3
            target = max(120, min(target, total - 240))
            self._mid_split.sashpos(0, total - target)
        except tk.TclError:
            pass

    # ==================== 右栏: 端口勾选 ====================

    def _build_right_panel(self):
        right_frame = ttk.LabelFrame(self._main_paned, text='端口勾选',
                                     padding=8)
        self._main_paned.add(right_frame, weight=0)
        self._right_frame = right_frame
        # 长文本随栏宽折行 (栏宽可拖动调整)
        self._wrap_widgets = []
        right_frame.bind('<Configure>', self._on_right_frame_configure)

        # ---- 端口自动勾选 (自原左栏移入) ----
        # 说明文字用支持折行的 ttk.Label 承载 (ttk 勾选框/单选框不支持折行)
        auto_group = ttk.LabelFrame(
            right_frame, text='端口自动勾选 (仅标准对称S参数可用)', padding=8)
        auto_group.pack(fill='x', pady=(0, 10))
        self.auto_select_var = ttk.BooleanVar(value=False)
        ttk.Checkbutton(auto_group, text='启用自动勾选',
                        variable=self.auto_select_var,
                        command=self.on_auto_select_toggle).pack(anchor='w')
        auto_hint = ttk.Label(
            auto_group, font='AppSmall', foreground='#555555',
            text='仅需手动选择受害端口，攻击端口会自动勾选。')
        auto_hint.pack(anchor='w', padx=(22, 0))
        self._wrap_widgets.append(auto_hint)
        self.arrange_var = ttk.StringVar(value='Type1')
        rb_frame = ttk.Frame(auto_group)
        rb_frame.pack(anchor='w', pady=(2, 0))
        ttk.Radiobutton(rb_frame, text='排列一', variable=self.arrange_var,
                        value='Type1',
                        command=self.on_arrange_changed).pack(side='left')
        ttk.Radiobutton(rb_frame, text='排列二', variable=self.arrange_var,
                        value='Type2',
                        command=self.on_arrange_changed).pack(side='left',
                                                              padx=(8, 0))
        arrange_hint = ttk.Label(
            auto_group, font='AppSmall', foreground='#555555',
            text='以N=8为例：排列一同侧(1,2,3,4) 对侧(5,6,7,8)；'
                 '排列二同侧(1,3,5,7) 对侧(2,4,6,8)。')
        arrange_hint.pack(anchor='w', padx=(22, 0))
        self._wrap_widgets.append(arrange_hint)

        self.table_msg_var = ttk.StringVar(
            value='▶ 差分模式：Victim 必须勾选 2 个；攻击端口必须成对勾选'
                  '（对子按上方「排列方式」划分）。')
        msg_label = ttk.Label(right_frame, textvariable=self.table_msg_var,
                              font='AppBold', foreground='#cc4d1a')
        msg_label.pack(fill='x', pady=(2, 8), anchor='w')
        self._wrap_widgets.append(msg_label)

        # 全选/全反选勾选框内嵌在端口列表表头 (见 _build_port_header),
        # 变量在此创建 (配置恢复依赖)
        self.chk_all_v_var = ttk.BooleanVar(value=False)
        self.chk_all_f_var = ttk.BooleanVar(value=False)
        self.chk_all_n_var = ttk.BooleanVar(value=False)

        align_frame = ttk.Frame(right_frame)
        align_frame.pack(fill='x', pady=(0, 8))
        ttk.Label(align_frame, text='端口名对齐:').pack(side='left', padx=(0, 3))
        align_combo = ttk.Combobox(align_frame, textvariable=self.name_align_var,
                                   state='readonly', width=4,
                                   values=['左', '居中', '右'])
        align_combo.pack(side='left')
        align_combo.bind('<<ComboboxSelected>>',
                         lambda e: self._apply_name_align())

        # ---- 端口勾选表: 真实勾选框 (现代默认样式),
        #      列序 Port/端口名/Victim/FEXT/NEXT;
        #      表头冻结在滚动区外 (不随数据行上下滚动),
        #      竖向滚动条固定显示, 避免鼠标移入移出导致列表宽度抖动 ----
        self.port_list = ttk.ScrolledFrame(right_frame, autohide=False,
                                           padding=0)
        sb_w = self.port_list._vbar.winfo_reqwidth()
        self._port_header = ttk.Frame(right_frame)
        # 表头右侧留出滚动条宽度, 使表头列与数据区列对齐
        self._port_header.pack(fill='x', pady=(2, 0), padx=(0, sb_w))
        self.port_list.pack(fill='both', expand=True)
        self._build_port_header()
        self._sync_port_columns()
        # 首帧后按滚动条实际渲染宽度校正一次表头留白
        self.root.after_idle(self._align_port_header)

    def _on_right_frame_configure(self, event):
        """右栏宽度变化时, 长文本控件按可用宽度折行。"""
        wrap = max(180, event.width - 84)
        for w in self._wrap_widgets:
            try:
                w.configure(wraplength=wrap)
            except tk.TclError:
                pass

    def _build_port_header(self):
        """端口勾选表表头 (冻结在滚动区外, 不随数据行上下滚动)。

        Victim/FEXT/NEXT 三列表头内嵌全选/全反选勾选框,
        取代独立的全选行, 省空间且正对目标列。
        """
        header = self._port_header
        for w in header.winfo_children():
            w.destroy()
        all_vars = (self.chk_all_v_var, self.chk_all_f_var,
                    self.chk_all_n_var)
        for col, text in enumerate(('Port', '端口名', 'Victim', 'FEXT', 'NEXT')):
            if col >= 2:
                idx = col - 2
                cell = ttk.Frame(header)
                cell.grid(row=0, column=col, padx=2, pady=(0, 3))
                ttk.Checkbutton(
                    cell, variable=all_vars[idx],
                    command=lambda i=idx: self.toggle_all_column(
                        i, all_vars[i].get())).pack(side='left')
                ttk.Label(cell, text=text, font='AppBold').pack(side='left')
            elif col == 1:
                lbl = ttk.Label(header, text=text, font='AppBold',
                                anchor=self._name_anchor())
                self._port_name_header = lbl
                lbl.grid(row=0, column=col, sticky='ew', padx=4, pady=(0, 3))
            else:
                ttk.Label(header, text=text, font='AppBold',
                          anchor='center').grid(row=0, column=col, sticky='ew',
                                                padx=4, pady=(0, 3))
        ttk.Separator(header, orient='horizontal').grid(
            row=1, column=0, columnspan=5, sticky='ew', pady=(0, 3))

    def _align_port_header(self):
        """按滚动条实际渲染宽度校正冻结表头右侧留白, 使表头列与数据列对齐。"""
        header = getattr(self, '_port_header', None)
        if header is None or not header.winfo_exists():
            return
        try:
            vbar = self.port_list._vbar
            sb_w = vbar.winfo_width() or vbar.winfo_reqwidth()
        except (AttributeError, tk.TclError):
            return
        header.pack_configure(padx=(0, sb_w))

    def _sync_port_columns(self):
        """同步冻结表头与滚动数据区的列宽配置:
        Port/勾选框列固定宽度, 端口名列随端口名文字长度自适应
        并吸收多余宽度 (两容器列宽一致, 保证表头与数据行对齐)。"""
        if getattr(self, '_port_header', None) is None:
            return
        port_w = self._font_bold.measure('Port') + 12
        name_min = self._font_bold.measure('端口名') + 12
        for (_, port_label, name_label) in self._port_rows:
            port_w = max(port_w,
                         self.default_font.measure(port_label.cget('text'))
                         + 12)
            name_min = max(name_min,
                           self.default_font.measure(name_label.cget('text'))
                           + 12)
        role_w = max(72, max(self._font_bold.measure(t)
                             for t in ('Victim', 'FEXT', 'NEXT')) + 56)
        minsizes = (port_w, name_min, role_w, role_w, role_w)
        for container in (self._port_header, self.port_list):
            for col, width in enumerate(minsizes):
                container.columnconfigure(
                    col, minsize=width, weight=1 if col == 1 else 0)

    # -------------------- 分割条位置自适应 --------------------

    def _layout_initial_sashes(self):
        """启动后按内容自适应左右栏默认宽度 (用户仍可拖动分割条)。"""
        try:
            self.root.update_idletasks()
            total = self._main_paned.winfo_width()
            if total <= 1:      # 窗口尚未映射, 稍后重试
                self.root.after(150, self._layout_initial_sashes)
                return
            left_w = self._measure_left_width()
            right_w = self._measure_right_width()
            self._main_paned.sashpos(0, left_w)
            self._main_paned.sashpos(1, max(left_w + 240, total - right_w))
            self._adapt_data_area()
        except tk.TclError:
            pass

    def _measure_left_width(self):
        """左栏内容自然宽度: ScrolledFrame 会把内容宽度钉死在视口宽度上,
        因此先量出各分组的自然请求宽度, 视口取最宽分组 (+滚动条余量)。"""
        try:
            panel = self._left_scrolled
            panel.update_idletasks()
            groups = panel.winfo_children()
            if not groups:
                return 300
            needed = max(c.winfo_reqwidth() for c in groups) + 24
            panel._canvas.configure(width=needed)
            return needed + 44      # LabelFrame 内边距 + 边框余量
        except (AttributeError, tk.TclError):
            return 300

    def _measure_right_width(self):
        """右栏内容自然宽度: Port 列 + 端口名列 (随端口名文字长度)
        + 勾选框 3 列 + 滚动条/边距余量, 并限制在屏幕 1/4 内。
        (列宽口径与 _sync_port_columns 一致)"""
        bold = self._font_bold
        port_w = bold.measure('Port') + 12
        name_px = bold.measure('端口名') + 12
        for (_, port_label, name_label) in self._port_rows:
            port_w = max(port_w,
                         self.default_font.measure(port_label.cget('text'))
                         + 12)
            name_px = max(name_px,
                          self.default_font.measure(name_label.cget('text'))
                          + 12)
        width = port_w + name_px
        for text in ('Victim', 'FEXT', 'NEXT'):
            # 表头为 全选勾选框 + 文字 的组合单元格
            width += max(72, bold.measure(text) + 56)
        try:
            width += self.port_list._vbar.winfo_reqwidth()
        except (AttributeError, tk.TclError):
            width += 16
        width += 20
        limit = max(300, self.root.winfo_screenwidth() // 4)
        return int(min(max(width + 36, 300), limit))

    def _adapt_right_pane(self):
        """导入 S 参数后按端口名长度重新自适应右栏宽度。"""
        try:
            total = self._main_paned.winfo_width()
            if total <= 1:
                self.root.after(120, self._adapt_right_pane)
                return
            right_w = self._measure_right_width()
            left = self._main_paned.sashpos(0)
            self._main_paned.sashpos(1, max(left + 240, total - right_w))
        except tk.TclError:
            pass

    # -------------------- 「计算数据」表格 --------------------

    def _data_table_columns(self):
        """当前应显示的数据列: [(列名, 单位)]。

        左侧 3 列为勾选曲线的 Mark 峰值 (dB), 未勾选的曲线不显示;
        右侧 6 列为 ICN/ccICN 结果, 左栏 ICN 开关关闭时不显示;
        ICN/ccICN 列单位随最近一次计算值自适应 (V/mV/µV/nV/pV)。
        """
        cols = []
        for name, chk in (('PSFEXT', self.chk_fext_var),
                          ('PSNEXT', self.chk_next_var),
                          ('PSXTLK', self.chk_psxtlk_var)):
            if chk.get():
                cols.append((name, 'dB'))
        if self.icn_enabled_var.get():
            res = self._last_icn_results
            for name in ('ICN_n', 'ICN_f', 'ICN', 'ccICN_n', 'ccICN_f', 'ccICN'):
                unit = 'mV'
                if res is not None:
                    _, unit = self._icn_display(res[name.lower()])
                cols.append((name, unit))
        return cols

    @staticmethod
    def _icn_display(value_v):
        """ICN 结果 (V) → (显示值, 单位): 单位按数值量级自适应缩放,
        如 0.12256 mV 显示为 122.56 µV; 缩放本身不损失精度
        (纯 10 的幂次换算), 仅在最终显示时保留 2 位小数。"""
        av = abs(value_v)
        if av >= 1.0:
            return value_v, 'V'
        if av >= 1e-3:
            return value_v * 1e3, 'mV'
        if av >= 1e-6:
            return value_v * 1e6, 'µV'
        if av >= 1e-9:
            return value_v * 1e9, 'nV'
        return value_v * 1e12, 'pV'

    def _refresh_data_table(self):
        """重建/刷新「计算数据」表格 (列结构变化时重建, 否则只更新数值)。"""
        outer = self._table_outer
        if outer is None or not outer.winfo_exists():
            self.data_table_vars = {}
            self._data_table_cols = None
            return

        cols = self._data_table_columns()
        if tuple(cols) != self._data_table_cols:
            for w in outer.winfo_children():
                w.destroy()
            # 清掉上一轮列的宽度权重, 避免列数变少后残留空列槽位把表格压窄
            for col in range(len(self._data_table_cols or ())):
                outer.columnconfigure(col, weight=0, uniform='')
            self.data_table_vars = {}
            self._data_table_cols = tuple(cols)
            if not cols:
                ttk.Label(outer,
                          text='(无可显示的数据列: 请勾选曲线或打开 ICN 开关)',
                          font='AppSmall').pack(padx=6, pady=4)
            else:
                for col, (name, unit) in enumerate(cols):
                    tk.Label(outer, text=f'{name} ({unit})',
                             font='AppBold',
                             bg='#e9ecef', relief='solid', bd=1).grid(
                        row=0, column=col, sticky='nsew', ipady=3)
                    var = ttk.StringVar(value='--')
                    self.data_table_vars[name] = var
                    tk.Entry(outer, textvariable=var, state='readonly',
                             font='AppMonoLg', relief='solid', bd=1,
                             readonlybackground='white', justify='center'
                             ).grid(row=1, column=col, sticky='nsew', ipady=3)
                    outer.columnconfigure(col, weight=1, uniform='dt')

        marks = self._last_mark_peaks
        res = self._last_icn_results
        for name, unit in cols:
            var = self.data_table_vars.get(name)
            if var is None:
                continue
            if unit == 'dB':
                point = marks.get(name)
                var.set(f'{point[1]:.2f}' if point is not None else '--')
            elif res is None:
                var.set('--')
            else:
                value, _ = self._icn_display(res[name.lower()])
                var.set(f'{value:.2f}')

        # 「批量导出」窗口开着时, 其列结构与单位跟随本表实时刷新
        if self._batch_tree is not None and self._batch_tree.winfo_exists():
            self._batch_refresh_tree()

    def _copy_data_table(self):
        """复制整表 (表头含单位 + 1 行数值, 制表符分隔), 可直接粘贴进 Excel。"""
        cols = self._data_table_columns()
        if not cols or not self.data_table_vars:
            return
        header = '\t'.join(f'{name} ({unit})' for name, unit in cols)
        values = '\t'.join(self.data_table_vars[name].get() for name, _ in cols)
        self.root.clipboard_clear()
        self.root.clipboard_append(f'{header}\n{values}')
        self._set_status('状态: 计算数据已复制到剪贴板 (可直接粘贴进 Excel)')

    def _on_curve_toggle(self):
        """曲线勾选变化: 重绘曲线并同步「计算数据」表格的 Mark 列显隐。"""
        self.draw_curves()
        self._refresh_data_table()

    def _on_icn_toggle(self):
        """ICN 开关变化: 重算 (含表格列显隐), 未加载文件时只刷新表格。"""
        if self.file_loaded:
            self.auto_calculate()
        else:
            self._refresh_data_table()

    def _on_main_window_close(self):
        """主窗口关闭时连带关闭「批量导出」窗口。"""
        if self.batch_window is not None:
            try:
                self.batch_window.destroy()
            except tk.TclError:
                pass
            self.batch_window = None
            self._batch_tree = None
        self.root.destroy()

    # -------------------- 「批量导出」窗口 --------------------

    def _open_batch_export(self):
        """创建或聚焦「批量导出」窗口 (与主窗口一一对应)。"""
        if self.batch_window is not None and self.batch_window.winfo_exists():
            self.batch_window.lift()
            self.batch_window.focus_force()
            return

        win = ttk.Toplevel(self.root)
        win.title('批量导出')
        win.transient(self.root)
        self.batch_window = win

        # 默认居中于主窗口
        self.root.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - 960) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - 460) // 2)
        win.geometry(f'960x460+{x}+{y}')
        win.minsize(640, 300)

        btn_bar = ttk.Frame(win, padding=(8, 8, 8, 4))
        btn_bar.pack(fill='x')
        ttk.Button(btn_bar, text='添加数据', width=12, bootstyle='success-outline',
                   command=self._batch_add_data).pack(side='left')
        ttk.Button(btn_bar, text='删除选中数据', width=13,
                   bootstyle='danger-outline',
                   command=self._batch_delete_selected).pack(side='left',
                                                             padx=(8, 0))
        ttk.Button(btn_bar, text='导出表格', width=12, bootstyle='info-outline',
                   command=self._batch_export_csv).pack(side='left', padx=(8, 0))
        ttk.Button(btn_bar, text='清空数据', width=12,
                   bootstyle='secondary-outline',
                   command=self._batch_clear).pack(side='left', padx=(8, 0))

        tree_frame = ttk.Frame(win, padding=(8, 0, 8, 8))
        tree_frame.pack(fill='both', expand=True)
        style = ttk.Style()
        style.configure('Treeview.Heading', font='AppBold')
        self._batch_tree = ttk.Treeview(tree_frame, show='headings', height=10)
        vsb = ttk.Scrollbar(tree_frame, orient='vertical',
                            command=self._batch_tree.yview)
        self._batch_vsb = vsb
        hsb = ttk.Scrollbar(tree_frame, orient='horizontal',
                            command=self._batch_tree.xview)
        self._batch_tree.configure(yscrollcommand=vsb.set,
                                   xscrollcommand=hsb.set)
        self._batch_tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        # 用户主动关闭: 只关本窗口, 缓存数据保留, 不影响主窗口
        win.protocol('WM_DELETE_WINDOW', self._on_batch_window_close)
        self._batch_refresh_tree()

    def _on_batch_window_close(self):
        if self.batch_window is not None:
            try:
                self.batch_window.destroy()
            except tk.TclError:
                pass
            self.batch_window = None
            self._batch_tree = None

    def _batch_columns(self):
        """当前列表应展示的列: Port/端口名 + 主界面「计算数据」表格
        当前数据列 (实时跟随, 含单位; 缓存行缺该列数据时显示 --)。"""
        return ['Port', '端口名'] + [
            f'{name} ({unit})' for name, unit in self._data_table_columns()]

    def _batch_cell(self, row, name, unit):
        """缓存行某数据列的显示值: 与「计算数据」表格口径一致
        (dB 保留 2 位小数; ICN 原始值 (V) 按当前列单位缩放后保留 2 位小数,
        缩放为纯 10 的幂次换算, 不损失精度)。"""
        value = row['_vals'].get(name)
        if value is None:
            return '--'
        if unit == 'dB':
            return f'{value:.2f}'
        return f'{value * self._BATCH_UNIT_SCALE.get(unit, 1.0):.2f}'

    def _batch_refresh_tree(self):
        """按缓存行重建/刷新列表 (列结构实时跟随主界面「计算数据」表格)。"""
        tree = self._batch_tree
        if tree is None or not tree.winfo_exists():
            return
        cols = self._batch_columns()
        tree.delete(*tree.get_children())
        tree['columns'] = cols
        bold = self._font_bold
        for col in cols:
            tree.heading(col, text=col)
            width = bold.measure(col) + 24
            if col == 'Port':
                for row in self._batch_rows:
                    width = max(width,
                                self.default_font.measure(row['Port']) + 28)
                tree.column(col, width=max(width, 80), anchor='center',
                            stretch=False)
            elif col == '端口名':
                for row in self._batch_rows:
                    width = max(width,
                                self.default_font.measure(row['端口名']) + 28)
                tree.column(col, width=max(width, 140), anchor='w',
                            stretch=False)
            else:
                tree.column(col, width=width, anchor='center', stretch=False)
        name_unit = list(self._data_table_columns())
        for i, row in enumerate(self._batch_rows):
            values = [row['Port'], row['端口名']] + [
                self._batch_cell(row, name, unit) for name, unit in name_unit]
            tree.insert('', 'end', iid=str(i), values=values)
        self._fit_batch_window_width()

    def _fit_batch_window_width(self):
        """窗口宽度随列表总宽自适应 (高度不变; 左右位置不动;
        超出屏幕可用宽度时由横向滚动条兜底)。"""
        win = self.batch_window
        tree = self._batch_tree
        if win is None or tree is None or not win.winfo_exists():
            return
        try:
            total_w = sum(tree.column(c, 'width') for c in tree['columns'])
            total_w += self._batch_vsb.winfo_reqwidth() + 16 + 4  # 边距+边框
            max_w = win.winfo_screenwidth() - 80
            new_w = int(min(max(total_w, 640), max_w))
            if abs(new_w - win.winfo_width()) > 4:
                win.geometry(f'{new_w}x{win.winfo_height()}')
        except tk.TclError:
            pass

    def _batch_add_data(self):
        """把主界面当前计算数据缓存进列表: 单端 1 行; 差分也是 1 行,
        两个 Victim 端口在 Port/端口名列内以 | 合并 (CSV 单元格允许 |)。
        缓存原始计算值 (dB / V), 展示与导出时按主表当前单位实时换算。"""
        if not self.file_loaded or not self.plot_data:
            self._set_status('状态: 请先导入 S 参数并完成一次计算, 再添加数据。',
                             'warn')
            return
        v_ports, _, _ = self._get_selected_ports()
        if not v_ports:
            self._set_status('状态: 当前没有勾选 Victim 端口, 无法添加数据。',
                             'warn')
            return
        names = self.spara_obj.port_names
        vals = {}
        for name, unit in self._data_table_columns():
            if unit == 'dB':
                point = self._last_mark_peaks.get(name)
                vals[name] = point[1] if point is not None else None
            else:
                res = self._last_icn_results
                vals[name] = res[name.lower()] if res is not None else None
        row = {'Port': '|'.join(str(p) for p in v_ports),
               '端口名': '|'.join(names[p - 1] if p - 1 < len(names) else '-'
                                  for p in v_ports),
               '_vals': vals}
        self._batch_rows.append(row)
        self._batch_refresh_tree()
        self._set_status(f"状态: 已缓存 Port {row['Port']} 的计算数据到"
                         f'「批量导出」列表 (共 {len(self._batch_rows)} 行)。',
                         'success')

    def _batch_delete_selected(self):
        """删除列表中选中的缓存行 (支持 Ctrl 多选 / Shift 批量选)。"""
        tree = self._batch_tree
        if tree is None or not tree.winfo_exists():
            return
        selected = tree.selection()
        if not selected:
            self._set_status('状态: 请先在「批量导出」列表中选中要删除的行。',
                             'warn')
            return
        for iid in sorted((int(i) for i in selected), reverse=True):
            if 0 <= iid < len(self._batch_rows):
                del self._batch_rows[iid]
        self._batch_refresh_tree()
        self._set_status(f'状态: 已删除 {len(selected)} 行缓存数据 '
                         f'(剩余 {len(self._batch_rows)} 行)。')

    def _batch_export_csv(self, filepath=None):
        """把列表导出为 CSV (utf-8-sig, 可直接用 Excel 打开);
        默认保存路径为 S 参数所在目录。数值展示口径与主表一致。"""
        if not self._batch_rows:
            self._set_status('状态: 批量导出列表为空, 请先「添加数据」。', 'warn')
            return
        if filepath is None:
            initialdir = ''
            initialfile = 'batch_export.csv'
            if self.filepath:
                initialdir = str(Path(self.filepath).parent)
                initialfile = Path(self.filepath).stem + '_batch.csv'
            filepath = ttk.filedialog.asksaveasfilename(
                parent=self.batch_window, title='导出批量计算数据',
                defaultextension='.csv', initialdir=initialdir or None,
                initialfile=initialfile,
                filetypes=[('CSV 表格', '*.csv'), ('All Files', '*.*')])
        if not filepath:
            return
        cols = self._batch_columns()
        name_unit = list(self._data_table_columns())
        try:
            with open(filepath, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                writer.writerow(cols)
                for row in self._batch_rows:
                    writer.writerow(
                        [row['Port'], row['端口名']] +
                        [self._batch_cell(row, name, unit)
                         for name, unit in name_unit])
        except OSError as e:
            ttk.Messagebox.show_error('错误', f'导出 CSV 失败：\n{e}')
            return
        self._set_status(f'状态: 批量导出完成 → {filepath}', 'success')

    def _batch_clear(self):
        """完全清空列表缓存, 可重新逐条添加。"""
        self._batch_rows.clear()
        self._batch_refresh_tree()
        self._set_status('状态: 批量导出列表已清空, 可重新添加数据。')

    # -------------------- 状态栏 / 防抖 --------------------

    def _set_status(self, text, kind='info'):
        self.status_var.set(text)
        self.status_label.configure(
            foreground=self._STATUS_COLORS.get(kind, self._STATUS_COLORS['info']))

    @contextlib.contextmanager
    def _paused(self):
        """暂停 auto_calculate 的自动触发 (支持嵌套)。"""
        self._suppress_auto += 1
        try:
            yield
        finally:
            self._suppress_auto -= 1

    def _schedule_recalculate(self):
        """输入防抖: 连续按键只在停顿后重算一次。"""
        if self._recalc_job is not None:
            self.root.after_cancel(self._recalc_job)
        self._recalc_job = self.root.after(self._RECALC_DELAY_MS,
                                           self._run_scheduled_recalculate)

    def _run_scheduled_recalculate(self):
        self._recalc_job = None
        self.auto_calculate()

    def _schedule_redraw(self):
        if self._redraw_job is not None:
            self.root.after_cancel(self._redraw_job)
        self._redraw_job = self.root.after(self._REDRAW_DELAY_MS,
                                           self._run_scheduled_redraw)

    def _run_scheduled_redraw(self):
        self._redraw_job = None
        self.draw_curves()

    # -------------------- 端口表操作 --------------------

    def _build_port_table(self, num_ports, port_names):
        """重建端口勾选表数据行: 每行 Port + 端口名 + 3 个真实勾选框
        (表头冻结在滚动区外, 见 _build_port_header, 此处不重建)。"""
        container = self.port_list
        for w in container.winfo_children():
            w.destroy()
        self.port_vars = []
        self._port_rows = []

        for i in range(num_ports):
            port_str = f'Port {i + 1}'
            name_str = port_names[i] if i < len(port_names) else '-'

            vars_tuple = (ttk.BooleanVar(value=False),
                          ttk.BooleanVar(value=False),
                          ttk.BooleanVar(value=False))
            self.port_vars.append(vars_tuple)
            port_label = ttk.Label(container, text=port_str, anchor='center')
            port_label.grid(row=i, column=0, sticky='ew', padx=(4, 2),
                            pady=1)
            name_label = ttk.Label(container, text=name_str,
                                   anchor=self._name_anchor())
            name_label.grid(row=i, column=1, sticky='ew', padx=(2, 6),
                            pady=1)
            v_cb = ttk.Checkbutton(container, variable=vars_tuple[0],
                                   command=lambda p=i + 1:
                                   self.on_victim_changed(p))
            f_cb = ttk.Checkbutton(container, variable=vars_tuple[1],
                                   command=self._on_attack_toggled)
            n_cb = ttk.Checkbutton(container, variable=vars_tuple[2],
                                   command=self._on_attack_toggled)
            v_cb.grid(row=i, column=2, padx=4)
            f_cb.grid(row=i, column=3, padx=4)
            n_cb.grid(row=i, column=4, padx=(4, 6))
            self._port_rows.append(((v_cb, f_cb, n_cb), port_label, name_label))

        self.sync_table_ui()
        self._sync_port_columns()
        self._adapt_right_pane()

    _NAME_ANCHORS = {'左': 'w', '居中': 'center', '右': 'e'}

    def _name_anchor(self):
        return self._NAME_ANCHORS.get(self.name_align_var.get(), 'e')

    def _apply_name_align(self):
        """应用端口名对齐方式到端口名列 (表头与各行)。"""
        anchor = self._name_anchor()
        if getattr(self, '_port_name_header', None) is not None:
            self._port_name_header.configure(anchor=anchor)
        for (_, _, name_label) in self._port_rows:
            name_label.configure(anchor=anchor)

    def sync_table_ui(self):
        """同步勾选状态到行高亮: Victim 蓝 / FEXT 红 / NEXT 绿
        (只改颜色, 不改字体/字号; 多角色同勾时按此优先级取色)。

        勾选框本身直接绑定 BooleanVar, 显示状态无需手动同步。
        """
        for i, (_, port_label, name_label) in enumerate(self._port_rows):
            v_val, f_val, n_val = (var.get() for var in self.port_vars[i])
            if v_val:
                style = 'Victim.TLabel'
            elif f_val:
                style = 'Fext.TLabel'
            elif n_val:
                style = 'Next.TLabel'
            else:
                style = 'TLabel'
            port_label.configure(style=style)
            name_label.configure(style=style)

    def _on_attack_toggled(self):
        """FEXT/NEXT 勾选变化: 更新行高亮并重算。"""
        self.sync_table_ui()
        self.auto_calculate()

    def on_victim_changed(self, clicked_port):
        """当Victim复选框状态改变时，感知具体的端口号"""
        self.sync_table_ui()
        if self.auto_select_var.get():
            if self.port_vars[clicked_port - 1][0].get():
                self.auto_select_ports(target_port=clicked_port)
            else:
                v_ports, _, _ = self._get_selected_ports()
                if not v_ports:
                    self._clear_attack_checks()
                    self.sync_table_ui()
                    self.auto_calculate()
                else:
                    self.auto_select_ports(target_port=v_ports[0])
        else:
            self.auto_calculate()

    def on_arrange_changed(self):
        self._refresh_selection()

    def on_auto_select_toggle(self):
        self._refresh_selection()

    def _refresh_selection(self):
        """自动勾选/排列/模式变化后, 依据当前 Victim 重新推演或直接重算。"""
        if self.auto_select_var.get():
            v_ports, _, _ = self._get_selected_ports()
            if v_ports:
                self.auto_select_ports(target_port=v_ports[0])
                return
        self.auto_calculate()

    def _clear_attack_checks(self):
        """仅清空 FEXT/NEXT 勾选"""
        with self._paused():
            for _, f_var, n_var in self.port_vars:
                f_var.set(False)
                n_var.set(False)

    def _clear_all_checks(self):
        """清空全部勾选"""
        with self._paused():
            for v_var, f_var, n_var in self.port_vars:
                v_var.set(False)
                f_var.set(False)
                n_var.set(False)

    def auto_select_ports(self, target_port=None):
        """根据排列方式和 Victim，自动推演并勾选攻击端口"""
        if not self.file_loaded or not self.auto_select_var.get():
            return

        is_diff = (self.mode_var.get() == self.DIFF_MODE)
        arrange = self.arrange_var.get()
        N = len(self.port_vars)

        def is_side_a(port):
            return port <= N // 2 if arrange == 'Type1' else port % 2 == 1

        def get_partner(port):
            if arrange == 'Type1':
                return port + N // 2 if port <= N // 2 else port - N // 2
            return port + 1 if port % 2 == 1 else port - 1

        # 差分对子划分与计算端共用 sparam_core.diff_pair,
        # 保证自动勾选的配对与功率和计算的配对永远一致
        def get_pair(port):
            return diff_pair(port, N, arrange)

        if target_port is None:
            v_ports, _, _ = self._get_selected_ports()
            if not v_ports:
                self._clear_all_checks()
                self.sync_table_ui()
                self.auto_calculate()
                return
            target_port = v_ports[0]

        if is_diff:
            v_p, v_n = get_pair(target_port)
            partner_v_p, partner_v_n = get_partner(v_p), get_partner(v_n)
            if not (1 <= v_p <= N and 1 <= v_n <= N
                    and 1 <= partner_v_p <= N and 1 <= partner_v_n <= N):
                self.sync_table_ui()
                self.auto_calculate()
                return

        with self._paused():
            self._clear_all_checks()
            if is_diff:
                self.port_vars[v_p - 1][0].set(True)
                self.port_vars[v_n - 1][0].set(True)

                processed_ports = {v_p, v_n, partner_v_p, partner_v_n}
                for p_num in range(1, N + 1):
                    if p_num in processed_ports:
                        continue
                    p_a, p_b = get_pair(p_num)
                    processed_ports.update((p_a, p_b))
                    if p_a > N or p_b > N:
                        continue
                    if is_side_a(v_p) == is_side_a(p_a):
                        self.port_vars[p_a - 1][2].set(True)
                        self.port_vars[p_b - 1][2].set(True)
                    else:
                        self.port_vars[p_a - 1][1].set(True)
                        self.port_vars[p_b - 1][1].set(True)
            else:
                v1 = target_port
                partner = get_partner(v1)
                self.port_vars[v1 - 1][0].set(True)
                for p_num in range(1, N + 1):
                    if p_num in (v1, partner):
                        continue
                    if is_side_a(v1) == is_side_a(p_num):
                        self.port_vars[p_num - 1][2].set(True)
                    else:
                        self.port_vars[p_num - 1][1].set(True)

        self.sync_table_ui()
        self.auto_calculate()

    def toggle_all_column(self, col_idx, state):
        for vars_tuple in self.port_vars:
            vars_tuple[col_idx].set(state)
        self.sync_table_ui()
        self.auto_calculate()

    def _get_selected_ports(self):
        v_ports, f_ports, n_ports = [], [], []
        for i, (v, f, n) in enumerate(self.port_vars):
            if v.get():
                v_ports.append(i + 1)
            if f.get():
                f_ports.append(i + 1)
            if n.get():
                n_ports.append(i + 1)
        return v_ports, f_ports, n_ports

    # -------------------- 模式切换 --------------------

    def mode_changed(self):
        if self.mode_var.get() == self.DIFF_MODE:
            self.table_msg_var.set('▶ 差分模式：Victim 必须勾选 2 个；攻击端口必须成对勾选'
                                   '（对子按上方「排列方式」划分）。')
        else:
            self.table_msg_var.set('▶ 单端模式：Victim 仅需勾选 1 个；攻击端口可任意数量(奇/偶)勾选。')
        self._refresh_selection()

    # -------------------- 导入 S 参数文件 --------------------

    def import_s_params(self, filepath=None):
        if filepath is None:
            filepath = ttk.filedialog.askopenfilename(
                title='Select S-Parameter File',
                filetypes=[('S-Parameter Files', '*.s*p'), ('All Files', '*.*')])
        if not filepath:
            return

        self.filepath = filepath
        filename = Path(filepath).name
        self.path_btn.configure(text=filename)
        self._set_status('状态: 正在读取矩阵与频率...', 'info')
        self.root.update_idletasks()

        try:
            self.spara_obj = SParameter(filepath)
            num_ports = self.spara_obj.num_ports
            freqs = self.spara_obj.frequencies

            self.file_z0_var.set(f'文件原始阻抗: {self.spara_obj.z0:g} Ω')

            self._sparam_cache = None  # 新文件: 旧重采样缓存作废

            if len(freqs) > 1:
                with self._paused():
                    # 界面频率输入单位为 Hz; fstep 默认取文件原始频点间隔。
                    # 用 repr 写回最短往返精度的十进制文本, 保证输入框
                    # 文本与文件值之间零精度损失 (计算链路不允许四舍五入,
                    # 只有最终 UI 展示数据时才按显示需要格式化)
                    self.fmin_var.set(repr(float(freqs[0])))
                    self.fmax_var.set(repr(float(freqs[-1])))
                    self.fstep_var.set(
                        repr(float((freqs[-1] - freqs[0]) / (len(freqs) - 1))))

            self._build_port_table(num_ports, self.spara_obj.port_names)

            for var in (self.chk_all_v_var, self.chk_all_f_var, self.chk_all_n_var):
                var.set(False)

            saved = self._load_file_config(self._get_file_config_path())
            if saved:
                self._apply_saved_config(saved, num_ports)
                self.sync_table_ui()

            self.file_loaded = True
            # 先完成模式刷新 (可能触发自动重算), 再设置导入结果状态,
            # 避免导入结果提示被随后的"计算完成"状态覆盖。
            self.mode_changed()
            if saved:
                self._set_status(f'状态: 识别到同名配置， [{filename}] 的设置已恢复。',
                                 'success')
            else:
                self._set_status('状态: 新文件导入成功！自动提取频率参数完成。', 'success')

        except Exception as e:
            self.file_loaded = False
            self._set_status('状态: 文件解析错误！', 'error')
            ttk.Messagebox.show_error('错误', f'读取文件失败：\n{str(e)}')

    # -------------------- 自动计算 --------------------

    def auto_calculate(self):
        if self._recalc_job is not None:
            self.root.after_cancel(self._recalc_job)
            self._recalc_job = None
        if self._suppress_auto or not self.file_loaded:
            return

        # 重新计算前的中间状态: 清空结果区, 计算成功后再刷新
        self._last_icn_results = None
        self._refresh_data_table()

        v_ports, f_ports, n_ports = self._get_selected_ports()
        is_diff = (self.mode_var.get() == self.DIFF_MODE)

        if not f_ports and not n_ports:
            self._set_status('状态: 等待中... 请至少勾选一组攻击端口(FEXT/NEXT)！', 'warn')
            return
        if is_diff:
            if len(v_ports) != 2:
                self._set_status('状态: 差分模式下，Victim 必须勾选且仅能勾选 2 个！', 'warn')
                return
            if len(f_ports) % 2 != 0 or len(n_ports) % 2 != 0:
                self._set_status('状态: 差分模式下，攻击端口数量必须是偶数对！', 'error')
                return
            # 按 DUT 引脚排列 (排列方式) 校验对子完整性并重排端口,
            # 使列表中相邻两个端口恰为一对 —— power_sum_db 按此约定配对。
            # 仅校验偶数不够: 两个"半对"(如勾选 1,4) 总数也是偶数,
            # 会被静默错配, 必须逐端口核对配对端口是否同列勾选。
            arrange = self.arrange_var.get()
            N = len(self.port_vars)
            broken = (find_broken_pairs(f_ports, N, arrange)
                      + find_broken_pairs(n_ports, N, arrange))
            if broken:
                arrange_name = '排列一' if arrange == 'Type1' else '排列二'
                self._set_status(
                    f'状态: 差分对子不完整！端口 {broken} 的配对端口未同列勾选'
                    f'（对子按{arrange_name}划分）。',
                    'error')
                return
            f_ports = order_ports_by_pairs(f_ports, N, arrange)
            n_ports = order_ports_by_pairs(n_ports, N, arrange)
            v_ports = order_ports_by_pairs(v_ports, N, arrange)
        else:
            if len(v_ports) != 1:
                self._set_status('状态: 单端模式下，Victim 必须勾选且仅能勾选 1 个！', 'warn')
                return

        try:
            z0 = self.z0_var.get()
            # 界面输入单位为 Hz (科学计数法/表达式), 重算时求值,
            # 重采样缓存内部使用 GHz
            fmin = float(evaluate_expr(self.fmin_var.get())) / 1e9
            fmax = float(evaluate_expr(self.fmax_var.get())) / 1e9
            fstep = float(evaluate_expr(self.fstep_var.get())) / 1e9
        except (ValueError, tk.TclError):
            self._set_status('状态: 输入参数无效！', 'error')
            return

        if fmin > fmax:
            self._set_status('状态: 最小频率不能大于最大频率！', 'error')
            return
        if z0 <= 0:
            self._set_status('状态: Z0 参考阻抗必须大于 0！', 'error')
            return
        if fmin < 0:
            self._set_status('状态: 频率下限不能为负数！', 'error')
            return
        if fstep <= 0:
            self._set_status('状态: 频率步长必须大于 0！', 'error')
            return
        num_points = int(round((fmax - fmin) / fstep)) + 1
        if num_points > 500_000:
            self._set_status(
                f'状态: 频率点数过多（{num_points} 个）！请增大步长或缩小范围。',
                'error')
            return

        self._set_status('状态: 正在计算并绘制曲线...', 'info')
        self.root.update_idletasks()

        # 重采样缓存: 基本参数 (z0/fmin/fmax/fstep) 任一变化即整体重建;
        # 端口勾选/公式/绘图开关变化时直接复用缓存, 避免重复插值。
        # 缓存的是"归一化 + 插值后"的数据副本, 原始 spara_obj 不被修改。
        try:
            key = (z0, fmin, fmax, fstep)
            cache = self._sparam_cache
            if cache is None or cache[0] != key:
                params, freqs = resample_sparams(
                    self.spara_obj, z0, fmin, fmax, fstep)
                cache = (key, types.SimpleNamespace(
                    parameters=params, frequencies=freqs,
                    z0=z0, num_ports=params.shape[0]))
                self._sparam_cache = cache
            self.plot_data = power_sum_db(
                cache[1], z0, v_ports, f_ports, n_ports, is_diff,
                fmin, fmax, formulas=self._current_formulas())
        except (ValueError, np.linalg.LinAlgError, ZeroDivisionError) as e:
            self._set_status(f'状态: 计算失败！{e}', 'error')
            return

        # 差分模式回显实际参与计算的端口对子, 便于核对配对是否符合预期
        if is_diff:
            def pair_text(ports):
                return ' '.join(
                    f'({ports[i]},{ports[i + 1]})'
                    for i in range(0, len(ports), 2)) or '无'
            self._set_status(
                f'状态: 计算完成！Victim 对 {tuple(v_ports)}；'
                f'NEXT 对: {pair_text(n_ports)}；FEXT 对: {pair_text(f_ports)}',
                'success')
        else:
            self._set_status('状态: 计算完成！参数修改可实时重算重绘。', 'success')
        self._update_icn_results()
        self.draw_curves()

    # -------------------- ICN / ccICN 计算与公式编辑 --------------------

    def _eval_icn_scalars(self):
        """求值 8 个标量输入框 (支持表达式/科学计数法, 不做四舍五入)。

        返回 (scalars dict, 错误信息); scalars 以公式环境名
        (Ant/Aft/fb/Tnt/Tft/PAM/ILn/ILf) 为键, 成功时错误信息为 None。
        """
        scalars = {}
        for key, var in self.icn_scalar_vars.items():
            env_name = self._ICN_SCALAR_ENV[key]
            try:
                scalars[env_name] = float(evaluate_expr(var.get()))
            except ValueError as e:
                return None, f'{env_name}: {e}'
        return scalars, None

    def _current_formulas(self):
        return {key: var.get() for key, var in self.formula_vars.items()}

    def _toggle_formula_edit(self, key):
        """公式编辑/完成按钮切换。同一时间只允许编辑一条公式。"""
        entry, btn = self.formula_widgets[key]
        if self._formula_editing is None:
            self._formula_editing = key
            entry.config(state='normal')
            btn.config(text='完成', bootstyle='success-outline')
            entry.focus_set()
            self._set_status(f'状态: 正在编辑公式 [{key}]，点击「完成」校验并生效。',
                             'info')
        elif self._formula_editing == key:
            err = self._validate_formulas()
            if err:
                ttk.Messagebox.show_error('公式校验失败', err)
                return
            self._formula_editing = None
            entry.config(state='readonly')
            btn.config(text='编辑', bootstyle='secondary-outline')
            self._set_status(f'状态: 公式 [{key}] 已更新，计算沿用新公式。', 'success')
            self._schedule_recalculate()
        else:
            self._set_status(
                f'状态: 请先点击「完成」结束公式 [{self._formula_editing}] 的编辑。',
                'warn')

    def _validate_formulas(self):
        """用合成环境 (不依赖已加载文件) 试算全部公式。

        返回错误信息字符串; 全部通过返回 None。
        """
        scalars, err = self._eval_icn_scalars()
        if err:
            return f'输入变量错误: {err}'
        try:
            fstep_hz = float(evaluate_expr(self.fstep_var.get()))
        except (ValueError, tk.TclError) as e:
            return f'fstep: {e}'
        if fstep_hz <= 0:
            return 'fstep 必须大于 0'
        scalars['fstep'] = fstep_hz
        fn = np.linspace(1e6, 1e10, 8)
        ones = np.ones_like(fn)
        formulas = self._current_formulas()
        try:
            icn_calculate(fn, ones, ones, scalars, formulas)
        except ValueError as e:
            return str(e)

        # FEXT_dB/NEXT_dB/PSFEXT/PSNEXT/PSXTLK 在 power_sum_db 中求值,
        # 这里用合成 (2 条路径 x 8 频点) 二维 dB 矩阵单独校验
        env = {'RAW_FEXT_dB': np.tile(-fn / 1e10, (2, 1)),
               'RAW_NEXT_dB': np.tile(-2 * fn / 1e10, (2, 1))}
        for name in ('FEXT_dB', 'NEXT_dB'):
            raw = env['RAW_' + name]
            try:
                arr = np.asarray(evaluate_expr(formulas[name], env),
                                 dtype=float)
            except ValueError as e:
                return f'公式 {name}: {e}'
            if arr.shape != raw.shape:
                return (f'公式 {name}: 结果形状 {arr.shape} 与 '
                        f'RAW_{name} {raw.shape} 不一致')
            env[name] = arr
        for name in ('PSFEXT', 'PSNEXT', 'PSXTLK'):
            try:
                arr = np.asarray(evaluate_expr(formulas[name], env),
                                 dtype=float)
            except ValueError as e:
                return f'公式 {name}: {e}'
            if arr.shape != fn.shape:
                return (f'公式 {name}: 结果形状 {arr.shape} '
                        f'与频点 {fn.shape} 不一致')
            env[name] = np.where(np.isfinite(arr), arr, -100.0)
        return None

    def _fail_icn(self, message):
        """ICN 计算失败: 置空结果并在状态栏提示, 不阻塞功率和曲线。"""
        self._last_icn_results = None
        self._refresh_data_table()
        self._set_status(f'状态: ICN/ccICN 计算失败 ({message})', 'warn')

    def _update_icn_results(self):
        """基于最近一次功率和结果计算 ICN / ccICN 并刷新结果区。

        左栏 ICN 开关关闭时跳过计算; ICN 参数/公式无效时不阻塞
        功率和曲线 (保持既有行为), 仅在状态栏提示。
        """
        if not self.icn_enabled_var.get():
            self._last_icn_results = None
            self._refresh_data_table()
            return
        if not self.plot_data:
            return
        scalars, err = self._eval_icn_scalars()
        if err:
            self._fail_icn(f'输入变量错误 {err}')
            return
        try:
            fstep_hz = float(evaluate_expr(self.fstep_var.get()))
        except (ValueError, tk.TclError):
            self._fail_icn('fstep 无效')
            return
        if fstep_hz <= 0:
            self._fail_icn('fstep 必须大于 0')
            return
        scalars['fstep'] = fstep_hz

        try:
            result = icn_calculate(
                self.plot_data['fn_hz'],
                self.plot_data['fext_power_lin'],
                self.plot_data['next_power_lin'],
                scalars, self._current_formulas())
        except ValueError as e:
            self._fail_icn(str(e))
            return

        self._last_icn_results = result
        self._refresh_data_table()

    # -------------------- 绘制曲线 --------------------

    def draw_curves(self):
        if self._redraw_job is not None:
            self.root.after_cancel(self._redraw_job)
            self._redraw_job = None
        # Mark 峰值随每次重绘重新计算 (先清空, 标注时逐条填入)
        self._last_mark_peaks = {}
        if not self.plot_data or self.ax is None or self.canvas_mpl is None:
            return

        data = self.plot_data
        self.ax.clear()
        self.ax.grid(True, alpha=0.4)

        if self.chk_fext_var.get() and data['has_fext']:
            self.ax.plot(data['fn_ghz'], data['MDFEXT'], color='#D95319',
                         linewidth=1.5, label='Power Sum FEXT')
        if self.chk_next_var.get() and data['has_next']:
            self.ax.plot(data['fn_ghz'], data['MDNEXT'], color='#0072BD',
                         linewidth=1.5, label='Power Sum NEXT')
        if self.chk_psxtlk_var.get() and (data['has_fext'] or data['has_next']):
            self.ax.plot(data['fn_ghz'], data['PSXTLK'], color='#77AC30',
                         linewidth=2, label='Total PSXTLK')

        self._annotate_mark_peak(data)

        fs = self._plot_fontsize()
        self.ax.set_xlabel('Frequency (GHz)', fontsize=fs, fontweight='bold')
        self.ax.set_ylabel('Magnitude (dB)', fontsize=fs, fontweight='bold')
        if self.ax.lines:
            self.ax.legend(loc='best', fontsize=fs - 1)
        self.ax.tick_params(labelsize=fs - 1)
        self.fig.tight_layout()
        self.canvas_mpl.draw()
        self._refresh_data_table()

    def _annotate_mark_peak(self, data):
        """在 Mark Freq 上限之前为各条可见曲线标注峰值。"""
        mark_str = self.mark_freq_var.get().strip()
        if not mark_str:
            return
        try:
            mark_target = float(mark_str)
        except ValueError:
            self._set_status('状态: Mark Freq 输入无效，已忽略。', 'warn')
            return

        if mark_target < data['fn_ghz'][0]:
            return
        mark_target = min(mark_target, data['fn_ghz'][-1])
        m_idx = int(np.argmin(np.abs(data['fn_ghz'] - mark_target))) + 1

        series = (
            ('PSFEXT', 'MDFEXT', 'has_fext', self.chk_fext_var, '#D95319'),
            ('PSNEXT', 'MDNEXT', 'has_next', self.chk_next_var, '#0072BD'),
            ('PSXTLK', 'PSXTLK', None, self.chk_psxtlk_var, '#77AC30'),
        )
        points = []
        for name, key, flag, chk, color in series:
            if not chk.get():
                continue
            if flag is not None and not data[flag]:
                continue
            seg = data[key][:m_idx]
            idx = int(np.argmax(seg))
            self._last_mark_peaks[name] = (data['fn_ghz'][idx], seg[idx])
            points.append((data['fn_ghz'][idx], seg[idx], color))
        if not points:
            return

        points.sort(key=lambda p: p[1], reverse=True)
        offsets = {3: [(0, 18), (18, -8), (0, -18)],
                   2: [(0, 18), (0, -18)],
                   1: [(12, 12)]}.get(len(points), [])

        for (x, y, color), offset in zip(points, offsets):
            self.ax.plot(x, y, 'o', markeredgecolor='k', markerfacecolor=color,
                         markersize=7, zorder=5)
            self.ax.annotate(
                f'{y:.2f} dB', (x, y), xytext=offset,
                textcoords='offset points', color=color, fontweight='bold',
                fontsize=max(8, self._plot_fontsize() - 2),
                arrowprops=dict(arrowstyle='->', color=color, lw=0.8),
                bbox=dict(boxstyle='round,pad=0.3', fc='white', ec=color, alpha=0.85))

    # -------------------- 配置文件读写 --------------------

    @staticmethod
    def _default_config_path():
        """默认配置文件固定在 exe (打包) 或脚本 (源码) 所在目录。"""
        if getattr(sys, 'frozen', False):
            base = Path(sys.executable).resolve().parent
        else:
            base = Path(__file__).resolve().parent
        return str(base / 'spara_ui_default.json')

    def _get_file_config_path(self):
        return self.filepath + '.json' if self.filepath else None

    @staticmethod
    def _load_file_config(config_path):
        if not config_path or not Path(config_path).exists():
            return None
        try:
            return json.loads(Path(config_path).read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def _save_file_config(config_path, config):
        try:
            Path(config_path).write_text(
                json.dumps(config, indent=2, ensure_ascii=False), encoding='utf-8')
            return True
        except OSError as e:
            ttk.Messagebox.show_error('错误', f'保存配置失败：\n{str(e)}')
            return False

    def _apply_saved_config(self, saved, num_ports=None):
        """把已保存的配置恢复到界面变量 (端口勾选需已知端口数)。"""
        with self._paused():
            for key, attr in self._SCALAR_FIELDS:
                if key in saved:
                    getattr(self, attr).set(saved[key])
            # 旧配置 (PSXT 改名 PSXTLK 之前) 的遗留键, 仅在新键缺失时补读
            for old_key, attr in self._LEGACY_SCALAR_ALIAS.items():
                new_key = next((k for k, a in self._SCALAR_FIELDS if a == attr),
                               None)
                if old_key in saved and (new_key is None or new_key not in saved):
                    getattr(self, attr).set(saved[old_key])
            if num_ports:
                for key, idx in self._PORT_FIELDS:
                    for p in saved.get(key, []):
                        if 1 <= p <= num_ports:
                            self.port_vars[p - 1][idx].set(True)

    def _load_last_config(self):
        saved = self._load_file_config(self._default_config_path())
        if not saved:
            return
        self._apply_saved_config(saved)
        self.mode_changed()

    def save_config(self):
        v_ports, f_ports, n_ports = self._get_selected_ports()

        curr = {key: getattr(self, attr).get()
                for key, attr in self._SCALAR_FIELDS}
        curr.update({'v_ports': v_ports, 'f_ports': f_ports, 'n_ports': n_ports})

        config_path = self._get_file_config_path()
        if config_path:
            if self._save_file_config(config_path, curr):
                ttk.Messagebox.show_info(
                    '保存成功',
                    f'设置已保存到：\n{config_path}\n\n'
                    f'以后重新导入该S参数时配置将自动恢复。')
        else:
            if self._save_file_config(self._default_config_path(), curr):
                ttk.Messagebox.show_info(
                    '保存成功',
                    '因为未导入任何S参数，当前界面数据\n'
                    '已被保存为【缺省默认配置】。')
