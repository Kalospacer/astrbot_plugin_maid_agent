"""把 MiSans 切成 unicode-range 分块 woff2，并生成对应的 @font-face CSS。

为什么要切：MiSans 单个字重的完整 woff2 是 4.6 MB。宿主
（AstrBot plugin_page_service）对插件页资源下发的是 `Cache-Control: no-store`，
每次打开控制台都会重新下载——整包塞进去等于把这次性能优化连本带利还回去。

切分后浏览器只下载「屏幕上真的出现了的字所在的那几块」，首屏通常只落在
少数几块上，其余按需。分块边界沿用 Google Fonts 给 Noto Sans SC 的 101 段
划分（按中文字频优化过），再补上 MiSans 独有、该划分没覆盖的码位——控制台是
agent 页面，输出文本任意，覆盖不全会在句子中间掉回系统字体。

用法:
    python scripts/build-cjk-font.py <MiSans 解压根目录>

依赖 fonttools + brotli（`pip install fonttools brotli`），不作为项目依赖声明。
产物（pages/console/assets/fonts/MiSans-*.woff2）与生成的 CSS 都提交进仓库，
正常开发不需要重跑本脚本。
"""

import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 产物直接落到插件页的发布目录，不经 Vite。字体只在仓库里存这一份：
# 早先 src/ 与 pages/ 各一份字节相同的副本，让安装 zip 白白多出 12 MB
# （AstrBot 装插件走 GitHub 分支 zip，仓库里每个字节都进安装包）。
# 对应的 @font-face 由 vite.config.ts 的 appendCjkFontFace 在构建时追加进
# console.css，url 相对 assets/console.css 写成 ./fonts/…，宿主按此改写。
OUT_DIR = ROOT.parent / "pages" / "console" / "assets" / "fonts"
CSS_OUT = ROOT / "src" / "ui" / "theme" / "fonts-cjk.css"

# 用静态字重，不用 MiSansVF——这一点实测过，结论与直觉相反。
#
# 直觉上可变字体更划算：一套 @font-face 声明覆盖全字重，CSS（每次打开都要
# 重下的固定成本）能省一半。实测确实省了 98 KB CSS，但每个分块都要带 gvar
# 字形变形数据，CJK 几千字形的 delta 让单块胀到 2.7 倍：同样命中 7 块，
# 字体传输从 161 KB 涨到 428 KB，净亏 181 KB/次。
#
#   两个静态字重  css 287 KB + 字体 161 KB = 2052 KB/次
#   可变字体      css 202 KB + 字体 428 KB = 2233 KB/次
#
# 字重取舍：界面 28 处用 500、17 处用 400，两者都要。600 以上只出现在拉丁
# 品牌字样上，交给 500 匹配。想再省约 94 KB CSS + 80 KB 字体，可以砍掉
# Medium 只留 Regular——代价是中文的 500 会按 400 渲染，而同一行里的拉丁
# （Anthropic Sans）仍是真 500，中英文粗细会不一致。
WEIGHTS = [("Regular", 400), ("Medium", 500)]

