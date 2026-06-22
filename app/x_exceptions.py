"""
Typed exceptions for the X Client module.

Callers (bot logic / job runners) catch these to decide what to do next —
e.g. retry a winner DM later vs. mark it permanently failed because the
user has DMs closed.
"""


class XClientError(Exception):
    """Base class for all X client errors."""


class RateLimitExceeded(XClientError):
    """All retry attempts were exhausted due to rate limiting."""


class DuplicateContentError(XClientError):
    """X rejected the post because it's a duplicate of a recent post (403)."""


class DirectMessageBlocked(XClientError):
    """Recipient does not accept DMs from the bot, or has blocked it."""


class TweetNotFound(XClientError):
    """The referenced tweet/conversation no longer exists or is inaccessible."""


class UserNotFound(XClientError):
    """The referenced user does not exist or is suspended."""


class XAPIError(XClientError):
    """Catch-all for unexpected X API failures after retries are exhausted."""

    def __init__(self, message: str, original_exception: Exception | None = None):
        super().__init__(message)
        self.original_exception = original_exception
