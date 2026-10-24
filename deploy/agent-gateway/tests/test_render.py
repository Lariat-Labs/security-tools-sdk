"""Tests for the APIM policy renderer (no Azure needed)."""

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

GATEWAY_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GATEWAY_DIR))

import render_policies as rp  # noqa: E402

EXAMPLE = GATEWAY_DIR / "gateway.example.yaml"


@pytest.fixture
def cfg():
    return rp.load_config(EXAMPLE)


@pytest.fixture
def raw():
    return yaml.safe_load(EXAMPLE.read_text())


def test_committed_policies_match_the_example_config():
    assert rp.main([str(EXAMPLE), "--check"]) == 0, "re-run render_policies.py"


def test_policies_are_well_formed_xml(cfg):
    for name, text in rp.render(cfg).items():
        root = ET.fromstring(text)
        assert root.tag == "policies", name
        assert [c.tag for c in root] == ["inbound", "backend", "outbound", "on-error"]


def _expressions(text: str) -> list[str]:
    root = ET.fromstring(text)
    found = []
    for el in root.iter():
        for value in [*el.attrib.values(), el.text or ""]:
            value = value.strip()
            if value.startswith("@"):
                found.append(value)
    return found


def _balanced(code: str) -> bool:
    code = re.sub(r'"(?:[^"\\]|\\.)*"', '""', code)  # ignore string literals
    pairs = {")": "(", "}": "{", "]": "["}
    stack: list[str] = []
    for ch in code:
        if ch in "({[":
            stack.append(ch)
        elif ch in ")}]" and (not stack or stack.pop() != pairs[ch]):
            return False
    return not stack


def test_every_expression_has_balanced_brackets(cfg):
    for text in rp.render(cfg).values():
        exprs = _expressions(text)
        assert exprs
        for expr in exprs:
            assert _balanced(expr), expr


def test_token_limits_cover_every_level(cfg):
    root = ET.fromstring(rp.render_models_api(cfg))
    limits = root.findall(".//llm-token-limit")
    keys = [el.attrib["counter-key"] for el in limits]
    expected = len(cfg.deployments) + 2 * len(cfg.agents) + 2 * len(cfg.tiers) + len(cfg.teams) + 1
    assert len(limits) == expected
    for prefix in ('"d:"', '"a:"', '"r:"', '"u:"', '"t:"'):
        assert any(prefix in k for k in keys), prefix
    for el in limits:
        assert el.attrib["estimate-prompt-tokens"] == "false"
        assert el.attrib["retry-after-header-name"].startswith("x-agw-retry-after-")
        if "tokens-per-minute" in el.attrib:  # must be a literal: APIM rejects expressions here
            assert el.attrib["tokens-per-minute"].isdigit()
    power = next(el for el in limits if ':power"' in el.attrib["counter-key"])
    assert power.attrib["tokens-per-minute"] == "60000"
    assert power.attrib["token-quota"] == "1500000"
    assert power.attrib["token-quota-period"] == "Daily"


def test_front_door_trusts_only_the_gateway(cfg):
    text = rp.render_agents_api(cfg)
    root = ET.fromstring(text)
    token = root.find(".//validate-azure-ad-token")
    claim = token.find("./required-claims/claim")
    assert claim.attrib["name"] == "scp" and claim.find("value").text == "Agents.Invoke"
    # Every context header is overwritten, so callers can't spoof them.
    set_headers = {el.attrib["name"]: el.attrib["exists-action"] for el in root.iter("set-header")}
    for header in ("x-agw-user", "x-agw-tier", "x-agw-team", "x-agw-allowed-agents", "x-agw-run"):
        assert set_headers[header] == "override"
    auth = root.find(".//authentication-managed-identity")
    assert auth.attrib["client-id"] == "{{agw-apim-client-id}}"
    # The most generous tier is checked first.
    tier = next(el for el in root.iter("set-variable") if el.attrib["name"] == "agw-tier")
    value = tier.attrib["value"]
    assert value.index('"power"') < value.index('"standard"')


def test_models_api_accepts_only_the_runtime(cfg):
    root = ET.fromstring(rp.render_models_api(cfg))
    token = root.find(".//validate-azure-ad-token")
    assert token.find("./client-application-ids/application-id").text == "{{agw-runtime-client-id}}"
    assert token.find("./required-claims/claim").attrib["name"] == "roles"
    deleted = [
        el.attrib["name"]
        for el in root.iter("set-header")
        if el.attrib.get("exists-action") == "delete"
    ]
    assert set(deleted) == set(rp.CONTEXT_HEADERS)  # not forwarded to the model
    dims = [d.attrib["name"] for d in root.iter("dimension")]
    assert dims == ["Tier", "Team", "Agent", "Deployment"]  # low cardinality only: no user id
    log = root.find(".//log-to-eventhub")
    assert log.attrib["logger-id"] == "agw-usage" and "promptTokens" in log.text


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda c: c["tiers"].append(dict(c["tiers"][0])), "duplicate tier"),
        (lambda c: c["agents"][0].update(deployments=["nope"]), "unknown deployments"),
        (lambda c: c["agents"][0].update(name='x"; return true; //'), "name"),
        (lambda c: c["tiers"][0].update(group_id="not-a-guid"), "group_id"),
        (lambda c: c["tiers"][0].update(tokens_per_day=10), "per-minute <= per-day"),
        (lambda c: c.update(surprise=1), "surprise"),
    ],
)
def test_invalid_configs_are_rejected(raw, mutate, message):
    mutate(raw)
    with pytest.raises(ValueError, match=message):
        rp.GatewayConfig.model_validate(raw)


def test_check_mode_detects_stale_files(tmp_path):
    assert rp.main([str(EXAMPLE), "--out", str(tmp_path)]) == 0
    assert rp.main([str(EXAMPLE), "--out", str(tmp_path), "--check"]) == 0
    (tmp_path / "models-api.xml").write_text("<policies />")
    assert rp.main([str(EXAMPLE), "--out", str(tmp_path), "--check"]) == 1


def test_workbook_is_valid_and_queries_the_gateway_tables():
    import json

    wb = json.loads((GATEWAY_DIR / "infra" / "workbook.json").read_text())
    assert wb["version"] == "Notebook/1.0"
    queries = [i["content"]["query"] for i in wb["items"] if i["type"] == 3]
    assert queries and all("{TimeRange}" in q for q in queries)
    joined = "\n".join(queries)
    for table in ("AppMetrics", "AppDependencies", "ApiManagementGatewayLogs"):
        assert table in joined
    assert '"Total Tokens"' not in joined  # APIM emits Prompt/Completion Tokens only