NOTO_CSS = (
    "https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400&display=swap"
)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def fetch_ranges() -> list[str]:
    """取 Google 对 Noto Sans SC 的 101 段 unicode-range 划分。"""
    req = urllib.request.Request(NOTO_CSS, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        css = resp.read().decode("utf-8")
    ranges = re.findall(r"unicode-range:\s*([^;]+);", css)
    if not ranges:
        raise SystemExit("未能解析 Noto Sans SC 的 unicode-range，检查网络或 UA")
    return [r.strip() for r in ranges]


def to_unicodes_arg(unicode_range: str) -> str:
    """`U+4e00-9fff, U+ff01` → pyftsubset 认的 `4e00-9fff,ff01`。"""
    return ",".join(part.strip()[2:] for part in unicode_range.split(","))


def parse_codepoints(unicode_range: str) -> set[int]:
    """把一段 unicode-range 展开成码位集合。"""
    out: set[int] = set()
    for part in unicode_range.split(","):
        token = part.strip()[2:]
        if "-" in token:
            lo, hi = token.split("-")
            out.update(range(int(lo, 16), int(hi, 16) + 1))
        else:
            out.add(int(token, 16))
    return out


def to_unicode_range(codepoints: list[int]) -> str:
    """把码位列表压成连续区间形式的 unicode-range（罕用字在码位上多半连续）。"""
    spans: list[str] = []
    start = prev = codepoints[0]
    for cp in codepoints[1:]:
        if cp == prev + 1:
            prev = cp
            continue
        spans.append(f"U+{start:x}" if start == prev else f"U+{start:x}-{prev:x}")
        start = prev = cp
    spans.append(f"U+{start:x}" if start == prev else f"U+{start:x}-{prev:x}")
    return ", ".join(spans)


def font_codepoints(src: Path) -> set[int]:
    """字体 cmap 里的全部码位。"""
    from fontTools.ttLib import TTFont

    font = TTFont(str(src))
    out: set[int] = set()
    for table in font["cmap"].tables:
        out |= set(table.cmap.keys())
    return out


def coarse_gap_ranges(
    have: set[int], covered: set[int], per_chunk: int = 900, max_gap: int = 0x400
) -> list[str]:
    """补缺块：用连续码位区间声明，而不是把差集的离散码位一个个列出来。

    差集里的 CJK 统一汉字是穿插在已覆盖字之间的，逐码位列出来会让 CSS 膨胀
    近 100 KB——而 CSS 是每次打开都要重下的固定成本，比按需下载的字体块贵。

    改成按码位空间切连续段（`U+4e00-53ff` 这种，CSS 里只占几十字节）。段内
    会含一部分已被字频块覆盖的字，但这些块只在「某个码位只匹配到粗段」时才
    下载——粗段声明在字频块之前，同一码位两者都匹配时后声明的字频块胜出。

    `max_gap` 是必要的：差集元素之间可能隔着一大片字体根本没有的码位，光按
    数量分组会把首尾拉成横跨十几万码位的区间（实测出现过 U+9e6f-2ce93，
    143396 个码位），等于把整个补充平面连 emoji 一起圈进来——那一段 MiSans
    一个字形都没有，但只要聊天里出现 emoji 就会白下载这一块。遇到大空洞断开。
    """
    missing = sorted(have - covered)
    if not missing:
        return []

    out: list[str] = []
    group: list[int] = []

    def flush() -> None:
        if group:
            out.append(f"U+{group[0]:x}-{group[-1]:x}")
            group.clear()

    for cp in missing:
        if group and (len(group) >= per_chunk or cp - group[-1] > max_gap):
            flush()
        group.append(cp)
    flush()
    return out


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    src_dir = Path(sys.argv[1])

    # 先取分块定义再清旧产物：反过来的话，一次网络抖动就会把已有产物删光
    # 却生成不出新的（实测被 SSL EOF 坑过一次）。
    ranges = fetch_ranges()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 只清 MiSans：同目录还有 Vite 产出的 Anthropic Sans / Outfit 和运行时物化的 KaTeX
    for stale in OUT_DIR.glob("MiSans-*.woff2"):
        stale.unlink()

    covered: set[int] = set()
    for unicode_range in ranges:
        covered |= parse_codepoints(unicode_range)
    # 粗段在前、字频段在后：同一码位两者都匹配时，后声明的字频段胜出，
    # 常用字仍走小块；只匹配到粗段的罕用字才拉粗块。
    #
    # 差集按全部字重 cmap 的并集算：只看 Regular 的话，若某字重多出几个码位，
    # 它们不会进任何 range，下面按字重跳过无交集段时就静默漏掉了。
    # （当前两个字重的 cmap 完全相同，这里是防御。）
    cmaps: dict[str, set[int]] = {}
    for name, _ in WEIGHTS:
        src = src_dir / "ttf" / f"MiSans-{name}.ttf"
        if not src.exists():
            raise SystemExit(f"找不到字重文件: {src}")
        cmaps[name] = font_codepoints(src)
    extra = coarse_gap_ranges(set().union(*cmaps.values()), covered)
    ranges = extra + ranges
    print(f"分块数: {len(ranges)}（补缺粗段 {len(extra)} + 字频划分 {len(ranges) - len(extra)}）")

    blocks: list[str] = []
    total = 0
    for name, weight in WEIGHTS:
        src = src_dir / "ttf" / f"MiSans-{name}.ttf"
        have = cmaps[name]
        made = 0
        for index, unicode_range in enumerate(ranges):
            # 该段与字体 cmap 无交集才跳过。早先按产物字节数（<=1KB）判空壳，
            # 结果把只含一两个字形的小块也删了，而它们的 range 同时不会写进
            # CSS——实测漏掉 13 个码位。这类沉默缺口正是要避免的。
            if not (parse_codepoints(unicode_range) & have):
                continue
            dst = OUT_DIR / f"MiSans-{name}.{index}.woff2"
            subprocess.run(
                [
                    sys.executable, "-m", "fontTools.subset", str(src),
                    f"--unicodes={to_unicodes_arg(unicode_range)}",
                    "--flavor=woff2",
                    "--layout-features=*",
                    "--no-hinting",
                    "--desubroutinize",
                    f"--output-file={dst}",
                ],
                check=True,
                capture_output=True,
            )
            made += 1
            total += dst.stat().st_size
            blocks.append(
                "@font-face {\n"
                '  font-family: "MiSans";\n'
                f'  src: url("./fonts/{dst.name}") format("woff2");\n'
                f"  font-weight: {weight};\n"
                "  font-style: normal;\n"
                "  font-display: swap;\n"
                f"  unicode-range: {unicode_range};\n"
                "}"
            )
        print(f"MiSans-{name} ({weight}): {made} 块")

    header = (
        "/* 本文件由 scripts/build-cjk-font.py 生成，不要手改。\n"
        " *\n"
        " * MiSans 单字重完整 woff2 是 4.6 MB，而宿主对插件页资源下发的是\n"
        " * Cache-Control: no-store——每次打开都会重下。这里按 unicode-range\n"
        " * 切块，浏览器只取屏幕上真的出现过的字所在的块。\n"
        " * 分块边界沿用 Google Fonts 对 Noto Sans SC 的字频优化划分。\n"
        " *\n"
        " * 许可：MiSans 由小米发布，允许免费商用。\n"
        " */\n\n"
    )
    CSS_OUT.write_text(header + "\n".join(blocks) + "\n", encoding="utf-8", newline="\n")
    print(f"总计 {total / 1048576:.2f} MB -> {CSS_OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
