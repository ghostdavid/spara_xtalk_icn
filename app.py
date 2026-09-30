"""S参数 PowerSum 串扰计算工具 —— GUI 主程序 (ttkbootstrap)。

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
from pathlib import Path

import numpy as np
import ttkbootstrap as ttk

from sparam_core import SParameter, power_sum_db


class SParamPowerSumApp:
    DIFF_MODE = '差分模式 (Differential)'
    SE_MODE = '单端模式 (Single-Ended)'

    # 配置字段映射: (JSON 键, 变量属性名)
    _SCALAR_FIELDS = (
        ('mode_val', 'mode_var'), ('Z0', 'z0_var'), ('fmin', 'fmin_var'),
        ('fmax', 'fmax_var'), ('fstep', 'fstep_var'),
        ('mark_freq', 'mark_freq_var'), ('chk_fext', 'chk_fext_var'),
        ('chk_next', 'chk_next_var'), ('chk_psxt', 'chk_psxt_var'),
        ('auto_select', 'auto_select_var'), ('arrange', 'arrange_var'),
    )
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
        self.root.title('S参数 PowerSum 串扰计算')

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

        self.fig = None
        self.ax = None
        self.canvas_mpl = None
        self.plot_window = None

        # 曲线开关与 Mark Freq 变量在主窗口构建前创建 (配置恢复依赖它们);
        # mark_freq 的防抖重绘 trace 只注册一次, 避免图表窗口重开时重复注册
        self.chk_fext_var = ttk.BooleanVar(value=True)
        self.chk_next_var = ttk.BooleanVar(value=True)
        self.chk_psxt_var = ttk.BooleanVar(value=True)
        self.mark_freq_var = ttk.StringVar(value='')
        self.mark_freq_var.trace_add('write', lambda *_: self._schedule_redraw())

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

        # ===== 左侧面板 =====
        left_frame = ttk.LabelFrame(main_paned, text='设置与操作', padding=10)
        main_paned.add(left_frame, weight=0)
        left_frame.columnconfigure(0, weight=1)

        file_group = ttk.LabelFrame(left_frame, text='S参数文件', padding=8)
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

        param_group = ttk.LabelFrame(left_frame, text='计算参数', padding=8)
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
                  text='Freq (GHz) [ min / max / step ]:').grid(row=3, column=0,
                                                                sticky='w')
        freq_frame = ttk.Frame(param_group)
        freq_frame.grid(row=4, column=0, sticky='ew', pady=(2, 0))
        freq_frame.columnconfigure((0, 1, 2), weight=1, uniform='freq')
        self.fmin_var = ttk.DoubleVar(value=0.01)
        self.fmax_var = ttk.DoubleVar(value=80.0)
        self.fstep_var = ttk.DoubleVar(value=0.01)
        for col, var in enumerate((self.fmin_var, self.fmax_var, self.fstep_var)):
            ttk.Entry(freq_frame, textvariable=var, width=8).grid(
                row=0, column=col, sticky='ew', padx=(0 if col == 0 else 2, 0))
            var.trace_add('write', lambda *_: self._schedule_recalculate())

        auto_group = ttk.LabelFrame(left_frame,
                                    text='端口自动勾选 (仅标准对称S参数可用)',
                                    padding=8)
        auto_group.grid(row=2, column=0, sticky='ew', pady=(0, 6))
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

        left_frame.rowconfigure(3, weight=1)
        ttk.Button(left_frame, text='保存当前文件配置', command=self.save_config,
                   bootstyle='success-outline').grid(row=4, column=0, sticky='ew',
                                                     pady=(6, 0))
        ttk.Button(left_frame, text='显示图表窗口', command=self._open_plot_window,
                   bootstyle='info-outline').grid(row=5, column=0, sticky='ew',
                                                  pady=(6, 0))

        # ===== 右侧面板 (端口勾选映射表; 图表已拆分为独立窗口) =====
        right_frame = ttk.Frame(main_paned)
        main_paned.add(right_frame, weight=1)

        table_group = ttk.LabelFrame(right_frame, text='端口勾选映射表', padding=8)
        table_group.pack(fill='both', expand=True)

        self.table_msg_var = ttk.StringVar(
            value='▶ 差分模式：Victim 必须勾选 2 个；攻击端口必须成对(偶数)勾选。')
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

    # -------------------- 独立图表窗口 --------------------

    def _open_plot_window(self):
        """创建或聚焦独立图表窗口 (主窗口与图表窗口一一对应)。

        已存在时仅置顶聚焦, 不会新建第二个; 被用户关闭后可经
        「显示图表窗口」按钮或本方法重新打开。
        """
        if self.plot_window is not None:
            if self.plot_window.winfo_exists():
                self.plot_window.lift()
                self.plot_window.focus_force()
                return
            self.plot_window = None

        win = ttk.Toplevel(self.root)
        win.title('S参数曲线图')
        self.plot_window = win

        # 默认停靠在主窗口右侧
        self.root.update_idletasks()
        x = self.root.winfo_rootx() + self.root.winfo_width() + 10
        y = self.root.winfo_rooty()
        win.geometry(f'860x520+{max(x, 0)}+{max(y, 0)}')
        win.minsize(500, 320)

        self._build_plot_window_content(win)

        # 用户主动关闭图表窗口: 只关图表, 不影响主窗口
        win.protocol('WM_DELETE_WINDOW', self._on_plot_window_close)

    def _build_plot_window_content(self, win):
        """在独立窗口内构建工具行 (曲线开关 + Mark Freq) 与绘图区。"""
        toolbar = ttk.Frame(win, padding=(8, 8, 8, 0))
        toolbar.pack(fill='x')
        ttk.Label(toolbar, text='曲线:').pack(side='left', padx=(0, 4))
        ttk.Checkbutton(toolbar, text='PSFEXT', variable=self.chk_fext_var,
                        command=self.draw_curves).pack(side='left', padx=3)
        ttk.Checkbutton(toolbar, text='PSNEXT', variable=self.chk_next_var,
                        command=self.draw_curves).pack(side='left', padx=3)
        ttk.Checkbutton(toolbar, text='PSXT', variable=self.chk_psxt_var,
                        command=self.draw_curves).pack(side='left', padx=3)
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
        self.tree.column('Name', width=max_name_width + 40, anchor='w', stretch=True)

        self.sync_table_ui()

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

        def get_pair(port):
            if arrange == 'Type1':
                return (port, port + 1) if port % 2 == 1 else (port - 1, port)
            if is_side_a(port):
                idx = (port - 1) // 4
                return (4 * idx + 1, 4 * idx + 3)
            idx = (port - 2) // 4
            return (4 * idx + 2, 4 * idx + 4)

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
            self.table_msg_var.set('▶ 差分模式：Victim 必须勾选 2 个；攻击端口必须成对(偶数)勾选。')
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

            if len(freqs) > 1:
                with self._paused():
                    self.fmin_var.set(round(freqs[0] / 1e9, 6))
                    self.fmax_var.set(round(freqs[-1] / 1e9, 6))
                    self.fstep_var.set(
                        round((freqs[-1] - freqs[0]) / (len(freqs) - 1) / 1e9, 6))

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
        else:
            if len(v_ports) != 1:
                self._set_status('状态: 单端模式下，Victim 必须勾选且仅能勾选 1 个！', 'warn')
                return

        try:
            z0 = self.z0_var.get()
            fmin = self.fmin_var.get()
            fmax = self.fmax_var.get()
            fstep = self.fstep_var.get()
        except tk.TclError:
            self._set_status('状态: 输入参数无效！', 'error')
            return

        if fstep <= 0:
            self._set_status('状态: 频率步进必须大于 0！', 'error')
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
        num_points = int(round((fmax - fmin) / fstep)) + 1
        if num_points > 500_000:
            self._set_status(
                f'状态: 频率点数过多 ({num_points} > 500000)！'
                '请增大步进或缩小频率范围。', 'error')
            return

        self._set_status('状态: 正在计算并绘制曲线...', 'info')
        self.root.update_idletasks()

        try:
            self.plot_data = power_sum_db(
                self.spara_obj, z0, v_ports, f_ports, n_ports, is_diff,
                fmin, fmax, fstep)
        except (ValueError, np.linalg.LinAlgError, ZeroDivisionError) as e:
            self._set_status(f'状态: 计算失败！{e}', 'error')
            return

        self._set_status('状态: 计算完成！参数修改可实时重算重绘。', 'success')
        self.draw_curves()

    # -------------------- 绘制曲线 --------------------

    def draw_curves(self):
        if self._redraw_job is not None:
            self.root.after_cancel(self._redraw_job)
            self._redraw_job = None
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
            ('MDFEXT', 'has_fext', self.chk_fext_var, '#D95319'),
            ('MDNEXT', 'has_next', self.chk_next_var, '#0072BD'),
            ('PSXT', None, self.chk_psxt_var, '#77AC30'),
        )
        points = []
        for key, flag, chk, color in series:
            if not chk.get():
                continue
            if flag is not None and not data[flag]:
                continue
            seg = data[key][:m_idx]
            idx = int(np.argmax(seg))
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
