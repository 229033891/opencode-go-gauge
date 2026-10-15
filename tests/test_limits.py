"""app.limits 官方分模型额度: 文档解析 / 缓存 / 匹配 测试."""
from __future__ import annotations

import json

import pytest

from app import limits

# 与官方文档页同构的最小 HTML (含 endpoint 对照表 + Go/Go+ 限额表 + 干扰表)
FIXTURE_HTML = """
<table><tr><th>Model</th><th>Input</th><th>Output</th><th>Cached Read</th><th>Cached Write</th><th>Monthly limit</th></tr>
<tr><td>GLM-5.3-Flash</td><td>$0.15</td><td>$0.50</td><td>$0.03</td><td>-</td><td>$60</td></tr>
<tr><td>GLM-5.3</td><td>$1.40</td><td>$4.40</td><td>$0.26</td><td>-</td><td>$15</td></tr>
<tr><td>Step 5 Preview Free</td><td>Free</td><td>Free</td><td>Free</td><td>-</td><td>Unlimited</td></tr>
<tr><td>Gemini 4 Argon Contributor</td><td>$4.00</td><td>$20.00</td><td>$0.20</td><td>-</td><td>$30 $602x · Limited time</td></tr>
<tr><td>Qwen3.7 Plus (≤ 256K tokens)</td><td>$0.40</td><td>$1.60</td><td>$0.04</td><td>$0.50</td><td>$60</td></tr>
<tr><td>Qwen3.7 Plus (&gt; 256K tokens)</td><td>$1.20</td><td>$4.80</td><td>$0.12</td><td>$1.50</td><td>$60</td></tr>
</table>
<table><tr><th>Model</th><th>Model ID</th><th>Endpoint</th><th>AI SDK Package</th></tr>
<tr><td>GLM-5.3-Flash</td><td>glm-5.3-flash</td><td>https://x/v1/chat/completions</td><td>@ai-sdk/openai-compatible</td></tr>
<tr><td>GLM-5.3</td><td>glm-5.3</td><td>https://x/v1/chat/completions</td><td>@ai-sdk/openai-compatible</td></tr>
<tr><td>Gemini 4 Argon Contributor</td><td>gemini-4-argon-contributor</td><td>https://x/v1/</td><td>@ai-sdk/google</td></tr>
<tr><td>Qwen3.7 Plus</td><td>qwen3.7-plus</td><td>https://x/v1/messages</td><td>@ai-sdk/anthropic</td></tr>
</table>
<table><tr><th>Model</th><th>Model training</th><th>Data retention</th></tr>
<tr><td>Space Bunny</td><td>Not used</td><td>0 days</td></tr>
</table>
<table><tr><th>Model</th><th>Input</th><th>Output</th><th>Cached Read</th><th>Cached Write</th><th>Monthly limit</th></tr>
<tr><td>GLM-5.3-Flash</td><td>$0.15</td><td>$0.50</td><td>$0.03</td><td>-</td><td>$180</td></tr>
<tr><td>GLM-5.3</td><td>$1.40</td><td>$4.40</td><td>$0.26</td><td>-</td><td>$120</td></tr>
</table>
"""


def test_parse_docs_limits():
    parsed = limits.parse_docs_limits(FIXTURE_HTML, min_rows=2)
    assert parsed["glm-5.3-flash"]["go"] == 60.0
    assert parsed["glm-5.3-flash"]["plus"] == 180.0
    # 单价 (USD / 1M tokens): 数值化解析, '-' -> None, 'Free' -> 0.0
    flash = parsed["glm-5.3-flash"]
    assert flash["price_input"] == 0.15
    assert flash["price_output"] == 0.50
    assert flash["price_cached_read"] == 0.03
    assert flash["price_cached_write"] is None
    assert parsed["glm-5.3"]["price_input"] == 1.40
    assert parsed["step5previewfree"]["price_input"] == 0.0
    assert parsed["glm-5.3"]["go"] == 15.0
    assert parsed["step5previewfree"]["go"] is None  # Unlimited
    # 促销价: 去掉 2x 粘连后取后值
    assert parsed["gemini-4-argon-contributor"]["go"] == 60.0
    # 变体行 (≤/>= tokens) 折叠到同一 ID, 首条生效
    assert parsed["qwen3.7-plus"]["go"] == 60.0
    assert len(parsed) >= 4
    # 训练表不得混入限额 (Space Bunny / Not used)
    assert "space-bunny" not in parsed and "notused" not in parsed


def test_parse_requires_minimum_rows():
    with pytest.raises(ValueError):
        limits.parse_docs_limits("<tr><td>x</td></tr>")


def test_match_and_plan():
    parsed = limits.parse_docs_limits(FIXTURE_HTML, min_rows=2)
    row = limits.match_limit_row(parsed, "glm-5.3-flash")
    assert row["name"] == "GLM-5.3-Flash"
    assert row is limits.match_limit_row(parsed, "glm-5.3-flash")
    unknown = limits.match_limit_row(parsed, "omen-alpha")
    assert unknown is None
    assert limits.plan_limit(parsed["glm-5.3"], "go") == 15.0
    assert limits.plan_limit(parsed["glm-5.3"], "plus") == 120.0
    assert limits.plan_limit(unknown, "go") is None


def test_load_fallback_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(limits, "_cache_path_override", str(tmp_path / "model_limits_cache.json"))
    monkeypatch.setattr(limits, "_fetch_html", lambda: (_ for _ in ()).throw(OSError("offline")))
    doc = limits.load_limits()
    assert "内置快照" in doc["source"]
    assert doc["limits"]["glm-5.3-flash"]["go"] == 60
    # 快照写缓存后再次加载命中缓存
    doc2 = limits.load_limits()
    assert doc2["limits"]["glm-5.3-flash"]["go"] == 60


def test_cache_writes_and_reload(tmp_path, monkeypatch):
    cache = tmp_path / "model_limits_cache.json"
    monkeypatch.setattr(limits, "_cache_path_override", str(cache))
    monkeypatch.setattr(limits, "_fetch_html", lambda: FIXTURE_HTML)
    import functools
    monkeypatch.setattr(limits, "parse_docs_limits",
                        functools.partial(limits.__dict__["parse_docs_limits"], min_rows=2))
    doc = limits.load_limits()
    assert doc["source"].startswith("docs")
    data = json.loads(cache.read_text(encoding="utf-8"))
    assert data["limits"]["glm-5.3"]["go"] == 15.0
    doc2 = limits.load_limits()
    assert doc2["source"].startswith("cache")
    assert doc2["limits"]["glm-5.3"]["plus"] == 120.0
