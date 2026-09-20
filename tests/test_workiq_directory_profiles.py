"""Inactive exact-directory reads: policy, pure projection, and owned MCP wire."""

from dataclasses import replace
import importlib
import json
from urllib.parse import quote

import pytest

from src.services import workiq_policy as policy, workiq_runtime as wire
from tests.test_workiq_runtime_peer import (
    source_runtime, source_envelope, source_calls, expected_source_calls,
    assert_source_scrubbed,
)


SELECT = "id,displayName,mail,userPrincipalName,userType"
SELF_PATH = f"/me?$select={SELECT}"


def aad(index=1):
    return f"abcdef00-0000-0000-0000-{index:012x}"


def profile(index=1, **changes):
    return {
        "id": aad(index), "displayName": f"Synthetic Person {index}",
        "mail": f"person{index}@example.test",
        "userPrincipalName": f"upn{index}@example.test", "userType": "Member",
        "providerSecret": "directory-provider-private", **changes,
    }


def projected(index=1, **changes):
    return {
        "aad_object_id": aad(index), "display_name": f"Synthetic Person {index}",
        "email": f"person{index}@example.test", "upn": f"upn{index}@example.test",
        "user_type": "Member", **changes,
    }


def aad_path(index=1):
    return f"/users/{aad(index)}?$select={SELECT}"


def email_path(address):
    literal = address.lower().replace("'", "''")
    query = quote(f"mail eq '{literal}' or userPrincipalName eq '{literal}'", safe="")
    return f"/users?$filter={query}&$select={SELECT}&$top=2"


def name_path(name):
    literal = name.replace("'", "''")
    query = quote(f"displayName eq '{literal}'", safe="")
    return f"/users?$filter={query}&$select={SELECT}&$top=10"


def assert_directory_scrubbed(runtime):
    assert_source_scrubbed(runtime)
    retained = json.dumps(runtime.snapshot()) + repr([
        vars(operation) for operation in runtime._operations.values()
    ]) + repr(list(runtime._incoming.queue)) + repr(list(runtime._commands.queue))
    for marker in [aad(), aad(2), "Synthetic Person", "person1@", "person2@", "upn1@",
                   "directory-provider-private", "membership-private", "directory-chat-private"]:
        assert marker not in retained
    for operation in runtime._operations.values():
        assert operation.directory is None and operation.saved_teams is None
        if operation.plan in {"directory", "saved_teams"}:
            assert "data" not in runtime.wait(operation.job_id)


@pytest.mark.parametrize("kind,value,path", [
    ("self", None, SELF_PATH),
    ("aad_exact", aad(), aad_path()),
    ("aad_exact", aad().upper(), aad_path()),
    ("email_exact", "O'NEIL+tag@example.test", email_path("o'neil+tag@example.test")),
    ("full_name_candidates", "O'Neil & $top=999", name_path("O'Neil & $top=999")),
])
def test_policy_fixed_paths_and_value_bound_mints(kind, value, path):
    operation = policy.build_directory_operation(kind, value)
    assert policy._directory_path(operation) == path
    assert policy.require_directory_operation(operation) == operation
    for forged in [replace(operation, query_value="foreign"), replace(operation, kind="self"),
                   replace(operation, _mint=object())]:
        if forged == operation and forged._mint == operation._mint:
            continue
        with pytest.raises(policy.CapabilityError):
            policy.require_directory_operation(forged)


@pytest.mark.parametrize("kind,value", [
    ("url", "/users"), ("self", "other"), ("aad_exact", "not-a-guid"),
    ("aad_exact", aad() + "/extra"), ("aad_exact", aad() + "?$top=2"),
    ("aad_exact", " " + aad()), ("aad_exact", {"id": aad()}),
    ("email_exact", "/o=Exchange/cn=Someone"), ("email_exact", "Name"),
    ("email_exact", "a@b\n"), ("email_exact", {"email": "a@example.test"}),
    ("full_name_candidates", ""), ("full_name_candidates", "x\n"),
    ("full_name_candidates", "x" * 257), ("full_name_candidates", ["/users"]),
])
def test_policy_bad_inputs_cannot_reach_startup(kind, value):
    with pytest.raises(policy.CapabilityError):
        policy.build_directory_operation(kind, value)


