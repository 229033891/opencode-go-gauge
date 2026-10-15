# ---------------------------------------------------------------------------
# API Key 过滤 (分 Key / 多选 / 全部 / 未归属)  —  docs/model-quota-key-filter.md
# ---------------------------------------------------------------------------

import pytest

from app import db, server
from tests.test_model_quota import _fake_quota, _rec


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """独立临时库: 重定向 data_dir 并重置模块级连接 (复用 test_model_quota 的口径)."""
    monkeypatch.setattr(db, "data_dir", lambda: str(tmp_path))
    db._DB = None
    yield tmp_path
    db.close_db()


def test_model_stats_since_key_filters(isolated, monkeypatch):
    monkeypatch.setattr(db, "get_active_account_id", lambda: 1)
    db.insert_usage_records([
        _rec("k1", hours_ago=1.0, model="glm-5.3-flash", cost_usd=3.0, key_id="kid_a"),
        _rec("k2", hours_ago=2.0, model="kimi-k3", cost_usd=4.0, key_id="kid_b"),
        _rec("k3", hours_ago=3.0, model="kimi-k3", cost_usd=5.0, key_id=""),
        _rec("k4", hours_ago=4.0, model="qwen3.8-max", cost_usd=6.0, key_id=""),
    ])

    # 单 Key
    rows = db.model_stats_since(None, 1, key_ids=["kid_a"])
    assert {r["model"] for r in rows} == {"glm-5.3-flash"}
    assert rows[0]["total_cost_usd"] == pytest.approx(3.0)

    # 未归属
    rows = db.model_stats_since(None, 1, key_ids=[db.NONE_KEY_SENTINEL])
    costs = {r["model"]: r["total_cost_usd"] for r in rows}
    assert costs == {"kimi-k3": pytest.approx(5.0), "qwen3.8-max": pytest.approx(6.0)}

    # 多 Key 合计
    rows = db.model_stats_since(None, 1, key_ids=["kid_b", db.NONE_KEY_SENTINEL])
    costs = {r["model"]: r["total_cost_usd"] for r in rows}
    assert costs == {"kimi-k3": pytest.approx(9.0), "qwen3.8-max": pytest.approx(6.0)}

    # 混合: Key + 未归属
    rows = db.model_stats_since(None, 1, key_ids=["kid_a", db.NONE_KEY_SENTINEL])
    costs = {r["model"]: r["total_cost_usd"] for r in rows}
    assert costs == {"glm-5.3-flash": pytest.approx(3.0), "kimi-k3": pytest.approx(5.0),
                     "qwen3.8-max": pytest.approx(6.0)}


def test_list_usage_key_ids_and_empty_account(isolated, monkeypatch):
    monkeypatch.setattr(db, "get_active_account_id", lambda: 1)
    db.insert_usage_records([
        _rec("l1", hours_ago=1.0, model="m1", cost_usd=1.0, key_id="kid_b"),
        _rec("l2", hours_ago=2.0, model="m2", cost_usd=1.0, key_id="kid_a"),
        _rec("l3", hours_ago=3.0, model="m3", cost_usd=1.0, key_id=""),
    ])
    listed = db.list_usage_key_ids(1)
    assert listed["keys"] == ["kid_a", "kid_b"]
    assert listed["has_unassigned"] is True
    assert db.list_usage_key_ids(99) == {"keys": [], "has_unassigned": False}


def test_build_model_quota_key_filter(isolated, monkeypatch):
    monkeypatch.setattr(server, "_fetch_quota_with_cache", _fake_quota())
    monkeypatch.setitem(server._quota_cache, 1, {"at": 0.0, "data": None})
    monkeypatch.setattr(db, "get_token", lambda: "tok")
    monkeypatch.setattr(db, "get_active_account_id", lambda: 1)
    monkeypatch.setattr(db, "get_account_credentials", lambda aid: ("tok", "Default"))
    db.insert_usage_records([
        _rec("q1", hours_ago=1.0, model="glm-5.3-flash", cost_usd=3.0, key_id="kid_a"),
        _rec("q2", hours_ago=2.0, model="glm-5.3-flash", cost_usd=4.0, key_id=""),
    ])

    # 全部 (默认)
    quota_all = server._build_model_quota({"key_ids": [""]})
    glm = next(m for m in quota_all["models"] if m["model"] == "glm-5.3-flash")
    assert glm["windows"]["5h"]["cost_usd"] == pytest.approx(7.0)
    assert quota_all["filter"]["key_ids"] == []
    assert {k["id"] for k in quota_all["filter"]["keys"]} == {"kid_a", "__none__"}

    # 单 Key
    quota_a = server._build_model_quota({"key_ids": ["kid_a"]})
    glm = next(m for m in quota_a["models"] if m["model"] == "glm-5.3-flash")
    assert glm["windows"]["5h"]["cost_usd"] == pytest.approx(3.0)
    assert quota_a["filter"]["key_ids"] == ["kid_a"]

    # Key + 未归属 (真实 HTTP 形式: 逗号分隔一个字符串)
    quota_mix = server._build_model_quota({"key_ids": ["kid_a,__none__"]})
    glm = next(m for m in quota_mix["models"] if m["model"] == "glm-5.3-flash")
    assert glm["windows"]["5h"]["cost_usd"] == pytest.approx(7.0)


def test_unused_official_models_listed(isolated, monkeypatch):
    """官方额度表中的模型即便本账号没用过也要出现在表尾, 状态 idle."""
    monkeypatch.setattr(server, "_fetch_quota_with_cache", _fake_quota())
    monkeypatch.setitem(server._quota_cache, 1, {"at": 0.0, "data": None})
    monkeypatch.setattr(db, "get_token", lambda: "tok")
    monkeypatch.setattr(db, "get_active_account_id", lambda: 1)
    monkeypatch.setattr(db, "get_account_credentials", lambda aid: ("tok", "Default"))
    fake_limits = {
        "limits": {
            "glm-5.3-flash": {"name": "GLM-5.3-Flash", "go": 60, "plus": 180,
                              "input": None, "output": None, "cached_read": None},
            "kimi-k3": {"name": "Kimi K3", "go": 15, "plus": 60,
                        "input": None, "output": None, "cached_read": None},
        },
        "source": "test",
        "fetched": "2026-10-15T00:00:00Z",
    }
    monkeypatch.setattr(server.limits, "load_limits", lambda force=False: dict(fake_limits))
    db.insert_usage_records([
        _rec("z1", hours_ago=1.0, model="glm-5.3-flash", cost_usd=2.0, key_id="kid_a"),
    ])

    quota = server._build_model_quota({})
    by_model = {m["model"]: m for m in quota["models"]}
    assert set(by_model) == {"glm-5.3-flash", "kimi-k3"}

    glm = by_model["glm-5.3-flash"]
    assert glm["status"] != "idle" and glm["windows"]["month"]["cost_usd"] > 0

    kimi = by_model["kimi-k3"]
    assert kimi["status"] == "idle"
    assert kimi["monthly_limit_usd"] == 15.0
    assert kimi["windows"]["month"] == {"requests": 0, "tokens": 0, "cost_usd": 0.0,
                                        "limit_usd": 15.0, "pct": 0.0}
    assert kimi["remaining_month_usd"] == 15.0
    # 零用量排在有用量之后
    assert quota["models"][-1]["status"] == "idle"
