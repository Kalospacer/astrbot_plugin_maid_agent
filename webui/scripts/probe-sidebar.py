"""侧栏字样视觉核对：真实浏览器里渲染构建产物，截侧栏顶部。

排版尺度这类问题 jsdom 冒烟测不出来（没有布局与字体），需要真实浏览器。
这是人工比对用的一次性探针，不进冒烟流程，也不在 npm scripts 里——
依赖 playwright（`pip install playwright && playwright install chromium`），
不作为项目依赖声明。

顺带核对：字样自然宽度是否放得下最窄侧栏、三个字体是否真的加载成功。

用法: python scripts/probe-sidebar.py [输出png]
"""

import json
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PORT = 7139
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "scripts" / "_sidebar.png"

SESSIONS = [
    {"id": "a" * 32, "title": "分析本地 nai 生图插件", "ago": 10 * 86400_000},
    {"id": "b" * 32, "title": "Number 11", "ago": 18 * 86400_000},
    {"id": "c" * 32, "title": "控制台前端性能排查", "ago": 3600_000},
]

MOCK = (
    """
(() => {
  window.__errs = [];
  window.addEventListener('error', (e) => {
    window.__errs.push((e.error && e.error.stack) || String(e.message));
  });
  window.addEventListener('unhandledrejection', (e) => {
    window.__errs.push('REJECT: ' + ((e.reason && e.reason.stack) || String(e.reason)));
  });
  const now = Date.now();
  const SESSIONS = __SESSIONS__;
  const items = SESSIONS.map((s) => ({
    sessionId: s.id, updatedAt: now - s.ago, running: false, blank: false,
    agentPreset: 'butler', umo: 'dashboard:FriendMessage:dashboard',
    projections: { asOfSeq: 1, values: { title: s.title } },
  }));
  const respond = (method) => {
    if (method === 'session.list') return { items };
    if (method === 'agentPreset.list') return { presets: [{ id: 'butler', trust: 'system', name: 'butler' }], authorable: false };
    if (method === 'settings.describe') return { namespaces: [{ ns: 'maid', schema: { type: 'object', properties: {} }, value: {}, applies: 'live', secrets: [], revision: 1 }] };
    if (method === 'session.history') return { events: [], hasMore: false };
    if (method === 'session.models') return { current: { provider: 'mock', model: 'mock-1', override: false }, providers: [] };
    return {};
  };
  window.AstrBotPluginPage = {
    async ready() { return { pluginName: 'probe' }; },
    async apiGet() { return null; },
    async apiPost(endpoint, body) {
      const env = body || {};
      return { type: 'server-response', rpcId: env.rpcId, result: { ok: true, value: respond(env.method) } };
    },
    async upload() { return {}; },
    async download() { return {}; },
    async subscribeSSE() { return 'sub_1'; },
    async unsubscribeSSE() {},
  };
})();
"""
).replace("__SESSIONS__", json.dumps(SESSIONS, ensure_ascii=False))

BRAND_PROBE = """() => {
  const b = [...document.querySelectorAll('button')]
    .find((x) => (x.textContent || '').includes('Maid'));
  if (!b) return { error: 'brand button not found',
                   body: document.body.innerHTML.slice(0, 300) };
  const cs = getComputedStyle(b);
  const kid = b.firstElementChild ? getComputedStyle(b.firstElementChild) : null;
  // 字样自然宽度：两个 span 的实际占位 + gap（按钮是 flex:1，量它没意义）
  const spans = [...b.children];
  const gap = parseFloat(cs.columnGap || '0') || 0;
  const natural = spans.reduce((sum, el) => sum + el.getBoundingClientRect().width, 0)
    + gap * Math.max(0, spans.length - 1);
  // 最窄侧栏 264px 扣掉根 padding 24 / 左轨 4 / gap 8 / 折叠按钮 28
  const budgetAtMinSidebar = 264 - 24 - 4 - 8 - 28;
  return {
    text: b.textContent,
    fontSize: cs.fontSize,
    family: cs.fontFamily.slice(0, 46),
    weightStrong: kid && kid.fontWeight,
    naturalWidth: Math.round(natural),
    budgetAtMinSidebar,
    fitsAtMinSidebar: Math.round(natural) <= budgetAtMinSidebar,
    fontsLoaded: {
      outfit: document.fonts.check('700 22px Outfit'),
      anthropicSans: document.fonts.check('400 14px "Anthropic Sans"'),
      anthropicItalic: document.fonts.check('italic 13px "Anthropic Sans"'),
    },
  };
}"""

