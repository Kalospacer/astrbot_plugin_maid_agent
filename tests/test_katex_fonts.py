"""覆盖 KaTeX 字体在标准部署与 AstrBot 桌面版（Tauri）路径下的探测与物化。"""

from __future__ import annotations

import logging
from pathlib import Path

from astrbot_plugin_maid_agent.katex_fonts import (
    discover_dashboard_dist_candidates,
    materialize_katex_fonts,
    materialize_katex_fonts_from_candidates,
    parse_webui_dir_arg,
)


def _seed_katex_dist(root: Path, files: dict[str, bytes] | None = None) -> Path:
    assets = root / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    payloads = files or {
        "KaTeX_Main-Regular-Ab12Cd34.woff2": b"main-regular",
        "KaTeX_Math-Italic-Xy98Zw76.woff2": b"math-italic",
    }
    for name, data in payloads.items():
        (assets / name).write_bytes(data)
    return root


def test_parse_webui_dir_arg_supports_split_and_equals_forms():
    assert parse_webui_dir_arg(["--foo", "--webui-dir", r"\\?\D:\AstrBot\webui"]) == r"\\?\D:\AstrBot\webui"
    assert parse_webui_dir_arg(["--webui-dir=D:/AstrBot/webui"]) == "D:/AstrBot/webui"
    assert parse_webui_dir_arg(["--webui-dir", "/first", "--webui-dir=/second"]) == "/second"
    assert parse_webui_dir_arg(["--other", "val"]) is None


def test_discover_candidates_prefers_desktop_cli_webui_dir_and_env(tmp_path: Path):
    desktop_webui = _seed_katex_dist(tmp_path / "AstrBot" / "webui")
    standard_dist = _seed_katex_dist(tmp_path / "data" / "dist")
    resolved_args: list[str | Path | None] = []

    def fake_resolver(explicit: str | Path | None = None) -> Path | None:
        resolved_args.append(explicit)
        if explicit is not None:
            return Path(explicit)
        return standard_dist

    # 1) CLI --webui-dir wins over env var and default dist.
    candidates = discover_dashboard_dist_candidates(
        argv=["--webui-dir", str(desktop_webui)],
        environ={"ASTRBOT_WEBUI_DIR": str(tmp_path / "ignored")},
        executable=tmp_path / "other" / "python.exe",
        resolver=fake_resolver,
    )
    assert candidates[0] == desktop_webui
    assert standard_dist in candidates
    assert resolved_args[0] == str(desktop_webui)

    # 2) ASTRBOT_WEBUI_DIR is used when --webui-dir is absent.
    env_candidates = discover_dashboard_dist_candidates(
        argv=[],
        environ={"ASTRBOT_WEBUI_DIR": str(desktop_webui)},
        executable=tmp_path / "other" / "python.exe",
        resolver=lambda explicit=None: None,
    )
    assert env_candidates == [desktop_webui]


def test_discover_candidates_finds_tauri_desktop_sibling_webui(tmp_path: Path):
    install_root = tmp_path / "AstrBot"
    desktop_webui = _seed_katex_dist(install_root / "webui")
    fake_python = install_root / "backend" / "python" / "python.exe"
    fake_python.parent.mkdir(parents=True, exist_ok=True)
    fake_python.write_bytes(b"")

    # Simulate desktop-managed mode where resolve_dashboard_dist(None) returns None.
    candidates = discover_dashboard_dist_candidates(
        argv=[],
        environ={},
        executable=fake_python,
        resolver=lambda explicit=None: None,
    )
    assert candidates == [desktop_webui]

    target = tmp_path / "plugin" / "pages" / "console" / "assets" / "fonts"
    count, chosen = materialize_katex_fonts_from_candidates(candidates, target)
    assert count == 2
    assert chosen == desktop_webui
    assert (target / "KaTeX_Main-Regular.woff2").read_bytes() == b"main-regular"
    assert (target / "KaTeX_Math-Italic.woff2").read_bytes() == b"math-italic"


def test_materialize_falls_back_from_empty_data_dist_to_desktop_webui(tmp_path: Path, caplog):
    empty_data_dist = tmp_path / "data" / "dist"
    empty_data_dist.mkdir(parents=True)
    desktop_webui = _seed_katex_dist(tmp_path / "AstrBot" / "webui")
    target = tmp_path / "fonts"

    with caplog.at_level(logging.WARNING, logger="maid_agent"):
        count, chosen = materialize_katex_fonts_from_candidates(
            [empty_data_dist, desktop_webui],
            target,
        )

    assert count == 2
    assert chosen == desktop_webui
    assert caplog.records == []


def test_materialize_skips_same_size_and_handles_missing_dir_without_warning(tmp_path: Path, caplog):
    dist = _seed_katex_dist(tmp_path / "webui")
    target = tmp_path / "fonts"

    assert materialize_katex_fonts(dist, target) == 2
    # Idempotent second run.
    assert materialize_katex_fonts(dist, target) == 2

    with caplog.at_level(logging.WARNING, logger="maid_agent"):
        assert materialize_katex_fonts(tmp_path / "does-not-exist", target) == 0
    assert caplog.records == []
