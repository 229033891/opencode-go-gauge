"""本地 HTTP 服务: 静态资源 + JSON API + 后台同步."""
from __future__ import annotations

import json
import mimetypes
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from . import __version__, db, limits
from .updater import RELEASE_PAGE_URL, check_update
from .opencode_api import (
    AuthError,
    OpenCodeAPIError,
    fetch_key_names,
    fetch_quota,
    fetch_usage_page,
    iso_to_ms,
    quota_pool,
    resolve_workspace_id,
)

PAGE_LIMIT = 100  # /request-logs 单页条数上限
QUOTA_CACHE_TTL = 30.0
INCREMENTAL_PAGES = 10  # 增量同步最多翻页数 (10*100=1000 条)
MAX_FULL_PAGES = 1000  # 全量同步翻页上限, 防失控 (服务端仅保留 30 天明细)
PAGE_RETRIES = 2  # 单页失败重试次数 (深页偶尔慢/连接被重置)
INCREMENTAL_OVERLAP_MS = 3600_000  # 增量同步回溯 1 小时, 防边界漏记


def _resource_path(rel: str) -> str:
    """定位资源文件 (开发/打包后通用)."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        return os.path.join(base, "app", "web", rel)
    return os.path.join(os.path.dirname(__file__), "web", rel)


# ---------------------------------------------------------------------------
# 同步状态 (跨线程)
# ---------------------------------------------------------------------------

_sync_lock = threading.Lock()
_sync_state: dict[str, Any] = {
    "running": False,
    "mode": "",
    "page": 0,
    "inserted": 0,
    "phase": "idle",  # idle | quota | usage | done | error
    "message": "",
    "account": "",  # 当前正在同步的账号名 (多账号顺序轮询)
}
_quota_cache: dict[int, dict[str, Any]] = {}  # {account_id: {"at": float, "data": ...}}
_quota_refreshing: set[int] = set()  # 防重入: 同一账号同一时刻只允许一个 quota 刷新线程
_exchange_cache: dict[str, Any] = {"at": 0.0, "usd_cny": 7.2}
_EXCHANGE_TTL = 6 * 3600  # 汇率缓存 6 小时
_DEFAULT_USD_CNY = 7.2


def _fetch_usd_cny() -> float:
    """从 open.er-api.com 获取 USD→CNY 汇率, 失败时返回上次缓存/默认值."""
    now = time.time()
    if now - _exchange_cache["at"] < _EXCHANGE_TTL:
        return _exchange_cache["usd_cny"]
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://open.er-api.com/v6/latest/USD",
            headers={"User-Agent": "GoGauge/1.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        rate = float(data.get("rates", {}).get("CNY") or 0)
        if rate > 0:
            _exchange_cache.update(at=now, usd_cny=rate)
    except Exception:  # noqa: BLE001 网络失败时保留旧值
        _exchange_cache["at"] = now
    return _exchange_cache["usd_cny"]


def _sync_progress_snapshot() -> dict[str, Any]:
    with _sync_lock:
        return dict(_sync_state)


def _set_phase(phase: str, message: str = "") -> None:
    with _sync_lock:
        _sync_state["phase"] = phase
        _sync_state["message"] = message
        _sync_state["running"] = phase in ("quota", "usage")


# ---------------------------------------------------------------------------
# 分模型额度规划 (/api/model-quota)
# ---------------------------------------------------------------------------

_WINDOW_DEFS = (
    # (窗口 key, 期限秒, 官方额度占比)
    ("5h", 5 * 3600, 0.20),
    ("week", 7 * 24 * 3600, 0.50),
    ("month", 30 * 24 * 3600, 1.00),
)
_LABEL2KEY = {"5h Rolling": "5h", "Weekly": "week", "Monthly": "month"}

# 共享池月度额度兜底值 (登录后一律以 /go/status 的 month meter 为准)
# Go 套餐实测 $60; Go Plus 约 4x
_DEFAULT_POOL_MONTHLY = {"go": 60.0, "plus": 240.0}


def _compute_window_starts(quota: dict[str, Any]) -> dict[str, Optional[str]]:
    """每个额度窗口的统计起点 (ISO 或 None=不限).

    优先用接口 meter.startsAt, 其次 resetAt 减去固定时长;
    月窗口额外优先用 access.startsAt (订阅周期真实起点) —— 订阅周期不一定是
    30 天 (如 10-15 -> 11-15 实为 31 天), 用 reset-30d 会漏掉周期开头的用量。
    新订阅 (起点在未来) 回退为不限.
    """
    now = datetime.now(timezone.utc)
    starts: dict[str, Optional[str]] = {"5h": None, "week": None, "month": None}
    for w in (quota or {}).get("windows") or []:
        key = _LABEL2KEY.get(w.get("label") or "")
        if not key:
            continue
        duration = {"5h": 5 * 3600, "week": 7 * 86400, "month": 30 * 86400}[key]
        start: Optional[str] = None
        if key == "month" and quota.get("period_start"):
            start = quota["period_start"]
        elif w.get("starts_at"):
            start = w["starts_at"]
        elif w.get("reset_at"):
            try:
                dt = datetime.fromisoformat(str(w["reset_at"]).replace("Z", "+00:00"))
                start = (dt - timedelta(seconds=duration)).isoformat().replace("+00:00", "Z")
            except ValueError:
                start = None
        try:
            if start and datetime.fromisoformat(start.replace("Z", "+00:00")) > now:
                start = None  # 新订阅锚点在未来 -> 全量统计
        except ValueError:
            start = None
        starts[key] = start
    if not any(starts.values()):
        # 未登录/无配额时: 短窗口用本地时间, 月窗口不限 (避免把历史全当成月度)
        starts["5h"] = (now - timedelta(hours=5)).isoformat().replace("+00:00", "Z")
        starts["week"] = (now - timedelta(days=7)).isoformat().replace("+00:00", "Z")
        starts["month"] = None
    return starts


def _current_quota() -> tuple[Optional[int], dict[str, Any], dict[str, Any]]:
    """当前活跃账号的 (account_id, 配额快照, 共享池)。

    - account_id 为 None 表示未登录/无凭证。
    - 已登录但取配额失败时仍返回 account_id, 由 pool["available"] 为 False
      表示"无额度数据", 调用方据此区分"未登录"与"无额度数据"。
    """
    aid = db.get_active_account_id()
    token = db.get_token()
    if not aid or not token:
        return None, {}, quota_pool(None)
    _, workspace_hint = db.get_account_credentials(aid)
    quota = _fetch_quota_with_cache(aid, token, workspace_hint) or {}
    return aid, quota, quota_pool(quota)


def _build_model_quota(query: dict[str, list[str]]) -> dict[str, Any]:
    """分模型 用量 vs 共享额度池 清单.

    每行: 模型 + 5h/周/月 {零售费用, 对共享池的估算占用, 占池上限%} + 请求数与 token,
    用于"这个模型在吃多少共享池, 该不该切模型"的规划。
    """
    plan = (query.get("plan", ["go"])[0] or "go").lower()
    if plan not in ("go", "plus"):
        plan = "go"
    refresh_limits = (query.get("refresh_limits", ["0"])[0] or "0") == "1"
    # key_ids: 逗号分隔; 空串/缺省 = 全部 (不筛 Key)
    raw_keys = (query.get("key_ids", [""])[0] or "").strip()
    key_ids = [p.strip() for p in raw_keys.split(",") if p.strip()] if raw_keys else []
    # scope: go (仅 Go 套餐用量, 默认) / all (全部产品线, 含免费模型)
    scope = (query.get("scope", ["go"])[0] or "go").lower()
    if scope not in ("go", "all"):
        scope = "go"
    products = ["go"] if scope == "go" else None

    doc = limits.load_limits(force=refresh_limits)
    limit_map = doc.get("limits") or {}

    aid, quota, pool = _current_quota()
    starts = _compute_window_starts(quota)
    # 共享池未拿到真实金额时 (未登录/无额度数据), 退化为文档默认档位
    pool_monthly = pool.get("monthly_limit_usd") or _DEFAULT_POOL_MONTHLY.get(plan, 60.0)
    if not pool.get("available"):
        pool["monthly_limit_usd"] = pool_monthly
        pool["windows"] = {
            key: {"limit_usd": round(pool_monthly * frac, 2)}
            for key, _dur, frac in _WINDOW_DEFS
        }
        pool["note"] = "未登录/无配额数据: 窗口上限为文档默认推算值, 已用需以登录后为准"

    # 每窗口分模型用量 (5h/周/月), 按 Key 与产品线筛选 (空 = 全部)
    keys_filter = key_ids or None
    usage: dict[str, dict[str, dict[str, Any]]] = {"5h": {}, "week": {}, "month": {}}
    for key, since in starts.items():
        usage[key] = {
            r["model"]: r
            for r in db.model_stats_since(since, aid, key_ids=keys_filter, products=products)
        }

    model_ids: list[str] = []
    for w in usage.values():
        for mid in w:
            if mid not in model_ids:
                model_ids.append(mid)

    # 本账号未使用、但官方额度表中存在的模型也进表 (值为 0, 置于表尾),
    # 让用户在切换前就能看到备选模型的额度档位
    official_only: list[str] = []
    matched_entries: set[int] = set()
    for mid in model_ids:
        entry = limits.match_limit_row(limit_map, mid)
        if entry is not None:
            matched_entries.add(id(entry))
    for mid, entry in limit_map.items():
        if id(entry) not in matched_entries and mid not in model_ids:
            official_only.append(mid)

    def _row_for(mid: str) -> dict[str, Any]:
        entry = limits.match_limit_row(limit_map, mid)
        monthly = limits.plan_limit(entry, plan)
        # 额度消耗系数 (估算): 官方口径 额度消耗 = 零售费用 x (共享池月额度 / 该模型档位)
        # 免费模型优先判定: 请求端 -free 后缀, 或官方表中无月额度 (Unlimited)
        # -> 0 (不进共享额度池; 勿用 0.1 假装 $600 档)
        go_limit = (entry or {}).get("go")
        is_free = "-free" in mid.lower() or (entry is not None and go_limit is None)
        if is_free:
            coefficient = 0.0
        elif isinstance(go_limit, (int, float)) and go_limit > 0 and pool_monthly:
            coefficient = round(pool_monthly / go_limit, 2)
        else:
            coefficient = None
        windows_out: dict[str, Any] = {}
        max_pct = 0.0
        total_req = 0
        for key, _dur, _frac in _WINDOW_DEFS:
            u = usage[key].get(mid)
            used_cost = float(u["total_cost_usd"]) if u else 0.0
            used_req = int(u["request_count"]) if u else 0
            total_req += used_req
            used_tokens = (int(u["total_input_tokens"] or 0) + int(u["total_output_tokens"] or 0)
                           + int(u["total_reasoning_tokens"] or 0)) if u else 0
            # 共享池该窗口的上限/已用 (全账号口径, 与用哪个模型无关) —— 来自 /go/status 原始美元
            pw = pool["windows"].get(key) or {}
            pool_limit = pw.get("limit_usd")
            pool_used = pw.get("used_usd")
            # 本模型对共享池的估算占用
            pool_cost = used_cost * coefficient if coefficient else 0.0
            if pool_limit:
                # 已知池上限即可算出占比 (零用量 = 0%); 上限未知才留空
                share_pct = round(pool_cost / pool_limit * 100.0, 2)
            else:
                share_pct = None
            if share_pct is not None:
                max_pct = max(max_pct, share_pct)
            windows_out[key] = {
                "requests": used_req,
                "tokens": used_tokens,
                "cost_usd": round(used_cost, 6),       # 零售 token 计费
                "pool_cost_usd": round(pool_cost, 6),  # 对共享池的估算占用
                "pool_limit_usd": pool_limit,          # 共享池该窗口上限 (同池同值)
                "pool_used_usd": pool_used,            # 共享池该窗口全账号已用
                "pool_share_pct": share_pct,           # 占用 / 池上限
            }
        # 共享池是全账号共用的: 只有池有"剩余", 单个模型没有独立余额。
        # 估算剩余次数 = 池剩余 ÷ (月均零售费用 x 系数) —— 按本模型当前速率推演。
        u_month = usage["month"].get(mid)
        used_month = float(u_month["total_cost_usd"]) if u_month else 0.0
        used_req_month = int(u_month["request_count"]) if u_month else 0
        avg_cost_per_req = (
            used_month / used_req_month if used_req_month > 0 and used_month > 0 else None
        )
        pool_remaining = (pool["windows"].get("month") or {}).get("remaining_usd")
        est_remaining_requests = None
        if pool_remaining is not None and avg_cost_per_req and coefficient:
            per_req_pool = avg_cost_per_req * coefficient
            if per_req_pool > 0:
                est_remaining_requests = int(pool_remaining / per_req_pool)  # 向下取整偏保守
        status = ("danger" if max_pct >= 80 else ("warn" if max_pct >= 50 else "ok"))
        if total_req == 0:
            status = "idle"  # 零用量 (官方表内模型, 本账号未使用)
        return {
            "model": mid,
            "name": (entry or {}).get("name") or mid,
            "limit_known": entry is not None,
            "is_free": bool(is_free),
            "doc_monthly_limit_usd": (round(monthly, 2) if isinstance(monthly, (int, float)) else None),
            "month_requests_used": used_req_month,
            "avg_cost_per_request": (
                round(avg_cost_per_req, 6) if avg_cost_per_req is not None else None
            ),
            "est_remaining_requests": est_remaining_requests,
            "est_sample_small": used_req_month > 0 and used_req_month < 5,
            "coefficient": coefficient,
            "price_input": (entry or {}).get("price_input"),
            "price_output": (entry or {}).get("price_output"),
            "price_cached_read": (entry or {}).get("price_cached_read"),
            "price_cached_write": (entry or {}).get("price_cached_write"),
            "windows": windows_out,
            "max_pct": round(max_pct, 1),
            "status": status,
        }

    rows = [_row_for(mid) for mid in model_ids]
    rows.extend(_row_for(mid) for mid in official_only)
    # 高危在前 -> 偏紧 -> 富余 -> 零用量; 同档内按月费用降序
    rank = {"danger": 0, "warn": 1, "ok": 2, "idle": 3}
    rows.sort(key=lambda r: (rank[r["status"]], -r["windows"]["month"]["cost_usd"]))

    # Key 目录 (账户历史出现过的 Key, 与时间窗无关; 未归属哨兵放最后)
    listed = db.list_usage_key_ids(aid) if aid else {"keys": [], "has_unassigned": False}
    names = db.get_key_names()
    filter_keys = [
        {"id": kid, "name": _display_key_name(kid, names)}
        for kid in listed["keys"]
    ]
    filter_keys.sort(key=lambda x: (x["name"].lower(), x["id"]))
    if listed.get("has_unassigned"):
        filter_keys.append({"id": db.NONE_KEY_SENTINEL, "name": "未归属"})

    return {
        "plan": plan,
        "scope": scope,
        "logged_in": aid is not None,
        "quota_available": bool(quota.get("success")),
        "quota_error": (quota.get("error") if isinstance(quota, dict) else None),
        "quota_mode": "shared_pool",
        "pool": pool,
        "window_starts": starts,
        "limits_source": doc.get("source"),
        "limits_fetched": doc.get("fetched") or doc.get("asof"),
        "filter": {"key_ids": key_ids, "keys": filter_keys},
        "note": ("usage_filtered_by_keys_limits_account_wide" if key_ids
                 else "usage_limits_account_wide"),
        "models": rows,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _build_go_raw_status() -> dict[str, Any]:
    """只读: 直接透出 /go/status 的共享额度池真实美元金额.

    这是账单真值; /api/model-quota 的分模型占用是在此基础上的估算拆解。
    """
    aid, quota, pool = _current_quota()
    if aid is None:
        return {"ok": False, "error": "未配置 token", "pool": pool}
    return {
        "ok": bool(pool.get("available")),
        "name": quota.get("name"),
        "workspace_id": quota.get("workspace_id"),
        "updated_at": quota.get("updated_at"),
        "error": quota.get("error"),
        "pool": pool,
    }


def _display_key_name(kid: str, names: dict[str, str]) -> str:
    """Key 显示名: 优先 keys 页同步的缓存名, 否则短尾号."""
    cached = (names or {}).get(kid)
    if cached:
        return cached
    kid = str(kid or "")
    return ("…" + kid[-8:]) if len(kid) > 8 else kid


# ---------------------------------------------------------------------------
# 同步执行
# ---------------------------------------------------------------------------

def _fetch_quota_with_cache(account_id: int, token: str, workspace_hint: str) -> dict[str, Any]:
    slot = _quota_cache.setdefault(account_id, {"at": 0.0, "data": None})
    now = time.time()
    if slot["data"] and now - slot["at"] < QUOTA_CACHE_TTL:
        return slot["data"]
    result = fetch_quota(token, workspace_hint)
    slot["at"] = now
    slot["data"] = result.to_dict()
    return slot["data"]


def _ensure_quota_async(account_id: Optional[int] = None) -> None:
    """若该账号配额缓存过期, 在后台线程刷新 (不阻塞 dashboard 响应, 防重入).

    支持任意账号 (非活跃账号凭证从 accounts 表直接读取, 供账户总览面板使用).
    """
    aid = account_id or db.get_active_account_id()
    if not aid:
        return
    slot = _quota_cache.get(aid)
    now = time.time()
    if slot and slot["data"] and now - slot["at"] < QUOTA_CACHE_TTL:
        return
    if aid in _quota_refreshing:
        return  # 该账号已有刷新线程在跑
    token, workspace_hint = db.get_account_credentials(aid)
    if not token:
        return
    _quota_refreshing.add(aid)

    def worker() -> None:
        # fetch_quota 内部已捕获全部异常并返回 success=False 的结果, 不会向上抛;
        # 失败同样会写入 _quota_cache, 因此 TTL 内不重试, 避免前端无限刷新。
        try:
            _fetch_quota_with_cache(aid, token, workspace_hint)
        finally:
            _quota_refreshing.discard(aid)

    threading.Thread(target=worker, daemon=True, name="gousage-quota").start()


def _sync_one_account(
    account_id: int, name: str, mode: str, window_days: Optional[int]
) -> dict[str, Any]:
    """同步单个账号的用量记录 (原单账号逻辑, 显式传入账号上下文).

    新接口 /console/api/request-logs 为游标分页 (按时间倒序), 无法并发跳页,
    改为顺序翻页: 每页 100 条, 直到游标耗尽 / 触达同步范围边界 / 连续空页.
    """
    token = db.get_db().execute(
        "SELECT token, workspace_id, resolved_workspace_id FROM accounts WHERE id = ?",
        (account_id,),
    ).fetchone()
    if token is None or not token["token"].strip():
        return {"ok": False, "error": "未登录"}
    token_str = token["token"].strip()
    workspace_id = token["resolved_workspace_id"] or token["workspace_id"] or "Default"

    with _sync_lock:
        _sync_state.update(account=name)

    try:
        # 确保工作区 ID 已解析
        try:
            resolved = resolve_workspace_id(workspace_id, token_str)
            if not workspace_id.startswith("wrk_"):
                workspace_id = resolved
                db.save_resolved_workspace(resolved, account_id)
        except (AuthError, OpenCodeAPIError) as exc:
            _set_phase("error", f"[{name}] 工作区解析失败: {exc}")
            db.update_sync_state("error", str(exc), 0, account_id)
            return {"ok": False, "error": str(exc)}

        total_inserted = 0
        max_pages = MAX_FULL_PAGES if mode == "full" else INCREMENTAL_PAGES
        page = 0
        cursor: Optional[str] = None
        empty_pages = 0
        failed_pages = 0
        page_retries = 0
        window_boundary_reached = False
        retention_days: Optional[int] = None

        # 增量同步: 只取最新记录之后的一段时间 (重叠 1 小时防边界漏记),
        # 命中不到旧数据就不会去翻历史页, 既快又少请求
        since_ms: Optional[int] = None
        if mode != "full":
            newest_local = (db.get_sync_state(account_id) or {}).get("newest_record_at")
            if newest_local:
                stamp = iso_to_ms(newest_local) - INCREMENTAL_OVERLAP_MS
                since_ms = stamp if stamp > 0 else None

        while page < max_pages:
            with _sync_lock:
                _sync_state["page"] = page
            try:
                usage_page = fetch_usage_page(
                    token_str, workspace_id, cursor=cursor, limit=PAGE_LIMIT,
                    since_ms=since_ms,
                )
            except AuthError as exc:
                _set_phase("error", f"[{name}] {exc}")
                db.update_sync_state("error", str(exc), total_inserted, account_id)
                return {"ok": False, "error": str(exc), "partial_inserted": total_inserted}
            except OpenCodeAPIError as exc:
                # 首页即失败 -> 整体报错; 翻页中途失败 -> 同一游标重试, 仍失败则保留已入库数据
                if page == 0:
                    _set_phase("error", f"[{name}] {exc}")
                    db.update_sync_state("error", str(exc), 0, account_id)
                    return {"ok": False, "error": str(exc)}
                if page_retries < PAGE_RETRIES:
                    page_retries += 1
                    time.sleep(1.5 * page_retries)
                    continue
                failed_pages += 1
                break

            page += 1
            page_retries = 0
            if usage_page.retention_days:
                retention_days = usage_page.retention_days
            records = usage_page.records

            if records:
                # 同步范围: 全量拉取时, 若本页最早记录早于窗口边界 -> 该页保留后停止
                if mode == "full" and window_days is not None:
                    earliest = min(r.created_at for r in records if r.created_at)
                    if earliest:
                        try:
                            et = datetime.fromisoformat(earliest.replace("Z", "+00:00"))
                            boundary = datetime.now(timezone.utc) - timedelta(days=window_days)
                            if et < boundary:
                                window_boundary_reached = True
                        except (ValueError, TypeError):
                            pass
                inserted = db.insert_usage_records(
                    [r.to_db_dict() for r in records], account_id
                )
                total_inserted += inserted
                with _sync_lock:
                    _sync_state["inserted"] = total_inserted
                empty_pages = empty_pages + 1 if inserted == 0 else 0
            else:
                empty_pages += 1

            cursor = usage_page.next_cursor
            if not cursor:
                break  # 已到最早一条
            if window_boundary_reached:
                break
            # 增量模式: 连续两页没有新数据 -> 停止
            if mode == "incremental" and empty_pages >= 2:
                break

        # 按同步范围裁剪窗口外记录 (与本次新增数独立)
        if window_days is not None:
            db.prune_old_records(window_days, account_id)

        # 顺带刷新该账号的 key 显示名称缓存 (合并写入: key_id 全局唯一,
        # 多账号各补各的条目; 单账号失败不影响已有缓存)
        try:
            fresh_keys = fetch_key_names(token_str, workspace_id)
            merged = dict(db.get_key_names())
            merged.update(fresh_keys)
            db.save_key_names(merged)
        except Exception:  # noqa: BLE001
            pass

        if failed_pages:
            msg = "完成, 但翻页中断 (数据不完整, 可再次全量同步补全)"
            db.update_sync_state("partial", msg, total_inserted, account_id)
            return {"ok": True, "partial": True, "failed_pages": failed_pages,
                    "inserted": total_inserted, "pages": page}
        db.update_sync_state("ok", None, total_inserted, account_id)
        return {"ok": True, "inserted": total_inserted, "pages": page,
                "retention_days": retention_days}
    except Exception as exc:  # noqa: BLE001
        db.update_sync_state("error", str(exc), 0, account_id)
        return {"ok": False, "error": str(exc)}


def sync_usage(mode: str = "incremental") -> dict[str, Any]:
    """同步用量记录.

    - incremental: 顺序轮询所有已登录账号 (各账号独立游标/状态)
    - full: 仅对当前活跃账号全量拉取
    进度快照在原有字段上追加 account (当前正在同步的账号名), 前端兼容旧字段.
    """
    window_days = db.get_settings().get("window_days")

    if mode == "full":
        aid = db.get_active_account_id()
        targets = [(aid, db.get_account().get("name") or f"#{aid}")] if aid and db.get_token() else []
    else:
        targets = [
            (a["id"], a["name"]) for a in db.list_accounts() if a["has_token"]
        ]
    if not targets:
        return {"ok": False, "error": "未登录"}

    with _sync_lock:
        if _sync_state["running"]:
            return {"ok": False, "error": "已有同步任务进行中"}
        _sync_state.update(running=True, mode=mode, page=0, inserted=0, phase="usage", message="")

    try:
        total_inserted = 0
        any_error = ""
        partial = False
        pages = 0
        for aid, name in targets:
            result = _sync_one_account(aid, name, mode, window_days)
            total_inserted += int(result.get("inserted") or 0)
            pages += int(result.get("pages") or 0)
            if not result.get("ok"):
                any_error = result.get("error") or "同步失败"
                if mode == "incremental":
                    _set_phase("error", f"[{name}] {any_error}")
                    return {"ok": False, "error": any_error, "partial_inserted": total_inserted}
            if result.get("partial"):
                partial = True

        if partial or (mode != "incremental" and any_error):
            msg = "部分账号同步异常" if any_error else "完成, 但部分页面拉取失败"
            _set_phase("done", msg)
            return {"ok": True, "partial": True, "inserted": total_inserted, "pages": pages}
        _set_phase("done", f"同步完成, 新增 {total_inserted} 条")
        return {"ok": True, "inserted": total_inserted, "pages": pages}
    except Exception as exc:  # noqa: BLE001
        _set_phase("error", str(exc))
        return {"ok": False, "error": str(exc)}
    finally:
        with _sync_lock:
            _sync_state["running"] = False


def sync_all_async(mode: str) -> None:
    """后台线程执行 用量同步 (配额由独立后台线程刷新, 不阻塞用量)."""
    def worker() -> None:
        try:
            _ensure_quota_async()  # 触发配额后台刷新 (独立线程, 防重入)
            sync_usage(mode)
        except Exception:  # noqa: BLE001
            _set_phase("error", "同步失败")

    thread = threading.Thread(target=worker, daemon=True, name="gousage-sync")
    thread.start()


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------

_on_open_login: Optional[Callable[[str], None]] = None
_server: Optional[ThreadingHTTPServer] = None


def set_login_callback(callback: Callable[[str], None]) -> None:
    """由 main.py 注册: 前端请求登录时触发窗口跳转.

    回调契约: callback(mode), mode 为 "add" (添加新用户) 或 "relogin" (重新登录当前用户).
    """
    global _on_open_login
    _on_open_login = callback


def _read_json_body(handler: BaseHTTPRequestHandler) -> Any:
    """读取并解析 JSON 请求体, 失败抛 ValueError."""
    length = int(handler.headers.get("Content-Length") or 0)
    return json.loads(handler.rfile.read(length).decode("utf-8", errors="replace"))


def _json_response(handler: BaseHTTPRequestHandler, data: Any, status: int = 200) -> None:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _static_response(handler: BaseHTTPRequestHandler, rel: str) -> None:
    # 防目录穿越
    rel = rel.lstrip("/")
    if ".." in rel.replace("\\", "/").split("/"):
        handler.send_error(403)
        return
    path = _resource_path(rel)
    if not os.path.isfile(path):
        handler.send_error(404)
        return
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    try:
        with open(path, "rb") as fh:
            body = fh.read()
    except OSError:
        handler.send_error(500)
        return
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    handler.wfile.write(body)


def _handle_api(handler: BaseHTTPRequestHandler, path: str, query: dict[str, list[str]]) -> None:
    method = handler.command
    route = path

    if route == "/api/version" and method == "GET":
        _json_response(handler, {"version": __version__})

    if route == "/api/update/check" and method == "GET":
        try:
            _json_response(handler, check_update())
        except Exception as exc:  # noqa: BLE001 网络/解析失败 -> 前端提示
            _json_response(handler, {"error": str(exc)}, status=502)

    if route == "/api/update/open" and method == "POST":
        # 用系统默认浏览器打开 GitHub Releases 页 (WebView 内 window.open 不可靠)
        import webbrowser

        webbrowser.open(RELEASE_PAGE_URL)
        _json_response(handler, {"ok": True})

    if route == "/api/state" and method == "GET":
        account = db.get_account()
        sync = db.get_sync_state()
        _json_response(
            handler,
            {
                "logged_in": bool(db.count_logged_in_accounts()),
                "account": account,
                "accounts": db.list_accounts(),
                "accounts_total": db.count_accounts(),
                "accounts_logged_in": db.count_logged_in_accounts(),
                "sync": sync,
                "progress": _sync_progress_snapshot(),
                "datadir": db.data_dir(),
            },
        )
        return

    if route == "/api/sync" and method == "POST":
        mode = (query.get("mode") or ["incremental"])[0]
        if mode not in ("incremental", "full"):
            _json_response(handler, {"ok": False, "error": "invalid mode"}, 400)
            return
        # incremental 轮询所有已登录账号; full 仅作用于活跃账号 (需其 token)
        if mode == "incremental":
            if not db.count_logged_in_accounts():
                _json_response(handler, {"ok": False, "error": "未登录"}, 401)
                return
        elif not db.get_token():
            _json_response(handler, {"ok": False, "error": "未登录"}, 401)
            return
        sync_all_async(mode)
        _json_response(handler, {"ok": True})
        return

    if route == "/api/dashboard" and method == "GET":
        # 时间范围: today / 7d / 30d / all
        range_param = query.get("range", ["today"])[0]
        if range_param == "today":
            period, days = "today", 1
        elif range_param == "7d":
            period, days = "7d", 7
        elif range_param == "all":
            period, days = "all", 365
        else:
            period, days = "30d", 30
        token = db.get_token()
        # quota 使用缓存 (按账号分槽), 过期时后台刷新, 不阻塞 dashboard 响应
        active_id = db.get_active_account_id()
        _ensure_quota_async(active_id)
        slot = _quota_cache.get(active_id) or {}
        quota = slot.get("data") if token else None
        account = db.get_account()
        _json_response(
            handler,
            {
                "logged_in": bool(token),
                "account": account,
                "account_name": account.get("name", ""),
                "accounts_total": db.count_accounts(),
                "accounts_logged_in": db.count_logged_in_accounts(),
                "quota": quota,
                "totals": db.totals(period),
                "today": db.totals("today"),
                "daily": db.daily_stats(7),  # 每日趋势固定显示近 7 天
                "trend": db.daily_stats(30),  # 用量趋势 (费用/请求双轴)
                "today_trend": db.today_trend(),  # 今日 24 小时趋势
                "models": db.model_stats(period),
                "sync": db.get_sync_state(),
                "progress": _sync_progress_snapshot(),
                "range": range_param,
                "exchange_rate": {"usd_cny": _fetch_usd_cny(), "currency": "CNY"},
                "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        return

    if route == "/api/logout" and method == "POST":
        # 退出前记录账号 id, 退出后清理其配额缓存槽 (防止残留旧配额)
        aid = db.get_active_account_id()
        db.clear_account()
        if aid:
            _quota_cache.pop(aid, None)
        _json_response(handler, {"ok": True})
        return

    if route == "/api/relogin" and method == "POST":
        if _on_open_login:
            _on_open_login("relogin")
        _json_response(handler, {"ok": True})
        return

    # ---------------- 多账号管理 ----------------

    if route == "/api/accounts" and method == "GET":
        _json_response(
            handler,
            {
                "ok": True,
                "accounts": db.list_accounts(),
                "active_id": db.get_active_account_id(),
            },
        )
        return

    if route == "/api/accounts/overview" and method == "GET":
        # 账户总览面板: 仅返回已登录账号 (退出登录即移除本地数据, 未登录账号无展示意义)
        active_id = db.get_active_account_id()
        accounts: list[dict[str, Any]] = []
        for acc in db.list_accounts():
            if not acc["has_token"]:
                continue
            aid = acc["id"]
            # 过期配额后台刷新 (不阻塞响应), 本次先返回缓存值
            _ensure_quota_async(aid)
            slot = _quota_cache.get(aid)
            quota = slot.get("data") if slot else None
            sync_state = db.get_sync_state(aid)
            accounts.append(
                {
                    "id": aid,
                    "name": acc["name"],
                    "logged_in": True,
                    "active": aid == active_id,
                    "quota": quota,
                    "today": db.totals("today", aid),
                    "today_trend": db.today_trend(aid),
                    "daily7": db.daily_stats(7, aid),
                    "last_sync_at": sync_state.get("last_sync_at"),
                    "last_sync_status": sync_state.get("last_sync_status"),
                }
            )
        _json_response(
            handler,
            {
                "ok": True,
                "accounts": accounts,
                "active_id": active_id,
                "exchange_rate": {"usd_cny": _fetch_usd_cny(), "currency": "CNY"},
                "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        return

    if route.startswith("/api/accounts/") and method == "POST":
        try:
            body = _read_json_body(handler)
            if not isinstance(body, dict):
                raise ValueError
        except Exception:  # noqa: BLE001
            _json_response(handler, {"ok": False, "error": "无效请求体"}, 400)
            return
        action = route[len("/api/accounts/"):]

        if action == "switch":
            try:
                aid = int(body.get("id"))
            except (TypeError, ValueError):
                aid = 0
            row = db.get_db().execute(
                "SELECT TRIM(token) AS t FROM accounts WHERE id = ?", (aid,)
            ).fetchone() if aid else None
            if row is None:
                _json_response(handler, {"ok": False, "error": "账号不存在"}, 404)
                return
            if not row["t"]:
                _json_response(handler, {"ok": False, "error": "该账号未登录"}, 400)
                return
            db.set_active_account(aid)
            _json_response(handler, {"ok": True, "active_id": aid})
            return

        if action == "rename":
            try:
                aid = int(body.get("id"))
            except (TypeError, ValueError):
                aid = 0
            if not db.rename_account(aid, str(body.get("name") or "")):
                _json_response(handler, {"ok": False, "error": "重命名失败 (账号不存在或名称为空)"}, 400)
                return
            _json_response(handler, {"ok": True})
            return

        if action == "delete":
            try:
                aid = int(body.get("id"))
            except (TypeError, ValueError):
                aid = 0
            remaining = db.delete_account(aid) if aid else -1
            if remaining < 0:
                _json_response(handler, {"ok": False, "error": "无效账号 id"}, 400)
                return
            _quota_cache.pop(aid, None)  # 清理该账号的配额缓存槽
            _json_response(handler, {"ok": True, "remaining": remaining})
            return

        if action == "add":
            # 触发登录窗口 (add 模式); 无窗口环境 (纯浏览器/冒烟) 时返回未打开状态
            opened = bool(_on_open_login)
            if opened:
                _on_open_login("add")
            _json_response(handler, {"ok": True, "opened": opened})
            return

        _json_response(handler, {"ok": False, "error": "未知操作"}, 404)
        return

    if route == "/api/model-quota" and method == "GET":
        _json_response(handler, _build_model_quota(query))
        return

    if route == "/api/go-raw-status" and method == "GET":
        _json_response(handler, _build_go_raw_status())
        return

    if route == "/api/usage/records" and method == "GET":
        try:
            page = max(1, int(query.get("page", ["1"])[0]))
        except ValueError:
            page = 1
        try:
            page_size = max(1, min(int(query.get("page_size", ["50"])[0]), 100))
        except ValueError:
            page_size = 50
        model = query.get("model", [""])[0] or None
        days_raw = query.get("days", [""])[0]
        try:
            days = max(1, min(int(days_raw), 365)) if days_raw else None
        except ValueError:
            days = None
        records, total = db.usage_records_page(page, page_size, model, days)
        key_names = db.get_key_names()
        for rec in records:
            rec["key_name"] = key_names.get(rec.get("key_id") or "", "")
        _json_response(
            handler,
            {
                "records": records,
                "total": total,
                "page": page,
                "page_size": page_size,
                "models": db.list_models(),
                "filter": {"model": model, "days": days},
            },
        )
        return

    if route == "/api/usage/sessions" and method == "GET":
        try:
            page = max(1, int(query.get("page", ["1"])[0]))
        except ValueError:
            page = 1
        try:
            page_size = max(1, min(int(query.get("page_size", ["10"])[0]), 50))
        except ValueError:
            page_size = 10
        days_raw = query.get("days", [""])[0]
        try:
            days = max(1, min(int(days_raw), 365)) if days_raw else None
        except ValueError:
            days = None
        records, total = db.session_stats_page(page, page_size, days)
        key_names = db.get_key_names()
        for rec in records:
            rec["key_name"] = key_names.get(rec.get("key_id") or "", "")
            # 无 session 的行分组键为 key_id (key_/sk_ 前缀), 前端据此显示"未归属"
            if rec["session_id"] and rec["session_id"].startswith(("key_", "sk_")):
                rec["session_id"] = ""
        _json_response(
            handler,
            {
                "records": records,
                "total": total,
                "page": page,
                "page_size": page_size,
                "filter": {"days": days},
            },
        )
        return

    if route == "/api/settings" and method == "GET":
        _json_response(handler, db.get_settings())
        return

    if route == "/api/settings" and method == "PUT":
        try:
            length = int(handler.headers.get("Content-Length") or 0)
            body = json.loads(handler.rfile.read(length).decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            _json_response(handler, {"ok": False, "error": "无效请求体"}, 400)
            return
        _json_response(handler, db.save_settings(body))
        return

    handler.send_error(404)


class _Handler(BaseHTTPRequestHandler):
    server_version = "GoGauge/1.0"
    protocol_version = "HTTP/1.1"  # keep-alive: Content-Length 均已携带, 避免浏览器并发首请求被误断

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默日志
        pass

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path.startswith("/api/"):
            try:
                _handle_api(self, path, query)
            except Exception as exc:  # noqa: BLE001
                _json_response(self, {"ok": False, "error": str(exc)}, 500)
            return
        if path == "/" or path == "":
            _static_response(self, "index.html")
        else:
            _static_response(self, path)

    def do_POST(self) -> None:  # noqa: N802
        self._handle_api_request()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle_api_request()

    def _handle_api_request(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path.startswith("/api/"):
            try:
                _handle_api(self, path, query)
            except Exception as exc:  # noqa: BLE001
                _json_response(self, {"ok": False, "error": str(exc)}, 500)
            return
        self.send_error(404)


def start_server(host: str = "127.0.0.1", port: int = 0) -> tuple[str, int]:
    """启动 HTTP 服务, 返回 (host, port)."""
    global _server
    _server = ThreadingHTTPServer((host, port), _Handler)
    thread = threading.Thread(target=_server.serve_forever, daemon=True, name="gousage-http")
    thread.start()
    return _server.server_address[0], _server.server_address[1]


def stop_server() -> None:
    global _server
    if _server:
        _server.shutdown()
        _server.server_close()
        _server = None
