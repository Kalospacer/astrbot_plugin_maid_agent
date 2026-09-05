"""Merge tiny MiSans chunks while preserving every unicode-range declaration."""

from pathlib import Path
import re

from fontTools.merge import Merger

ROOT = Path(__file__).resolve().parents[2]
FONT_DIR = ROOT / "pages" / "console" / "assets" / "fonts"
CSS = ROOT / "webui" / "src" / "ui" / "theme" / "fonts-cjk.css"
LIMIT = 10_240


def main() -> None:
    css = CSS.read_text(encoding="utf-8")
    for weight in ("Regular", "Medium"):
        paths = sorted(
            (p for p in FONT_DIR.glob(f"MiSans-{weight}.*.woff2") if p.stat().st_size < LIMIT),
            key=lambda p: p.name,
        )
        if len(paths) < 2:
            continue
        merged_name = f"MiSans-{weight}.small.woff2"
        merged = Merger().merge([str(p) for p in paths])
        merged.flavor = "woff2"
        merged.save(str(FONT_DIR / merged_name))
        for path in paths:
            css = css.replace(path.name, merged_name)
            path.unlink()
        print(f"{weight}: {len(paths)} chunks -> {merged_name}")
    CSS.write_text(css, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
