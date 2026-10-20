"""S参数串扰计算工具 —— GUI 主程序 (ttkbootstrap)。

布局:
- 主窗口左栏: 「S参数文件」「计算参数」「端口自动勾选」三个分组
  + 保存配置 / 显示图表窗口按钮
- 主窗口右栏: 端口勾选映射表
- 独立图表窗口 (Toplevel): 画布上方工具行 (曲线开关 + Mark Freq),
  启动时自动弹出; 用户主动关闭不影响主窗口, 可经按钮重新打开;
  主窗口关闭时连带关闭; 主窗口与图表窗口一一对应
- 主窗口底部: 全宽状态栏 (info/success/warn/error 四色语义)

行为与 V3.3 保持一致, 重构点:
- 输入 (Z0/频率/Mark) 采用 300ms 级防抖, 避免每次按键全量重算;
- 配置恢复/保存统一走 _SCALAR_FIELDS/_PORT_FIELDS 映射表;
- 默认配置文件固定在 exe (打包) 或脚本 (源码) 所在目录, 不再依赖 CWD。
"""

import contextlib
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
        ('chk_next', 'chk_next_var'), ('chk_psxt', 'chk_psxt_var'),
        ('auto_select', 'auto_select_var'), ('arrange', 'arrange_var'),
        ('icn_enabled', 'icn_enabled_var'),
    ) + tuple((key, key + '_var') for key, _ in _ICN_SCALAR_DEFAULTS) \
      + tuple(('formula_' + key, 'formula_var_' + key)
              for key in DEFAULT_FORMULAS)
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

    def __init__(self, root):
        self.root = root
        self.root.title('S参数串扰计算')

        self._setup_scaling()

        self.file_loaded = False
        self.spara_obj = None
        self.plot_data = {}
        self.filepath = ''
        self.port_vars = []
        self.port_items = {}
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
        self.plot_window = None

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

        # 曲线开关与 Mark Freq 变量在主窗口构建前创建 (配置恢复依赖它们);
        # mark_freq 的防抖重绘 trace 只注册一次, 避免图表窗口重开时重复注册
        self.chk_fext_var = ttk.BooleanVar(value=True)
        self.chk_next_var = ttk.BooleanVar(value=True)
        self.chk_psxt_var = ttk.BooleanVar(value=True)
        self.mark_freq_var = ttk.StringVar(value='')
        self.mark_freq_var.trace_add('write', lambda *_: self._schedule_redraw())
        # 端口名对齐方式: 左 / 居中 / 右
        self.name_align_var = ttk.StringVar(value='左')

        self._build_ui()
        self._load_last_config()

        # 主窗口关闭时连带关闭图表窗口
        self.root.protocol('WM_DELETE_WINDOW', self._on_main_window_close)
        # 启动后异步弹出独立图表窗口
        self.root.after(100, self._open_plot_window)

    # -------------------- 窗口与样式 --------------------

    def _setup_scaling(self):
        """根据屏幕分辨率计算合适的字体与窗口大小"""
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()

        win_w = max(900, min(int(sw * 0.65), 1400))
        win_h = max(680, min(int(sh * 0.7), 900))
        x = (sw - win_w) // 2
        y = (sh - win_h) // 2
        self.root.geometry(f'{win_w}x{win_h}+{max(x, 0)}+{max(y, 0)}')
        self.root.minsize(900, 680)

        base_size = 9 if sw < 1500 else 10
        self.default_font = tkfont.nametofont('TkDefaultFont')
        self.default_font.configure(size=base_size)

    def _build_ui(self):
        # 底部状态栏先布局, 主区域占据其余全部空间
        status_bar = ttk.Frame(self.root, padding=(10, 2, 10, 6))
        status_bar.pack(side='bottom', fill='x')
        self.status_var = ttk.StringVar(value='状态: 请点击左侧按钮导入 S 参数文件')
        self.status_label = ttk.Label(status_bar, textvariable=self.status_var,
                                      font=('TkDefaultFont', 9, 'bold'))
        self.status_label.pack(side='left')
        ttk.Sizegrip(status_bar).pack(side='right')

        main_paned = ttk.Panedwindow(self.root, orient='horizontal')
        main_paned.pack(fill='both', expand=True, padx=10, pady=(8, 0))

        # ===== 左侧面板 (内容过长, 用可滚动容器) =====
        left_frame = ttk.LabelFrame(main_paned, text='设置与操作', padding=10)
        main_paned.add(left_frame, weight=0)
        left_frame.columnconfigure(0, weight=1)
        left_frame.rowconfigure(0, weight=1)
        panel = ttk.ScrolledFrame(left_frame, autohide=True)
        panel.grid(row=0, column=0, sticky='nsew')

        file_group = ttk.LabelFrame(panel, text='S参数文件', padding=8)
        file_group.grid(row=0, column=0, sticky='ew', pady=(0, 6))
        file_group.columnconfigure(0, weight=1)
        self.path_btn = ttk.Button(file_group, text='点击此处选择 .sNp 文件...',
                                   command=self.import_s_params,
                                   bootstyle='info-outline')
        self.path_btn.grid(row=0, column=0, sticky='ew')
        self.file_z0_var = ttk.StringVar(value='文件原始阻抗: --')
        ttk.Label(file_group, textvariable=self.file_z0_var,
                  font=('TkDefaultFont', 8)).grid(row=1, column=0, sticky='w',
                                                  pady=(4, 0))

        param_group = ttk.LabelFrame(panel, text='基本参数', padding=8)
        param_group.grid(row=1, column=0, sticky='ew', pady=(0, 6))
        param_group.columnconfigure(0, weight=1)

        ttk.Label(param_group, text='计算模式:').grid(row=0, column=0, sticky='w')
        self.mode_var = ttk.StringVar(value=self.DIFF_MODE)
        mode_combo = ttk.Combobox(param_group, textvariable=self.mode_var,
                                  state='readonly',
                                  values=[self.DIFF_MODE, self.SE_MODE])
        mode_combo.grid(row=1, column=0, sticky='ew', pady=(2, 6))
        mode_combo.bind('<<ComboboxSelected>>', lambda e: self.mode_changed())

        z0_frame = ttk.Frame(param_group)
        z0_frame.grid(row=2, column=0, sticky='ew', pady=(0, 6))
        ttk.Label(z0_frame, text='Z0 (参考阻抗 Ω):').pack(side='left')
        self.z0_var = ttk.DoubleVar(value=50.0)
        ttk.Entry(z0_frame, textvariable=self.z0_var, width=8).pack(side='right')
        self.z0_var.trace_add('write', lambda *_: self._schedule_recalculate())

        ttk.Label(param_group,
                  text='Freq (Hz) [ min / max / step ]:').grid(row=3, column=0,
                                                               sticky='w')
        freq_frame = ttk.Frame(param_group)
        freq_frame.grid(row=4, column=0, sticky='ew', pady=(2, 0))
        freq_frame.columnconfigure((0, 1, 2), weight=1, uniform='freq')
        # StringVar + 科学计数法显示 (如 1e+07), 支持表达式输入,
        # 重算时经 evaluate_expr 求值; 修改任一基本参数都会触发
        # S 参数重采样缓存重建 (见 auto_calculate)
        self.fmin_var = ttk.StringVar(value='1e+07')
        self.fmax_var = ttk.StringVar(value='8e+10')
        self.fstep_var = ttk.StringVar(value='1e+07')
        for col, var in enumerate((self.fmin_var, self.fmax_var, self.fstep_var)):
            ttk.Entry(freq_frame, textvariable=var, width=8,
                      font=('Consolas', 9)).grid(
                row=0, column=col, sticky='ew', padx=(0 if col == 0 else 2, 0))
            var.trace_add('write', lambda *_: self._schedule_recalculate())

        # ===== ICN / ccICN 参数: 启用开关 + 变量输入 + 公式编辑区 =====
        icn_group = ttk.LabelFrame(panel, text='ICN相关计算', padding=8)
        icn_group.grid(row=2, column=0, sticky='ew', pady=(0, 6))
        icn_group.columnconfigure(1, weight=1)

        # 启用开关: 关闭时不计算 ICN/ccICN, 「计算数据」表格也不显示对应列
        ttk.Checkbutton(icn_group, text='启用 ICN / ccICN 计算',
                        variable=self.icn_enabled_var,
                        bootstyle='round-toggle',
                        command=self._on_icn_toggle).grid(
            row=0, column=0, columnspan=3, sticky='w', pady=(0, 4))

        icn_labels = {
            'ant_v': 'Ant 近端幅度 (V) 非pk-pk值',
            'aft_v': 'Aft 远端幅度 (V) 非pk-pk值',
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
                row=row, column=0, sticky='w', pady=1)
            var = ttk.StringVar(value=default)
            self.icn_scalar_vars[key] = var
            setattr(self, key + '_var', var)
            ttk.Entry(icn_group, textvariable=var, width=16,
                      font=('Consolas', 9)).grid(
                row=row, column=1, sticky='e', pady=1)
            var.trace_add('write', lambda *_: self._schedule_recalculate())

        # ---- 公式区: 只读展示; 点「编辑」方可修改, 点「完成」校验并生效 ----
        ttk.Separator(icn_group, orient='horizontal').grid(
            row=9, column=0, columnspan=3, sticky='ew', pady=(6, 4))
        ttk.Label(icn_group,
                  text='计算公式 (点击「编辑」修改, 「完成」生效):',
                  font=('TkDefaultFont', 8, 'bold')).grid(
            row=10, column=0, columnspan=3, sticky='w')

        self.formula_vars = {}
        self.formula_widgets = {}
        for row, (key, default) in enumerate(DEFAULT_FORMULAS.items(),
                                             start=11):
            ttk.Label(icn_group, text=key,
                      font=('TkDefaultFont', 8)).grid(
                row=row, column=0, sticky='w', pady=1)
            var = ttk.StringVar(value=default)
            self.formula_vars[key] = var
            setattr(self, 'formula_var_' + key, var)
            # 输入框固定宽度展示 (公式可点选全量复制查看)
            entry = ttk.Entry(icn_group, textvariable=var, width=30,
                              font=('Consolas', 8), state='readonly')
            entry.grid(row=row, column=1, sticky='ew', pady=1, padx=(4, 2))
            btn = ttk.Button(icn_group, text='编辑', width=4,
                             bootstyle='secondary-outline',
                             command=lambda k=key: self._toggle_formula_edit(k))
            btn.grid(row=row, column=2, sticky='e', pady=1)
            self.formula_widgets[key] = (entry, btn)

        # (计算结果不在此处展示; 在「计算结果」窗口曲线图下方的「计算数据」表格中)

        auto_group = ttk.LabelFrame(panel,
                                    text='端口自动勾选 (仅标准对称S参数可用)',
                                    padding=8)
        auto_group.grid(row=3, column=0, sticky='ew', pady=(0, 6))
        self.auto_select_var = ttk.BooleanVar(value=False)
        ttk.Checkbutton(
            auto_group,
            text='启用自动勾选 (仅需手动选择受害端口。攻击端口会自动选择)',
            variable=self.auto_select_var,
            command=self.on_auto_select_toggle).pack(anchor='w')
        self.arrange_var = ttk.StringVar(value='Type1')
        ttk.Radiobutton(auto_group, text='排列一: 如N=8, 同侧(1,2,3,4) 对侧(5,6,7,8)',
                        variable=self.arrange_var, value='Type1',
                        command=self.on_arrange_changed).pack(anchor='w', padx=(10, 0))
        ttk.Radiobutton(auto_group, text='排列二: 如N=8, 同侧(1,3,5,7) 对侧(2,4,6,8)',
                        variable=self.arrange_var, value='Type2',
                        command=self.on_arrange_changed).pack(anchor='w', padx=(10, 0))

        ttk.Button(panel, text='保存当前文件配置', command=self.save_config,
                   bootstyle='success-outline').grid(row=4, column=0, sticky='ew',
                                                     pady=(6, 0))
        ttk.Button(panel, text='显示图表窗口', command=self._open_plot_window,
                   bootstyle='info-outline').grid(row=5, column=0, sticky='ew',
                                                  pady=(6, 0))

        # ===== 右侧面板 (端口勾选映射表; 图表已拆分为独立窗口) =====
        right_frame = ttk.Frame(main_paned)
        main_paned.add(right_frame, weight=1)

        table_group = ttk.LabelFrame(right_frame, text='端口勾选映射表', padding=8)
        table_group.pack(fill='both', expand=True)

        self.table_msg_var = ttk.StringVar(
            value='▶ 差分模式：Victim 必须勾选 2 个；攻击端口必须成对勾选'
                  '（对子按左侧「排列方式」划分）。')
        ttk.Label(table_group, textvariable=self.table_msg_var,
                  font=('TkDefaultFont', 9, 'bold'),
                  foreground='#cc4d1a').pack(fill='x', pady=(0, 5), anchor='w')

        sel_frame = ttk.Frame(table_group)
        sel_frame.pack(fill='x', pady=(0, 5))
        ttk.Label(sel_frame, text='快捷全选列:').pack(side='left')
        self.chk_all_v_var = ttk.BooleanVar(value=False)
        self.chk_all_f_var = ttk.BooleanVar(value=False)
        self.chk_all_n_var = ttk.BooleanVar(value=False)
        ttk.Checkbutton(sel_frame, text='全选 Victim', variable=self.chk_all_v_var,
                        command=lambda: self.toggle_all_column(0, self.chk_all_v_var.get())
                        ).pack(side='left', padx=5)
        ttk.Checkbutton(sel_frame, text='全选 FEXT', variable=self.chk_all_f_var,
                        command=lambda: self.toggle_all_column(1, self.chk_all_f_var.get())
                        ).pack(side='left', padx=5)
        ttk.Checkbutton(sel_frame, text='全选 NEXT', variable=self.chk_all_n_var,
                        command=lambda: self.toggle_all_column(2, self.chk_all_n_var.get())
                        ).pack(side='left', padx=5)
        ttk.Label(sel_frame, text='端口名对齐:').pack(side='left', padx=(15, 3))
        align_combo = ttk.Combobox(sel_frame, textvariable=self.name_align_var,
                                   state='readonly', width=4,
                                   values=['左', '居中', '右'])
        align_combo.pack(side='left')
        align_combo.bind('<<ComboboxSelected>>',
                         lambda e: self._apply_name_align())

        tree_container = ttk.Frame(table_group)
        tree_container.pack(fill='both', expand=True)

        style = ttk.Style()
        style.configure('Treeview', rowheight=25)
        style.configure('Treeview.Heading', font=('TkDefaultFont', 9, 'bold'))

        self.tree = ttk.Treeview(tree_container,
                                 columns=('Port', 'Name', 'Victim', 'FEXT', 'NEXT'),
                                 show='headings', height=10)
        self.tree.heading('Port', text='Port')
        self.tree.heading('Name', text='端口名 (解析自S参数注释)')
        self.tree.heading('Victim', text='Victim')
        self.tree.heading('FEXT', text='FEXT')
        self.tree.heading('NEXT', text='NEXT')
        self.tree.column('Port', width=80, anchor='center', stretch=False)
        self.tree.column('Name', width=200, anchor='w', stretch=True)
        self.tree.column('Victim', width=65, anchor='center', stretch=False)
        self.tree.column('FEXT', width=65, anchor='center', stretch=False)
        self.tree.column('NEXT', width=65, anchor='center', stretch=False)
        # 交替行底色 + Victim 行高亮
        self.tree.tag_configure('odd', background='#f5f8fb')
        self.tree.tag_configure('victim', background='#e7f1ff')

        vsb = ttk.Scrollbar(tree_container, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')
        self.tree.bind('<Button-1>', self.on_table_click)

        # ---- 分割条位置自适应左侧内容宽度 ----
        # ScrolledFrame 会把内容宽度钉死在视口宽度上, 因此先量出左栏
        # 各分组的自然请求宽度, 视口取最宽分组 (+滚动条余量), 并在
        # 首次布局后显式放置分割条, 避免每次启动手动调整。
        try:
            inner = panel._canvas.winfo_children()[0]
            inner.update_idletasks()
            groups = inner.winfo_children()
            needed = max(c.winfo_reqwidth() for c in groups) + 24
            panel._canvas.configure(width=needed)
            self.root.after_idle(
                lambda: main_paned.sashpos(0, needed + 44))
        except (AttributeError, IndexError, tk.TclError):
            pass

    # -------------------- 独立图表窗口 --------------------

    def _open_plot_window(self):
        """创建或聚焦独立图表窗口 (主窗口与图表窗口一一对应)。

        已存在时置顶聚焦并重算刷新, 不会新建第二个; 被用户关闭后可经
        「显示图表窗口」按钮或本方法重新打开。每次打开/聚焦都重新
        计算, 保证展示的不是旧结果。
        """
        if self.plot_window is not None:
            if self.plot_window.winfo_exists():
                self.plot_window.lift()
                self.plot_window.focus_force()
                if self.file_loaded:
                    self.auto_calculate()
                return
            self.plot_window = None

        win = ttk.Toplevel(self.root)
        win.title('计算结果')
        self.plot_window = win

        # 默认停靠在主窗口右侧
        self.root.update_idletasks()
        x = self.root.winfo_rootx() + self.root.winfo_width() + 10
        y = self.root.winfo_rooty()
        win.geometry(f'860x640+{max(x, 0)}+{max(y, 0)}')
        win.minsize(500, 460)

        self._build_plot_window_content(win)

        # 用户主动关闭图表窗口: 只关图表, 不影响主窗口
        win.protocol('WM_DELETE_WINDOW', self._on_plot_window_close)

        # 打开后重新计算刷新 (不展示旧结果)
        if self.file_loaded:
            self.auto_calculate()

    def _build_plot_window_content(self, win):
        """在独立窗口内构建工具行 (曲线开关 + Mark Freq) 与绘图区。"""
        toolbar = ttk.Frame(win, padding=(8, 8, 8, 0))
        toolbar.pack(fill='x')
        ttk.Label(toolbar, text='曲线:').pack(side='left', padx=(0, 4))
        ttk.Checkbutton(toolbar, text='PSFEXT', variable=self.chk_fext_var,
                        command=self._on_curve_toggle).pack(side='left', padx=3)
        ttk.Checkbutton(toolbar, text='PSNEXT', variable=self.chk_next_var,
                        command=self._on_curve_toggle).pack(side='left', padx=3)
        ttk.Checkbutton(toolbar, text='PSXT', variable=self.chk_psxt_var,
                        command=self._on_curve_toggle).pack(side='left', padx=3)
        ttk.Separator(toolbar, orient='vertical').pack(side='left', fill='y', padx=10)
        ttk.Label(toolbar, text='Mark Freq (上限 GHz):').pack(side='left')
        ttk.Entry(toolbar, textvariable=self.mark_freq_var, width=10).pack(
            side='left', padx=(4, 0))

        self.plot_frame = ttk.Frame(win)
        self.plot_frame.pack(fill='both', expand=True, padx=8, pady=8)

        try:
            import matplotlib
            matplotlib.use('TkAgg')
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

            self.fig = Figure(figsize=(7, 3.5), dpi=100)
            self.ax = self.fig.add_subplot(111)
            self.canvas_mpl = FigureCanvasTkAgg(self.fig, master=self.plot_frame)
            self.canvas_mpl.get_tk_widget().pack(fill='both', expand=True)
            self.ax.set_xlabel('Frequency (GHz)', fontweight='bold')
            self.ax.set_ylabel('Magnitude (dB)', fontweight='bold')
            self.ax.grid(True, alpha=0.4)
            self.fig.tight_layout()
            self.canvas_mpl.draw()

            if self.plot_data:
                self.draw_curves()
        except Exception as e:
            self.fig = None
            self.ax = None
            self.canvas_mpl = None
            ttk.Label(self.plot_frame, text=f'图表初始化失败: {e}',
                      bootstyle='danger').pack(expand=True)

        # ---- 计算数据区: 曲线图下方, 1 行表格 ----
        # 列 = 勾选曲线的 Mark 峰值 (PSFEXT/PSNEXT/PSXT, dB)
        #       + ICN 开关打开时的 6 个 ICN/ccICN 结果 (mV);
        # 复制按钮在表格上方; 单元格用只读 Entry (tk): 文本可自由框选复制
        res_frame = ttk.LabelFrame(win, text='计算数据', padding=(8, 4))
        res_frame.pack(fill='x', padx=8, pady=(0, 8))
        ttk.Button(res_frame, text='复制表格', width=10,
                   bootstyle='secondary-outline',
                   command=self._copy_data_table).pack(anchor='e', pady=(0, 4))
        # 表格外框容器: highlightthickness 提供明显外框线,
        # 各单元格 relief='solid' 提供内框线
        self._table_outer = tk.Frame(res_frame, bg='white',
                                     highlightbackground='#5a5a5a',
                                     highlightcolor='#5a5a5a',
                                     highlightthickness=1, bd=0)
        self._table_outer.pack(fill='x')
        self._refresh_data_table()

    # -------------------- 「计算数据」表格 --------------------

    def _data_table_columns(self):
        """当前应显示的数据列: [(列名, 单位)]。

        左侧 3 列为勾选曲线的 Mark 峰值 (dB), 未勾选的曲线不显示;
        右侧 6 列为 ICN/ccICN 结果 (mV), 主界面 ICN 开关关闭时不显示。
        """
        cols = []
        for name, chk in (('PSFEXT', self.chk_fext_var),
                          ('PSNEXT', self.chk_next_var),
                          ('PSXT', self.chk_psxt_var)):
            if chk.get():
                cols.append((name, 'dB'))
        if self.icn_enabled_var.get():
            cols.extend((name, 'mV') for name in
                        ('ICN_n', 'ICN_f', 'ICN', 'ccICN_n', 'ccICN_f', 'ccICN'))
        return cols

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
            self.data_table_vars = {}
            self._data_table_cols = tuple(cols)
            if not cols:
                ttk.Label(outer,
                          text='(无可显示的数据列: 请勾选曲线或打开 ICN 开关)',
                          font=('TkDefaultFont', 8)).pack(padx=4, pady=2)
            else:
                for col, (name, unit) in enumerate(cols):
                    tk.Label(outer, text=f'{name} ({unit})',
                             font=('TkDefaultFont', 9, 'bold'),
                             bg='#e9ecef', relief='solid', bd=1).grid(
                        row=0, column=col, sticky='nsew')
                    var = ttk.StringVar(value='--')
                    self.data_table_vars[name] = var
                    tk.Entry(outer, textvariable=var, state='readonly',
                             font=('Consolas', 9), relief='solid', bd=1,
                             readonlybackground='white', justify='center'
                             ).grid(row=1, column=col, sticky='nsew')
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
            else:
                var.set(f'{res[name.lower()] * 1e3:.6g}' if res else '--')

    def _copy_data_table(self):
        """复制整表 (表头含单位 + 1 行数值, 制表符分隔), 可直接粘贴进 Excel。"""
        cols = self._data_table_columns()
        if not cols or not self.data_table_vars or self.plot_window is None:
            return
        header = '\t'.join(f'{name} ({unit})' for name, unit in cols)
        values = '\t'.join(self.data_table_vars[name].get() for name, _ in cols)
        self.plot_window.clipboard_clear()
        self.plot_window.clipboard_append(f'{header}\n{values}')
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

    def _on_plot_window_close(self):
        """销毁图表窗口并复位绘图引用 (可由用户关闭或主窗口关闭触发)。"""
        if self.plot_window is not None:
            try:
                self.plot_window.destroy()
            except tk.TclError:
                pass
            self.plot_window = None
        self.fig = None
        self.ax = None
        self.canvas_mpl = None
        self.data_table_vars = {}
        self._data_table_cols = None
        self._table_outer = None

    def _on_main_window_close(self):
        """主窗口关闭时连带关闭图表窗口。"""
        self._on_plot_window_close()
        self.root.destroy()

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
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.port_vars = []
        self.port_items = {}

        max_name_width = self.default_font.measure('端口名 (解析自S参数注释)')
        max_port_width = self.default_font.measure('Port')

        for i in range(num_ports):
            port_str = f'Port {i + 1}'
            name_str = port_names[i] if i < len(port_names) else '-'

            max_name_width = max(max_name_width, self.default_font.measure(name_str))
            max_port_width = max(max_port_width, self.default_font.measure(port_str))

            self.port_vars.append((ttk.BooleanVar(value=False),
                                   ttk.BooleanVar(value=False),
                                   ttk.BooleanVar(value=False)))
            tags = ('odd',) if i % 2 else ()
            item_id = self.tree.insert('', 'end', tags=tags,
                                       values=(port_str, name_str, '☐', '☐', '☐'))
            self.port_items[i + 1] = item_id

        self.tree.column('Port', width=max_port_width + 40, anchor='center',
                         stretch=False)
        self.tree.column('Name', width=max_name_width + 40,
                         anchor=self._name_anchor(), stretch=True)

        self.sync_table_ui()

    _NAME_ANCHORS = {'左': 'w', '居中': 'center', '右': 'e'}

    def _name_anchor(self):
        return self._NAME_ANCHORS.get(self.name_align_var.get(), 'w')

    def _apply_name_align(self):
        """应用端口名对齐方式到表格列。"""
        self.tree.column('Name', anchor=self._name_anchor())

    def sync_table_ui(self):
        """同步 BooleanVar 状态到 Treeview 的显示文本与行高亮"""
        for p, item_id in self.port_items.items():
            v_val, f_val, n_val = (var.get() for var in self.port_vars[p - 1])
            vals = list(self.tree.item(item_id, 'values'))
            vals[2] = '☑' if v_val else '☐'
            vals[3] = '☑' if f_val else '☐'
            vals[4] = '☑' if n_val else '☐'
            if v_val:
                tags = ('victim',)
            elif (p - 1) % 2:
                tags = ('odd',)
            else:
                tags = ()
            self.tree.item(item_id, values=vals, tags=tags)

    def on_table_click(self, event):
        """处理Treeview中的勾选点击"""
        region = self.tree.identify('region', event.x, event.y)
        if region != 'cell':
            return

        col = self.tree.identify_column(event.x)
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            return

        port_num = None
        for p, item_id in self.port_items.items():
            if item_id == row_id:
                port_num = p
                break
        if port_num is None:
            return

        col_idx = int(col.replace('#', ''))
        if col_idx < 3:
            return

        var = self.port_vars[port_num - 1][col_idx - 3]
        var.set(not var.get())
        self.sync_table_ui()

        if col_idx == 3:
            self.on_victim_changed(port_num)
        else:
            self.auto_calculate()

    def on_victim_changed(self, clicked_port):
        """当Victim复选框状态改变时，感知具体的端口号"""
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
                                   '（对子按左侧「排列方式」划分）。')
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
                    # 界面频率输入单位为 Hz, 科学计数法显示;
                    # fstep 默认取文件原始频点间隔, 可手动修改
                    self.fmin_var.set(f'{freqs[0]:.6g}')
                    self.fmax_var.set(f'{freqs[-1]:.6g}')
                    self.fstep_var.set(
                        f'{(freqs[-1] - freqs[0]) / (len(freqs) - 1):.6g}')

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

        # FEXT_dB/NEXT_dB/PSFEXT/PSNEXT/PSXT 在 power_sum_db 中求值,
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
        for name in ('PSFEXT', 'PSNEXT', 'PSXT'):
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

        主界面 ICN 开关关闭时跳过计算; ICN 参数/公式无效时不阻塞
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
        if not self.plot_data or self.ax is None:
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
        if self.chk_psxt_var.get() and (data['has_fext'] or data['has_next']):
            self.ax.plot(data['fn_ghz'], data['PSXT'], color='#77AC30',
                         linewidth=2, label='Total PSXT')

        self._annotate_mark_peak(data)

        self.ax.set_xlabel('Frequency (GHz)', fontweight='bold')
        self.ax.set_ylabel('Magnitude (dB)', fontweight='bold')
        if self.ax.lines:
            self.ax.legend(loc='best', fontsize=9)
        self.ax.tick_params(labelsize=9)
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
            ('PSXT', 'PSXT', None, self.chk_psxt_var, '#77AC30'),
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
                         markersize=6, zorder=5)
            self.ax.annotate(
                f'{y:.2f} dB', (x, y), xytext=offset,
                textcoords='offset points', color=color, fontweight='bold',
                fontsize=8, arrowprops=dict(arrowstyle='->', color=color, lw=0.8),
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
