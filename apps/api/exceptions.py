"""Exceptions raised inside the API process."""


class ViewerLeft(Exception):
    """A WebSocket viewer disconnected.

    Not an error: it is how the listener task ends its TaskGroup, so the group
    cancels the sender. Caught by `forward_until_disconnect` and never logged.
    """
