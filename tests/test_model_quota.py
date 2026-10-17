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
        "plan": "go",
    }


def _fake_quota(reset_in_hours: float = 1.0, pool_month: float = 60.0, used_frac: float = 0.2):
    """go/status 缓存替身: 账号级共享池, 5h/week/month = 20%/50%/100% 月额度.

    窗口上限/已用是真实美元金额 (limitMicroCents/1e-8), 不是百分比 —— 这是
    账单真值, 也是共享池口径的根据。
    """
    base = datetime.now(timezone.utc)

    def _fetch(account_id: int, token: str, hint: str) -> dict:
        windows = []
        for label, frac, hours in (("5h Rolling", 0.20, reset_in_hours),
                                   ("Weekly", 0.50, 24),
                                   ("Monthly", 1.00, reset_in_hours)):
            limit = round(pool_month * frac, 4)
            used_usd = round(limit * used_frac, 4)
            reset = base + timedelta(hours=hours)
            windows.append({
                "label": label,
                "used": round(used_frac * 100, 2), "remaining": round((1 - used_frac) * 100, 2),
                "total": 100, "unit": "%",
                "reset_at": reset.isoformat().replace("+00:00", "Z"),
                "reset_in_sec": int(hours * 3600),
                "starts_at": "",
                "limit_usd": limit, "used_usd": used_usd,
            })
        return {"name": hint, "workspace_id": "wrk_x", "success": True,
                "updated_at": base.isoformat().replace("+00:00", "Z"), "windows": windows}

    return _fetch


def test_build_model_quota_windows_and_limits(isolated, monkeypatch):
    # 账号级共享池: 月 $60 -> 5h $12 / 周 $30 / 月 $60; 各模型扣减同一个池子
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
    # 共享池: 顶层暴露真实美元额度, 分模型只是对池的估算拆解
    assert quota["quota_mode"] == "shared_pool"
    pool = quota["pool"]
    assert pool["monthly_limit_usd"] == 60.0
    assert pool["windows"]["5h"]["limit_usd"] == 12.0
    assert pool["windows"]["week"]["limit_usd"] == 30.0
    assert pool["windows"]["month"]["limit_usd"] == 60.0
    # used_frac=0.2 -> 月已用 $12 / 剩余 $48
    assert pool["windows"]["month"]["used_usd"] == pytest.approx(12.0)
    assert pool["windows"]["month"]["remaining_usd"] == pytest.approx(48.0)

    by_model = {m["model"]: m for m in quota["models"]}

    glm = by_model["glm-5.3-flash"]
    w5h, week, month = glm["windows"]["5h"], glm["windows"]["week"], glm["windows"]["month"]
    # glm 档位 $60 -> 系数 60/60 = 1.0, 池占用 = 零售费用 x 1.0
    assert glm["coefficient"] == 1.0
    assert glm["is_free"] is False
    assert w5h["cost_usd"] == pytest.approx(4.0)
    # 分母是共享池窗口上限 (同池同值, 不是该模型的独立桶)
    assert w5h["pool_limit_usd"] == 12.0
    assert w5h["pool_cost_usd"] == pytest.approx(4.0)
    assert w5h["pool_share_pct"] == pytest.approx(4.0 / 12.0 * 100, abs=0.2)
    assert week["cost_usd"] == pytest.approx(6.0)
    assert week["pool_limit_usd"] == 30.0
    assert month["pool_limit_usd"] == 60.0
    assert month["pool_share_pct"] == pytest.approx(10.0, abs=0.2)
    assert glm["status"] in ("ok", "warn")  # 5h 占池 ~33%
    assert glm["limit_known"] is True
    # 共享池下不存在 per-model 剩余余额
    assert "remaining_month_usd" not in glm

    qwen = by_model["qwen3.8-max"]
    assert qwen["windows"]["5h"]["cost_usd"] == 0
    assert qwen["doc_monthly_limit_usd"] == 15.0          # 文档档位权重, 非独立额度
    assert qwen["coefficient"] == 4.0                    # 60 / 15
    # 1 次请求 $8 -> 均价 $8 -> 池占用 $32/次 -> 池剩 $48 -> 估算 1 次; 样本 <5
    assert qwen["month_requests_used"] == 1
    assert qwen["avg_cost_per_request"] == pytest.approx(8.0)
    assert qwen["est_remaining_requests"] == 1
    assert qwen["est_sample_small"] is True

    # glm: 月内 2 次共 $6, 均价 $3, 系数 1.0 -> 池剩 $48 / $3 -> 估算 16 次
    assert glm["month_requests_used"] == 2
    assert glm["avg_cost_per_request"] == pytest.approx(3.0)
    assert glm["est_remaining_requests"] == 16
    assert glm["est_sample_small"] is True

    omen = by_model["omen-alpha"]
    assert omen["limit_known"] is False
    assert omen["doc_monthly_limit_usd"] is None
    assert omen["est_remaining_requests"] is None

    # 状态排序: danger 最前, 零用量(idle) 最后
    statuses = [m["status"] for m in quota["models"]]
    assert statuses == sorted(statuses, key=lambda s: {"danger": 0, "warn": 1, "ok": 2, "idle": 3}[s])


