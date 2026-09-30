"""项目版本备份工具。

每次更新代码前运行一次，把当前版本的源文件备份到
backup/<时间戳>_<版本标签>/ 独立版本文件夹中。

用法:
    python backup.py            # backup/20260904_153000/
    python backup.py v3.4       # backup/20260904_153000_v3.4/
"""

import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
BACKUP_ROOT = PROJECT_ROOT / 'backup'

# 只备份源码与项目文件，不备份虚拟环境、打包产物和 IDE 配置
INCLUDE_FILES = ('*.py', '*.spec', 'README.md', 'requirements.txt')
EXCLUDE_DIRS = {'.venv', 'venv', '.idea', '__pycache__', 'dist', 'build',
                'backup', '.git', '.zcode', 'plans'}


def iter_project_files():
    for pattern in INCLUDE_FILES:
        for path in PROJECT_ROOT.rglob(pattern):
            if EXCLUDE_DIRS & set(path.relative_to(PROJECT_ROOT).parts[:-1]):
                continue
            yield path


def create_backup(label=''):
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    dest = BACKUP_ROOT / (f'{timestamp}_{label}' if label else timestamp)
    dest.mkdir(parents=True, exist_ok=False)

    copied = []
    for src in sorted(iter_project_files()):
        target = dest / src.relative_to(PROJECT_ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        copied.append(src.relative_to(PROJECT_ROOT).as_posix())

    print(f'备份完成: {dest}')
    for name in copied:
        print(f'  {name}')
    return dest


if __name__ == '__main__':
    create_backup(sys.argv[1] if len(sys.argv) > 1 else '')
