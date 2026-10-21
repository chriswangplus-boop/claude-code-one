"""Exception types. Everything Relay raises deliberately derives from RelayError."""


class RelayError(Exception):
    """Base class for expected, user-facing failures."""


class ConfigError(RelayError):
    pass


class StateError(RelayError):
    pass


class JournalError(RelayError):
    pass


class IntegrityError(RelayError):
    pass


class OwnershipViolation(IntegrityError):
    pass


class SecretDetected(IntegrityError):
    pass


class SentinelInvalid(RelayError):
    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


class BrainError(RelayError):
    pass


class ContextGuardTripped(RelayError):
    def __init__(self, ctx_pct: float, call_id: str | None = None):
        self.ctx_pct = ctx_pct
        self.call_id = call_id
        super().__init__(f"DeepSeek context at {ctx_pct:.1%} (guard tripped)")


class BudgetExceeded(RelayError):
    pass


class VisionUnavailable(RelayError):
    pass


class AppMismatch(RelayError):
    pass


class RecoveryFailed(RelayError):
    pass


class PauseRequested(RelayError):
    pass


class WaitTimeout(RelayError):
    pass


class MonitorHalt(RelayError):
    pass
