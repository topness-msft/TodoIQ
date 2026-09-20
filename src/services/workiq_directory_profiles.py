"""Pure directory projections; no transport, persistence, or canonical binding.

Profiles contain aad_object_id, display_name, email, upn, and user_type.
Guest profiles additionally carry resolution=external_unresolved, regardless
of their addresses. Exact-name results are candidate lists, never selections.
Future backfill callers supply their own captured lookup/slot metadata.
"""

from .workiq_policy import (
    CapabilityError, DirectoryLookupOperation, require_directory_operation,
    _directory_aad, _directory_name, _recovery_email,
)


class DirectoryProfileError(ValueError):
    """Provider data cannot establish the requested identity."""


class DirectoryNoMatchError(DirectoryProfileError):
    """A complete exact-address query returned no profiles."""


class DirectoryAmbiguityError(DirectoryProfileError):
    """A complete exact-address query returned two distinct candidates."""


def project_profile(data: object, *, expected_aad: str | None = None) -> dict:
    """Validate one complete profile without trusting names as identity."""
    if not isinstance(data, dict) or "value" in data or "@odata.nextLink" in data:
        raise DirectoryProfileError("Directory profile was not a complete object.")
    try:
        identifier = _directory_aad(data.get("id"))
        expected_aad = _directory_aad(expected_aad) if expected_aad is not None else None
        name = _directory_name(data.get("displayName"))
        email = _recovery_email(data["mail"]) if data.get("mail") is not None else None
        upn = _recovery_email(data["userPrincipalName"]) if data.get("userPrincipalName") is not None else None
    except CapabilityError:
        raise DirectoryProfileError("Directory profile identity was invalid.") from None
    if expected_aad is not None and identifier != expected_aad:
        raise DirectoryProfileError("Directory profile did not match the exact AAD query.")
    user_type = data.get("userType")
    if user_type not in ("Member", "Guest") or (user_type == "Member" and not (email or upn)):
        raise DirectoryProfileError("Directory profile was not an email-backed member or guest.")
    result = {
        "aad_object_id": identifier, "display_name": name,
        "email": email, "upn": upn, "user_type": user_type,
    }
    if user_type == "Guest":
        result["resolution"] = "external_unresolved"
    return result


def project_lookup(data: object, operation: DirectoryLookupOperation) -> dict:
    """Validate exact-query results; candidate pages are never auto-selected.

    Transport validates status and header completeness before this function.
    Body pagination is independently rejected here, including for direct pure
    use. Duplicate IDs, including case variants, cannot imply uniqueness.
    """
    operation = require_directory_operation(operation)
    if operation.kind in ("self", "aad_exact"):
        return project_profile(
            data, expected_aad=operation.query_value if operation.kind == "aad_exact" else None,
        )
    if not isinstance(data, dict) or "id" in data or "@odata.nextLink" in data:
        raise DirectoryProfileError("Directory candidates were not a complete collection.")
    values = data.get("value")
    limit = 2 if operation.kind == "email_exact" else 10
    if not isinstance(values, list) or len(values) > limit:
        raise DirectoryProfileError("Directory candidates exceeded the fixed bound.")
    profiles = [project_profile(value) for value in values]
    identifiers = [profile["aad_object_id"].lower() for profile in profiles]
    if len(set(identifiers)) != len(identifiers):
        raise DirectoryProfileError("Directory candidates contained duplicate identities.")
    if operation.kind == "email_exact":
        if not profiles:
            raise DirectoryNoMatchError("Directory returned no exact address match.")
        # A full two-row page is ambiguous even when one row looks preferable.
        if len(profiles) == 2:
            raise DirectoryAmbiguityError("Directory returned multiple address candidates.")
        profile = profiles[0]
        if operation.query_value not in (profile["email"], profile["upn"]):
            raise DirectoryProfileError("Directory profile did not match the exact address.")
        return profile
    if any(profile["display_name"].casefold() != operation.query_value.casefold() for profile in profiles):
        raise DirectoryProfileError("Directory candidate did not match the exact full name.")
    return {"candidates": profiles}
