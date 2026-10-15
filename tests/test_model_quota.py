"""server._build_model_quota 分模型额度规划测试: 窗口起点 / 分模型用量 / 占比与状态."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import db, server


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """独立临时库 + 不打网络的配额缓存."""
    monkeypatch.setattr(db, "data_dir", lambda: str(tmp_path))
    db._DB = None
    yield tmp_path
    db.close_db()


def _rec(usg_id: str, hours_ago: float, model: str, cost_usd: float, key_id: str = "key_1") -> dict:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    created = dt.isoformat().replace("+00:00", "Z")
    return {
        "usg_id": usg_id, "created_at": created, "model": model, "provider": "opencode-go",
        "input_tokens": 100, "output_tokens": 50, "reasoning_tokens": 10,
        "cache_read_tokens": 0, "cache_write_5m_tokens": 0, "cache_write_1h_tokens": 0,
        "cost_raw": 0, "cost_usd": cost_usd, "key_id": key_id, "session_id": "ses_x",
        "plan": "lite",
    }


def _fake_quota(reset_in_hours: float = 1.0, used: float = 2.0):
    """go/status 缓存替身: 月窗口 reset 在未来 1h -> 起点锚定回退."""
    base = datetime.now(timezone.utc)

    def _fetch(account_id: int, token: str, hint: str) -> dict:
        windows = []
        for label, hours in (("5h Rolling", reset_in_hours), ("Weekly", 24), ("Monthly", reset_in_hours)):
            reset = base + timedelta(hours=hours)
            windows.append({
                "label": label,
                "used": used, "remaining": 100 - used, "total": 100, "unit": "%",
                "reset_at": reset.isoformat().replace("+00:00", "Z"),
                "reset_in_sec": int(hours * 3600),
                "starts_at": "",
            })
        return {"name": hint, "workspace_id": "wrk_x", "success": True,
                "updated_at": base.isoformat().replace("+00:00", "Z"), "windows": windows}

    return _fetch


def test_build_model_quota_windows_and_limits(isolated, monkeypatch):
    # 官方额度: 月 $60 -> 5h $12 / 周 $30 / 月 $60 (gloul)
    monkeypatch.setattr(server, "_fetch_quota_with_cache", _fake_quota())
    monkeypatch.setitem(server._quota_cache, 1, {"at": 0.0, "data": None})
    monkeypatch.setattr(db, "get_token", lambda: "tok")
    monkeypatch.setattr(db, "get_active_account_id", lambda: 1)
    monkeypatch.setattr(db, "get_account_credentials", lambda aid: ("tok", "Default"))

    db.insert_usage_records([
        _rec("u1", hours_ago=1.0, model="glm-5.3-flash", cost_usd=4.0),       # 5h 内
        _rec("u2", hours_ago=6.0, model="glm-5.3-flash", cost_usd=2.0),       # 仅周/月
        _rec("u3", hours_ago=10 * 24.0, model="qwen3.8-max", cost_usd=8.0),   # 仅月
        _rec("u4", hours_ago=20 * 24.0, model="omen-alpha", cost_usd=1.0),    # 月窗口内 (quota 未对齐时全量)
    ])

    quota = server._build_model_quota({})
    assert quota["plan"] == "go" and quota["quota_available"] is True
    by_model = {m["model"]: m for m in quota["models"]}

    glm = by_model["glm-5.3-flash"]
    w5h, week, month = glm["windows"]["5h"], glm["windows"]["week"], glm["windows"]["month"]
    assert w5h["cost_usd"] == pytest.approx(4.0)
    assert w5h["limit_usd"] == 12.0
    assert w5h["pct"] == pytest.approx(4.0 / 12.0 * 100, abs=0.2)
    assert week["cost_usd"] == pytest.approx(6.0)
    assert week["limit_usd"] == 30.0
    assert month["limit_usd"] == 60.0
    assert glm["status"] in ("ok", "warn")  # 45% 上下
    assert glm["limit_known"] is True

    qwen = by_model["qwen3.8-max"]
    assert qwen["windows"]["5h"]["cost_usd"] == 0
    assert qwen["monthly_limit_usd"] == 15.0
    assert qwen["remaining_month_usd"] == pytest.approx(7.0)

    omen = by_model["omen-alpha"]
    assert omen["limit_known"] is False
    assert omen["monthly_limit_usd"] is None

    # 状态排序: danger 最前, 零用量(idle) 最后
    statuses = [m["status"] for m in quota["models"]]
    assert statuses == sorted(statuses, key=lambda s: {"danger": 0, "warn": 1, "ok": 2, "idle": 3}[s])


def test_plan_plus_multiplier(isolated, monkeypatch):
    monkeypatch.setattr(server, "_fetch_quota_with_cache", _fake_quota())
    monkeypatch.setitem(server._quota_cache, 1, {"at": 0.0, "data": None})
    monkeypatch.setattr(db, "get_token", lambda: "tok")
    monkeypatch.setattr(db, "get_active_account_id", lambda: 1)
    monkeypatch.setattr(db, "get_account_credentials", lambda aid: ("tok", "Default"))
    db.insert_usage_records([_rec("p1", hours_ago=1.0, model="glm-5.3-flash", cost_usd=90.0)])

    quota = server._build_model_quota({"plan": ["plus"]})
    glm = next(m for m in quota["models"] if m["model"] == "glm-5.3-flash")
    assert glm["monthly_limit_usd"] == 180.0
    assert glm["windows"]["month"]["limit_usd"] == 180.0
    assert glm["windows"]["5h"]["limit_usd"] == 36.0
    assert glm["status"] == "danger"
