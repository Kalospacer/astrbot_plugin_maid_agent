"""KaTeX 字体的运行时物化：复用 AstrBot dashboard dist 自带的字体文件。

前端构建不打包字体 —— katex css 里的 ./fonts/KaTeX_*.woff2 由这里在插件
启动时从 dashboard dist 剥掉 vite hash 复制到插件页静态目录。dashboard
依赖 katex，其 dist 必然带这套字体，同源同版本，插件 zip 不必再背一份。
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from logging import getLogger
from pathlib import Path

logger = getLogger("maid_agent")

_HASHED_RE = re.compile(r"^(KaTeX_[A-Za-z0-9]+(?:-[A-Za-z]+)*?)-[A-Za-z0-9_-]{8}\.woff2$")


def parse_webui_dir_arg(argv: Sequence[str] | None = None) -> str | None:
    """从启动参数中解析 `--webui-dir`（AstrBot 桌面版 Tauri 通过该参数传入 WebUI 目录）。"""
    args = list(sys.argv[1:] if argv is None else argv)
    found: str | None = None
    idx = 0
    while idx < len(args):
        token = str(args[idx])
        if token == "--webui-dir" and idx + 1 < len(args):
            candidate = str(args[idx + 1]).strip()
            if candidate:
                found = candidate
            idx += 2
            continue
        if token.startswith("--webui-dir="):
            candidate = token.split("=", 1)[1].strip()
            if candidate:
                found = candidate
        idx += 1
    return found


def _normalize_dedup_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def discover_dashboard_dist_candidates(
    *,
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    executable: str | Path | None = None,
    resolver: Callable[[str | Path | None], Path | None] | None = None,
    app_static_folder: str | Path | None = None,
) -> list[Path]:
    """按优先级探测可能存放 AstrBot WebUI 静态资源的目录列表（去重保序）。

    兼容：
    1. 显式 `--webui-dir` / `ASTRBOT_WEBUI_DIR`（Tauri 桌面版及自定义部署）；
    2. 已初始化的 Dashboard 应用静态目录（`dashboard.server.APP.static_folder`）；
    3. AstrBot 标准 `resolve_dashboard_dist()`（`data/dist` 与内置 `dashboard/dist`）；
    4. Tauri 桌面版目录结构兜底（`<install>/backend/python/python.exe` -> `<install>/webui`）。
    """
    env = os.environ if environ is None else environ
    candidates: list[Path] = []
    seen: set[str] = set()

    def _add(raw: str | Path | None) -> None:
        if not raw:
            return
        raw_str = str(raw).strip()
        if not raw_str:
            return
        path = Path(raw_str)
        key = _normalize_dedup_key(path)
        if key not in seen:
            seen.add(key)
            candidates.append(path)

    explicit = parse_webui_dir_arg(argv) or (env.get("ASTRBOT_WEBUI_DIR") or "").strip() or None
    if explicit:
        if resolver is not None:
            try:
                _add(resolver(explicit))
            except Exception as exc:  # noqa: BLE001
                logger.debug("[maid] resolve_dashboard_dist(explicit) 失败: %s", exc)
        _add(explicit)

    _add(app_static_folder)

    if resolver is not None:
        try:
            _add(resolver(None))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[maid] resolve_dashboard_dist() 失败: %s", exc)

    exe_raw = sys.executable if executable is None else executable
    if exe_raw:
        try:
            exe_path = Path(exe_raw).resolve()
            # Tauri 桌面版常见布局：
            # - <root>/backend/python/python.exe -> <root>/webui (parents[2])
            # - <root>/python/python.exe -> <root>/webui (parents[1])
            for parent_idx in (2, 1):
                if len(exe_path.parents) > parent_idx:
                    desktop_webui = exe_path.parents[parent_idx] / "webui"
                    if desktop_webui.is_dir():
                        _add(desktop_webui)
        except OSError:
            pass

    return candidates


def _collect_katex_font_sources(dist_dir: Path) -> dict[str, Path]:
    sources: dict[str, Path] = {}
    root = Path(dist_dir)
    if not root.is_dir():
        return sources
    try:
        for path in root.rglob("KaTeX_*.woff2"):
            match = _HASHED_RE.match(path.name)
            key = match.group(1) if match else path.stem
            sources.setdefault(key, path)
    except OSError as exc:
        logger.debug("[maid] 扫描 KaTeX 字体目录失败: %s err=%s", root, exc)
    return sources


def materialize_katex_fonts(dist_dir: Path, target_dir: Path, *, warn_if_missing: bool = True) -> int:
    """把 dist_dir 里的 KaTeX_*.woff2 剥 hash 复制到 target_dir。

    返回物化的字体种数；找不到返回 0（前端数学公式将退回系统字体渲染）。
    已存在且体积一致的文件跳过，重复启动零拷贝。
    """
    sources = _collect_katex_font_sources(Path(dist_dir))
    if not sources:
        if warn_if_missing:
            logger.info("[maid] dashboard dist 中未找到 KaTeX 字体: %s", dist_dir)
        return 0
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("[maid] 创建 KaTeX 字体目录失败: %s err=%s", target_dir, exc)
        return 0
    materialized = 0
    for name, src in sources.items():
        dst = target_dir / f"{name}.woff2"
        try:
            if not dst.exists() or dst.stat().st_size != src.stat().st_size:
                shutil.copyfile(src, dst)
            materialized += 1
        except OSError as exc:
            logger.warning("[maid] 复制 KaTeX 字体失败: %s err=%s", src, exc)
    return materialized


def materialize_katex_fonts_from_candidates(
    candidates: Sequence[Path], target_dir: Path
) -> tuple[int, Path | None]:
    """按优先级依次探测候选目录，在首个包含 KaTeX 字体的目录完成物化。"""
    for candidate in candidates:
        count = materialize_katex_fonts(candidate, target_dir, warn_if_missing=False)
        if count:
            return count, Path(candidate)
    return 0, None
