"""Who may do what. On Databricks the platform authenticates people (the review notebook runs as them) and the
`reviewers` table maps them to a role; this module only holds the errors the rules raise."""

from aml_copilot.errors import AMLCopilotError

ROLES = ("investigator", "supervisor")


class AuthError(AMLCopilotError):
    """The caller could not be identified."""


class ForbiddenError(AMLCopilotError):
    """The caller is known but their role does not allow this."""


__all__ = ["ROLES", "AuthError", "ForbiddenError"]
