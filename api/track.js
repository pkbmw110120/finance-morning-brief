/**
 * 埋点数据 CORS 代理 - Vercel Serverless Function v1.0
 * 部署位置: finance-morning-brief/api/track.js
 * 访问地址: https://pkbmw110120.github.io/finance-morning-brief/api/track
 * 
 * 注意：部署到 Vercel 后，域名会变成 xxx.vercel.app
 * 需要把 tracker.js 的 WEBHOOK_URL 改为 Vercel 分配的域名
 */

const FEISHU_APP_ID = 'cli_a96ac01690799cb3';
const FEISHU_APP_SECRET = 'xWB7UFirpIjVno9Xgd17eb5ZzeSMXR2V';
const BITABLE_TOKEN = 'E9CebRUs0a0bIrsxb0zccNMCn4d';
const BITABLE_TABLE_ID = 'tbl9KGKukLxSwB0P';

let tokenCache = { token: '', expireAt: 0 };

async function getFeishuToken() {
  if (tokenCache.token && Date.now() < tokenCache.expireAt) {
    return tokenCache.token;
  }
  const r = await fetch(
    'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        app_id: FEISHU_APP_ID,
        app_secret: FEISHU_APP_SECRET
      })
    }
  );
  const d = await r.json();
  if (d.code !== 0) throw new Error('Token error: ' + d.msg);
  tokenCache = {
    token: d.tenant_access_token,
    expireAt: Date.now() + (d.expire - 300) * 1000
  };
  return tokenCache.token;
}

async function writeToBitable(t, f) {
  const r = await fetch(
    'https://open.feishu.cn/open-apis/bitable/v1/apps/' +
      BITABLE_TOKEN +
      '/tables/' +
      BITABLE_TABLE_ID +
      '/records',
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: 'Bearer ' + t
      },
      body: JSON.stringify({ fields: f })
    }
  );
  return r.json();
}

function buildFields(b) {
  var e = b.event_data || b;
  var et = e.event_type || b.event_name || '';
  return {
    '\u9875\u9762ID': e.page_id || '',
    '\u7528\u6237ID': e.user_id || b.user_id || '',
    '\u4f1a\u8bddID': e.session_id || b.session_id || '',
    '\u4e8b\u4ef6\u7c7b\u578b': et.split('|')[0] || '',
    '\u4e8b\u4ef6\u6570\u636e': JSON.stringify(e),
    '\u65f6\u95f4\u6233': e.timestamp || b.timestamp || new Date().toISOString(),
    '\u505c\u7559\u65f6\u957f': e.duration || 0,
    '\u6765\u6e90': e.referrer || ''
  };
}

function corsHeaders(origin) {
  return {
    'Access-Control-Allow-Origin': origin || '*',
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Max-Age': '86400'
  };
}

export default async function handler(req) {
  var origin = req.headers.get('Origin') || '';

  if (req.method === 'OPTIONS') {
    return new Response(null, {
      status: 204,
      headers: corsHeaders(origin)
    });
  }

  if (req.method !== 'POST') {
    return new Response(JSON.stringify({ error: 'Method not allowed' }), {
      status: 405,
      headers: Object.assign({ 'Content-Type': 'application/json' }, corsHeaders(origin))
    });
  }

  try {
    var body = await req.json();
    var fields = buildFields(body);
    var token = await getFeishuToken();
    var result = await writeToBitable(token, fields);

    if (result.code === 0) {
      return new Response(JSON.stringify({ success: true }), {
        status: 200,
        headers: Object.assign({ 'Content-Type': 'application/json' }, corsHeaders(origin))
      });
    } else {
      return new Response(JSON.stringify({ success: false, error: result.msg }), {
        status: 500,
        headers: Object.assign({ 'Content-Type': 'application/json' }, corsHeaders(origin))
      });
    }
  } catch (err) {
    return new Response(JSON.stringify({ success: false, error: err.message }), {
      status: 500,
      headers: Object.assign({ 'Content-Type': 'application/json' }, corsHeaders(origin))
    });
  }
}
