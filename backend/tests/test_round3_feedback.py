"""An effective preference must carry author intent and have a live linked rule."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select
from test_round2_references import initial_request

from novel_harness.db.models import PreferenceCandidate, StyleRule


def create_feedback(client, project, comment=""):
    response = client.post(f"/api/v1/projects/{project['id']}/feedback", json={
        "project_id": project["id"], "rating": 3, "comment": comment,
        "original_text": "original sample", "corrected_text": "corrected sample",
    })
    assert response.status_code == 201
    return response.json()["preference_candidate"]


def test_blank_feedback_is_archived_but_needs_explicit_instruction(client, project):
    candidate = create_feedback(client, project, "  \n ")
    base = f"/api/v1/projects/{project['id']}"
    result = client.post(base + f"/feedback/preferences/{candidate['id']}/confirm")
    assert result.status_code == 422
    assert result.json()["detail"]["code"] == "PREFERENCE_INSTRUCTION_REQUIRED"
    assert candidate["requires_instruction"] is True
    assert client.get(base + "/styles/active-context").json()["rules"] == []
    feedback = client.get(base + f"/feedback/{candidate['source_feedback_ids'][0]}").json()
    assert feedback["corrected_text"] == "corrected sample"


def test_explicit_confirmation_instruction_reaches_actual_model_request(client, project):
    candidate = create_feedback(client, project)
    base = f"/api/v1/projects/{project['id']}"
    instruction = "AUTHOR_PREFERENCE_927: use short action sentences."
    result = client.post(base + f"/feedback/preferences/{candidate['id']}/confirm",
                         json={"instruction": instruction})
    assert result.status_code == 200
    assert result.json()["instruction"] == instruction
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        session.get(StyleRule, result.json()["linked_style_rule_id"]).is_pinned = True
        _, fragments = initial_request(session, project["id"], None, "write an opening")
        assert instruction in json.dumps(fragments, ensure_ascii=False)
        assert "corrected sample" not in json.dumps(fragments, ensure_ascii=False)


@pytest.mark.parametrize("instruction", ["", "  \n ", "x" * 2001],
                         ids=["empty", "whitespace", "over-budget"])
def test_invalid_confirmation_instruction_has_no_side_effects(client, project, instruction):
    candidate = create_feedback(client, project, "Keep action concise.")
    base = f"/api/v1/projects/{project['id']}"
    response = client.post(base + f"/feedback/preferences/{candidate['id']}/confirm",
                           json={"instruction": instruction})
    assert response.status_code == 422
    assert client.get(base + "/styles/active-context").json()["rules"] == []
    assert client.get(base + "/preferences").json()[0]["status"] == "candidate"


def test_deleted_rule_is_not_silently_confirmed_or_recreated(client, project):
    candidate = create_feedback(client, project, "Keep action concise.")
    base = f"/api/v1/projects/{project['id']}"
    url = base + f"/feedback/preferences/{candidate['id']}/confirm"
    confirmed = client.post(url).json()
    rule_id = confirmed["linked_style_rule_id"]
    assert client.delete(base + f"/library/style_rule/{rule_id}").status_code == 200
    result = client.post(url, json={"instruction": "Do not override a deletion."})
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "PREFERENCE_RULE_UNAVAILABLE"
    assert "回收站" in result.json()["detail"]["message"]
    assert client.get(base + "/styles/active-context").json()["rules"] == []
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        rules = list(session.scalars(select(StyleRule).execution_options(include_deleted=True)))
        assert len(rules) == 1 and rules[0].deleted_at
        stored = session.get(PreferenceCandidate, candidate["id"])
        assert stored.instruction == "Keep action concise."
    assert client.post(base + f"/trash/style_rule/{rule_id}/restore").status_code == 200
    assert client.post(url).status_code == 200
    assert len(client.get(base + "/styles/active-context").json()["rules"]) == 1


def test_confirmation_is_idempotent_and_disabled_live_rule_can_be_enabled(client, project):
    candidate = create_feedback(client, project, "Keep action concise.")
    base = f"/api/v1/projects/{project['id']}"
    url = base + f"/feedback/preferences/{candidate['id']}/confirm"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.post(url), range(2)))
    assert all(result.status_code == 200 for result in results)
    assert len({result.json()["linked_style_rule_id"] for result in results}) == 1
    rules = client.get(base + "/styles/active-context").json()["rules"]
    assert len(rules) == 1
    revision = rules[0]["revision"]
    assert client.post(url).status_code == 200
    assert client.get(base + "/styles/active-context").json()["rules"][0]["revision"] == revision
    assert client.post(base + f"/feedback/preferences/{candidate['id']}/disable").status_code == 200
    assert client.get(base + "/styles/active-context").json()["rules"] == []
    assert client.post(url).status_code == 200
    assert len(client.get(base + "/styles/active-context").json()["rules"]) == 1


def test_legacy_placeholder_rule_is_not_an_effective_preference(client, project):
    legacy = "后续生成应参考作者在“general”类别下的修正，避免重复同类问题。"
    candidate = create_feedback(client, project)
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        rule = StyleRule(project_id=project["id"], instruction=legacy,
                         rule_type="feedback", status="confirmed")
        session.add(rule)
        session.flush()
        stored = session.get(PreferenceCandidate, candidate["id"])
        stored.instruction = legacy
        stored.status = "confirmed"
        stored.linked_style_rule_id = rule.id
        rule_id = rule.id
    base = f"/api/v1/projects/{project['id']}"
    assert client.get(base + "/styles/active-context").json()["rules"] == []
    listed = client.get(base + "/preferences").json()[0]
    assert listed["requires_instruction"] is True
    with vault.database.session_scope() as session:
        _, fragments = initial_request(session, project["id"], None, "general 修正")
        assert rule_id not in {fragment["source_id"] for fragment in fragments}
        # Read-time protection must not rewrite historical evidence.
        assert session.get(StyleRule, rule_id).instruction == legacy
    response = client.post(base + f"/feedback/preferences/{candidate['id']}/confirm",
                           json={"instruction": "Use short sentences."})
    assert response.status_code == 200
    assert response.json()["linked_style_rule_id"] == rule_id
    assert response.json()["requires_instruction"] is False
    assert client.get(base + "/styles/active-context").json()["rules"][0]["instruction"] == (
        "Use short sentences."
    )