@pytest.mark.parametrize("mode", ["directory", "saved_teams", "directory_lookup", "fetch"])
def test_generic_submit_is_not_a_new_proxy(mode):
    runtime = wire.WorkIQRuntime(command=lambda: pytest.fail("must not start"))
    with pytest.raises(policy.CapabilityError):
        runtime.submit(mode)


def test_pure_projection_has_no_display_name_authority_or_provider_extras():
    pure = importlib.import_module("src.services.workiq_directory_profiles")
    assert pure.project_profile(profile(), expected_aad=aad()) == projected()
    assert pure.project_profile(profile(mail=None)) == projected(email=None)
    assert pure.project_profile(profile(userType="Guest", mail=None, userPrincipalName=None)) == projected(
        email=None, upn=None, user_type="Guest", resolution="external_unresolved",
    )
    assert pure.project_profile(profile(userType="Guest")) == projected(
        user_type="Guest", resolution="external_unresolved",
    )


@pytest.mark.parametrize("returned_id,expected_id", [
    (aad().upper(), aad()), (aad(), aad().upper()), (aad().upper(), aad().upper()),
])
def test_pure_aad_identity_is_case_insensitive_and_output_is_canonical(returned_id, expected_id):
    pure = importlib.import_module("src.services.workiq_directory_profiles")
    assert pure.project_profile(profile(id=returned_id), expected_aad=expected_id) == projected()


@pytest.mark.parametrize("changes", [
    {"id": None}, {"id": "private-not-guid"}, {"id": aad(2).upper()},
    {"displayName": None}, {"displayName": " "}, {"displayName": "x\u0000"},
    {"displayName": "x" * 257}, {"mail": "Display Name"}, {"mail": ""},
    {"userPrincipalName": "/o=Org/ou=Unit/cn=Recipients/cn=Person"},
    {"mail": None, "userPrincipalName": None}, {"userType": "member"},
    {"userType": None}, {"userType": "External"}, {"value": []},
    {"@odata.nextLink": "https://graph.microsoft.com/next"},
])
def test_pure_projection_rejects_unproven_or_partial_profile(changes):
    pure = importlib.import_module("src.services.workiq_directory_profiles")
    with pytest.raises(pure.DirectoryProfileError):
        pure.project_profile(profile(**changes), expected_aad=aad())


