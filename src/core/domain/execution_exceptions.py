"""
Stage 7 domain exceptions.
"""


class Stage7ExecutionError(Exception):
    """Base exception for Stage 7 execution and observation errors."""


class InvalidDecisionForExecutionError(Stage7ExecutionError):
    """Raised when a non-ACT decision or invalid decision is presented for execution."""


class CommandNotFoundError(Stage7ExecutionError):
    """Raised when a referenced command cannot be found."""


class IllegalStateTransitionError(Stage7ExecutionError):
    """Raised when an illegal command state machine transition is attempted."""


class CommandExpiredError(Stage7ExecutionError):
    """Raised when attempting to execute a command that has passed its expiry deadline."""


class CommandNotNeededError(Stage7ExecutionError):
    """Raised or used when a payment was already terminal prior to dispatch."""


class RouteDisabledError(Stage7ExecutionError):
    """Raised when the selected route is disabled by policy."""
