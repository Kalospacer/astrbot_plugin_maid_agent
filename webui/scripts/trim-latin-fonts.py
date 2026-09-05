"""裁掉拉丁可变字体里界面用不到的轴范围。

这是无损压缩：只删掉「我们永远不会取到的轴区间」的变形数据，取值范围内的
渲染逐像素不变。宿主对插件页资源下发 `Cache-Control: no-store`，字体每次
打开都要重下，所以这些字节是每次都省。

  Anthropic Sans  opsz 16–48 → 16–24   界面最大字号是 markdown h1 的 24px
                  wght 300–800 → 300–700   实际用到的最大字重是 600
                  （.markdown strong 是 600；唯一的 700 在品牌字样上，那是 Outfit）
  Outfit          wght 100–900 → 500–700   只用于侧栏字样的 500 / 700 两档

实测省 68 KB（Roman −27.6、Italic −30.4、Outfit −9.7）。

注意别把 opsz 直接钉死成单值：那会让 24px 的标题用 16px 的光学设计，是有损的。
这里保留 16–24 整段，浏览器的 font-optical-sizing: auto 仍能正常插值。

用法:
    python scripts/trim-latin-fonts.py [--check]

`--check` 只报告不写盘，用于确认产物已是裁剪后的版本。
依赖 fonttools + brotli，不作为项目依赖声明；裁剪后的字体直接提交进仓库。
"""

import sys
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

FONT_DIR = Path(__file__).resolve().parent.parent / "src" / "ui" / "theme" / "fonts"

# 文件名 → 保留的轴范围
LIMITS: dict[str, dict[str, tuple[int, int]]] = {
    "AnthropicSans-Roman.woff2": {"opsz": (16, 24), "wght": (300, 700)},
    "AnthropicSans-Italic.woff2": {"opsz": (16, 24), "wght": (300, 700)},
    "Outfit-latin.woff2": {"wght": (500, 700)},
}


def axes_of(font: TTFont) -> dict[str, tuple[float, float]]:
    if "fvar" not in font:
        return {}
    return {a.axisTag: (a.minValue, a.maxValue) for a in font["fvar"].axes}


def main() -> None:
    check_only = "--check" in sys.argv[1:]
    total_before = total_after = 0
    stale: list[str] = []

    for name, limits in LIMITS.items():
        path = FONT_DIR / name
        if not path.exists():
            raise SystemExit(f"找不到字体: {path}")
        before = path.stat().st_size
        font = TTFont(str(path))
        current = axes_of(font)

        wanted = {tag: (float(lo), float(hi)) for tag, (lo, hi) in limits.items()}
        if all(current.get(tag) == span for tag, span in wanted.items()):
            print(f"{name:28} 已是裁剪后版本 {before / 1024:6.1f} KB  轴 {current}")
            total_before += before
            total_after += before
            continue

        stale.append(name)
        trimmed = instancer.instantiateVariableFont(font, limits, updateFontNames=False)
        trimmed.flavor = "woff2"
        if check_only:
            print(f"{name:28} 待裁剪（当前轴 {current}）")
            continue
        trimmed.save(str(path))
        after = path.stat().st_size
        print(
            f"{name:28} {before / 1024:6.1f} -> {after / 1024:6.1f} KB"
            f"  (省 {(before - after) / 1024:5.1f})  轴 {axes_of(TTFont(str(path)))}"
        )
        total_before += before
        total_after += after

    if check_only:
        if stale:
            print("FAIL 以下字体未裁剪:", ", ".join(stale))
            raise SystemExit(1)
        print("OK 全部已裁剪")
        return
    print(f"合计 {total_before / 1024:.1f} -> {total_after / 1024:.1f} KB")


if __name__ == "__main__":
    main()
