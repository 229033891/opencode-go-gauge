"""OpenCode Go 官方分模型月度额度 (运营方随文档公布).

数据获取策略 (三级):
1. 运行时解析官方文档页 https://opencode.ai/v2/docs/console/go 的限额表
   (页面自带 Model -> Model ID 对照表, 并区分 Go / Go Plus 两套限额);
2. 本地缓存 (data_dir/model_limits_cache.json, 24h TTL);
3. 内置快照 (FALLBACK_LIMITS, 离线兜底, 版本足已注明).

官方口径: 每模型的 5 小时额度 = 月额度 x 20%, 周额度 = 月额度 x 50%.
注意: 官方窗口记账附模型系数 (可视费用 x 60/模型月额度), 本数据仅用于
规划对比, 以 /console/api/go/status 的窗口百分比为准.
"""
from __future__ import annotations

import json
import html as _htmlmod
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Optional

DOCS_URL = "https://opencode.ai/v2/docs/console/go"
DOCS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
DOCS_TIMEOUT = 30.0
CACHE_TTL = 24 * 3600.0
CACHE_SCHEMA = 2  # 缓存结构版本: 不匹配时自动重新解析 (v2 起含单价字段)

FALLBACK_ASO = "2026-10-15"

# 内置快照: 模型ID -> {"go": 月额度$, "plus": 月额度$} (None = Unlimited)
_FALLBACK_RAW: dict[str, dict[str, Any]] = {
    "glm-5.3-flash": {"go": 60, "plus": 180},
    "glm-5.3": {"go": 15, "plus": 120},
    "glm-5.2": {"go": 60, "plus": 180},
    "kimi-k3": {"go": 15, "plus": 60},
    "kimi-k2.7-code": {"go": 60, "plus": 180},
    "kimi-k2.6": {"go": 60, "plus": 240},
    "longcat-2.0": {"go": 60, "plus": 240},
    "longcat-2.5-preview-free": {"go": None, "plus": None},
    "step-5-preview-free": {"go": None, "plus": None},
    "cat-jam-free": {"go": None, "plus": None},
    "mimo-v2.6-flash": {"go": 60, "plus": 120},
    "mimo-v2.6-pro": {"go": 15, "plus": 60},
    "mimo-v2.5": {"go": 60, "plus": 120},
    "mimo-v2.5-pro": {"go": 15, "plus": 60},
    "minimax-m3": {"go": 60, "plus": 180},
    "minimax-m2.7": {"go": 60, "plus": 240},
    "muse-spark-1.3-contributor": {"go": 60, "plus": 120},
    "muse-spark-1.2-contributor": {"go": 60, "plus": 120},
    "qwen3.8-max": {"go": 15, "plus": 60},
    "qwen3.8-flash": {"go": 30, "plus": 90},
    "qwen3.7-plus": {"go": 60, "plus": 180},
    "deepseek-v4.1-flash": {"go": 80, "plus": 160},
    "deepseek-v4-pro": {"go": 15, "plus": 60},
    "deepseek-v4-flash": {"go": 30, "plus": 120},
    "deepseek-v4-flash-vision-exp": {"go": 15, "plus": 60},
    "hy4-preview": {"go": 30, "plus": 120},
    "hy3": {"go": 60, "plus": 240},
    "space-bunny": {"go": 30, "plus": 120},
    "grok-4.7": {"go": 15, "plus": 60},
    "grok-4.6": {"go": 15, "plus": 60},
    "gpt-6-luna": {"go": 15, "plus": 60},
    "gpt-5.6-luna": {"go": 15, "plus": 60},
    "claude-haiku-5-5": {"go": 15, "plus": 60},
    "gemini-4-argon-contributor": {"go": 60, "plus": 240},
    "omen-alpha": {"go": None, "plus": None},
}

FALLBACK_LIMITS = {
    mid: {"name": mid, "go": v.get("go"), "plus": v.get("plus"),
          "price_input": None, "price_output": None,
          "price_cached_read": None, "price_cached_write": None}
    for mid, v in _FALLBACK_RAW.items()
}

_cache_path_override: Optional[str] = None


def set_cache_path(path: str) -> None:
    global _cache_path_override
    _cache_path_override = path


def _cache_path() -> str:
    if _cache_path_override:
        return _cache_path_override
    from . import db  # 延迟导入避免循环依赖
    return os.path.join(db.data_dir(), "model_limits_cache.json")


def _unstrip(s: str) -> str:
    return re.sub(r"\s+", " ", _htmlmod.unescape(re.sub(r"<[^>]+>", "", s))).strip()


def _parse_amount(cell: str) -> Optional[float]:
    """'Unlimitedlimited time' -> None; '$60' -> 60; '$30 $602x ...' -> 60 (促销取后值)."""
    if not cell or "unlimited" in cell.lower():
        return None
    cleaned = re.sub(r"2x", " ", cell)
    amounts = [float(a) for a in re.findall(r"\$?(\d+(?:\.\d+)?)", cleaned)]
    if not amounts:
        return None
    return amounts[-1]


def _parse_price(cell: str) -> Optional[float]:
    """单价单元格 -> USD/1M tokens; 'Free' -> 0.0, '-'/空 -> None."""
    if not cell:
        return None
    text = cell.strip()
    low = text.lower()
    if low.startswith("free"):
        return 0.0
    if low in ("-", "–", "—", "n/a"):
        return None
    m = re.search(r"\$?\s*(\d+(?:\.\d+)?)", text)
    return float(m.group(1)) if m else None