@pytest.mark.parametrize("method,arg,page,expected,path", [
    ("read_self_profile", None, profile(), projected(), SELF_PATH),
    ("read_self_profile", None, profile(id=aad().upper()), projected(), SELF_PATH),
    ("read_directory_user_by_aad", aad(), profile(), projected(), aad_path()),
    ("read_directory_user_by_aad", aad().upper(), profile(), projected(), aad_path()),
    ("read_directory_user_by_aad", aad(), profile(id=aad().upper()), projected(), aad_path()),
    ("read_directory_user_by_aad", aad().upper(), profile(id=aad().upper()), projected(), aad_path()),
    ("read_directory_user_by_email", "PERSON1@EXAMPLE.TEST", {"value": [profile()]}, projected(), email_path("person1@example.test")),
    ("read_directory_user_by_email", "upn1@example.test", {"value": [profile(mail=None)]}, projected(email=None), email_path("upn1@example.test")),
    ("read_directory_user_by_email", "person1@example.test", {"value": [profile(userType="Guest")]},
     projected(user_type="Guest", resolution="external_unresolved"), email_path("person1@example.test")),
])
def test_exact_directory_wire_and_projection(tmp_path, method, arg, page, expected, path):
    runtime, trace = source_runtime(tmp_path, [page])
    try:
        call = getattr(runtime, method)
        result = call(timeout=3) if arg is None else call(arg, timeout=3)
        assert result == expected
        assert source_calls(trace) == expected_source_calls([path])
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("page,code", [
    ({"value": []}, "directory_not_found"),
    ({"value": [profile(), profile(2)]}, "directory_ambiguous"),
    ({"value": [profile(), profile()]}, "source_unreadable"),
    ({"value": [profile(), profile(id=aad().upper())]}, "source_unreadable"),
    ({"value": [profile(), profile(2), profile(3)]}, "source_unreadable"),
    ({"value": [profile(2)]}, "source_unreadable"),
    ({"value": [None]}, "source_unreadable"),
    ({"value": [profile(mail=None, userPrincipalName=None)]}, "source_unreadable"),
    ({"value": [profile()], "@odata.nextLink": "https://graph.microsoft.com/next"}, "source_partial"),
])
def test_email_never_selects_first_or_matches_display_name(tmp_path, page, code):
    runtime, trace = source_runtime(tmp_path, [page])
    try:
        with pytest.raises(wire.WorkIQError) as error:
            runtime.read_directory_user_by_email("person1@example.test", timeout=3)
        assert error.value.code == code
        assert source_calls(trace) == expected_source_calls([email_path("person1@example.test")])
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("count", [0, 1, 2, 10])
def test_exact_name_always_returns_candidates_without_binding(tmp_path, count):
    name = "Synthetic Exact Name"
    values = [profile(i, displayName=name, userType="Guest" if i == 2 else "Member") for i in range(count)]
    runtime, trace = source_runtime(tmp_path, [{"value": values}])
    try:
        candidates = runtime.find_directory_users_by_exact_name(name, timeout=3)
        assert candidates == [
            projected(i, display_name=name, **(
                {"user_type": "Guest", "resolution": "external_unresolved"} if i == 2 else {}
            )) for i in range(count)
        ]
        assert all("confirmed" not in row and "canonical_person_id" not in row for row in candidates)
        assert source_calls(trace) == expected_source_calls([name_path(name)])
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("page", [
    {"value": [profile()] * 2}, {"value": [profile(id=aad()), profile(id=aad().upper())]},
    {"value": [profile(i) for i in range(11)]}, {"value": [profile(displayName="Name Extra")]},
    {"value": [profile(userType="Guest", displayName="Other")]},
    {"value": [profile(displayName="Name"), {}]}, {"value": "wrong"},
    {"value": [profile(displayName="Name", mail=None, userPrincipalName=None)]},
])
def test_name_malformed_duplicate_or_query_mismatch_is_not_a_candidate(tmp_path, page):
    runtime, trace = source_runtime(tmp_path, [page])
    try:
        with pytest.raises(wire.SourceUnreadableError):
            runtime.find_directory_users_by_exact_name("Name", timeout=3)
        assert len(source_calls(trace)) == 1
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("method,page", [
    ("read_self_profile", profile()),
    ("read_directory_user_by_aad", profile()),
    ("read_directory_user_by_email", {"value": [profile()]}),
    ("find_directory_users_by_exact_name", {"value": [profile()]}),
])
@pytest.mark.parametrize("pagination", ["body", "header"])
def test_only_context_may_be_partial_not_any_directory_read(tmp_path, method, page, pagination):
    result = source_envelope(page)
    if pagination == "body":
        page["@odata.nextLink"] = "https://graph.microsoft.com/private-next"
    else:
        result["structuredContent"]["results"][0]["headers"] = {"Link": "<https://graph.microsoft.com/private-next>; rel=next"}
    runtime, trace = source_runtime(tmp_path, steps=[{"result": result}])
    try:
        args = {"read_self_profile": [], "read_directory_user_by_aad": [aad()],
                "read_directory_user_by_email": ["person1@example.test"],
                "find_directory_users_by_exact_name": ["Synthetic Person 1"]}[method]
        with pytest.raises(wire.SourcePartialError):
            getattr(runtime, method)(*args, timeout=3)
        assert len(source_calls(trace)) == 1
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("changes", [{"id": aad(2)}, {"id": aad(2).upper()}, {"value": []},
                                    {"userType": "Other"}, {"mail": None, "userPrincipalName": None}])