server = subprocess.Popen(
    ["node", "node_modules/vite/bin/vite.js", "preview", "--port", str(PORT), "--strictPort"],
    cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    time.sleep(3)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800}, device_scale_factor=2)
        page.on("console", lambda m: print("[console]", m.type, m.text[:300]))
        page.on("response", lambda r: print(f"[HTTP {r.status}]", r.url[-70:])
                if r.status >= 400 else None)

        # 统计首屏实际拉取的资源：CJK 分块字体的真实成本只能测，不能估——
        # 汉字会散布在多个字频段，命中的块数远不是「常用字都在头几块」那么简单。
        # 两个口径都记：解压后字节 = 宿主（FastAPI 直出，无压缩）每次打开的真实
        # 下载量；编码后字节 = vite preview 已 gzip 的线上传输量，也就是前面
        # 加一层反代开压缩后能到的水平。只报其中一个都会误导。
        #
        # 事件回调里只收集、不测量：在回调里做 body()/sizes() 这种阻塞往返会让
        # 后续事件的派发被推后，实测 italic 字体的响应就这样漏到汇总之后才到。
        responses: list = []

        def classify(url: str) -> str | None:
            if ".woff2" in url:
                return "font:misans" if "MiSans" in url else "font:latin"
            if url.endswith(".js"):
                return "js"
            if url.endswith(".css"):
                return "css"
            return None

        page.on("response", lambda r: responses.append(r) if r.status < 400 else None)
        page.add_init_script(MOCK)
        page.goto(f"http://127.0.0.1:{PORT}/", wait_until="networkidle")
        rendered = True
        try:
            page.wait_for_selector("[data-composer-seat]", timeout=8000)
        except Exception:
            rendered = False
            print("!! 应用未渲染")

        for i, err in enumerate(page.evaluate("() => window.__errs || []")):
            print(f"--- err[{i}] ---")
            print(err[:1500])

        page.wait_for_timeout(1000)  # 等字体 swap 落地
        # Italic 只在渲染斜体文字时才下载；显式 load 一次，验证文件可取且有效
        italic = page.evaluate(
            "async () => { try { const f = await document.fonts.load('italic 13px \"Anthropic Sans\"');"
            " return { loadedFaces: f.length, ok: document.fonts.check('italic 13px \"Anthropic Sans\"') }; }"
            " catch (e) { return { error: String(e) }; } }"
        )
        print("italic 按需加载:", json.dumps(italic, ensure_ascii=False))
        brand = page.evaluate(BRAND_PROBE)
        print(json.dumps(brand, ensure_ascii=False, indent=2))

        page.wait_for_timeout(300)  # 让最后几条响应事件派发完
        decoded: dict[str, int] = {}
        encoded: dict[str, int] = {}
        misans_hits = 0
        for resp in responses:
            key = classify(resp.url)
            if key is None:
                continue
            # body() 会等响应结束，之后 sizes() 才可靠
            decoded[key] = decoded.get(key, 0) + len(resp.body())
            encoded[key] = encoded.get(key, 0) + resp.request.sizes()["responseBodySize"]
            if key == "font:misans":
                misans_hits += 1
        print(f"--- 首屏实际传输（宿主 no-store，每次打开；MiSans 命中 {misans_hits} 块）---")
        print(f"  {'':<14} {'解压后/宿主直连':>16} {'编码后/preview gzip':>20}")
        for key in sorted(decoded):
            print(f"  {key:<14} {decoded[key] / 1024:13.1f} KB {encoded[key] / 1024:17.1f} KB")
        print(
            f"  {'合计':<14} {sum(decoded.values()) / 1024:13.1f} KB "
            f"{sum(encoded.values()) / 1024:17.1f} KB"
        )
        # 生产构建里 CSS Modules 类名被压成纯哈希，按类名选不到；侧栏恒定贴左。
        page.screenshot(path=str(OUT), clip={"x": 0, "y": 0, "width": 760, "height": 420})
        print("screenshot ->", OUT)
        browser.close()

    # 探针得能当门禁用：截图存下来但状态码要如实反映失败，
    # 否则「白屏」「字体没加载」「字样放不下」都会被 exit 0 掩盖过去。
    failures = []
    if not rendered:
        failures.append("应用未渲染")
    if brand.get("error"):
        failures.append(f"字样未找到: {brand['error']}")
    else:
        if not brand.get("fitsAtMinSidebar"):
            failures.append(
                f"字样在最窄侧栏放不下: {brand.get('naturalWidth')} > "
                f"{brand.get('budgetAtMinSidebar')}"
            )
        for name, ok in (brand.get("fontsLoaded") or {}).items():
            # italic 首屏不渲染斜体文字时本就不会下载，由上面的按需 load 单独核对
            if name != "anthropicItalic" and not ok:
                failures.append(f"字体未加载: {name}")
    if italic.get("error") or not italic.get("ok"):
        failures.append(f"italic 按需加载失败: {json.dumps(italic, ensure_ascii=False)}")

    if failures:
        for f in failures:
            print("FAIL", f)
        sys.exit(1)
    print("PROBE OK")
finally:
    server.terminate()
