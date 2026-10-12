"""Exceptions raised by the ingestion layer."""


class AMLCopilotError(Exception):
    """Base class for all project errors."""


class ConfigError(AMLCopilotError):
    """A required setting (credential, connection string) is missing."""


class InvalidAccountIdError(AMLCopilotError, ValueError):
    """The account id is not in the ACC-[S|L|N]-nnnnn format."""


class SourceUnavailableError(AMLCopilotError):
    """A source system could not be queried (network, auth, driver error)."""

    def __init__(self, source: str, detail: str = ""):
        self.source = source
        super().__init__(f"{source} unavailable" + (f": {detail}" if detail else ""))


class ModelOutputError(AMLCopilotError):
    """The LLM's answer did not match the structured schema the agent asked for."""


class IndexNotBuiltError(AMLCopilotError):
    """The vector index does not exist yet - run `python -m aml_copilot.retrieval build`."""


class SchemaError(AMLCopilotError):
    """A source table does not have the columns the pipeline relies on."""


class EntityResolutionError(AMLCopilotError):
    """Records from the sources do not all belong to the requested account."""
