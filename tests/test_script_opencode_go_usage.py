"""scripts/opencode_go_usage.py 离线测试: Cookie 归一化 / 解析 / 聚合 / CLI 分支.

全部打桩, 不触网.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    spec = importlib.util.spec_from_file_location(
        "opencode_go_usage", os.path.join(_ROOT, "scripts", "opencode_go_usage.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["opencode_go_usage"] = mod  # dataclass 需要模块已注册
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


cli = _load()

GO_STATUS = {
    "access": {
        "startsAt": "2026-09-26T12:06:25.000Z",
        "endsAt": "2026-10-26T12:06:25.000Z",
        "meters": {
            "fiveHour": {
                "startsAt": "2026-09-26T12:27:01.804Z",
                "resetsAt": "2026-09-26T17:27:01.804Z",
                "limitMicroCents": "1200000000",
                "usedMicroCents": "600000000",
            },
            "week": {"limitMicroCents": "3000000000", "usedMicroCents": "3000000000"},
            "month": {"limitMicroCents": "6000000000", "usedMicroCents": "0"},
        },
    }
}
ORGS = [{"id": "wrk_AAA", "name": "team"}, {"id": "wrk_BBB", "name": "second"}]

# 明细 fixture 的时间戳 (ms, 近当前时间, 落在 --days 1 窗口内)
_NOW_MS = int(time.time() * 1000)
LOG_TS_1 = _NOW_MS - 3600_000  # 1 小时前
LOG_TS_2 = _NOW_MS - 1800_000  # 30 分钟前


@pytest.fixture
def fake_api(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def _api_get(path, cookie, org_id=None, params=None, **kw):
        calls.append((path, {"cookie": cookie, "org": org_id, "params": params}))
        if path == "/orgs":
            return ORGS
        if path == "/go/status":
            return GO_STATUS
        if path == "/service-accounts":
            return {"items": [{"keys": [{"id": "sk_1", "name": "laptop"}]}]}
        if path == "/request-logs":
            if params and params.get("cursor") == "c2":
                return {"items": [
                    {"category": "inference", "id": "r2", "startedAt": LOG_TS_2, "model": "kimi-k3",
                     "inputTokens": 10, "outputTokens": 5, "cost": 0.5, "serviceAPIKeyID": "sk_1"},
                ]}
            return {"items": [
                {"category": "api", "id": "x", "startedAt": _NOW_MS},
                {"category": "inference", "id": "r1", "startedAt": LOG_TS_1, "model": "deepseek",
                 "inputTokens": 100, "outputTokens": 50, "reasoningTokens": 10,
                 "cacheReadTokens": 100, "cacheWriteTokens": 7, "cost": "0.25",
                 "serviceAPIKeyID": "sk_1", "sessionID": "s1"},
            ], "nextCursor": "c2"}
        raise AssertionError(path)

    monkeypatch.setattr(cli, "api_get", _api_get)
    return calls


def test_normalize_cookie_variants():
    assert cli._normalize_cookie("st_abc") == "__Host-console_session=st_abc"
    assert cli._normalize_cookie("__Host-console_session=st_x") == "__Host-console_session=st_x"
    assert cli._normalize_cookie("Cookie: a=1; auth=Fe26.2**y; b=2") == "auth=Fe26.2**y"
    assert cli._normalize_cookie("  ") == ""


def test_resolve_workspace(fake_api):
    # 任意大小写的 wrk_ 前缀都视作 ID (与 app/opencode_api.extract_workspace_id 不同:
    # 该实现会把小写化 ID 解析失败, 这里更宽松, 直接透传原值)
    assert cli.resolve_workspace("WRK_ZZZ", "st_x") == ("WRK_ZZZ", "")
    assert fake_api == []  # 已是 ID: 不请求 /orgs
    assert cli.resolve_workspace("wrk_AAA", "st_x") == ("wrk_AAA", "")
    assert cli.resolve_workspace("second", "st_x") == ("wrk_BBB", "second")
    assert cli.resolve_workspace("", "st_x") == ("wrk_AAA", "team")


def test_fetch_quota_windows(fake_api):
    windows, period = cli.fetch_quota("st_x", "wrk_AAA")
    assert period == "2026-09-26T12:06:25.000Z"
    rolling, weekly, monthly = windows
    assert (rolling.key, rolling.limit_usd, rolling.used_usd) == ("fiveHour", 12.0, 6.0)
    assert rolling.pct == 50.0
    assert weekly.pct == 100.0
    assert monthly.resets_at == "2026-10-26T12:06:25.000Z"  # 回落到 endsAt
    assert fake_api[-1][1]["org"] == "wrk_AAA"


def test_fetch_quota_no_subscription(monkeypatch):
    monkeypatch.setattr(cli, "api_get", lambda *a, **k: {"access": {}})
    with pytest.raises(cli.ApiError):
        cli.fetch_quota("st_x", "wrk_AAA")


def test_request_logs_pagination_and_filter(fake_api):
    items = list(cli.iter_request_logs("st_x", "wrk_AAA"))
    assert [i["id"] for i in items] == ["r1", "r2"]  # category=api 被过滤
    summary = cli.summarize_usage(items, {"sk_1": "laptop"})
    t = summary["totals"]
    assert t["requests"] == 2
    assert t["total_tokens"] == 100 + 50 + 10 + 10 + 5
    # 缓存命中率 = 缓存读 / (缓存读 + 输入) = 100 / (100 + 110)
    assert t["cache_hit_rate"] == pytest.approx(100 / 210 * 100)
    assert t["sessions"] == 1
    assert summary["models"]["deepseek"]["cost_usd"] == 0.25
    assert summary["keys"]["laptop"]["requests"] == 2


def test_main_json_from_cookie(fake_api, capsys):
    rc = cli.main(["--cookie", "st_x", "--workspace", "wrk_AAA", "--usage", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["workspace"] == {"id": "wrk_AAA", "name": ""}
    assert [w["key"] for w in out["windows"]] == ["fiveHour", "week", "month"]
    assert out["usage"]["totals"]["requests"] == 2


def test_main_no_credentials(monkeypatch, capsys):
    monkeypatch.delenv("OPENCODE_SESSION", raising=False)
    monkeypatch.setattr(cli, "load_cached_session", lambda: None)
    monkeypatch.setattr(sys, "argv", ["x"])
    assert cli.main([]) == 4
    assert "没有凭证" in capsys.readouterr().err


def test_main_auth_error(monkeypatch, capsys):
    def boom(*a, **k):
        raise cli.AuthError("401")

    monkeypatch.setattr(cli, "api_get", boom)
    assert cli.main(["--cookie", "st_x", "--workspace", "wrk_AAA"]) == 2
    assert "认证失败" in capsys.readouterr().err


def test_load_session_from_gogauge_db(tmp_path):
    import sqlite3

    db = tmp_path / "gousage.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT, workspace_id TEXT,
                               resolved_workspace_id TEXT, token TEXT);
        CREATE TABLE settings (id INTEGER PRIMARY KEY, payload TEXT);
        INSERT INTO accounts VALUES (1, 'a', 'wrk_1', NULL, '');
        INSERT INTO accounts VALUES (2, 'b', 'Default', 'wrk_2', '__Host-console_session=st_b');
        INSERT INTO settings VALUES (1, '{"active_account_id": 1}');
        """
    )
    conn.commit()
    conn.close()
    cookie, hint = cli.load_session_from_gogauge_db(str(tmp_path))
    # 活跃账号未登录 -> 回退到第一个已登录账号
    assert cookie == "__Host-console_session=st_b"
    assert hint == "wrk_2"
