"""
Domain exceptions for Stage 8 Counterfactual Attribution.
"""


class AttributionError(Exception):
    """Base domain exception for Stage 8 attribution errors."""
    pass


class MissingUpstreamDataError(AttributionError):
    """Raised when required upstream data for attribution evaluation cannot be found."""
    pass


class InvalidAttributionInputError(AttributionError):
    """Raised when attribution input parameters or timestamps are invalid."""
    pass
