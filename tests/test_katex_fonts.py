"""KaTeX 字体物化：从 dashboard dist 剥 hash 复制到插件页静态目录。"""

from __future__ import annotations

from pathlib import Path

from astrbot_plugin_maid_agent.katex_fonts import materialize_katex_fonts


class TestMaterializeKatexFonts:
    def _seed_dist(self, dist: Path) -> None:
        assets = dist / "assets"
        assets.mkdir(parents=True)
        (assets / "KaTeX_Main-Regular-B22Nviop.woff2").write_bytes(b"main-regular")
        (assets / "KaTeX_Math-Italic-t53AETM-.woff2").write_bytes(b"math-italic")
        (assets / "KaTeX_AMS-Regular-BQhdFMY1.woff2").write_bytes(b"ams")
        (assets / "KaTeX_Main-Regular-Dr94JaBh.woff").write_bytes(b"legacy")
        (assets / "index-Qx7Zp2AA.js").write_bytes(b"js")

    def test_strips_hash_and_copies(self, tmp_path: Path) -> None:
        dist = tmp_path / "dist"
        self._seed_dist(dist)
        target = tmp_path / "pages" / "console" / "assets" / "fonts"

        count = materialize_katex_fonts(dist, target)

        assert count == 3
        assert (target / "KaTeX_Main-Regular.woff2").read_bytes() == b"main-regular"
        assert (target / "KaTeX_Math-Italic.woff2").read_bytes() == b"math-italic"
        assert (target / "KaTeX_AMS-Regular.woff2").read_bytes() == b"ams"
        assert not (target / "KaTeX_Main-Regular.woff").exists()

    def test_rerun_is_idempotent(self, tmp_path: Path) -> None:
        dist = tmp_path / "dist"
        self._seed_dist(dist)
        target = tmp_path / "fonts"
        materialize_katex_fonts(dist, target)
        marker = target / "KaTeX_Main-Regular.woff2"
        marker.write_bytes(b"sentinel")
        materialize_katex_fonts(dist, target)
        assert marker.read_bytes() == b"main-regular"

    def test_missing_fonts_returns_zero(self, tmp_path: Path) -> None:
        dist = tmp_path / "dist"
        dist.mkdir()
        assert materialize_katex_fonts(dist, tmp_path / "fonts") == 0
        assert not (tmp_path / "fonts").exists()

    def test_unhashed_names_pass_through(self, tmp_path: Path) -> None:
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "KaTeX_Main-Regular.woff2").write_bytes(b"plain")
        target = tmp_path / "fonts"
        assert materialize_katex_fonts(dist, target) == 1
        assert (target / "KaTeX_Main-Regular.woff2").read_bytes() == b"plain"
