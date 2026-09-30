"""S参数 PowerSum 串扰计算工具 —— 程序入口。

用法:
    python main.py                  # 打开空界面
    python main.py 路径/文件.s4p    # 打开界面并自动导入指定 S 参数文件

依赖安装: pip install numpy scipy matplotlib ttkbootstrap
运行环境: Python 3.10+ (项目 venv 为 3.14)
"""

import sys

import ttkbootstrap as ttk

from app import SParamPowerSumApp


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)

    root = ttk.Window(title='S参数 PowerSum 串扰计算', themename='cosmo')
    app = SParamPowerSumApp(root)

    if argv:
        root.after(200, lambda: app.import_s_params(argv[0]))

    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
