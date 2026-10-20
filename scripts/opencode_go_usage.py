#!/usr/bin/env python3
"""OpenCode Go 用量查询 CLI (纯标准库, 无 GUI / 无第三方依赖).

它复刻 GoGauge 应用的登录与取数链路, 但把"登录"拆成两条路:

  A. --login   : 内嵌 WebView2 登录窗口 (仅 Windows + pywebview), 自动捕获
                 __Host-console_session Cookie 与工作区 ID, 与 app/auth.py 一致;
  B. 直接给凭证 : --cookie / 环境变量 OPENCODE_SESSION / 从 GoGauge 本地库读取.

随后调用 /console/api 取数 (与 app/opencode_api.py 同口径):
  GET /console/api/orgs              -> 工作区列表 (x-org-id 的来源)
  GET /console/api/go/status         -> 5h / 周 / 月 三个额度窗口
  GET /console/api/service-accounts  -> API Key 名称
  GET /console/api/request-logs      -> 请求明细 (游标分页, 服务端只留 30 天)

用法示例:
  # 1) 交互式 WebView 登录并显示配额 (凭证缓存到 ~/.opencode-go-gauge/session.json)
  python scripts/opencode_go_usage.py --login

  # 2) 用已有 Cookie (从浏览器 F12 复制, 或纯值 st_xxx)
  python scripts/opencode_go_usage.py --cookie "st_xxx" --json

  # 3) 沿用 GoGauge 应用已登录的凭证 (只读打开 data/gousage.db)
  python scripts/opencode_go_usage.py --from-db

  # 4) 顺带拉今日明细聚合 (请求数 / token / 费用 / 模型排行)
  python scripts/opencode_go_usage.py --from-db --usage --days 1

环境变量: OPENCODE_SESSION (Cookie 或纯值), OPENCODE_WORKSPACE (工作区 ID/名称),
          GOUSAGE_DATA (GoGauge 数据目录, 默认 <repo>/data)
退出码: 0 成功, 2 认证失败, 3 接口/网络错误, 4 参数或环境错误
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 常量 (与 app/opencode_api.py 保持一致)
# ---------------------------------------------------------------------------

CONSOLE_ORIGIN = "https://opencode.ai"
CONSOLE_LOGIN_URL = "https://opencode.ai/console/login"
API_BASE = f"{CONSOLE_ORIGIN}/console/api"

SESSION_COOKIE = "__Host-console_session"
LEGACY_SESSION_COOKIE = "auth"
SESSION_COOKIE_NAMES = (SESSION_COOKIE, LEGACY_SESSION_COOKIE)

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Gecko/20100101 Firefox/148.0"
REQUEST_TIMEOUT = 30.0
MAX_BODY_BYTES = 8 << 20
FETCH_RETRIES = 3
RETRY_BACKOFF = (0.5, 1.5, 3.0)

MICRO_USD = 1e8  # limitMicroCents / usedMicroCents / cost 的单位都是 1e-8 USD
PAGE_SIZE = 100  # request-logs 单页上限

# 额度窗口: (meter 键, 显示名, 在 Go 套餐里占月额度的比例)
METERS = (
    ("fiveHour", "5h Rolling", 0.2),
    ("week", "Weekly", 0.5),
    ("month", "Monthly", 1.0),
)

_WORKSPACE_ID_RE = re.compile(r"wrk_[A-Za-z0-9]+")
_SESSION_PATH = os.path.join(
    os.path.expanduser("~"), ".opencode-go-gauge", "session.json"
)


class ApiError(RuntimeError):
    """接口/网络错误."""


class AuthError(ApiError):
    """会话无效或过期."""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    return int(_as_float(value, default))


def _iso_from_ms(value: Any) -> str:
    ms = _as_float(value)
    if ms <= 0:
        return ""
    return (
        datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_iso(text: str) -> Optional[datetime]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _normalize_cookie(token: str) -> str:
    """把输入规范成 ``Cookie`` 头的值 (逻辑同 build_cookie_header).

    接受: 纯值 ``st_xxx`` / ``__Host-console_session=st_xxx`` /
    ``Cookie: a=1; __Host-console_session=st_xxx``
    """
    raw = (token or "").strip()
    if raw.lower().startswith("cookie:"):
        raw = raw[7:].strip()
    if not raw:
        return ""
    for part in raw.split(";"):
        part = part.strip()
        if part and part.split("=", 1)[0].strip().lower() in (
            SESSION_COOKIE.lower(),
            LEGACY_SESSION_COOKIE,
        ):
            return part
    if re.fullmatch(r"[A-Za-z0-9_.\-]+\s*=\s*\S+", raw):
        return raw
    return f"{SESSION_COOKIE}={raw}"


# ---------------------------------------------------------------------------
# HTTP (仅标准库)
# ---------------------------------------------------------------------------


def api_get(
    path: str,
    cookie: str,
    org_id: Optional[str] = None,
    params: Optional[dict[str, Any]] = None,
    timeout: float = REQUEST_TIMEOUT,
    retries: int = FETCH_RETRIES,
) -> Any:
    """GET /console/api<path>, 返回解析后的 JSON.

    关键点: 除 Cookie 外, 除 /orgs 以外的接口都要带 ``x-org-id: wrk_xxx`` 头.
    """
    header_cookie = _normalize_cookie(cookie)
    if not header_cookie:
        raise AuthError("没有会话 Cookie")
    url = f"{API_BASE}{path}"
    if params:
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    headers = {
        "Cookie": header_cookie,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "Origin": CONSOLE_ORIGIN,
        "Referer": f"{CONSOLE_ORIGIN}/console/",
    }
    if org_id:
        headers["x-org-id"] = org_id

    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read(MAX_BODY_BYTES).decode("utf-8", errors="replace")
            try:
                return json.loads(body)
            except ValueError as exc:
                raise ApiError(f"{path} 返回非 JSON 数据") from exc
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise AuthError("登录已过期 (HTTP 401), 请重新登录") from exc
            if exc.code == 403:
                raise ApiError(f"无访问权限 (HTTP 403): {path}") from exc
            if exc.code == 404:
                raise ApiError(f"工作区不存在或接口不可用 (HTTP 404): {path}") from exc
            last_exc = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
        if attempt < retries - 1:
            time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])
    reason = getattr(last_exc, "reason", last_exc)
    raise ApiError(f"请求失败 {path}: {reason}")


# ---------------------------------------------------------------------------
# 数据获取
# ---------------------------------------------------------------------------


@dataclass
class Window:
    key: str
    label: str
    limit_usd: float
    used_usd: float
    resets_at: str
    starts_at: str
    share: float = 0.0

    @property
    def pct(self) -> float:
        return min(100.0, self.used_usd / self.limit_usd * 100.0) if self.limit_usd > 0 else 0.0

    @property
    def reset_in(self) -> str:
        end = _parse_iso(self.resets_at)
        if not end:
            return "-"
        sec = max(0, int((end - datetime.now(timezone.utc)).total_seconds()))
        days, rem = divmod(sec, 86400)
        hours, rem = divmod(rem, 3600)
        mins = rem // 60
        return f"{days}d {hours}h {mins}m" if days else f"{hours}h {mins}m"


def fetch_workspaces(cookie: str) -> list[tuple[str, str]]:
    """GET /orgs -> [(wrk_id, name)].  org 即"工作区"."""
    data = api_get("/orgs", cookie)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        wid = str(item.get("id") or "").strip()
        if wid and wid not in seen:
            seen.add(wid)
            out.append((wid, str(item.get("name") or "").strip()))
    if not out:
        raise ApiError("账号下没有工作区 (/orgs 为空)")
    return out


def resolve_workspace(hint: str, cookie: str) -> tuple[str, str]:
    """工作区提示 -> (wrk_id, name). 已是 wrk_xxx (任意大小写) 则不再请求 /orgs."""
    hint = (hint or "").strip()
    if re.fullmatch(r"[Ww][Rr][Kk]_[A-Za-z0-9]+", hint):
        return hint, ""
    refs = fetch_workspaces(cookie)
    for wid, name in refs:
        if hint and (wid.lower() == hint.lower() or name.lower() == hint.lower()):
            return wid, name
    return refs[0]


def fetch_quota(cookie: str, workspace_id: str) -> tuple[list[Window], str]:
    """GET /go/status -> (三个额度窗口, 订阅周期起点 ISO).

    响应结构: access.meters.{fiveHour,week,month} 各有
    limitMicroCents / usedMicroCents (1e-8 USD) 与可选 startsAt/resetsAt;
    月窗口没有 resetsAt 时用 access.endsAt 作为重置时间.
    """
    payload = api_get("/go/status", cookie, org_id=workspace_id, timeout=20.0, retries=2)
    access = payload.get("access") if isinstance(payload, dict) else None
    meters = access.get("meters") if isinstance(access, dict) else None
    if not isinstance(meters, dict):
        raise ApiError("账号未订阅 OpenCode Go (/go/status 无 access.meters)")
    period_end = str(access.get("endsAt") or "")
    windows: list[Window] = []
    for key, label, share in METERS:
        meter = meters.get(key)
        if not isinstance(meter, dict):
            continue
        limit = _as_float(meter.get("limitMicroCents")) / MICRO_USD
        if limit <= 0:
            continue
        windows.append(
            Window(
                key=key,
                label=label,
                limit_usd=limit,
                used_usd=_as_float(meter.get("usedMicroCents")) / MICRO_USD,
                resets_at=str(meter.get("resetsAt") or period_end),
                starts_at=str(meter.get("startsAt") or ""),
                share=share,
            )
        )
    if not windows:
        raise ApiError("账号未订阅 OpenCode Go (额度为 0)")
    return windows, str(access.get("startsAt") or "")


def fetch_key_names(cookie: str, workspace_id: str) -> dict[str, str]:
    """GET /service-accounts -> {key_id: 名称}; 失败返回空 (不影响主流程)."""
    try:
        payload = api_get("/service-accounts", cookie, org_id=workspace_id, timeout=15.0, retries=2)
    except ApiError:
        return {}
    names: dict[str, str] = {}
    items = payload.get("items") if isinstance(payload, dict) else None
    for entry in items if isinstance(items, list) else []:
        keys = entry.get("keys") if isinstance(entry, dict) else None
        for key in keys if isinstance(keys, list) else []:
            if isinstance(key, dict) and key.get("id") and key.get("name"):
                names.setdefault(str(key["id"]), str(key["name"]))
    return names


def iter_request_logs(
    cookie: str,
    workspace_id: str,
    since_ms: Optional[int] = None,
    max_pages: int = 200,
    limit: int = PAGE_SIZE,
):
    """逐页产出 /request-logs 的 inference 明细 (游标分页, 按时间倒序).

    只保留 category == "inference" (另一类 "api" 是控制台自身调用, 不计用量).
    遇到早于 since_ms 的记录即停止 (按时间倒序, 后续只会更早).
    """
    cursor: Optional[str] = None
    for _ in range(max_pages):
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if since_ms:
            params["since"] = int(since_ms)
        payload = api_get("/request-logs", cookie, org_id=workspace_id, params=params)
        if not isinstance(payload, dict):
            return
        items = payload.get("items") or []
        stop = False
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("category") or "").strip().lower() != "inference":
                continue
            started = _as_int(item.get("startedAt"))
            if since_ms and started and started < since_ms:
                stop = True
                continue
            yield item
        cursor = payload.get("nextCursor")
        if not cursor or stop or not items:
            return


def summarize_usage(items, key_names: dict[str, str]) -> dict[str, Any]:
    """明细聚合: 请求数 / token 构成 / 费用 / 模型与 Key 排行."""
    models: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"requests": 0, "cost_usd": 0.0, "input": 0, "output": 0, "reasoning": 0,
                 "cache_read": 0, "cache_write": 0}
    )
    keys: dict[str, dict[str, Any]] = defaultdict(lambda: {"requests": 0, "cost_usd": 0.0})
    totals = {"requests": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0,
              "reasoning_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
              "sessions": set()}
    for item in items:
        model = str(item.get("model") or "unknown")
        cost = _as_float(item.get("cost"))
        inp = _as_int(item.get("inputTokens"))
        out = _as_int(item.get("outputTokens"))
        rea = _as_int(item.get("reasoningTokens"))
        crd = _as_int(item.get("cacheReadTokens"))
        cwr = _as_int(item.get("cacheWriteTokens"))
        totals["requests"] += 1
        totals["cost_usd"] += cost
        totals["input_tokens"] += inp
        totals["output_tokens"] += out
        totals["reasoning_tokens"] += rea
        totals["cache_read_tokens"] += crd
        totals["cache_write_tokens"] += cwr
        if item.get("sessionID"):
            totals["sessions"].add(str(item["sessionID"]))

        m = models[model]
        m["requests"] += 1
        m["cost_usd"] += cost
        m["input"] += inp
        m["output"] += out
        m["reasoning"] += rea
        m["cache_read"] += crd
        m["cache_write"] += cwr

        key_id = str(item.get("serviceAPIKeyID") or "")
        k = keys[key_id or "unattributed"]
        k["requests"] += 1
        k["cost_usd"] += cost

    totals["sessions"] = len(totals["sessions"])
    totals["total_tokens"] = (
        totals["input_tokens"] + totals["output_tokens"] + totals["reasoning_tokens"]
    )
    denom = totals["cache_read_tokens"] + totals["input_tokens"]
    totals["cache_hit_rate"] = (
        totals["cache_read_tokens"] / denom * 100.0 if denom else 0.0
    )
    result = {
        "totals": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in totals.items()},
        "models": {
            name: {**{k: (round(v, 6) if isinstance(v, float) else v) for k, v in m.items()}}
            for name, m in sorted(models.items(), key=lambda kv: -kv[1]["cost_usd"])
        },
        "keys": {
            key_names.get(kid, kid or "unattributed"): {
                "requests": v["requests"], "cost_usd": round(v["cost_usd"], 6)
            }
            for kid, v in sorted(keys.items(), key=lambda kv: -kv[1]["cost_usd"])
        },
    }
    return result


# ---------------------------------------------------------------------------
# 登录方式 A: 内嵌 WebView 登录 (可选, 仅 Windows)
# ---------------------------------------------------------------------------


def _pick_session_cookie(cookies) -> Optional[str]:
    """从 pywebview 的 cookie 列表里挑出会话 Cookie, 返回 ``name=value``."""
    jar: dict[str, str] = {}
    for cookie in cookies or []:
        if hasattr(cookie, "keys") and "name" in cookie:  # dict 形态
            jar[str(cookie.get("name") or "")] = str(cookie.get("value") or "")
            continue
        for name in SESSION_COOKIE_NAMES:  # http.cookiejar.SimpleCookie 形态
            try:
                jar[name] = cookie[name].value
            except Exception:  # noqa: BLE001 该 cookie 不存在
                continue
    for name in SESSION_COOKIE_NAMES:
        if jar.get(name, "").strip():
            return f"{name}={jar[name]}"
    return None


def webview_login(timeout_sec: float = 300.0) -> tuple[str, str]:
    """打开 opencode.ai 控制台登录窗, 捕获会话 Cookie 与工作区 ID.

    与 app/auth.py 同思路: 登录完成后窗口位于 opencode.ai 域, 用
    window.get_cookies() 读取 HttpOnly cookie; 工作区 ID 从 URL
    (/console/wrk_xxx) 正则提取. 这里用后台线程轮询实现, 不依赖 GoGauge 代码.

    返回 (token_cookie_string, workspace_hint).
    """
    try:
        import webview  # type: ignore[import-not-found]  # pywebview
    except ImportError as exc:
        raise SystemExit(
            "缺少 pywebview, 无法使用 --login。安装: pip install pywebview\n"
            "或改用 --cookie / 浏览器 F12 复制 Cookie。"
        ) from exc

    state: dict[str, Any] = {"cookie": "", "ws": "Default", "done": False}

    def _poll(win) -> None:
        """后台轮询登录窗: 位于 opencode.ai 且能读到会话 Cookie 即视为成功."""
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            try:
                url = win.get_current_url() or ""
                if url.startswith(CONSOLE_ORIGIN):
                    picked = _pick_session_cookie(win.get_cookies())
                    if picked:
                        match = _WORKSPACE_ID_RE.search(url)
                        state.update(cookie=picked, ws=match.group(0) if match else "Default",
                                     done=True)
                        win.destroy()
                        return
            except Exception:  # noqa: BLE001 窗口未就绪/已销毁: 继续等
                pass
            time.sleep(1.0)
        try:
            win.destroy()
        except Exception:  # noqa: BLE001
            pass

    win = webview.create_window(
        "OpenCode Go Login",
        f"{CONSOLE_LOGIN_URL}?next=%2Fconsole%2F",
        width=720,
        height=640,
    )

    def _start_poll() -> None:
        threading.Thread(target=_poll, args=(win,), daemon=True).start()

    try:
        win.events.shown += _start_poll  # 窗口出现后再读 cookie, 避免未就绪异常
    except Exception:  # noqa: BLE001 老版本无 shown 事件: 立即启动
        _start_poll()

    webview.start()  # 阻塞直到登录窗关闭
    if not state["done"]:
        raise SystemExit("登录窗已关闭, 未捕获到会话 Cookie")
    return state["cookie"], state["ws"]


# ---------------------------------------------------------------------------
# 登录方式 B: 本地凭证来源
# ---------------------------------------------------------------------------


def load_cached_session() -> Optional[dict[str, str]]:
    """读取 --login 缓存的凭证 (~/.opencode-go-gauge/session.json)."""
    try:
        with open(_SESSION_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) and data.get("cookie") else None
    except (OSError, ValueError):
        return None


def save_cached_session(cookie: str, workspace: str) -> None:
    """缓存凭证 (仅本机, 权限 0600); 敏感文件, 不上传任何地方."""
    os.makedirs(os.path.dirname(_SESSION_PATH), exist_ok=True)
    with open(_SESSION_PATH, "w", encoding="utf-8") as fh:
        json.dump({"cookie": cookie, "workspace": workspace}, fh)
    try:
        os.chmod(_SESSION_PATH, 0o600)
    except OSError:
        pass


def load_session_from_gogauge_db(data_dir: Optional[str] = None) -> tuple[str, str]:
    """从 GoGauge 应用的 SQLite 库读取活跃账号凭证 (只读打开).

    schema: accounts(token, workspace_id/resolved_workspace_id) +
    settings.payload.active_account_id.
    """
    base = data_dir or os.environ.get("GOUSAGE_DATA") or os.path.abspath(
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
    )
    path = os.path.join(base, "gousage.db")
    if not os.path.isfile(path):
        raise SystemExit(f"找不到 GoGauge 数据库: {path}")
    # 只读 URI: 应用可能正在运行 (WAL 模式), 绝不写用户库
    uri = "file:" + urllib.parse.quote(path.replace("\\", "/")) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        active: Optional[int] = None
        row = conn.execute("SELECT payload FROM settings WHERE id = 1").fetchone()
        if row:
            try:
                active = json.loads(row["payload"] or "{}").get("active_account_id")
            except (ValueError, AttributeError):
                active = None
        account = None
        if isinstance(active, int) and active > 0:
            account = conn.execute(
                "SELECT token, resolved_workspace_id, workspace_id FROM accounts"
                " WHERE id = ? AND TRIM(token) != ''",
                (active,),
            ).fetchone()
        if account is None:  # 活跃账号未登录: 回退到第一个已登录账号
            account = conn.execute(
                "SELECT token, resolved_workspace_id, workspace_id FROM accounts"
                " WHERE TRIM(token) != '' ORDER BY id LIMIT 1"
            ).fetchone()
    finally:
        conn.close()
    if account is None:
        raise SystemExit("GoGauge 数据库里没有已登录账号, 请先在应用里登录或改用 --cookie")
    hint = account["resolved_workspace_id"] or account["workspace_id"] or "Default"
    return account["token"], hint


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def _usd(value: float) -> str:
    return f"${value:,.4f}"


def print_report(quota: dict[str, Any], usage: Optional[dict[str, Any]] = None) -> None:
    ws = quota.get("workspace") or {}
    print()
    print(f"  工作区: {ws.get('name') or '-'}  ({ws.get('id')})")
    if quota.get("period_start"):
        print(f"  订阅周期起点: {quota['period_start']}")
    print()
    print(f"  {'窗口':<12}{'已用':>14}{'额度':>12}{'占比':>9}  重置倒计时")
    print("  " + "-" * 62)
    for w in quota.get("windows", []):
        bar_len = int(round(min(100.0, w["pct"]) / 5))
        bar = "█" * bar_len + "░" * (20 - bar_len)
        print(
            f"  {w['label']:<12}{_usd(w['used_usd']):>14}{_usd(w['limit_usd']):>12}"
            f"{w['pct']:>8.2f}%  {w['reset_in']}"
        )
        print(f"  {'':<12}[{bar}]")
    if usage:
        t = usage["totals"]
        print()
        print(f"  明细范围: 最近 {usage.get('days')} 天 (服务端仅保留 30 天)")
        print(
            f"  请求 {t['requests']}  会话 {t['sessions']}  费用 {_usd(t['cost_usd'])}  "
            f"总 token {t['total_tokens']:,}  缓存命中率 {t['cache_hit_rate']:.1f}%"
        )
        if usage["models"]:
            print()
            print(f"  {'模型':<28}{'请求':>7}{'费用':>12}")
            print("  " + "-" * 48)
            for name, m in list(usage["models"].items())[:15]:
                print(f"  {name:<28}{m['requests']:>7}{_usd(m['cost_usd']):>12}")
    print()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="opencode_go_usage",
        description="查询 OpenCode Go 用量 (复刻 GoGauge 的登录与 /console/api 取数链路)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    src = p.add_argument_group("凭证来源 (三选一)")
    src.add_argument("--cookie", help="会话 Cookie: 纯值 st_xxx 或 'name=value[; ...]'")
    src.add_argument("--from-db", action="store_true", help="从 GoGauge 本地库读取已登录账号")
    src.add_argument("--data-dir", help="GoGauge 数据目录 (配合 --from-db, 默认 <repo>/data)")
    src.add_argument("--login", action="store_true",
                     help="打开内嵌登录窗 (需 Windows + pywebview), 成功后缓存凭证")
    p.add_argument("--workspace", default=os.environ.get("OPENCODE_WORKSPACE", ""),
                   help="工作区 ID (wrk_xxx) 或名称; 默认账号下第一个")
    p.add_argument("--usage", action="store_true", help="额外拉取请求明细聚合并打印")
    p.add_argument("--days", type=float, default=1.0, help="明细回看天数 (默认 1, 最大 30)")
    p.add_argument("--json", action="store_true", help="以 JSON 输出 (便于脚本集成)")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    # 1) 取凭证
    cookie = (args.cookie or os.environ.get("OPENCODE_SESSION") or "").strip()
    workspace_hint = args.workspace
    try:
        if args.login:
            print("请在弹出的窗口里完成 opencode.ai 控制台登录 …", file=sys.stderr)
            cookie, ws = webview_login()
            workspace_hint = workspace_hint or ws
            save_cached_session(cookie, workspace_hint)
            print(f"已缓存凭证到 {_SESSION_PATH}", file=sys.stderr)
        if not cookie and args.from_db:
            cookie, hint = load_session_from_gogauge_db(args.data_dir)
            workspace_hint = workspace_hint or hint
        if not cookie:
            cached = load_cached_session()
            if cached:
                cookie = cached.get("cookie", "")
                workspace_hint = workspace_hint or cached.get("workspace", "")
        if not cookie:
            print(
                "没有凭证: 使用 --login / --cookie / --from-db (或设置 OPENCODE_SESSION)",
                file=sys.stderr,
            )
            return 4

        # 2) 解析工作区 + 取配额
        ws_id, ws_name = resolve_workspace(workspace_hint, cookie)
        windows, period_start = fetch_quota(cookie, ws_id)
        result: dict[str, Any] = {
            "workspace": {"id": ws_id, "name": ws_name},
            "period_start": period_start,
            "windows": [
                {
                    "key": w.key,
                    "label": w.label,
                    "used_usd": round(w.used_usd, 6),
                    "limit_usd": round(w.limit_usd, 6),
                    "remaining_usd": round(max(0.0, w.limit_usd - w.used_usd), 6),
                    "pct": round(w.pct, 2),
                    "resets_at": w.resets_at,
                    "starts_at": w.starts_at,
                    "reset_in": w.reset_in,
                    "share_of_month": w.share,
                }
                for w in windows
            ],
        }

        # 3) 可选: 明细
        if args.usage:
            days = max(0.0, min(float(args.days), 30.0))
            since_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
            key_names = fetch_key_names(cookie, ws_id)
            summary = summarize_usage(iter_request_logs(cookie, ws_id, since_ms), key_names)
            summary["days"] = days
            summary["since"] = _iso_from_ms(since_ms)
            result["usage"] = summary

        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print_report(result, result.get("usage"))
        return 0
    except AuthError as exc:
        print(f"认证失败: {exc}", file=sys.stderr)
        return 2
    except ApiError as exc:
        print(f"接口错误: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
