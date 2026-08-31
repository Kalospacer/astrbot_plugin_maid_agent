"""KaTeX 字体的运行时物化：复用 AstrBot dashboard dist 自带的字体文件。

前端构建不打包字体 —— katex css 里的 ./fonts/KaTeX_*.woff2 由这里在插件
启动时从 dashboard dist 剥掉 vite hash 复制到插件页静态目录。dashboard
依赖 katex，其 dist 必然带这套字体，同源同版本，插件 zip 不必再背一份。
"""

from __future__ import annotations

import re
import shutil
from logging import getLogger
from pathlib import Path

logger = getLogger("maid_agent")

_HASHED_RE = re.compile(r"^(KaTeX_[A-Za-z0-9]+(?:-[A-Za-z]+)*?)-[A-Za-z0-9_-]{8}\.woff2$")


def materialize_katex_fonts(dist_dir: Path, target_dir: Path) -> int:
    """把 dist_dir 里的 KaTeX_*.woff2 剥 hash 复制到 target_dir。

    返回物化的字体种数；找不到返回 0（前端数学公式将退回系统字体渲染）。
    已存在且体积一致的文件跳过，重复启动零拷贝。
    """
    sources: dict[str, Path] = {}
    for path in Path(dist_dir).rglob("KaTeX_*.woff2"):
        match = _HASHED_RE.match(path.name)
        key = match.group(1) if match else path.stem
        sources.setdefault(key, path)
    if not sources:
        logger.warning("[maid] dashboard dist 中未找到 KaTeX 字体: %s", dist_dir)
        return 0
    target_dir.mkdir(parents=True, exist_ok=True)
    for name, src in sources.items():
        dst = target_dir / f"{name}.woff2"
        try:
            if not dst.exists() or dst.stat().st_size != src.stat().st_size:
                shutil.copyfile(src, dst)
        except OSError as exc:
            logger.warning("[maid] 复制 KaTeX 字体失败: %s err=%s", src, exc)
    return len(sources)
