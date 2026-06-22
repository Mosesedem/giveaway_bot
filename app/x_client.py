"""
X Client Module for the Nigeria Giveaway Bot
Handles all X (Twitter) API v2 interactions using Tweepy.

This module supports the 2-Phase Monitoring strategy:
- Phase 1: Broad search (crawling)
- Phase 2: Precise mentions, DMs, and thread monitoring

Design notes (v2):
  - All transient/rate-limit failures retry, then raise typed exceptions
    (see x_exceptions.py) instead of silently returning None/False.
  - search/mentions/thread calls fully paginate up to a configurable cap.
  - since_id cursors and dedup state are persisted via StateStore (SQLite),
    so a crash/restart never reprocesses entries or double-DMs a winner.
  - The client is NOT instantiated at import time — call get_x_client()
    so importing this module doesn't require live credentials (testable).
"""

import os
import logging
from typing import Optional, List, Dict, Any

import requests
import tweepy
from tenacity import (
    Retrying,
    stop_after_attempt,
    wait_exponential_jitter,
    retry_if_exception_type,
)
from dotenv import load_dotenv

from app.x_exceptions import (
    RateLimitExceeded,
    DuplicateContentError,
    DirectMessageBlocked,
    TweetNotFound,
    UserNotFound,
    XAPIError,
)
from app.state_store import StateStore

load_dotenv()

logger = logging.getLogger(__name__)

MAX_TWEET_LENGTH = 280
DEFAULT_MAX_PAGES = 10  # safety cap so a runaway stream can't paginate forever

_REQUIRED_CREDENTIAL_KEYS = (
    "X_BEARER_TOKEN",
    "X_API_KEY",
    "X_API_SECRET",
    "X_ACCESS_TOKEN",
    "X_ACCESS_TOKEN_SECRET",
)


def x_credentials_configured() -> bool:
    """True when all five X API credential env vars are set and non-empty."""
    return all(os.getenv(key) for key in _REQUIRED_CREDENTIAL_KEYS)


def missing_credential_keys() -> list[str]:
    return [key for key in _REQUIRED_CREDENTIAL_KEYS if not os.getenv(key)]


