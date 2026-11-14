"""The keepcode window: the same scan and backup as the terminal, in an app window."""


def run(smoke_test: bool = False) -> int:
    """Opens the window and returns the exit code. Raises AppUnavailable when it cannot open."""
    from keepcode.app.window import run as run_window

    return run_window(smoke_test=smoke_test)
