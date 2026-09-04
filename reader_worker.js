/**
 * News Reader Worker — 新闻阅读代理
 * 
 * 功能：
 * - 抓取原站文章内容（桌面 UA）
 * - Readability 提取正文
 * - 响应式模板渲染
 * - 提取失败则 302 跳转原站
 * 
 * 部署：wrangler deploy
 * 路由：/reader?url=<encoded_url>
 */

// ===== 安全：允许抓取的域名白名单 =====
// 只允许抓取这些域名的内容，防止 SSRF 攻击
const ALLOWED_HOSTS = new Set([
  // 交易所
  'sse.com.cn', 'szse.cn', 'bse.cn',
  // 政府/监管
  'miit.gov.cn', 'pbc.gov.cn', 'csrc.gov.cn', 'gov.cn',
  'cbirc.gov.cn', 'mof.gov.cn', 'ndrc.gov.cn', 'samr.gov.cn', 'mofcom.gov.cn',
  // 评级机构
  'lhratings.com', 'ccxi.com.cn', 'dagongcredit.com', 'chinaratings.com.cn',
  // 招标/采购
  'bidcenter.com.cn', 'chinabidding.com', 'dlzb.com',
  // 新闻聚合
  'myzaker.com', 'ifeng.com', 'www.ifeng.com',
  // 科技
  'blog.csdn.net', 'csdn.net',
  // 财经门户（备用，通常直链已够用）
  'scol.com.cn', 'dahecube.com',
  // 通用放行：所有 .gov.cn 子域
]);

// 检查域名是否允许
function isAllowedHost(hostname) {
  // 去除 www. 前缀
  const cleanHost = hostname.replace(/^www\d*\./, '');
  
  // 直接匹配
  if (ALLOWED_HOSTS.has(cleanHost)) return true;
  
  // .gov.cn 兜底
  if (cleanHost.endsWith('.gov.cn')) return true;
  
  // 子域名匹配：如 cbgc.scol.com.cn 匹配 scol.com.cn
  for (const allowed of ALLOWED_HOSTS) {
    if (cleanHost.endsWith('.' + allowed)) return true;
  }
  
  return false;
}

// ===== 简洁 Readability 实现 =====
// 从 HTML 中提取正文内容（简化版，不依赖外部库）
function extractContent(html, url) {
  // 移除 script/style
  let cleaned = html
    .replace(/<script[\s\S]*?<\/script>/gi, '')
    .replace(/<style[\s\S]*?<\/style>/gi, '')
    .replace(/<nav[\s\S]*?<\/nav>/gi, '')
    .replace(/<header[\s\S]*?<\/header>/gi, '')
    .replace(/<footer[\s\S]*?<\/footer>/gi, '')
    .replace(/<aside[\s\S]*?<\/aside>/gi, '')
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/<div[^>]*class="[^"]*(?:ad|banner|sidebar|nav|menu|footer|comment)[^"]*"[^>]*>[\s\S]*?<\/div>/gi, '');

  // 提取标题
  let title = '';
  const titleMatch = cleaned.match(/<h1[^>]*>([\s\S]*?)<\/h1>/i)
    || cleaned.match(/<title[^>]*>([\s\S]*?)<\/title>/i)
    || cleaned.match(/<meta[^>]*property="og:title"[^>]*content="([^"]+)"/i);
  if (titleMatch) {
    title = titleMatch[1].replace(/<[^>]+>/g, '').trim();
  }

  // 提取正文：尝试常见正文容器
  const contentSelectors = [
    /<article[^>]*class="[^"]*(?:content|body|article|text|main)[^"]*"[^>]*>([\s\S]*?)<\/article>/i,
    /<div[^>]*class="[^"]*(?:article[_-]?content|post[_-]?content|entry[_-]?content|rich_media_content|content[_-]?body)[^"]*"[^>]*>([\s\S]*?)<\/div>/i,
    /<div[^>]*id="[^"]*(?:article|content|main|post)[^"]*"[^>]*>([\s\S]*?)<\/div>/i,
    /<main[^>]*>([\s\S]*?)<\/main>/i,
  ];

  let content = '';
  for (const sel of contentSelectors) {
    const match = cleaned.match(sel);
    if (match && match[1].length > 200) {
      content = match[1];
      break;
    }
  }

  // 如果没找到，提取所有 <p> 标签内容
  if (!content) {
    const paragraphs = [];
    const pRegex = /<p[^>]*>([\s\S]*?)<\/p>/gi;
    let m;
    while ((m = pRegex.exec(cleaned)) !== null) {
      const text = m[1].replace(/<[^>]+>/g, '').trim();
      if (text.length > 20) {
        paragraphs.push(`<p>${m[1]}</p>`);
      }
    }
    content = paragraphs.join('\n');
  }

  // 清理 content 中的相对链接为绝对链接
  if (content && url) {
    const baseUrl = new URL(url);
    content = content.replace(/(href|src)="(\/[^"]+)"/g, (match, attr, path) => {
      return `${attr}="${baseUrl.origin}${path}"`;
    });
  }

  // 提取来源/时间
  let source = '';
  const sourceMatch = cleaned.match(/<span[^>]*class="[^"]*(?:source|author|from)[^"]*"[^>]*>([\s\S]*?)<\/span>/i)
    || cleaned.match(/来源[：:]\s*([^<]+)/i);
  if (sourceMatch) {
    source = sourceMatch[1].replace(/<[^>]+>/g, '').trim();
  }

  return { title, content, source };
}