def test_aad_exact_profile_mismatch_fails_on_wire(tmp_path, changes):
    runtime, trace = source_runtime(tmp_path, [profile(**changes)])
    try:
        with pytest.raises(wire.SourceUnreadableError):
            runtime.read_directory_user_by_aad(aad(), timeout=3)
        assert source_calls(trace) == expected_source_calls([aad_path()])
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("status,code", [(401, "auth_required"), (403, "source_forbidden"),
                                        (404, "directory_not_found"), (429, "source_http"), (503, "source_http")])
def test_directory_http_failures_are_not_empty_matches(tmp_path, status, code):
    runtime, trace = source_runtime(tmp_path, steps=[{"result": source_envelope({}, status)}])
    try:
        with pytest.raises(wire.WorkIQError) as error:
            runtime.read_directory_user_by_email("person1@example.test", timeout=3)
        assert error.value.code == code
        assert len(source_calls(trace)) == 1
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("code", ["auth_required", "consent_required", "eula_required"])
@pytest.mark.parametrize("rpc_error", [False, True])
def test_directory_uses_existing_private_source_cooldown(tmp_path, code, rpc_error):
    now = [100.0]
    blocker = (
        {"error": {"code": code, "message": "directory-provider-private"}}
        if rpc_error else {"result": {
            "content": [{"type": "text", "text": "directory-provider-private"}],
            "isError": True, "_meta": {"code": code},
        }}
    )
    runtime, trace = source_runtime(tmp_path, steps=[
        blocker, {"result": source_envelope(profile())},
    ], monotonic_clock=lambda: now[0])
    try:
        for _ in range(2):
            with pytest.raises(wire.WorkIQError) as error:
                runtime.read_self_profile(timeout=3)
            assert error.value.code == code
        assert len(source_calls(trace)) == 1
        now[0] += wire.SOURCE_AUTH_COOLDOWN_SECONDS + 1
        assert runtime.read_self_profile(timeout=3) == projected()
        assert len(source_calls(trace)) == 2
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_private_admission_rejects_forgery_and_cross_payloads_before_startup():
    runtime = wire.WorkIQRuntime(command=lambda: pytest.fail("must not launch"))
    minted = policy.build_directory_operation("aad_exact", aad())
    for invalid in [replace(minted, query_value=aad(2)), {"kind": "self"}, "/me"]:
        with pytest.raises(policy.CapabilityError):
            runtime._read_sealed_source(invalid, runtime._monotonic_clock() + 3)
    for plan in ["source", "recovery", "ask", "calendar"]:
        with pytest.raises(policy.CapabilityError):
            runtime.submit(plan, **{plan: minted})
    assert not runtime._operations


@pytest.mark.parametrize("step,code", [
    ({"protocol": True}, "protocol"), ({"eof": True}, "transport"),
    ({"error": {"code": -32603, "message": "directory-provider-private"}}, "remote"),
    ({"result": source_envelope([])}, "source_unreadable"),
    ({"result": {"content": [], "structuredContent": {"results": []}}}, "source_unreadable"),
])
def test_directory_transport_and_malformed_failures_are_private_not_no_match(tmp_path, step, code):
    runtime, trace = source_runtime(tmp_path, steps=[step])
    try:
        with pytest.raises(wire.WorkIQError) as error:
            runtime.read_self_profile(timeout=3)
        assert error.value.code == code
        assert len(source_calls(trace)) == 1
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()


def test_wrong_returned_entity_url_never_proves_a_directory_profile(tmp_path):
    result = source_envelope(profile())
    result["structuredContent"]["results"][0]["entityUrl"] = aad_path(2)
    runtime, trace = source_runtime(tmp_path, steps=[{"result": result}])
    try:
        with pytest.raises(wire.SourceUnreadableError):
            runtime.read_directory_user_by_aad(aad(), timeout=3)
        assert source_calls(trace) == expected_source_calls([aad_path()])
        assert_directory_scrubbed(runtime)
    finally:
        runtime.shutdown()