class XClient:
    """Wrapper around tweepy.Client for the giveaway bot."""

    def __init__(self, state_store: Optional[StateStore] = None):
        self._api_key = os.getenv("X_API_KEY")
        self._api_secret = os.getenv("X_API_SECRET")
        self._access_token = os.getenv("X_ACCESS_TOKEN")
        self._access_token_secret = os.getenv("X_ACCESS_TOKEN_SECRET")
        self.client = self._get_authenticated_client()
        self._api_v1: Optional[tweepy.API] = None
        self.bot_user_id: Optional[int] = None
        self.bot_username: Optional[str] = None
        self.state = state_store or StateStore()

    def _get_authenticated_client(self) -> tweepy.Client:
        """Create authenticated Tweepy Client."""
        bearer_token = os.getenv("X_BEARER_TOKEN")

        if not x_credentials_configured():
            missing = ", ".join(missing_credential_keys())
            raise ValueError(f"Missing required X API credentials: {missing}")

        return tweepy.Client(
            bearer_token=bearer_token,
            consumer_key=self._api_key,
            consumer_secret=self._api_secret,
            access_token=self._access_token,
            access_token_secret=self._access_token_secret,
            wait_on_rate_limit=True,
        )

    def _get_api_v1(self) -> tweepy.API:
        """Lazy v1.1 API — used for friendship lookups not exposed in v2."""
        if self._api_v1 is None:
            auth = tweepy.OAuth1UserHandler(
                self._api_key,
                self._api_secret,
                self._access_token,
                self._access_token_secret,
            )
            self._api_v1 = tweepy.API(auth, wait_on_rate_limit=True)
        return self._api_v1

    # ============================================================
    # HELPER: Retry + typed-exception translation
    # ============================================================
    _RETRYABLE = (
        tweepy.TooManyRequests,
        tweepy.TwitterServerError,
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    )

    def _call_with_retry(self, fn, *args, **kwargs):
        """
        Run an X API call with retry on transient errors, then translate
        any failure (including exhausted retries) into a typed exception.
        """
        retryer = Retrying(
            stop=stop_after_attempt(5),
            wait=wait_exponential_jitter(initial=2, max=60),
            retry=retry_if_exception_type(self._RETRYABLE),
            reraise=True,
        )
        try:
            for attempt in retryer:
                with attempt:
                    return fn(*args, **kwargs)
        except tweepy.TooManyRequests as e:
            logger.warning(f"Rate limit exhausted after retries: {e}")
            raise RateLimitExceeded(str(e)) from e
        except tweepy.Forbidden as e:
            msg = str(e).lower()
            if "duplicate" in msg:
                logger.warning(f"Duplicate content rejected: {e}")
                raise DuplicateContentError(str(e)) from e
            if "not allowed to send" in msg or "cannot send" in msg:
                logger.warning(f"DM blocked by recipient: {e}")
                raise DirectMessageBlocked(str(e)) from e
            logger.error(f"Forbidden: {e}")
            raise XAPIError(f"Forbidden: {e}", original_exception=e) from e
        except tweepy.NotFound as e:
            logger.warning(f"Not found: {e}")
            raise TweetNotFound(str(e)) from e
        except tweepy.TweepyException as e:
            logger.error(f"Unhandled X API error: {e}")
            raise XAPIError(str(e), original_exception=e) from e
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            logger.error(f"Network error talking to X API (retries exhausted): {e}")
            raise XAPIError(f"Network error: {e}", original_exception=e) from e

    # ============================================================
    # BOT IDENTITY
    # ============================================================
    def get_bot_identity(self) -> Dict[str, Any]:
        """Get and cache the bot's user ID and username."""
        if self.bot_user_id and self.bot_username:
            return {"user_id": self.bot_user_id, "username": self.bot_username}

        me = self._call_with_retry(self.client.get_me, user_fields=["id", "username"])
        self.bot_user_id = me.data.id
        self.bot_username = me.data.username

        logger.info(f"Bot authenticated as @{self.bot_username} (ID: {self.bot_user_id})")
        return {"user_id": self.bot_user_id, "username": self.bot_username}

    # ============================================================
    # SHARED PAGINATION HELPER
    # ============================================================
    def _paginate_search(
        self,
        query: str,
        since_id: Optional[str],
        max_results_per_page: int,
        max_pages: int,
        tweet_fields: List[str],
        expansions: List[str],
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        next_token = None
        pages_fetched = 0

        while pages_fetched < max_pages:
            page = self._call_with_retry(
                self.client.search_recent_tweets,
                query=query,
                max_results=max_results_per_page,
                since_id=since_id,
                next_token=next_token,
                tweet_fields=tweet_fields,
                expansions=expansions,
            )
            pages_fetched += 1

            if page.data:
                for tweet in page.data:
                    results.append({
                        "id": str(tweet.id),
                        "text": tweet.text,
                        "author_id": str(tweet.author_id),
                        "conversation_id": str(getattr(tweet, "conversation_id", tweet.id)),
                        "created_at": tweet.created_at,
                    })

            next_token = page.meta.get("next_token") if page.meta else None
            if not next_token:
                break

        if pages_fetched >= max_pages and next_token:
            logger.warning(
                f"Pagination cap ({max_pages} pages) hit for query '{query}'; "
                f"more results may exist. Consider narrowing the query or "
                f"running the crawl more frequently."
            )

        return results

    # ============================================================
    # PHASE 1: BROAD SEARCH / CRAWLING
    # ============================================================
    def search_recent_tweets(
        self,
        query: str,
        max_results: int = 100,
        since_id: Optional[str] = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        tweet_fields: Optional[List[str]] = None,
        expansions: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Phase 1 - Broad, fully-paginated crawl for giveaway commands and activity."""
        default_fields = ["created_at", "author_id", "conversation_id", "text"]
        default_expansions = ["author_id"]

        return self._paginate_search(
            query=query,
            since_id=since_id,
            max_results_per_page=min(max_results, 100),
            max_pages=max_pages,
            tweet_fields=tweet_fields or default_fields,
            expansions=expansions or default_expansions,
        )

    # ============================================================
    # PHASE 2: PRECISE MENTIONS
    # ============================================================
    def get_user_mentions(
        self,
        user_id: Optional[int] = None,
        max_results: int = 100,
        since_id: Optional[str] = None,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> List[Dict[str, Any]]:
        """Phase 2 - Fully-paginated recent mentions to the bot."""
        if user_id is None:
            identity = self.get_bot_identity()
            user_id = identity["user_id"]

        results: List[Dict[str, Any]] = []
        next_token = None
        pages_fetched = 0

        while pages_fetched < max_pages:
            page = self._call_with_retry(
                self.client.get_users_mentions,
                id=user_id,
                max_results=min(max_results, 100),
                since_id=since_id,
                pagination_token=next_token,
                tweet_fields=["created_at", "author_id", "conversation_id", "text"],
                expansions=["author_id"],
            )
            pages_fetched += 1

            if page.data:
                for tweet in page.data:
                    results.append({
                        "id": str(tweet.id),
                        "text": tweet.text,
                        "author_id": str(tweet.author_id),
                        "conversation_id": str(getattr(tweet, "conversation_id", tweet.id)),
                        "created_at": tweet.created_at,
                    })

            next_token = page.meta.get("next_token") if page.meta else None
            if not next_token:
                break

        if pages_fetched >= max_pages and next_token:
            logger.warning(f"Pagination cap ({max_pages} pages) hit for mentions of user {user_id}.")

        return results

    def get_new_mentions(self, max_pages: int = DEFAULT_MAX_PAGES) -> List[Dict[str, Any]]:
        """
        Convenience: fetches only mentions newer than the last persisted
        cursor, advances the cursor, and filters out anything already
        marked processed (belt-and-braces dedup).
        """
        since_id = self.state.get_since_id("mentions")
        mentions = self.get_user_mentions(since_id=since_id, max_pages=max_pages)

        if mentions:
            newest_id = max(mentions, key=lambda t: int(t["id"]))["id"]
            self.state.set_since_id("mentions", newest_id)

        unprocessed_ids = set(self.state.filter_unprocessed([m["id"] for m in mentions]))
        return [m for m in mentions if m["id"] in unprocessed_ids]

    # ============================================================
    # THREAD / GIVEAWAY ENTRY COLLECTION
    # ============================================================
    def get_thread_replies(
        self,
        conversation_id: str,
        max_results: int = 100,
        since_id: Optional[str] = None,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> List[Dict[str, Any]]:
        """
        Fully-paginated collection of participant entries from a giveaway thread.

        Note: this uses the recent-search index (last 7 days, best-effort
        completeness). For giveaways that must run longer than 7 days or
        require guaranteed-complete entry capture, poll frequently and
        persist results via state.mark_processed() as you go, rather than
        relying on a single late call to reconstruct history.
        """
        query = f"conversation_id:{conversation_id}"

        return self._paginate_search(
            query=query,
            since_id=since_id,
            max_results_per_page=min(max_results, 100),
            max_pages=max_pages,
            tweet_fields=["created_at", "author_id", "text"],
            expansions=["author_id"],
        )

    def get_new_thread_replies(
        self, conversation_id: str, max_pages: int = DEFAULT_MAX_PAGES
    ) -> List[Dict[str, Any]]:
        """Convenience: cursor-tracked + dedup'd entry collection for one thread."""
        stream_key = f"thread:{conversation_id}"
        since_id = self.state.get_since_id(stream_key)
        replies = self.get_thread_replies(conversation_id, since_id=since_id, max_pages=max_pages)

        if replies:
            newest_id = max(replies, key=lambda t: int(t["id"]))["id"]
            self.state.set_since_id(stream_key, newest_id)

        unprocessed_ids = set(self.state.filter_unprocessed([r["id"] for r in replies]))
        return [r for r in replies if r["id"] in unprocessed_ids]

    # ============================================================
    # POSTING (REPLIES)
    # ============================================================
    def create_reply(
        self,
        text: str,
        in_reply_to_tweet_id: str,
        quote_tweet_id: Optional[str] = None,
    ) -> str:
        """
        Post a reply in a thread (used for confirmations, announcements, etc.).

        Raises:
            ValueError: text exceeds X's length limit.
            RateLimitExceeded, DuplicateContentError, TweetNotFound, XAPIError
        """
        if len(text) > MAX_TWEET_LENGTH:
            raise ValueError(
                f"Reply text is {len(text)} chars, exceeds the {MAX_TWEET_LENGTH} limit"
            )

        kwargs = {
            "text": text,
            "in_reply_to_tweet_id": in_reply_to_tweet_id,
            "user_auth": True,
        }
        if quote_tweet_id is not None:
            kwargs["quote_tweet_id"] = quote_tweet_id

        response = self._call_with_retry(self.client.create_tweet, **kwargs)
        new_tweet_id = response.data.get("id")
        logger.info(f"Posted reply. New Tweet ID: {new_tweet_id}")
        return new_tweet_id

    # ============================================================
    # DIRECT MESSAGES
    # ============================================================
    def send_direct_message(
        self,
        user_id: str,
        text: str,
        dedup_key: Optional[str] = None,
    ) -> bool:
        """
        Send a DM (used for winner notifications and bank detail collection).

        If `dedup_key` is given (recommended for anything money-related,
        e.g. f"winner_notice:{giveaway_id}:{user_id}"), this call is a
        no-op returning True if that key was already sent successfully —
        protecting against double-paying-instructions on retry/restart.

        Raises:
            ValueError: text exceeds X's length limit.
            RateLimitExceeded, DirectMessageBlocked, UserNotFound, XAPIError
        """
        if dedup_key and self.state.was_dm_sent(dedup_key):
            logger.info(f"DM '{dedup_key}' already sent to {user_id}; skipping.")
            return True

        if len(text) > MAX_TWEET_LENGTH:
            raise ValueError(
                f"DM text is {len(text)} chars, exceeds the {MAX_TWEET_LENGTH} limit"
            )

        try:
            self._call_with_retry(
                self.client.create_direct_message,
                participant_id=user_id,
                text=text,
            )
        except TweetNotFound as e:
            # X's NotFound here means the recipient account doesn't exist.
            raise UserNotFound(str(e)) from e

        logger.info(f"DM sent to user {user_id}" + (f" (key={dedup_key})" if dedup_key else ""))
        if dedup_key:
            self.state.mark_dm_sent(dedup_key, user_id)
        return True

    # ============================================================
    # INBOUND DIRECT MESSAGES
    # ============================================================
    def get_new_dm_events(self, max_results: int = 50) -> List[Dict[str, Any]]:
        """Fetch DM events newer than persisted cursor (MessageCreate only)."""
        since_id = self.state.get_since_id("dm_events")
        page = self._call_with_retry(
            self.client.get_dm_events,
            max_results=min(max_results, 100),
            dm_event_fields=["id", "text", "event_type", "sender_id", "created_at"],
            event_types=["MessageCreate"],
            user_auth=True,
        )
        events: List[Dict[str, Any]] = []
        for event in page.data or []:
            event_id = str(event.id)
            if since_id and int(event_id) <= int(since_id):
                continue
            sender_id = str(getattr(event, "sender_id", "") or "")
            text = str(getattr(event, "text", "") or "")
            events.append({"id": event_id, "sender_id": sender_id, "text": text})

        if events:
            newest = max(events, key=lambda e: int(e["id"]))["id"]
            self.state.set_since_id("dm_events", newest)

        unprocessed = set(self.state.filter_unprocessed([e["id"] for e in events]))
        return [e for e in events if e["id"] in unprocessed]

    # ============================================================
    # UTILITY: Get User Info (for KYC / trust scoring later)
    # ============================================================
    def user_follows(self, follower_id: str, followed_id: str) -> bool:
        """
        True if follower_id follows followed_id.

        Uses the v1.1 friendships/show endpoint (one cheap lookup per pair).
        """
        friendship = self._call_with_retry(
            self._get_api_v1().get_friendship,
            source_id=follower_id,
            target_id=followed_id,
        )
        return bool(getattr(friendship, "following", False))

    def get_user_by_id(self, user_id: str) -> Dict[str, Any]:
        """
        Fetch basic user information.

        Raises:
            UserNotFound, RateLimitExceeded, XAPIError
        """
        try:
            user = self._call_with_retry(
                self.client.get_user,
                id=user_id,
                user_fields=["created_at", "public_metrics", "verified"],
            )
        except TweetNotFound as e:
            raise UserNotFound(str(e)) from e

        if not user.data:
            raise UserNotFound(f"No data returned for user {user_id}")

        return {
            "id": str(user.data.id),
            "username": user.data.username,
            "created_at": user.data.created_at,
            "followers_count": user.data.public_metrics.get("followers_count", 0),
            "verified": user.data.verified,
        }


# ============================================================
# Lazy singleton accessor (NOT instantiated at import time, so
# importing this module — e.g. in tests — doesn't require live
# credentials or hit the network).
# ============================================================
_client_instance: Optional[XClient] = None


def get_x_client() -> XClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = XClient()
    return _client_instance


# ============================================================
# Convenience Functions (for quick use in handlers/jobs)
# ============================================================

def search_for_commands(since_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Phase 1 convenience wrapper"""
    client = get_x_client()
    query = f"@{client.get_bot_identity()['username']} (giveaway OR start OR begin)"
    return client.search_recent_tweets(query=query, since_id=since_id)


def get_bot_mentions(since_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Phase 2 convenience wrapper (manual since_id; prefer get_new_mentions() for auto-cursor)"""
    return get_x_client().get_user_mentions(since_id=since_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Testing X Client authentication...")
    identity = get_x_client().get_bot_identity()
    print(f"Successfully authenticated as @{identity['username']}")
