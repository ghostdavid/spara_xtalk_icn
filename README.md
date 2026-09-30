# S参数 PowerSum 串扰计算工具

解析 Touchstone (.sNp) S 参数文件并计算功率和 (PowerSum) 串扰指标 **PSFEXT / PSNEXT / PSXT**，提供 ttkbootstrap cosmo 主题的图形界面，支持**差分 (Differential)** 与**单端 (Single-Ended)** 两种模式。由 V3.3 单文件版本重构而来。

## 环境要求

- Python 3.10+（项目 venv 为 3.14）
- 依赖版本见 `requirements.txt`（numpy / scipy / matplotlib / ttkbootstrap）

## 安装

```bash
pip install -r requirements.txt
```

如需隔离环境，可先创建并激活 venv 后再安装：

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

## 使用方法

### 1. 源码运行（打开空界面）

```bash
python main.py
```

### 2. 命令行自动导入

```bash
python main.py 路径/文件.s4p
```

启动后自动导入指定的 S 参数文件。

### 3. 独立 exe

在 PyCharm 中运行 `pyinstaller main.spec` 打包，生成 `dist/S参数PowerSum串扰计算.exe`，双击即可使用，可将 .sNp 文件拖入窗口或在界面中选择文件。

## 项目结构

```text
project2/
├── main.py                     # 程序入口（约 30 行）：构建主窗口，处理命令行自动导入
├── app.py                      # GUI 主程序（ttkbootstrap cosmo 主题，差分/单端模式）
├── sparam_core.py              # 纯计算核心（不依赖 tkinter）：SParameter 解析器
│                               #   + snp2smp / s2sdd / safe_db / interp_spline / power_sum_db
├── backup.py                   # 版本备份工具
├── main.spec                   # PyInstaller 打包配置
├── requirements.txt            # 依赖清单
├── README.md                   # 本文件
├── backup/                     # 备份输出目录 backup/<时间戳>_<版本>/
├── sample_data/
│   └── demo.s8p                # 示例 S 参数文件
└── tests/
    ├── test_sparam.py          # 回归测试（python tests/test_sparam.py，纯 assert 无需 pytest）
    ├── _smoke_gui.py           # GUI 端到端冒烟测试（python tests/_smoke_gui.py）
    ├── synth_touchstone.py     # 确定性合成 Touchstone 用例生成器
    └── snapshot_v3.3.json      # 旧版（v3.3）参考快照
```

## 备份流程

每次修改代码前运行一次：

```bash
python backup.py            # 生成 backup/20260904_153000/
python backup.py v3.4       # 生成 backup/20260904_153000_v3.4/
```

自动将 `*.py`、`*.spec`、`README.md`、`requirements.txt` 备份到 `backup/<时间戳>_<标签>/`，排除 `.venv`、`dist`、`.idea`、`__pycache__` 等目录。

## 打包

```bash
pyinstaller main.spec
```

产出目录版 `dist/S参数PowerSum串扰计算_dir/`（启动约 0.5–2 秒）。把整个文件夹复制到目标位置，对其中 exe 右键「发送到桌面快捷方式」即可。

说明：曾验证过单文件版（onefile），但每次启动需向 `%TEMP%\_MEI*` 解包并被 Windows Defender 持续扫描，实测 8–9 秒且偶发 120 秒级尖峰，故 spec 已改为只产出目录版。若确需单文件分发，加 Defender 排除项是唯一有效提速手段。另外，PyInstaller 无 ttkbootstrap 钩子，spec 中已通过 `collect_data_files('ttkbootstrap')` 显式收集主题图标字体，否则打包后启动即报 `bootstrap.ttf` 缺失。

## 测试

```bash
python tests/test_sparam.py
```

回归测试对比两组基线：旧版快照（`tests/snapshot_v3.3.json`）与解析解（由 `tests/synth_touchstone.py` 的确定性公式推得），输出全部 **PASS** 即通过。纯 assert 实现，无需 pytest。
