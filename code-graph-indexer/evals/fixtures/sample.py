"""Small fixture: one class, two funcs, with and without docstring/logging."""
import logging

logger = logging.getLogger(__name__)


class Greeter:
    """A friendly greeter."""

    def greet(self, name: str) -> str:
        """Return a greeting for `name`."""
        return _format_message(name)


def _format_message(name):
    try:
        return f"Hello, {name}!"
    except Exception as e:
        logger.error("format failed: %s", e)
        raise


def silent_divide(a, b):
    try:
        return a / b
    except ZeroDivisionError:
        return None
