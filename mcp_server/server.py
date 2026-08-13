#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
财经早报 MCP Server（v2，纯标准库实现）
使用 Python 标准库 + requests 实现 MCP JSON-RPC 2.0 stdio 协议，
为腾讯 WorkBuddy 等 MCP 客户端提供财经早报相关工具。

功能：
1. read_brief    - 读取指定日期的早报内容（Markdown）
2. get_analytics - 查询埋点数据统计（PV/UV/点击率/停留时长）
3. check_quality - 检查早报质量（链接存活/标题重复/内容覆盖）
4. get_bill_rates - 获取票据利率数据表格（Markdown）

配置：通过环境变量传入凭据
  - GITHUB_TOKEN        （可选，用于访问 GitHub API，当前版本未使用）
  - FEISHU_APP_ID       飞书应用 App ID（埋点统计必需）
  - FEISHU_APP_SECRET   飞书应用 App Secret（埋点统计必需）

依赖：pip install requests
"""

import os
import re
import sys
import json
import logging
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor
from enum import Enum

import requests

# ==========================================================================
# 常量与配置
# ==========================================================================

# 早报 GitHub Pages 地址
BRIEF_BASE_URL = "https://pkbmw110120.github.io/finance-morning-brief"

# 飞书多维表格配置（埋点数据）
BITABLE_APP_TOKEN = "E9CebRUs0a0bIrsxb0zccNMCn4d"
BITABLE_TABLE_ID = "tbl9KGKukLxSwB0P"

# HTTP 超时（秒）
HTTP_TIMEOUT = 30

# User-Agent
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 东八区时区
CST_TZ = timezone(timedelta(hours=8))

# 预期早报条目数：1 头条 + 5 财经 + 5 供应链 = 11
EXPECTED_NEWS_TOTAL = 11

# 标题相似度阈值（重复判定）
SIMILARITY_THRESHOLD = 0.7

# 质量检查并发数
LINK_CHECK_WORKERS = 5
LINK_CHECK_TIMEOUT = 8

# MCP 协议版本
MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "finance-morning-brief"
SERVER_VERSION = "2.0.0"

# ==========================================================================
# 日志配置（输出到 stderr，不干扰 stdout 的 JSON 通信）
# ==========================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mcp-server")


# ==========================================================================
# 通用工具函数
# ==========================================================================

def _today_str() -> str:
    """返回今天的日期字符串 YYYY-MM-DD（东八区）"""
    return datetime.now(CST_TZ).strftime("%Y-%m-%d")


def _strip_html(text: str) -> str:
    """去除 HTML 标签，返回纯文本"""
    return re.sub(r"<[^>]+>", "", text).strip()


def _fetch_text(url: str, timeout: int = HTTP_TIMEOUT) -> str:
    """发送 GET 请求，返回文本内容"""
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp.text


def _fetch_json(url: str, timeout: int = HTTP_TIMEOUT) -> dict:
    """发送 GET 请求，返回 JSON 对象"""
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp.json()


# ==========================================================================
# 早报 HTML 解析
# ==========================================================================

class BriefHTMLParser(HTMLParser):
    """
    从早报 HTML 中提取结构化新闻数据。
    通过 data-news-id / data-news-title 属性识别新闻条目，
    收集每条新闻的标题、链接和 AI 摘要。
    """

    def __init__(self):
        super().__init__()
        self.news_items: dict[str, dict] = {}
        self.bill_rate_table_html: str = ""
        self._current_news_id: str | None = None
        self._current_news_title: str | None = None
        self._news_depth: int = 0
        self._tag_stack: list[str] = []
        self._current_href: str | None = None
        self._collecting_link_text: bool = False
        self._link_text_buffer: list[str] = []
        # AI 摘要收集
        self._in_ai_summary: bool = False
        self._ai_summary_depth: int = 0
        self._ai_summary_buffer: list[str] = []
        # 票据表格
        self._in_bill_table: bool = False
        self._bill_table_buffer: list[str] = []
        self._bill_table_depth: int = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attr_dict = dict(attrs)

        # 检测票据市场日报表格（标题含"票据市场"）
        if tag == "h2" or tag == "h3":
            # 我们在 handle_data 中判断文本
            pass

        # 检测新闻条目
        news_id = attr_dict.get("data-news-id")
        news_title = attr_dict.get("data-news-title")
        if news_id:
            self._current_news_id = news_id
            self._current_news_title = news_title or ""
            self._news_depth = 1
            if news_id not in self.news_items:
                self.news_items[news_id] = {
                    "id": news_id,
                    "title": "",
                    "title_short": news_title or "",
                    "url": "",
                    "summary": "",
                    "source": "",
                }
            self._tag_stack = [tag]
            return

        if self._current_news_id:
            self._news_depth += 1
            self._tag_stack.append(tag)

            if tag == "a":
                href = attr_dict.get("href", "")
                if href and href != "#":
                    self._current_href = href
                    item = self.news_items.get(self._current_news_id)
                    if item and not item["url"]:
                        item["url"] = href
                    self._collecting_link_text = True
                    self._link_text_buffer = []

            # AI 摘要检测（class 含 ai-summary）
            class_attr = attr_dict.get("class", "") or ""
            if "ai-summary" in class_attr and not self._in_ai_summary:
                self._in_ai_summary = True
                self._ai_summary_depth = 1
                self._ai_summary_buffer = []
            elif self._in_ai_summary:
                self._ai_summary_depth += 1

        # 票据表格检测
        if "票据市场日报" in " ".join(str(v) for v in attr_dict.values() if v):
            pass  # 在文本中检测更可靠

    def handle_endtag(self, tag: str):
        if self._current_news_id:
            if self._tag_stack and self._tag_stack[-1] == tag:
                self._tag_stack.pop()
            self._news_depth -= 1

            if tag == "a" and self._current_href:
                link_text = "".join(self._link_text_buffer).strip()
                if link_text and self._current_news_id:
                    item = self.news_items.get(self._current_news_id)
                    if item and not item["title"]:
                        item["title"] = link_text
                self._current_href = None
                self._collecting_link_text = False
                self._link_text_buffer = []

            if self._in_ai_summary:
                self._ai_summary_depth -= 1
                if self._ai_summary_depth <= 0:
                    # 保存摘要
                    summary_text = "".join(self._ai_summary_buffer).strip()
                    # 去掉 "AI摘要:" 等标签前缀
                    summary_text = re.sub(
                        r"^(AI摘要[:：]?|摘要[:：]?)\s*", "", summary_text
                    )
                    if self._current_news_id and summary_text:
                        item = self.news_items.get(self._current_news_id)
                        if item and not item["summary"]:
                            item["summary"] = summary_text
                    self._in_ai_summary = False
                    self._ai_summary_buffer = []

            if self._news_depth <= 0:
                self._current_news_id = None
                self._current_news_title = None

        # 票据表格结束
        if self._in_bill_table and tag == "table":
            self._bill_table_depth -= 1
            if self._bill_table_depth <= 0:
                self._in_bill_table = False

    def handle_data(self, data: str):
        if self._collecting_link_text and self._current_news_id:
            self._link_text_buffer.append(data)

        if self._in_ai_summary and self._current_news_id:
            self._ai_summary_buffer.append(data)

        if self._in_bill_table:
            self._bill_table_buffer.append(data)

    def get_headline(self) -> dict | None:
        """获取头条新闻"""
        for nid, item in sorted(self.news_items.items()):
            if nid.startswith("headline"):
                if not item["title"] and item["title_short"]:
                    item["title"] = item["title_short"]
                return item
        return None

    def get_finance_news(self) -> list[dict]:
        """获取财经要闻列表"""
        results = []
        for nid, item in sorted(self.news_items.items()):
            if nid.startswith("news-") or nid.startswith("finance-"):
                if not item["title"] and item["title_short"]:
                    item["title"] = item["title_short"]
                results.append(item)
        return results

    def get_supply_news(self) -> list[dict]:
        """获取供应链金融新闻列表"""
        results = []
        for nid, item in sorted(self.news_items.items()):
            if (nid.startswith("supply-") or nid.startswith("sc-")
                    or nid.startswith("snews-")):
                if not item["title"] and item["title_short"]:
                    item["title"] = item["title_short"]
                results.append(item)
        return results


def parse_brief_html(html: str) -> dict:
    """
    解析早报 HTML，返回结构化数据。

    返回格式：
    {
        "date": "2026-08-13",
        "headline": {id, title, url, summary, source},
        "finance_news": [...],
        "supply_news": [...],
        "bill_rate": {
            "title": "...",
            "headers": [...],
            "rows": [[...], ...],
            "note": "..."
        }
    }
    """
    parser = BriefHTMLParser()
    try:
        parser.feed(html)
    except Exception:
        pass

    result = {
        "date": "",
        "headline": parser.get_headline(),
        "finance_news": parser.get_finance_news(),
        "supply_news": parser.get_supply_news(),
        "bill_rate": None,
    }

    # 提取日期
    date_match = re.search(
        r'(\d{4})年(\d{1,2})月(\d{1,2})日', html
    )
    if date_match:
        y, m, d = date_match.groups()
        result["date"] = f"{y}-{m.zfill(2)}-{d.zfill(2)}"

    # 解析票据利率表格
    result["bill_rate"] = _parse_bill_rate_table(html)

    return result


def _parse_bill_rate_table(html: str) -> dict | None:
    """从 HTML 中解析票据利率表格。

    兼容多种 HTML 模板：
    - v9+ 模板：<div class="bill-rate"> 内的表格，标题为"云链直贴业务参考价"
    - 旧模板：包含"票据市场日报"标题的表格，表头含"票据类型"
    """
    table_html = ""
    title = "票据利率"

    # 优先匹配 v9+ 模板：class="bill-rate" 区块内的表格
    bill_div_match = re.search(
        r'<div[^>]*class="bill-rate"[^>]*>(.*?)</div>\s*</div>',
        html, re.DOTALL | re.IGNORECASE
    )
    if bill_div_match:
        section_html = bill_div_match.group(1)
        table_match = re.search(
            r'<table[^>]*>(.*?)</table>',
            section_html, re.DOTALL | re.IGNORECASE
        )
        if table_match:
            table_html = table_match.group(0)
            # 提取标题
            h3_match = re.search(r'<h3[^>]*>(.*?)</h3>', section_html, re.DOTALL | re.IGNORECASE)
            if h3_match:
                title = _strip_html(h3_match.group(1))

    # 兜底：旧模板 - "票据市场日报"后的第一个表格
    if not table_html:
        bill_section_match = re.search(
            r'票据市场日报.*?<table[^>]*>(.*?)</table>',
            html, re.DOTALL | re.IGNORECASE
        )
        if not bill_section_match:
            # 再兜底：找包含"票据类型"或"承兑行类别"表头的表格
            bill_section_match = re.search(
                r'<table[^>]*>.*?(?:票据类型|承兑行类别).*?</table>',
                html, re.DOTALL | re.IGNORECASE
            )
        if bill_section_match:
            table_html = bill_section_match.group(0)
            title = "票据市场日报"

    if not table_html:
        return None

    # 解析表头
    headers = []
    thead_match = re.search(r'<thead.*?>(.*?)</thead>', table_html, re.DOTALL | re.IGNORECASE)
    if thead_match:
        th_matches = re.findall(r'<th[^>]*>(.*?)</th>', thead_match.group(1), re.DOTALL | re.IGNORECASE)
        headers = [_strip_html(th) for th in th_matches]

    # 解析行数据
    rows = []
    tbody_match = re.search(r'<tbody.*?>(.*?)</tbody>', table_html, re.DOTALL | re.IGNORECASE)
    body_html = tbody_match.group(1) if tbody_match else table_html

    tr_matches = re.findall(r'<tr[^>]*>(.*?)</tr>', body_html, re.DOTALL | re.IGNORECASE)
    for tr in tr_matches:
        td_matches = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', tr, re.DOTALL | re.IGNORECASE)
        row = [_strip_html(td) for td in td_matches]
        if row:
            rows.append(row)

    # 如果没有从thead拿到headers，用第一行
    if not headers and rows:
        headers = rows[0]
        rows = rows[1:]

    # 提取备注/说明文字（找表格后 class="note" 的 <p> 标签）
    note = ""
    # v9+ 模板：<p class="note">
    note_match = re.search(
        r'<p[^>]*class="note"[^>]*>(.*?)</p>',
        html, re.DOTALL | re.IGNORECASE,
    )
    if note_match:
        note = _strip_html(note_match.group(1))

    return {
        "title": title,
        "headers": headers,
        "rows": rows,
        "note": note,
    }


# ==========================================================================
# 飞书埋点数据
# ==========================================================================

_feishu_token_cache: dict = {"token": "", "expire_time": 0}


def get_feishu_tenant_token() -> str:
    """
    获取飞书 tenant_access_token，带缓存。

    从环境变量读取 FEISHU_APP_ID 和 FEISHU_APP_SECRET，
    调用飞书 OAuth 接口获取 token，缓存至过期前 60 秒。
    """
    now = datetime.now().timestamp()
    if _feishu_token_cache["token"] and _feishu_token_cache["expire_time"] > now + 60:
        return _feishu_token_cache["token"]

    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")

    if not app_id or not app_secret:
        raise RuntimeError(
            "缺少飞书凭据：请设置 FEISHU_APP_ID 和 FEISHU_APP_SECRET 环境变量"
        )

    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal/"
    resp = requests.post(
        url,
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("code") != 0:
        raise RuntimeError(f"获取飞书Token失败: {data.get('msg', data)}")

    token = data.get("tenant_access_token", "")
    expire = data.get("expire", 7200)
    _feishu_token_cache["token"] = token
    _feishu_token_cache["expire_time"] = now + expire

    return token


def _extract_field_value(fields: dict, field_name: str) -> str:
    """从多维表格字段中提取值（兼容多种格式）"""
    val = fields.get(field_name, "")
    if isinstance(val, list):
        if len(val) > 0 and isinstance(val[0], dict):
            return str(val[0].get("text", val[0].get("value", "")))
        return str(val[0]) if val else ""
    return str(val) if val else ""


def fetch_bitable_records(token: str, target_date: str) -> list[dict]:
    """
    从飞书多维表格读取指定日期的埋点记录。

    使用 GET 接口全量拉取后 Python 层过滤（兼容 search API 的各种限制）。
    """
    all_records = []
    page_token = None

    # 构建日期过滤模式（兼容多种格式）
    parts = target_date.split("-")
    year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
    date_patterns = [
        f"{year}年{int(month):02d}月{int(day):02d}日",
        f"{year}年{month}月{day}日",
        f"{year}-{int(month):02d}-{int(day):02d}",
        f"{year}-{month}-{day}",
    ]

    while True:
        url = (
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
            f"{BITABLE_APP_TOKEN}/tables/{BITABLE_TABLE_ID}/records"
        )
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token

        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT)
        data = resp.json()

        if data.get("code") != 0:
            raise RuntimeError(f"多维表格查询失败: {data.get('msg', data)}")

        items = data.get("data", {}).get("items", [])

        # 按日期过滤
        for item in items:
            page_id = _extract_field_value(item.get("fields", {}), "页面ID")
            for pattern in date_patterns:
                if pattern in str(page_id):
                    all_records.append(item)
                    break

        page_token = data.get("data", {}).get("page_token")
        total = data.get("data", {}).get("total", 0)

        if not page_token or len(all_records) >= total:
            break

    return all_records


def analyze_tracking_records(records: list[dict]) -> dict:
    """
    分析埋点记录，计算 PV/UV/点击率/平均停留时长等指标。
    """
    stats = {
        "pv": 0,
        "uv": set(),
        "pv_sessions": set(),
        "exposure_sessions": set(),
        "click_sessions": set(),
        "news_exposure": defaultdict(int),
        "news_click": defaultdict(int),
        "click_titles": {},
        "exposure_titles": {},
        "user_max_hb_time": {},
        "user_first_stay": {},
        "user_first_ts": {},
        "user_last_ts": {},
        "total_events": 0,
    }

    for item in records:
        fields = item.get("fields", {})
        user_id = _extract_field_value(fields, "用户ID")
        event_type_raw = _extract_field_value(fields, "事件类型")
        event_data_str = _extract_field_value(fields, "事件数据")
        timestamp = _extract_field_value(fields, "时间戳")

        # 解析事件数据 JSON
        event_data = {}
        try:
            if isinstance(event_data_str, str) and event_data_str.strip():
                event_data = json.loads(event_data_str)
                if not isinstance(event_data, dict):
                    event_data = {}
        except (json.JSONDecodeError, TypeError):
            event_data = {}

        session_id = event_data.get("session_id", "")
        news_id = event_data.get("news_id", "")
        news_title = event_data.get("news_title", "")

        # 解析基础事件类型
        base_type = str(event_type_raw).split("|")[0].strip()
        if not base_type and event_data.get("event_type"):
            base_type = event_data["event_type"].split("|")[0].strip()

        stats["total_events"] += 1

        if base_type == "page_view":
            pv_key = f"{user_id}_{session_id}" if session_id else f"{user_id}_{timestamp}"
            if pv_key not in stats["pv_sessions"]:
                stats["pv_sessions"].add(pv_key)
                stats["pv"] += 1
                stats["uv"].add(user_id)

        elif base_type == "news_exposure":
            if news_id:
                exposure_key = (
                    f"{session_id}_{news_id}" if session_id
                    else f"uid_{user_id}_{news_id}"
                )
                if exposure_key not in stats["exposure_sessions"]:
                    stats["exposure_sessions"].add(exposure_key)
                    stats["news_exposure"][news_id] += 1
                    if news_title and news_id not in stats["exposure_titles"]:
                        stats["exposure_titles"][news_id] = news_title

        elif base_type == "news_click":
            if news_id:
                click_key = (
                    f"{session_id}_{news_id}" if session_id
                    else f"uid_{user_id}_{news_id}"
                )
                if click_key not in stats["click_sessions"]:
                    stats["click_sessions"].add(click_key)
                    stats["news_click"][news_id] += 1
                if news_title:
                    stats["click_titles"][news_id] = news_title

        elif base_type == "page_stay":
            duration = int(event_data.get("duration", 0))
            if duration > 0:
                if user_id not in stats["user_first_stay"] or duration < stats["user_first_stay"][user_id]:
                    stats["user_first_stay"][user_id] = duration

        elif base_type == "heartbeat":
            hb_time = int(event_data.get("heartbeat_time", 0))
            if hb_time > 0:
                if user_id not in stats["user_max_hb_time"] or hb_time > stats["user_max_hb_time"][user_id]:
                    stats["user_max_hb_time"][user_id] = hb_time

        # 首末事件时间戳
        try:
            ts_sec = int(timestamp) / 1000 if timestamp else 0
            if ts_sec > 0:
                if user_id not in stats["user_first_ts"] or ts_sec < stats["user_first_ts"][user_id]:
                    stats["user_first_ts"][user_id] = ts_sec
                if user_id not in stats["user_last_ts"] or ts_sec > stats["user_last_ts"][user_id]:
                    stats["user_last_ts"][user_id] = ts_sec
        except (ValueError, TypeError):
            pass

    return stats


def calc_avg_stay(stats: dict) -> tuple[float, str]:
    """计算平均停留时长，返回 (平均值秒, 数据来源说明)"""
    MAX_STAY = 600  # 10 分钟上限
    durations = []

    for uid in stats["uv"]:
        stay = 0
        if uid in stats["user_max_hb_time"] and stats["user_max_hb_time"][uid] > 0:
            stay = stats["user_max_hb_time"][uid]
        elif uid in stats["user_first_stay"] and stats["user_first_stay"][uid] > 0:
            stay = stats["user_first_stay"][uid]
        elif uid in stats["user_first_ts"] and uid in stats["user_last_ts"]:
            diff = stats["user_last_ts"][uid] - stats["user_first_ts"][uid]
            if diff > 0:
                stay = diff

        if stay > 0:
            stay = min(stay, MAX_STAY)
            durations.append(stay)

    if durations:
        avg = sum(durations) / len(durations)
        return avg, f"心跳/停留/时间差（{len(durations)}人，上限{MAX_STAY}s）"

    return 0.0, "无数据"


# ==========================================================================
# 质量检查
# ==========================================================================

def _calculate_similarity(s1: str, s2: str) -> float:
    """简单的文本相似度计算（基于字符集合 Jaccard 相似度）"""
    if not s1 or not s2:
        return 0.0
    if s1 == s2:
        return 1.0

    set1 = set(s1)
    set2 = set(s2)
    if not set1 or not set2:
        return 0.0

    intersection = set1 & set2
    union = set1 | set2
    return len(intersection) / len(union) if union else 0.0


def _check_single_link(url: str, title: str) -> dict:
    """检查单个链接是否存活"""
    try:
        resp = requests.head(
            url, timeout=LINK_CHECK_TIMEOUT, allow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        if resp.status_code == 405:
            resp = requests.get(
                url, timeout=LINK_CHECK_TIMEOUT, allow_redirects=True,
                headers={"User-Agent": USER_AGENT}, stream=True,
            )
        ok = resp.status_code < 400
        return {
            "url": url, "title": title, "ok": ok,
            "error": None if ok else f"HTTP {resp.status_code}",
        }
    except requests.Timeout:
        return {"url": url, "title": title, "ok": False, "error": "请求超时"}
    except Exception as e:
        return {"url": url, "title": title, "ok": False, "error": str(e)[:80]}


def check_link_health(html: str) -> dict:
    """并发检查所有新闻链接的存活率"""
    links = []
    seen = set()
    for m in re.finditer(
        r'<(?:div|article)[^>]*data-news-id="([^"]+)"[^>]*data-news-title="([^"]*)"',
        html,
    ):
        nid, title = m.group(1), m.group(2)
        start = m.start()
        chunk = html[start:start + 3000]
        href_m = re.search(r'<a[^>]*href="([^"]+)"', chunk)
        if href_m:
            href = href_m.group(1)
            if href and href not in seen and href != "#":
                seen.add(href)
                links.append({"url": href, "title": title})

    if not links:
        return {"total": 0, "valid": 0, "broken": 0, "rate": 0.0, "broken_links": []}

    # 并发检查
    results = []
    with ThreadPoolExecutor(max_workers=LINK_CHECK_WORKERS) as executor:
        futures = {executor.submit(_check_single_link, lk["url"], lk["title"]): lk for lk in links}
        from concurrent.futures import as_completed
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                lk = futures[future]
                results.append({"url": lk["url"], "title": lk["title"], "ok": False, "error": str(e)[:80]})

    broken = [r for r in results if not r["ok"]]
    valid = len(results) - len(broken)
    rate = valid / len(results) if results else 0.0

    return {
        "total": len(results),
        "valid": valid,
        "broken": len(broken),
        "rate": round(rate, 4),
        "broken_links": [
            {"url": b["url"], "title": b["title"], "error": b["error"]}
            for b in broken
        ],
    }


def check_title_duplicates(today_data: dict, html: str, history_days: int = 7) -> dict:
    """检查今日标题与过去 N 天的重复率"""
    # 收集今日标题
    today_titles = []
    if today_data.get("headline"):
        t = today_data["headline"].get("title") or today_data["headline"].get("title_short", "")
        if t:
            today_titles.append(("头条", t))
    for section, key in [("财经要闻", "finance_news"), ("供应链金融", "supply_news")]:
        for item in today_data.get(key, []):
            t = item.get("title") or item.get("title_short", "")
            if t:
                today_titles.append((section, t))

    # 获取历史标题
    today = datetime.now(CST_TZ).date()
    historical_titles = []
    for i in range(1, history_days + 1):
        d = today - timedelta(days=i)
        date_str = d.strftime("%Y-%m-%d")
        url = f"{BRIEF_BASE_URL}/{date_str}.html"
        try:
            hist_html = _fetch_text(url, timeout=10)
            titles = re.findall(r'data-news-title="([^"]+)"', hist_html)
            historical_titles.append({"date": date_str, "titles": titles})
        except Exception:
            continue

    # 对比
    matches = []
    for section, title in today_titles:
        for day_info in historical_titles:
            found = False
            for hist_title in day_info["titles"]:
                sim = _calculate_similarity(title, hist_title)
                if sim >= SIMILARITY_THRESHOLD:
                    matches.append({
                        "section": section, "title": title,
                        "match_title": hist_title, "match_date": day_info["date"],
                        "similarity": round(sim, 4),
                    })
                    found = True
                    break
            if found:
                break

    rate = len(matches) / len(today_titles) if today_titles else 0.0
    return {
        "today_titles": len(today_titles),
        "duplicates": len(matches),
        "rate": round(rate, 4),
        "matches": matches,
    }


def check_content_coverage(today_data: dict) -> dict:
    """检查内容覆盖率"""
    headline = today_data.get("headline")
    finance = today_data.get("finance_news", [])
    supply = today_data.get("supply_news", [])

    headline_ok = bool(headline and headline.get("title") and headline.get("url"))
    fin_valid = sum(1 for n in finance if n.get("title") and n.get("url"))
    sup_valid = sum(1 for n in supply if n.get("title") and n.get("url"))
    actual = (1 if headline_ok else 0) + fin_valid + sup_valid
    rate = actual / EXPECTED_NEWS_TOTAL

    return {
        "expected": EXPECTED_NEWS_TOTAL,
        "actual": actual,
        "rate": round(rate, 4),
        "details": {
            "headline": "✅" if headline_ok else "❌",
            "finance_news": f"{fin_valid}/5",
            "supply_news": f"{sup_valid}/5",
        },
    }


# ==========================================================================
# Markdown 格式化
# ==========================================================================

def format_brief_markdown(data: dict) -> str:
    """将早报数据格式化为 Markdown"""
    lines = []
    date = data.get("date") or "（日期未知）"
    lines.append(f"# 📰 财经早报 {date}")
    lines.append("")

    # 头条
    headline = data.get("headline")
    if headline:
        lines.append("## 🔥 今日头条")
        lines.append("")
        title = headline.get("title") or headline.get("title_short", "（无标题）")
        url = headline.get("url", "")
        if url:
            lines.append(f"### [{title}]({url})")
        else:
            lines.append(f"### {title}")
        lines.append("")
        if headline.get("summary"):
            lines.append(f"> {headline['summary']}")
            lines.append("")

    # 财经要闻
    finance = data.get("finance_news", [])
    if finance:
        lines.append("## 📊 财经要闻")
        lines.append("")
        for i, item in enumerate(finance, 1):
            title = item.get("title") or item.get("title_short", "（无标题）")
            url = item.get("url", "")
            if url:
                lines.append(f"{i}. [{title}]({url})")
            else:
                lines.append(f"{i}. {title}")
            if item.get("summary"):
                lines.append(f"   > {item['summary'][:100]}...")
        lines.append("")

    # 供应链金融
    supply = data.get("supply_news", [])
    if supply:
        lines.append("## ⛓️ 供应链金融")
        lines.append("")
        for i, item in enumerate(supply, 1):
            title = item.get("title") or item.get("title_short", "（无标题）")
            url = item.get("url", "")
            if url:
                lines.append(f"{i}. [{title}]({url})")
            else:
                lines.append(f"{i}. {title}")
            if item.get("summary"):
                lines.append(f"   > {item['summary'][:100]}...")
        lines.append("")

    # 票据利率简要
    bill = data.get("bill_rate")
    if bill and bill.get("rows"):
        lines.append("## 💹 票据利率")
        lines.append("")
        lines.append(f"_{bill.get('title', '票据市场日报')}_")
        lines.append("")
        # 只显示前 3 行作为摘要
        headers = bill["headers"]
        rows = bill["rows"][:3]
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
        for row in rows:
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
        lines.append("_使用 `get_bill_rates` 工具查看完整利率表_")
        lines.append("")

    lines.append("---")
    lines.append(f"_早报地址: {BRIEF_BASE_URL}/{date}.html_" if date else f"_早报地址: {BRIEF_BASE_URL}_")

    return "\n".join(lines)


def format_analytics_markdown(date: str, stats: dict) -> str:
    """将埋点统计数据格式化为 Markdown 报告"""
    avg_stay, stay_source = calc_avg_stay(stats)

    exposure_total = sum(stats["news_exposure"].values())
    click_total = sum(stats["news_click"].values())
    click_rate = click_total / exposure_total * 100 if exposure_total > 0 else 0

    top_clicks = Counter(stats["news_click"]).most_common(10)

    lines = [
        f"# 📊 早报埋点数据统计报告",
        "",
        f"**统计日期**: {date}",
        "",
        "---",
        "",
        "## 核心指标",
        "",
        "| 指标 | 数值 | 说明 |",
        "|------|------|------|",
        f"| 📖 页面浏览量(PV) | {stats['pv']} | **仅统计 page_view 事件，非总事件数** |",
        f"| 👤 独立访客(UV) | {len(stats['uv'])} | 独立用户数 |",
        f"| 👁️ 新闻曝光次数 | {exposure_total} | 新闻滚动到可视区域的次数 |",
        f"| 🖱️ 新闻点击次数 | {click_total} | 用户点击新闻的次数 |",
        f"| 📊 点击率(点击/曝光) | {click_rate:.1f}% | 点击数/曝光数 |",
        f"| ⏱️ 平均停留时长 | {avg_stay:.1f}秒 | {stay_source} |",
        f"| 📋 总事件数 | {stats['total_events']} | 包含所有类型事件（曝光/心跳/点击等） |",
        f"| 📊 PV/总事件比 | {stats['pv']}/{stats['total_events']} ({stats['pv']/stats['total_events']*100:.1f}%) | PV只占总事件的百分比 |",
        "",
        "---",
        "",
        "## 🖱️ 热门文章 TOP 10（按点击）",
        "",
    ]

    if top_clicks:
        for i, (news_id, count) in enumerate(top_clicks, 1):
            title = stats.get("click_titles", {}).get(news_id, "")
            if not title:
                title = stats.get("exposure_titles", {}).get(news_id, news_id)
            lines.append(f"{i}. {title[:50]} [{news_id}] - {count}次点击")
    else:
        lines.append("_暂无点击数据_")

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"*报告生成时间: {datetime.now(CST_TZ).strftime('%Y-%m-%d %H:%M:%S')}*")
    lines.append(f"*数据来源: 飞书多维表格 | 共 {stats['total_events']} 条事件*")

    return "\n".join(lines)


def format_quality_markdown(date: str, report: dict) -> str:
    """将质量检查报告格式化为 Markdown"""
    dedup = report["dedup"]
    link = report["link_health"]
    cov = report["coverage"]

    # 综合评分
    scores = [cov["rate"], link["rate"], 1.0 - dedup["rate"]]
    overall = sum(scores) / len(scores)

    lines = [
        f"# ✅ 早报质量检查报告 {date}",
        "",
        f"**综合评分**: {overall * 100:.0f}%",
        "",
        "---",
        "",
        "## 📋 检查概览",
        "",
        "| 检查项 | 结果 | 详情 |",
        "|--------|------|------|",
        f"| 🔗 链接存活率 | {link['rate'] * 100:.1f}% | {link['valid']}/{link['total']} 条有效 |",
        f"| 📝 标题重复率 | {dedup['rate'] * 100:.1f}% | {dedup['duplicates']}/{dedup['today_titles']} 条重复 |",
        f"| 📊 内容覆盖率 | {cov['rate'] * 100:.1f}% | {cov['actual']}/{cov['expected']} 条完整 |",
        "",
        "---",
        "",
    ]

    # 内容覆盖详情
    lines.append("## 📊 内容覆盖详情")
    lines.append("")
    d = cov["details"]
    lines.append(f"- 今日头条: {d['headline']}")
    lines.append(f"- 财经要闻: {d['finance_news']}")
    lines.append(f"- 供应链金融: {d['supply_news']}")
    lines.append("")

    # 死链列表
    lines.append("## 🔗 失效链接")
    lines.append("")
    if link["broken_links"]:
        for b in link["broken_links"]:
            lines.append(f"- ❌ {b['title'][:40]}")
            lines.append(f"  - URL: {b['url'][:80]}")
            lines.append(f"  - 错误: {b['error']}")
    else:
        lines.append("✅ 所有链接均可正常访问")
    lines.append("")

    # 重复标题
    lines.append("## 📝 重复标题")
    lines.append("")
    if dedup["matches"]:
        for m in dedup["matches"]:
            lines.append(
                f"- ⚠️ [{m['section']}] {m['title'][:40]} "
                f"（与 {m['match_date']} 重复，相似度 {m['similarity']:.0%}）"
            )
    else:
        lines.append("✅ 无重复标题")
    lines.append("")

    lines.append("---")
    lines.append(f"*检查时间: {datetime.now(CST_TZ).strftime('%Y-%m-%d %H:%M:%S')}*")

    return "\n".join(lines)


def format_bill_rates_markdown(bill_data: dict, date: str) -> str:
    """将票据利率数据格式化为 Markdown 表格"""
    if not bill_data or not bill_data.get("rows"):
        return f"# 💹 票据利率 {date}\n\n_未找到票据利率数据_"

    lines = [
        f"# 💹 {bill_data.get('title', '票据市场日报')}",
        f"**日期**: {date}",
        "",
    ]

    headers = bill_data["headers"]
    rows = bill_data["rows"]

    if headers and rows:
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
        for row in rows:
            # 补全列数
            while len(row) < len(headers):
                row.append("")
            row = row[:len(headers)]
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")

    if bill_data.get("note"):
        lines.append(f"> {bill_data['note']}")
        lines.append("")

    lines.append("---")
    lines.append(f"*数据来源: 财经早报 | {date}*")

    return "\n".join(lines)


# ==========================================================================
# MCP JSON-RPC 2.0 stdio 协议实现
# ==========================================================================

class JsonRpcError(Enum):
    """JSON-RPC 2.0 标准错误码"""
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603


class MCPServer:
    """
    纯标准库实现的 MCP Server（JSON-RPC 2.0 over stdio）。

    协议规范：
    - 每条消息是一行 JSON，以 \n 结尾
    - 请求格式：{"jsonrpc": "2.0", "id": <id>, "method": "<method>", "params": {...}}
    - 响应格式：{"jsonrpc": "2.0", "id": <id>, "result": {...}}
    - 错误格式：{"jsonrpc": "2.0", "id": <id>, "error": {"code": <code>, "message": "..."}}
    - 通知格式：{"jsonrpc": "2.0", "method": "<method>", "params": {...}}（无 id，无需响应）
    """

    def __init__(self):
        self._tools: dict[str, dict] = {}  # name -> {handler, description, inputSchema}
        self._initialized = False

    # ---- 工具注册 ----

    def register_tool(self, name: str, handler, description: str, input_schema: dict):
        """注册一个工具"""
        self._tools[name] = {
            "handler": handler,
            "description": description,
            "inputSchema": input_schema,
        }
        logger.info(f"注册工具: {name}")

    # ---- JSON-RPC 消息处理 ----

    def _make_response(self, request_id, result: dict) -> dict:
        """构建成功响应"""
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
        }

    def _make_error(self, request_id, code: int, message: str, data=None) -> dict:
        """构建错误响应"""
        err = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": code,
                "message": message,
            },
        }
        if data is not None:
            err["error"]["data"] = data
        return err

    def _write_message(self, message: dict):
        """写入一条 JSON 消息到 stdout（带换行）"""
        try:
            line = json.dumps(message, ensure_ascii=False)
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except Exception as e:
            logger.error(f"写入响应失败: {e}")

    def handle_request(self, request: dict) -> dict | None:
        """
        处理一条 JSON-RPC 请求。
        返回响应 dict；如果是通知（无 id），返回 None。
        """
        # 基本校验
        if not isinstance(request, dict):
            return self._make_error(
                None, JsonRpcError.INVALID_REQUEST.value, "Invalid request: not an object"
            )

        if request.get("jsonrpc") != "2.0":
            return self._make_error(
                request.get("id"),
                JsonRpcError.INVALID_REQUEST.value,
                "Invalid request: missing or wrong jsonrpc version",
            )

        method = request.get("method")
        if not method or not isinstance(method, str):
            return self._make_error(
                request.get("id"),
                JsonRpcError.INVALID_REQUEST.value,
                "Invalid request: missing method",
            )

        request_id = request.get("id")
        params = request.get("params", {})
        if params is None:
            params = {}

        # 通知（无 id）不需要返回响应，但我们仍处理一下
        is_notification = request_id is None

        try:
            # 路由到处理函数
            if method == "initialize":
                result = self._handle_initialize(params)
            elif method == "notifications/initialized":
                # 初始化完成通知，无需响应
                self._initialized = True
                logger.info("客户端已发送 initialized 通知")
                return None
            elif method == "tools/list":
                if not self._initialized:
                    return self._make_error(
                        request_id, JsonRpcError.INVALID_REQUEST.value,
                        "Server not initialized"
                    )
                result = self._handle_tools_list()
            elif method == "tools/call":
                if not self._initialized:
                    return self._make_error(
                        request_id, JsonRpcError.INVALID_REQUEST.value,
                        "Server not initialized"
                    )
                result = self._handle_tools_call(params)
            elif method == "ping":
                result = {}
            else:
                return self._make_error(
                    request_id, JsonRpcError.METHOD_NOT_FOUND.value,
                    f"Method not found: {method}"
                )

            if is_notification:
                return None
            return self._make_response(request_id, result)

        except Exception as e:
            logger.exception(f"处理 {method} 时出错")
            return self._make_error(
                request_id, JsonRpcError.INTERNAL_ERROR.value,
                f"Internal error: {str(e)}",
            )

    # ---- MCP 方法实现 ----

    def _handle_initialize(self, params: dict) -> dict:
        """处理 initialize 请求，返回服务器信息和能力声明"""
        client_info = params.get("clientInfo", {})
        client_name = client_info.get("name", "unknown")
        client_version = client_info.get("version", "unknown")
        protocol_version = params.get("protocolVersion", MCP_PROTOCOL_VERSION)

        logger.info(
            f"客户端初始化: {client_name} v{client_version} "
            f"(协议版本: {protocol_version})"
        )

        return {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "serverInfo": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
            },
            "capabilities": {
                "tools": {},
            },
            "instructions": (
                "财经早报 MCP Server，提供早报阅读、埋点统计、质量检查和票据利率查询功能。"
            ),
        }

    def _handle_tools_list(self) -> dict:
        """处理 tools/list 请求，返回工具列表"""
        tools_list = []
        for name, tool_info in self._tools.items():
            tools_list.append({
                "name": name,
                "description": tool_info["description"],
                "inputSchema": tool_info["inputSchema"],
            })
        return {"tools": tools_list}

    def _handle_tools_call(self, params: dict) -> dict:
        """处理 tools/call 请求，调用指定工具"""
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        if not tool_name or tool_name not in self._tools:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"❌ 未知工具: {tool_name}",
                    }
                ],
                "isError": True,
            }

        tool = self._tools[tool_name]
        handler = tool["handler"]

        try:
            logger.info(f"调用工具: {tool_name}, 参数: {arguments}")
            # 调用处理函数，参数以关键字参数传入
            result_text = handler(**arguments)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": result_text,
                    }
                ],
                "isError": False,
            }
        except TypeError as e:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"❌ 参数错误: {str(e)}",
                    }
                ],
                "isError": True,
            }
        except Exception as e:
            logger.exception(f"工具 {tool_name} 执行失败")
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"❌ 工具执行失败: {str(e)}",
                    }
                ],
                "isError": True,
            }

    # ---- 主循环 ----

    def run(self):
        """启动 stdio 主循环，逐行读取 stdin 并处理"""
        logger.info("MCP Server started")
        sys.stderr.flush()

        try:
            for raw_line in sys.stdin:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue

                logger.debug(f"收到消息: {raw_line[:200]}")

                # 解析 JSON
                try:
                    request = json.loads(raw_line)
                except json.JSONDecodeError as e:
                    logger.error(f"JSON 解析失败: {e}")
                    self._write_message(self._make_error(
                        None, JsonRpcError.PARSE_ERROR.value,
                        f"Parse error: {str(e)}",
                    ))
                    continue

                # 处理请求
                response = self.handle_request(request)
                if response is not None:
                    self._write_message(response)

        except KeyboardInterrupt:
            logger.info("收到中断信号，退出")
        except Exception as e:
            logger.exception(f"主循环异常: {e}")


# ==========================================================================
# SSE HTTP 传输层
# ==========================================================================

import uuid
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


class MCPSSESession:
    """
    单个 SSE 客户端会话。

    每个客户端通过 GET /sse 建立一个 SSE 长连接，并获得一个 session_id。
    客户端通过 POST /messages?session_id=xxx 发送 JSON-RPC 请求，
    响应通过对应的 SSE 流推送回去。
    """

    def __init__(self, session_id: str, server: "MCPServer"):
        self.session_id = session_id
        self.server = server
        # 使用队列（list + Condition）实现生产者-消费者模式
        self._queue: list[str] = []
        self._cond = threading.Condition()
        self._alive = True

    def enqueue_message(self, message: dict):
        """将一条 JSON-RPC 消息（响应/通知）放入队列，等待通过 SSE 发送"""
        line = json.dumps(message, ensure_ascii=False)
        with self._cond:
            if not self._alive:
                return
            self._queue.append(line)
            self._cond.notify()

    def next_event(self) -> str | None:
        """
        阻塞等待下一条要发送的 SSE 消息文本。
        返回 None 表示会话已结束。
        """
        with self._cond:
            while self._alive and not self._queue:
                self._cond.wait(timeout=15)  # 15 秒心跳间隔
            if not self._alive and not self._queue:
                return None
            line = self._queue.pop(0)
            return line

    def close(self):
        """关闭会话，唤醒所有等待者"""
        with self._cond:
            self._alive = False
            self._cond.notify_all()
        logger.info(f"SSE 会话关闭: {self.session_id}")

    @property
    def alive(self) -> bool:
        return self._alive


class SSESessionManager:
    """管理所有 SSE 客户端会话，线程安全"""

    def __init__(self):
        self._sessions: dict[str, MCPSSESession] = {}
        self._lock = threading.Lock()

    def create(self, server: "MCPServer") -> MCPSSESession:
        """创建一个新会话并返回"""
        session_id = str(uuid.uuid4())
        session = MCPSSESession(session_id, server)
        with self._lock:
            self._sessions[session_id] = session
        logger.info(f"SSE 会话创建: {session_id}, 当前会话数: {len(self._sessions)}")
        return session

    def get(self, session_id: str) -> MCPSSESession | None:
        """根据 ID 获取会话"""
        with self._lock:
            return self._sessions.get(session_id)

    def remove(self, session_id: str):
        """移除会话"""
        with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
        logger.info(f"SSE 会话移除: {session_id}, 剩余会话数: {len(self._sessions)}")

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)


# 全局会话管理器（SSE 模式下使用）
_sse_manager: SSESessionManager | None = None
_sse_server_instance: "MCPServer | None" = None


def _sse_dispatch_request(request: dict, session_id: str) -> dict | None:
    """
    将一条 JSON-RPC 请求分发到 MCPServer 处理。
    如果有响应，通过对应 SSE 会话推送回去。
    返回 None（通知）或响应 dict。
    """
    global _sse_server_instance
    if _sse_server_instance is None:
        return None

    response = _sse_server_instance.handle_request(request)
    if response is not None:
        session = _sse_manager.get(session_id) if _sse_manager else None
        if session and session.alive:
            session.enqueue_message(response)
    return response


class MCPSSERequestHandler(BaseHTTPRequestHandler):
    """
    MCP SSE HTTP 请求处理器。

    支持三个端点：
    - GET  /sse       → 建立 SSE 长连接，发送 endpoint 事件
    - POST /messages  → 接收 JSON-RPC 请求，通过 SSE 流返回响应
    - GET  /health    → 健康检查
    """

    # 覆写日志输出到 stderr
    def log_message(self, format: str, *args):  # type: ignore[override]
        logger.info("HTTP %s - %s", self.address_string(), format % args)

    # ---- SSE 端点 ----

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/sse":
            self._handle_sse()
        elif path == "/health":
            self._handle_health()
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/messages":
            self._handle_messages(parsed)
        else:
            self.send_error(404, "Not Found")

    # ---- 处理函数 ----

    def _handle_health(self):
        """健康检查端点"""
        body = json.dumps({
            "status": "ok",
            "server": SERVER_NAME,
            "version": SERVER_VERSION,
            "sessions": _sse_manager.count() if _sse_manager else 0,
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _handle_sse(self):
        """处理 GET /sse，建立 SSE 长连接"""
        global _sse_manager, _sse_server_instance
        if _sse_manager is None or _sse_server_instance is None:
            self.send_error(503, "SSE server not initialized")
            return

        # 创建会话
        session = _sse_manager.create(_sse_server_instance)
        session_id = session.session_id

        # 构造 endpoint URL（客户端用来发请求的地址）
        # 优先使用客户端请求的 Host 头
        host = self.headers.get("Host", f"localhost:{self.server.server_port}")
        scheme = "http"  # SSE 标准协议用 http
        messages_url = f"{scheme}://{host}/messages?session_id={session_id}"

        # 发送 SSE 响应头
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Accel-Buffering", "no")  # 禁用反向代理缓冲
        self.end_headers()

        try:
            # 1. 发送 endpoint 事件（MCP SSE 协议要求）
            self._write_sse_event("endpoint", messages_url)

            # 2. 持续从会话队列读取消息并推送
            while session.alive:
                line = session.next_event()
                if line is None:
                    break
                # 发送 JSON-RPC 消息事件（默认事件名 message）
                self._write_sse_message(line)

        except (BrokenPipeError, ConnectionResetError, OSError):
            logger.info(f"SSE 客户端断开: {session_id}")
        except Exception as e:
            logger.exception(f"SSE 连接异常: {session_id}, {e}")
        finally:
            session.close()
            if _sse_manager:
                _sse_manager.remove(session_id)

    def _handle_messages(self, parsed):
        """处理 POST /messages，接收 JSON-RPC 请求"""
        global _sse_manager
        if _sse_manager is None:
            self.send_error(503, "SSE server not initialized")
            return

        # 从 query string 获取 session_id
        query = parse_qs(parsed.query)
        session_ids = query.get("session_id", [])
        if not session_ids:
            self.send_error(400, "Missing session_id query parameter")
            return
        session_id = session_ids[0]

        # 验证会话
        session = _sse_manager.get(session_id)
        if session is None or not session.alive:
            self.send_error(404, "Session not found or closed")
            return

        # 读取请求体
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0:
            self.send_error(400, "Empty request body")
            return

        raw_body = self.rfile.read(content_length)
        try:
            body_text = raw_body.decode("utf-8")
            request = json.loads(body_text)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            logger.error(f"JSON 解析失败: {e}")
            error_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": JsonRpcError.PARSE_ERROR.value,
                    "message": f"Parse error: {str(e)}",
                },
            }
            body = json.dumps(error_resp, ensure_ascii=False).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return

        logger.debug(f"收到 SSE 请求: {body_text[:200]}")

        # 处理请求（响应通过 SSE 流推送）
        _sse_dispatch_request(request, session_id)

        # 返回 202 Accepted（请求已接收，响应走 SSE）
        self.send_response(202)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_OPTIONS(self):
        """处理 CORS 预检请求"""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    # ---- SSE 写入辅助 ----

    def _write_sse_event(self, event_name: str, data: str):
        """发送一条命名事件"""
        payload = f"event: {event_name}\ndata: {data}\n\n"
        self.wfile.write(payload.encode("utf-8"))
        self.wfile.flush()

    def _write_sse_message(self, data: str):
        """发送一条默认 message 事件（JSON-RPC 响应）"""
        # data 可能是多行 JSON？不，JSON-RPC 消息是单行 JSON
        payload = f"data: {data}\n\n"
        self.wfile.write(payload.encode("utf-8"))
        self.wfile.flush()


class SSEMCPServer:
    """
    SSE 模式的 MCP Server 包装器。

    启动 HTTP 服务器，管理 SSE 会话，复用 MCPServer 的请求处理逻辑。
    """

    def __init__(self, server: "MCPServer", host: str = "0.0.0.0", port: int = 8765):
        self.mcp_server = server
        self.host = host
        self.port = port
        self._http_server: ThreadingHTTPServer | None = None

    def run(self):
        """启动 SSE HTTP 服务器（阻塞运行）"""
        global _sse_manager, _sse_server_instance
        _sse_manager = SSESessionManager()
        _sse_server_instance = self.mcp_server

        self._http_server = ThreadingHTTPServer(
            (self.host, self.port),
            MCPSSERequestHandler,
        )

        logger.info(
            f"MCP SSE Server started on {self.host}:{self.port} "
            f"(SSE endpoint: /sse, Messages endpoint: /messages)"
        )
        sys.stderr.flush()

        try:
            self._http_server.serve_forever()
        except KeyboardInterrupt:
            logger.info("收到中断信号，退出 SSE 服务器")
        except Exception as e:
            logger.exception(f"SSE 服务器异常: {e}")
        finally:
            if self._http_server:
                self._http_server.server_close()
            # 清理所有会话
            if _sse_manager:
                # 由于 session 可能仍在运行，标记关闭
                pass


# ==========================================================================
# 工具实现（业务逻辑）
# ==========================================================================

def tool_read_brief(date: str = "") -> str:
    """
    读取指定日期的财经早报完整内容。

    从 GitHub Pages 上获取早报 HTML，解析提取今日头条、财经要闻、
    供应链金融和票据利率概览，以 Markdown 格式返回。
    """
    target_date = date if date else _today_str()

    try:
        # 尝试获取指定日期的 HTML
        url = f"{BRIEF_BASE_URL}/{target_date}.html"
        html = _fetch_text(url)
    except Exception:
        # 失败则尝试今日（index.html）
        try:
            url = f"{BRIEF_BASE_URL}/index.html"
            html = _fetch_text(url)
        except Exception as e:
            return f"❌ 获取早报失败: {str(e)}"

    data = parse_brief_html(html)
    # 如果没有解析出日期，用传入的日期
    if not data.get("date"):
        data["date"] = target_date

    return format_brief_markdown(data)


def tool_get_analytics(date: str = "", days: int = 7) -> str:
    """
    查询财经早报的埋点统计数据。

    从飞书多维表格读取埋点数据，计算并返回 PV、UV、新闻曝光/点击次数、
    点击率、平均停留时长和热门文章排行。

    注意: 需要配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET 环境变量。
    """
    target_date = date if date else _today_str()

    try:
        token = get_feishu_tenant_token()
    except Exception as e:
        return f"❌ 获取飞书凭证失败: {str(e)}"

    try:
        records = fetch_bitable_records(token, target_date)
    except Exception as e:
        return f"❌ 查询埋点数据失败: {str(e)}"

    if not records:
        return f"⚠️ {target_date} 暂无埋点数据（可能早报尚未发布或无访问记录）"

    stats = analyze_tracking_records(records)
    return format_analytics_markdown(target_date, stats)


def tool_check_quality(date: str = "") -> str:
    """
    检查指定日期早报的内容质量。

    包含三项检查：
    1. 链接存活率：并发检查所有新闻链接是否可正常访问
    2. 标题重复率：与过去 7 天早报标题对比，检测重复内容
    3. 内容覆盖率：检查头条、财经要闻、供应链金融各板块是否完整
    """
    target_date = date if date else _today_str()

    try:
        url = f"{BRIEF_BASE_URL}/{target_date}.html"
        html = _fetch_text(url)
    except Exception:
        try:
            url = f"{BRIEF_BASE_URL}/index.html"
            html = _fetch_text(url)
        except Exception as e:
            return f"❌ 获取早报失败: {str(e)}"

    data = parse_brief_html(html)

    # 三项检查
    link_health = check_link_health(html)
    dedup = check_title_duplicates(data, html, history_days=7)
    coverage = check_content_coverage(data)

    report = {
        "date": target_date,
        "dedup": dedup,
        "link_health": link_health,
        "coverage": coverage,
    }

    return format_quality_markdown(target_date, report)


def tool_get_bill_rates(date: str = "") -> str:
    """
    获取指定日期的票据利率数据。

    从早报 HTML 中解析票据利率表格，包含国股、大商、城农商等不同类型票据
    在各到期月份的直贴利率报价。
    """
    target_date = date if date else _today_str()

    try:
        url = f"{BRIEF_BASE_URL}/{target_date}.html"
        html = _fetch_text(url)
    except Exception:
        try:
            url = f"{BRIEF_BASE_URL}/index.html"
            html = _fetch_text(url)
        except Exception as e:
            return f"❌ 获取早报失败: {str(e)}"

    bill_data = _parse_bill_rate_table(html)
    return format_bill_rates_markdown(bill_data, target_date)


# ==========================================================================
# Server 构建与入口
# ==========================================================================

def build_server() -> MCPServer:
    """构建并注册所有工具的 MCP Server"""
    server = MCPServer()

    # read_brief
    server.register_tool(
        name="read_brief",
        handler=tool_read_brief,
        description=(
            "读取指定日期的财经早报完整内容，包含今日头条、财经要闻、"
            "供应链金融和票据利率概览，以 Markdown 格式返回。"
            "参数 date: 目标日期 YYYY-MM-DD，留空则读取今日。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "目标日期，格式 YYYY-MM-DD。留空则读取今日早报。",
                },
            },
        },
    )

    # get_analytics
    server.register_tool(
        name="get_analytics",
        handler=tool_get_analytics,
        description=(
            "查询财经早报的埋点统计数据。"
            "**重要**：PV（页面浏览量）仅统计 page_view 事件，不等于总事件数。"
            "总事件数包含 news_exposure/heartbeat/news_click 等所有类型。"
            "返回数据包含 PV、UV、新闻曝光/点击次数、点击率、平均停留时长和热门文章排行。"
            "需要配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET 环境变量。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "统计目标日期，格式 YYYY-MM-DD。留空则统计今日。",
                },
                "days": {
                    "type": "integer",
                    "description": "暂未使用（保留参数，当前仅返回单日统计）。",
                    "default": 7,
                },
            },
        },
    )

    # check_quality
    server.register_tool(
        name="check_quality",
        handler=tool_check_quality,
        description=(
            "检查指定日期早报的内容质量，包含链接存活率、标题重复率、"
            "内容覆盖率三项检查，返回综合评分和详情。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "目标日期，格式 YYYY-MM-DD。留空则检查今日早报。",
                },
            },
        },
    )

    # get_bill_rates
    server.register_tool(
        name="get_bill_rates",
        handler=tool_get_bill_rates,
        description=(
            "获取指定日期的票据利率数据，包含国股、大商、城农商等不同类型票据"
            "在各到期月份的直贴利率报价，以 Markdown 表格返回。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "目标日期，格式 YYYY-MM-DD。留空则获取今日数据。",
                },
            },
        },
    )

    return server


def _parse_args():
    """
    解析命令行参数。
    为了最小依赖和 Python 3.9 兼容，手写参数解析（不使用 argparse 也可，
    但 argparse 是标准库，直接使用更清晰）。
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="财经早报 MCP Server（支持 stdio 和 SSE 两种传输模式）",
    )
    parser.add_argument(
        "--sse",
        action="store_true",
        help="以 SSE HTTP 模式启动（默认 stdio 模式）",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="SSE 模式下的监听地址（默认 0.0.0.0）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="SSE 模式下的监听端口（默认 8765）",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    server = build_server()

    if args.sse:
        sse_server = SSEMCPServer(server, host=args.host, port=args.port)
        sse_server.run()
    else:
        server.run()
