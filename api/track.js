/**
 * 埋点数据 CORS 代理 - Vercel Edge Function v2.0
 * 
 * 纯转发到飞书 Webhook，解决浏览器 CORS 问题
 * tracker 原始数据格式不变，只是多了一层代理
 * 
 * 访问地址: https://finance-morning-brief-a93shxl1o-pkbmw110120s-projects.vercel.app/api/track
 */

export const config = {
  runtime: 'edge'
};

const FEISHU_WEBHOOK_URL = 'https://open.feishu.cn/open-apis/bot/v2/hook/621a5ea9-5b95-4176-8229-ccf30cdbb683';

function corsHeaders(origin) {
  return {
    'Access-Control-Allow-Origin': origin || '*',
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Max-Age': '86400'
  };
}

export default async function handler(req) {
  const origin = req.headers.get('Origin') || '';

  if (req.method === 'OPTIONS') {
    return new Response(null, { status: 204, headers: corsHeaders(origin) });
  }

  if (req.method !== 'POST') {
    return new Response(JSON.stringify({ error: 'Method not allowed' }), {
      status: 405,
      headers: { 'Content-Type': 'application/json', ...corsHeaders(origin) }
    });
  }

  try {
    const body = await req.json();
    
    const webhookResult = await fetch(FEISHU_WEBHOOK_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    
    const webhookData = await webhookResult.json();
    
    if (webhookData.code === 0 || webhookData.StatusCode === 0) {
      return new Response(JSON.stringify({ success: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json', ...corsHeaders(origin) }
      });
    } else {
      return new Response(JSON.stringify({ 
        success: false, 
        error: 'Webhook forward failed',
        detail: webhookData 
      }), {
        status: 502,
        headers: { 'Content-Type': 'application/json', ...corsHeaders(origin) }
      });
    }
  } catch (err) {
    return new Response(JSON.stringify({ success: false, error: err.message }), {
      status: 500,
      headers: { 'Content-Type': 'application/json', ...corsHeaders(origin) }
    });
  }
}