def _alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def parse_docs_limits(html: str, min_rows: int = 15) -> dict[str, dict[str, Any]]:
    """从文档 HTML 解析限额表 -> {modelID: {"name", "go", "plus", "input", "output", "cached_read"}}."""
    rows: list[list[str]] = []
    for tr in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        cells = [_unstrip(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        if cells:
            rows.append(cells)

    lim_header = {"model", "input", "output", "cached read", "cached write", "monthly limit"}
    end_header = {"model", "model id", "endpoint", "ai sdk package"}

    # 文档名 -> 官方 Model ID (仅在 endpoint 对照表中收集)
    name2id: dict[str, str] = {}
    in_end_table = False
    for cells in rows:
        low = {c.strip().lower() for c in cells}
        if low == end_header:
            in_end_table = True
            continue
        if in_end_table:
            if len(cells) < 4:  # 进入下一张表
                in_end_table = False
                continue
            if len(cells) >= 3 and cells[2].startswith("http"):
                name2id[cells[0]] = cells[1].strip()

    def resolve_mid(doc_name: str) -> str:
        if doc_name in name2id:
            return name2id[doc_name]
        base = re.sub(r"\(.*?\)", "", doc_name).strip()  # 去掉 "(≤ 256K tokens)" 等变体后缀
        if base in name2id:
            return name2id[base]
        return _alnum(doc_name)

    lim_tables: list[list[list[str]]] = []
    for i, cells in enumerate(rows):
        if {c.strip().lower() for c in cells} == lim_header:
            lim_tables.append(rows[i + 1:])
    limits_out: dict[str, dict[str, Any]] = {}
    for plan, group in zip(("go", "plus"), lim_tables[:2]):
        for r in group:
            if len(r) < 6:
                continue
            name, limit_cell = r[0], r[5]
            if name == "Model" or limit_cell == "Monthly limit":  # 嵌套表头
                continue
            mid = resolve_mid(name)
            if plan == "go" and mid in limits_out:
                continue  # 同一月额度的 <=/>= tokens 变体只保留首条
            entry = limits_out.setdefault(mid, {})
            entry[plan] = _parse_amount(limit_cell)
            if entry[plan] is None and "unlimited" in limit_cell.lower():
                entry["unlimited"] = True  # 有限额表中的 Unlimited (免费模型)
            if plan == "go":
                entry["name"] = name
                entry["price_input"] = _parse_price(r[1])
                entry["price_output"] = _parse_price(r[2])
                entry["price_cached_read"] = _parse_price(r[3])
                entry["price_cached_write"] = _parse_price(r[4])
    valid = {k: v for k, v in limits_out.items()
             if k != "model" and (v.get("go") is not None or v.get("plus") is not None
                                  or v.get("unlimited"))}
    if len(valid) < min_rows:
        raise ValueError(f"解析结果异常 (仅 {len(valid)} 条)")
    return valid


def _fetch_html() -> str:
    req = urllib.request.Request(DOCS_URL, headers={"User-Agent": DOCS_UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=DOCS_TIMEOUT) as resp:
        return resp.read(10 << 20).decode("utf-8", errors="replace")


def _read_cache(cache: str, min_count: int = 3) -> Optional[dict[str, Any]]:
    try:
        with open(cache, encoding="utf-8") as fh:
            data = json.load(fh)
        if (isinstance(data.get("limits"), dict) and len(data["limits"]) >= min_count
                and data.get("schema") == CACHE_SCHEMA):
            return data
    except (OSError, ValueError):
        pass
    return None


def load_limits(force: bool = False) -> dict[str, Any]:
    """返回 {"limits": {...}, "fetched": iso, "source": str}; 顺序 快照 <- 缓存 <- 在线解析."""
    cache = _cache_path()
    if not force:
        cached = _read_cache(cache)
        if cached and cached.get("_at", 0) > time.time() - CACHE_TTL:
            return {k: v for k, v in cached.items() if k != "_at"} | {
                "source": f"cache ({cached.get('fetched', '')[:10]})",
            }
    try:
        html = _fetch_html()
        limits = parse_docs_limits(html)
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        payload = {"limits": limits, "fetched": now_iso, "url": DOCS_URL,
                   "schema": CACHE_SCHEMA, "_at": time.time()}
        try:
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
        except OSError:
            pass
        payload["source"] = f"docs ({DOCS_URL})"
        return {k: v for k, v in payload.items() if k != "_at"}
    except Exception as exc:  # noqa: BLE001 - 文档页/网络不可用时回退
        cached = _read_cache(cache)
        if cached:
            return {k: v for k, v in cached.items() if k != "_at"} | {"source": "cache (在线解析失败回退)"}
        return {"limits": FALLBACK_LIMITS, "asof": FALLBACK_ASO,
                "source": f"内置快照 {FALLBACK_ASO} (在线解析失败: {exc})"}


def match_limit_row(limits: dict[str, dict[str, Any]], model_id: str) -> Optional[dict[str, Any]]:
    """模型ID (可能带请求端后缀) -> 限额条目; 精确匹配 -> 去后缀 -> 字母数字模糊."""
    row = limits.get(model_id)
    if row:
        return row
    base = re.split(r"[-/]", model_id or "")[0]
    nid = _alnum(model_id)
    for k, v in limits.items():
        if _alnum(k) == nid:
            return v
    for k, v in limits.items():  # kimi-k2.7-code -> "kimi-k2.7"; 请求端可能带变体后缀
        if base and (model_id.startswith(k) or k.startswith((model_id, base))):
            kw = _alnum(k)
            if kw and (kw.startswith(nid) or nid.startswith(kw)):
                return v
    return None


def plan_limit(entry: Optional[dict[str, Any]], plan: str = "go") -> Optional[float]:
    """计划 (go/plus) 月度额度; 条目缺失时 None (未知), None 值表示 Unlimited."""
    if entry is None:
        return None  # 未知模型 (不在官方限额表)
    return entry.get("go" if plan != "plus" else "plus")
