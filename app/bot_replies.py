"""Optional public tweet replies from the bot account."""

import logging
import os

from app.models import Giveaway, Winner
from app.x_client import XClient
from app.x_exceptions import XClientError

logger = logging.getLogger(__name__)


def bot_replies_enabled() -> bool:
    return os.getenv("ENABLE_BOT_REPLIES", "true").lower() == "true"


def reply_giveaway_started(client: XClient, giveaway: Giveaway, in_reply_to_tweet_id: str) -> bool:
    if not bot_replies_enabled():
        return False
    if not in_reply_to_tweet_id:
        return False

    text = (
        f"Giveaway registered: {giveaway.title}. "
        f"Reply to this thread to enter. {giveaway.num_winners} winner(s) will be picked."
    )
    try:
        client.create_reply(text, in_reply_to_tweet_id=in_reply_to_tweet_id)
        return True
    except XClientError as exc:
        logger.warning("Could not post giveaway-start reply: %s", exc)
        return False


def reply_winners_announced(
    client: XClient,
    giveaway: Giveaway,
    winners: list[Winner],
) -> bool:
    if not bot_replies_enabled():
        return False
    if not giveaway.host_tweet_id:
        return False
    if not winners:
        return False

    handles = ", ".join(f"@{w.username}" if w.username else f"user {w.user_id}" for w in winners)
    text = f"Winners for {giveaway.title}: {handles}. Congrats — check your DMs!"
    if len(text) > 280:
        text = f"{len(winners)} winner(s) picked for {giveaway.title}. Check your DMs!"

    try:
        client.create_reply(text, in_reply_to_tweet_id=giveaway.host_tweet_id)
        return True
    except XClientError as exc:
        logger.warning("Could not post winner announcement: %s", exc)
        return False