"""覆盖 KaTeX 字体在标准部署与 AstrBot 桌面版（Tauri）路径下的探测与物化。"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from astrbot_plugin_maid_agent.katex_fonts import (
    EXPECTED_KATEX_FONTS,
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
    assert (
        parse_webui_dir_arg(["--foo", "--webui-dir", r"\\?\D:\AstrBot\webui"])
        == r"\\?\D:\AstrBot\webui"
    )
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

    ignored_env_dir = _seed_katex_dist(tmp_path / "ignored")
    # 1) CLI --webui-dir wins over env var and default dist.
    candidates = discover_dashboard_dist_candidates(
        argv=["--webui-dir", str(desktop_webui)],
        environ={"ASTRBOT_WEBUI_DIR": str(ignored_env_dir)},
        executable=tmp_path / "other" / "python.exe",
        resolver=fake_resolver,
    )
    assert candidates[0] == desktop_webui
    assert ignored_env_dir not in candidates
    assert standard_dist in candidates
    assert resolved_args[0] == str(desktop_webui)

    # 2) ASTRBOT_WEBUI_DIR is used when --webui-dir is absent.
    env_candidates = discover_dashboard_dist_candidates(
        argv=[],
        environ={"ASTRBOT_WEBUI_DIR": str(desktop_webui)},
        executable=tmp_path / "other" / "python.exe",
        resolver=lambda _explicit=None: None,
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
        resolver=lambda _explicit=None: None,
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


def test_materialize_skips_same_size_and_handles_missing_dir_without_warning(
    tmp_path: Path, caplog, monkeypatch
):
    dist = _seed_katex_dist(tmp_path / "webui")
    target = tmp_path / "fonts"
    copy_calls: list[tuple[Path, Path]] = []
    real_copyfile = shutil.copyfile

    def spy_copyfile(src, dst):
        copy_calls.append((Path(src), Path(dst)))
        return real_copyfile(src, dst)

    monkeypatch.setattr("astrbot_plugin_maid_agent.katex_fonts.shutil.copyfile", spy_copyfile)

    assert materialize_katex_fonts(dist, target) == 2
    assert len(copy_calls) == 2

    # Idempotent second run skips copying same-size files.
    copy_calls.clear()
    assert materialize_katex_fonts(dist, target) == 2
    assert copy_calls == []

    with caplog.at_level(logging.WARNING, logger="maid_agent"):
        assert materialize_katex_fonts(tmp_path / "does-not-exist", target) == 0
    assert caplog.records == []


def test_materialize_falls_back_when_first_candidate_copy_fails(tmp_path: Path, monkeypatch):
    bad_dist = _seed_katex_dist(
        tmp_path / "bad_dist",
        {"KaTeX_Main-Regular-Ab12Cd34.woff2": b"unreadable-src"},
    )
    good_dist = _seed_katex_dist(
        tmp_path / "good_dist",
        {
            "KaTeX_Main-Regular-Ab12Cd34.woff2": b"good-main",
            "KaTeX_Math-Italic-Xy98Zw76.woff2": b"good-math",
        },
    )
    target = tmp_path / "fonts"

    real_copyfile = shutil.copyfile

    def flaky_copyfile(src, dst):
        if Path(src).is_relative_to(bad_dist):
            raise PermissionError("access denied")
        return real_copyfile(src, dst)

    monkeypatch.setattr("astrbot_plugin_maid_agent.katex_fonts.shutil.copyfile", flaky_copyfile)

    assert materialize_katex_fonts(bad_dist, target, warn_if_missing=False) == 0
    count, chosen = materialize_katex_fonts_from_candidates([bad_dist, good_dist], target)
    assert count == 2
    assert chosen == good_dist
    assert (target / "KaTeX_Main-Regular.woff2").read_bytes() == b"good-main"
    assert (target / "KaTeX_Math-Italic.woff2").read_bytes() == b"good-math"


def test_materialize_falls_back_from_partial_candidate_without_overwriting(
    tmp_path: Path, monkeypatch
):
    partial_dist = _seed_katex_dist(
        tmp_path / "partial_dist",
        {"KaTeX_Main-Regular-Ab12Cd34.woff2": b"partial-main-v1"},
    )
    fallback_dist = _seed_katex_dist(
        tmp_path / "fallback_dist",
        {
            "KaTeX_Main-Regular-Ab12Cd34.woff2": b"fallback-main-different-size",
            "KaTeX_Math-Italic-Xy98Zw76.woff2": b"fallback-math",
        },
    )
    target = tmp_path / "fonts"
    copied_sources: list[Path] = []
    real_copyfile = shutil.copyfile

    def spy_copyfile(src, dst):
        copied_sources.append(Path(src))
        return real_copyfile(src, dst)

    monkeypatch.setattr("astrbot_plugin_maid_agent.katex_fonts.shutil.copyfile", spy_copyfile)

    count, chosen = materialize_katex_fonts_from_candidates([partial_dist, fallback_dist], target)
    assert count == 2
    assert chosen == partial_dist
    assert (target / "KaTeX_Main-Regular.woff2").read_bytes() == b"partial-main-v1"
    assert (target / "KaTeX_Math-Italic.woff2").read_bytes() == b"fallback-math"
    assert copied_sources == [
        partial_dist / "assets" / "KaTeX_Main-Regular-Ab12Cd34.woff2",
        fallback_dist / "assets" / "KaTeX_Math-Italic-Xy98Zw76.woff2",
    ]


def test_materialize_falls_back_after_partial_copy_failure_without_overwriting(
    tmp_path: Path, monkeypatch
):
    partial_bad_dist = _seed_katex_dist(
        tmp_path / "partial_bad_dist",
        {
            "KaTeX_Main-Regular-Ab12Cd34.woff2": b"primary-main",
            "KaTeX_Math-Italic-Xy98Zw76.woff2": b"unreadable-math",
        },
    )
    fallback_dist = _seed_katex_dist(
        tmp_path / "fallback_dist",
        {
            "KaTeX_Main-Regular-Ab12Cd34.woff2": b"fallback-main-different-size",
            "KaTeX_Math-Italic-Xy98Zw76.woff2": b"fallback-math",
        },
    )
    target = tmp_path / "fonts"
    copied_sources: list[Path] = []
    real_copyfile = shutil.copyfile

    def flaky_copyfile(src, dst):
        src_path = Path(src)
        if src_path == partial_bad_dist / "assets" / "KaTeX_Math-Italic-Xy98Zw76.woff2":
            raise PermissionError("access denied")
        copied_sources.append(src_path)
        return real_copyfile(src, dst)

    monkeypatch.setattr("astrbot_plugin_maid_agent.katex_fonts.shutil.copyfile", flaky_copyfile)

    count, chosen = materialize_katex_fonts_from_candidates(
        [partial_bad_dist, fallback_dist], target
    )
    assert count == 2
    assert chosen == partial_bad_dist
    assert (target / "KaTeX_Main-Regular.woff2").read_bytes() == b"primary-main"
    assert (target / "KaTeX_Math-Italic.woff2").read_bytes() == b"fallback-math"
    assert copied_sources == [
        partial_bad_dist / "assets" / "KaTeX_Main-Regular-Ab12Cd34.woff2",
        fallback_dist / "assets" / "KaTeX_Math-Italic-Xy98Zw76.woff2",
    ]


def test_materialize_stops_early_when_all_expected_fonts_succeed(tmp_path: Path, monkeypatch):
    all_files = {f"{name}-Ab12Cd34.woff2": name.encode() for name in sorted(EXPECTED_KATEX_FONTS)}
    complete_dist = _seed_katex_dist(tmp_path / "complete_dist", all_files)
    later_dist = _seed_katex_dist(tmp_path / "later_dist", all_files)
    target = tmp_path / "fonts"

    scanned: list[Path] = []
    from astrbot_plugin_maid_agent import katex_fonts as kf

    real_collect = kf._collect_katex_font_sources

    def spy_collect(dist_dir: Path) -> dict[str, Path]:
        scanned.append(dist_dir)
        return real_collect(dist_dir)

    monkeypatch.setattr(kf, "_collect_katex_font_sources", spy_collect)

    count, chosen = materialize_katex_fonts_from_candidates([complete_dist, later_dist], target)
    assert count == len(EXPECTED_KATEX_FONTS)
    assert chosen == complete_dist
    assert scanned == [complete_dist]
