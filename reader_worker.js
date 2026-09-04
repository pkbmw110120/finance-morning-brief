/**
 * 移动端阅读端点（Cloudflare Worker）
 * 部署：wrangler deploy（esm.sh 依赖在构建时自动 bundle）
 * 路由：https://<subdomain>.workers.dev/reader?url=<encoded_article_url>
 *
 * 逻辑：
 *   1) 校验 url 合法 + 仅允许 ALLOWED_HOSTS（SSRF 防护）
 *   2) 服务端 fetch 原文（移动浏览器 UA，让 302 兜底也是移动版）
 *   3) Readability + linkedom 提取正文
 *   4) 净化后套响应式模板返回；提取失败 / 异常则 302 跳原站
 */

import { Readability } from "https://esm.sh/@mozilla/readability@0.5.0";
import { parseHTML } from "https://esm.sh/linkedom@0.15.0";

// SSRF 防护：仅抓取白名单域名。留空则放行所有 http(s)（不推荐公开部署）。
// 由 verify_mobile_links.py 实测填充，例如：
const ALLOWED_HOSTS = new Set([
  // "eastmoney.com", "10jqka.com.cn", "yicai.com",
]);

// 用手机浏览器 UA 抓取（关键决策，2026-09-04 实测后调整）：
//   - 提取成功 → Reader 渲染页天生移动友好
//   - 提取失败 → 302 跳回的是该 URL 的移动版，而非桌面版（用户实际体验场景）
// 残余缺口：JS 渲染站（chinanews/qcc/app.dahecube 等）Readability 提取不到正文，
//   仍会 302 → 这些站要么走直链（m.xxx.com 或主域移动版可接受）、要么换 Cloudflare Browser Rendering。
const UA_FETCH =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1";

function sanitize(html) {
  // 基础净化：去 script/style/iframe/object/embed，去 on* 事件属性与 javascript:
  return html
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/<style[\s\S]*?<\/style>/gi, "")
    .replace(/<iframe[\s\S]*?<\/iframe>/gi, "")
    .replace(/<object[\s\S]*?<\/object>/gi, "")
    .replace(/<embed[\s\S]*?<\/embed>/gi, "")
    .replace(/\son\w+\s*=\s*"[^"]*"/gi, "")
    .replace(/\son\w+\s*=\s*'[^']*'/gi, "")
    .replace(/javascript:/gi, "");
}

function esc(s) {
  return (s || "").replace(/[<>&]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]));
}

function render(article, sourceUrl) {
  const title = esc(article.title || "阅读");
  const content = sanitize(article.content || "");
  const host = new URL(sourceUrl).hostname;
  return `<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>${title}</title>
<style>
  :root{color-scheme:light dark}
  body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
       line-height:1.75;font-size:17px;color:#1a1a1a;background:#fff;
       max-width:720px;margin:0 auto;padding:env(safe-area-inset-top) 16px calc(24px + env(safe-area-inset-bottom))}
  h1{font-size:22px;line-height:1.4;margin:18px 0 8px}
  .meta{color:#888;font-size:13px;margin-bottom:16px}
  img{max-width:100%;height:auto;border-radius:8px;margin:12px 0}
  a{color:#1a6cff;word-break:break-all}
  .src{margin-top:28px;padding-top:14px;border-top:1px solid #eee;font-size:13px;color:#888}
  @media(prefers-color-scheme:dark){body{background:#111;color:#e6e6e6}a{color:#6cb0ff}.src{border-color:#333}}
</style></head><body>
<h1>${title}</h1>
<div class="meta">来源：${esc(host)}</div>
${content}
<div class="src">原文链接：<a href="${esc(sourceUrl)}">${esc(sourceUrl)}</a><br>（如排版异常可点击原文查看）</div>
</body></html>`;
}

export default {
  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname !== "/reader") return new Response("Not Found", { status: 404 });

    const target = url.searchParams.get("url");
    if (!target) return new Response("Missing ?url=", { status: 400 });

    let parsed;
    try {
      parsed = new URL(target);
    } catch {
      return new Response("Invalid url", { status: 400 });
    }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:")
      return new Response("Bad protocol", { status: 400 });

    const host = parsed.hostname.replace(/^www\./, "");
    if (ALLOWED_HOSTS.size > 0 && !ALLOWED_HOSTS.has(host))
      return Response.redirect(target, 302);

    const cache = caches.default;
    const cacheKey = new Request(url.toString(), request);
    const cached = await cache.match(cacheKey);
    if (cached) return cached;

    try {
      const r = await fetch(target, {
        headers: { "User-Agent": UA_FETCH, "Accept": "text/html,application/xhtml+xml" },
        redirect: "follow",
      });
      if (!r.ok) throw new Error("status " + r.status);
      const html = await r.text();
      const { document } = parseHTML(html);
      const article = new Readability(document).parse();
      if (!article || !article.content || article.content.length < 200)
        return Response.redirect(target, 302);

      const out = new Response(render(article, target), {
        headers: {
          "Content-Type": "text/html; charset=utf-8",
          "Cache-Control": "public, max-age=1800",
        },
      });
      await cache.put(cacheKey, out.clone());
      return out;
    } catch (e) {
      return Response.redirect(target, 302);
    }
  },
};
