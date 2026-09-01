class IngestionError(Exception):
    """Base class for all ingestion related exceptions."""
    pass


class MalformedInputError(IngestionError):
    """Input cannot be parsed into the expected payload representation."""
    pass


class SchemaValidationError(IngestionError):
    """Required fields are missing or types/structures are invalid."""
    pass


class UnsupportedSourceError(IngestionError):
    """The source is not supported."""
    pass


class NormalizationError(IngestionError):
    """The payload is structurally valid but contains invalid semantic values."""
    pass


class DuplicateEventConflictError(IngestionError):
    """The same source identity has already been persisted with a different payload hash."""
    pass


class PersistenceError(IngestionError):
    """Database/persistence operation failed."""
    pass


class InvalidWebhookSignatureError(IngestionError):
    """Webhook signature verification failed."""
    pass
