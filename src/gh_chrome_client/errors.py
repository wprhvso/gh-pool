from gh_chrome_protocol import CommandError, ErrorCode


class GhChromeError(Exception):
    pass


class CommandTimeout(GhChromeError, TimeoutError):
    pass


class ElementNotFound(GhChromeError):
    pass


class ElementIntercepted(GhChromeError):
    pass


class NavigationFailed(GhChromeError):
    pass


class Cancelled(GhChromeError):
    pass


class SessionDead(GhChromeError):
    pass


class RunnerError(GhChromeError):
    pass


class SessionUnavailable(GhChromeError):
    pass


class TooManySessions(GhChromeError):
    pass


class SessionNotReady(GhChromeError):
    pass


BY_CODE: dict[ErrorCode, type[GhChromeError]] = {
    ErrorCode.TIMEOUT: CommandTimeout,
    ErrorCode.NOT_FOUND: ElementNotFound,
    ErrorCode.INTERCEPTED: ElementIntercepted,
    ErrorCode.NAVIGATION_FAILED: NavigationFailed,
    ErrorCode.CANCELLED: Cancelled,
    ErrorCode.SESSION_DEAD: SessionDead,
    ErrorCode.RUNNER_ERROR: RunnerError,
}


def to_exception(error: CommandError) -> GhChromeError:
    return BY_CODE[error.code](error.message)