def test_plan_plus_multiplier(isolated, monkeypatch):
    # Go Plus: 共享池月额度 4 倍 ($240), 文档档位也 4 倍
    monkeypatch.setattr(server, "_fetch_quota_with_cache", _fake_quota(pool_month=240.0))
    monkeypatch.setitem(server._quota_cache, 1, {"at": 0.0, "data": None})
    monkeypatch.setattr(db, "get_token", lambda: "tok")
    monkeypatch.setattr(db, "get_active_account_id", lambda: 1)
    monkeypatch.setattr(db, "get_account_credentials", lambda aid: ("tok", "Default"))
    db.insert_usage_records([_rec("p1", hours_ago=1.0, model="glm-5.3-flash", cost_usd=90.0)])

    quota = server._build_model_quota({"plan": ["plus"]})
    assert quota["pool"]["monthly_limit_usd"] == 240.0
    glm = next(m for m in quota["models"] if m["model"] == "glm-5.3-flash")
    assert glm["doc_monthly_limit_usd"] == 180.0     # 文档档位 (plus)
    # 系数用文档 go 档位 ($60) 做分母, 池上限来自接口 ($240) -> 4x
    assert glm["coefficient"] == 4.0
    # 窗口上限是共享池的 ($240 / 20% / 50%), 与模型无关
    assert glm["windows"]["month"]["pool_limit_usd"] == 240.0
    assert glm["windows"]["week"]["pool_limit_usd"] == 120.0
    assert glm["windows"]["5h"]["pool_limit_usd"] == 48.0
    # $90 x 4 = $360 池占用 > $240 月上限 -> danger
    assert glm["windows"]["month"]["pool_cost_usd"] == pytest.approx(360.0)
    assert glm["status"] == "danger"


def test_shared_pool_single_meter_is_account_wide(isolated, monkeypatch):
    """回归: 三个 meter 无 model 维度 -> 额度是账号级共享池, 不是分模型独立桶."""
    monkeypatch.setattr(server, "_fetch_quota_with_cache", _fake_quota())
    monkeypatch.setitem(server._quota_cache, 1, {"at": 0.0, "data": None})
    monkeypatch.setattr(db, "get_token", lambda: "tok")
    monkeypatch.setattr(db, "get_active_account_id", lambda: 1)
    monkeypatch.setattr(db, "get_account_credentials", lambda aid: ("tok", "Default"))
    db.insert_usage_records([
        _rec("s1", hours_ago=1.0, model="glm-5.3-flash", cost_usd=4.0),
        _rec("s2", hours_ago=1.0, model="kimi-k3", cost_usd=1.0),
    ])

    quota = server._build_model_quota({})
    pool = quota["pool"]
    # 所有模型看到的是同一个池上限
    limits = {m["windows"]["month"]["pool_limit_usd"] for m in quota["models"]
              if m["windows"]["month"]["pool_limit_usd"] is not None}
    assert limits == {60.0}
    assert pool["available"] is True
    assert pool["mode"] == "shared_pool"