// ===== HTML 响应式模板 =====
function renderPage(title, content, source, originalUrl) {
  const safeTitle = escapeHtml(title || '新闻阅读');
  const safeSource = source ? `<div class="source">${escapeHtml(source)}</div>` : '';
  const safeUrl = escapeHtml(originalUrl);

  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>${safeTitle}</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Microsoft YaHei', sans-serif;
  line-height: 1.8;
  color: #333;
  background: #f8f9fa;
  padding: 16px;
  max-width: 720px;
  margin: 0 auto;
}
.header {
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
  padding: 16px 20px;
  border-radius: 12px;
  margin-bottom: 16px;
}
.header h1 { font-size: 20px; line-height: 1.4; }
.source {
  font-size: 13px;
  color: rgba(255,255,255,0.8);
  margin-top: 8px;
}
.article {
  background: white;
  border-radius: 12px;
  padding: 20px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.06);
}
.article p {
  margin-bottom: 16px;
  font-size: 16px;
  text-align: justify;
}
.article img {
  max-width: 100%;
  height: auto;
  border-radius: 8px;
  margin: 12px 0;
}
.article a { color: #667eea; }
.footer {
  text-align: center;
  padding: 16px;
  font-size: 12px;
  color: #999;
}
.footer a { color: #667eea; }
.fallback {
  background: #fff3cd;
  border: 1px solid #ffc107;
  border-radius: 8px;
  padding: 16px;
  margin: 16px 0;
  text-align: center;
}
.fallback a {
  display: inline-block;
  background: #667eea;
  color: white;
  padding: 10px 24px;
  border-radius: 8px;
  text-decoration: none;
  margin-top: 8px;
}
@media (max-width: 480px) {
  body { padding: 8px; }
  .header h1 { font-size: 18px; }
  .article { padding: 16px; }
  .article p { font-size: 15px; }
}
</style>
</head>
<body>
  <div class="header">
    <h1>${safeTitle}</h1>
    ${safeSource}
  </div>
  <div class="article">
    ${content || `<div class="fallback">
      <p>⚠️ 内容提取失败，请前往原站阅读</p>
      <a href="${safeUrl}" target="_blank">打开原文</a>
    </div>`}
  </div>
  <div class="footer">
    <p>原文链接：<a href="${safeUrl}" target="_blank">${safeUrl}</a></p>
    <p>由扣子早报阅读器提供</p>
  </div>
</body>
</html>`;
}

function escapeHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ===== 主处理逻辑 =====
async function handleReader(request) {
  const url = new URL(request.url);
  const targetUrl = url.searchParams.get('url');

  if (!targetUrl) {
    return new Response(JSON.stringify({ error: 'Missing ?url= parameter' }), {
      status: 400,
      headers: { 'Content-Type': 'application/json' }
    });
  }

  // 解析目标 URL
  let target;
  try {
    target = new URL(targetUrl);
  } catch (e) {
    return new Response(JSON.stringify({ error: 'Invalid URL' }), {
      status: 400,
      headers: { 'Content-Type': 'application/json' }
    });
  }

  // SSRF 防护：检查域名白名单
  if (!isAllowedHost(target.hostname)) {
    // 不在白名单中，直接 302 跳转原站（不抓取）
    return Response.redirect(targetUrl, 302);
  }

  // 抓取原站内容（使用桌面 UA）
  try {
    const response = await fetch(targetUrl, {
      headers: {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
      },
      redirect: 'follow',
    });

    if (!response.ok) {
      // 抓取失败，302 跳转原站
      return Response.redirect(targetUrl, 302);
    }

    const html = await response.text();

    // 提取正文
    const { title, content, source } = extractContent(html, targetUrl);

    // 提取失败则跳转原站
    if (!content || content.length < 100) {
      return Response.redirect(targetUrl, 302);
    }

    // 渲染响应式页面
    const rendered = renderPage(title, content, source, targetUrl);
    return new Response(rendered, {
      headers: {
        'Content-Type': 'text/html; charset=utf-8',
        'X-Reader-Source': target.hostname,
      }
    });

  } catch (e) {
    // 网络错误，302 跳转原站
    return Response.redirect(targetUrl, 302);
  }
}

// ===== Worker 入口 =====
export default {
  async fetch(request) {
    const url = new URL(request.url);

    if (url.pathname === '/reader') {
      return handleReader(request);
    }

    // 健康检查
    if (url.pathname === '/' || url.pathname === '/health') {
      return new Response(JSON.stringify({
        status: 'ok',
        service: 'news-reader',
        version: '1.0',
      }), {
        headers: { 'Content-Type': 'application/json' }
      });
    }

    return new Response('Not Found', { status: 404 });
  }
};
