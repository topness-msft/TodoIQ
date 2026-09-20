import json

import pytest

from src.services import generation, parsing
from tests.test_skill_workflow import OUTPUTS, PEOPLE


@pytest.mark.parametrize("skill", list(OUTPUTS))
def test_strict_inner_rendering_matches_parser(skill):
    value = OUTPUTS[skill]
    checked = generation.validate_output(json.dumps(value), generation.Skill(skill))
    rendered = generation.render_output(checked, skill, PEOPLE)
    assert rendered == parsing._render_skill(value, skill, PEOPLE)
    assert "<<<" not in rendered


@pytest.mark.parametrize("bad", [
    '```json\n{}\n```', '{} {}', '{"blocked":"a","blocked":"b"}',
    '{"blocked":true}', '{"blocked":"' + 'x' * 1001 + '"}', 'null',
    '{"blocked":"x","email":"invented@example.test"}',
    '{"slots":[]}', '{"task_id":1}', '{"blocked":"<<<SKILL_OUTPUT>>>"}',
])
def test_malformed_or_untrusted_transport_is_not_output(bad):
    with pytest.raises(ValueError):
        generation.validate_output(bad, generation.Skill.PREPARE)


@pytest.mark.parametrize("recipient", [-1, True, 50, "0", {"email": "invented@example.test"}])
def test_recipient_indexes_are_strict(recipient):
    with pytest.raises(ValueError):
        generation.validate_output(json.dumps({**OUTPUTS["respond-email"], "to": recipient}),
                                   generation.Skill.RESPOND_EMAIL)


@pytest.mark.parametrize("people", [[], [{"name": "Chosen", "unresolved": True}],
                                    [{"name": "Chosen"}], [{"name": "Guest", "email": "x@example.test", "user_type": "Guest"}]])
def test_unverified_or_empty_selected_people_never_become_recipients(people):
    with pytest.raises(ValueError):
        generation.render_output(OUTPUTS["respond-email"], "respond-email", people)
