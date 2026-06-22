"""
Bot orchestration logic — the piece that was missing before.

This module is intentionally plain, procedural Python: each function does
one job and takes a DB session explicitly, so it's easy to call from
either the background scheduler (bot.py) or directly from a dashboard
button (e.g. "pick winners now").
"""

import logging
import os
import random
import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import select

from app.models import Giveaway, GiveawayStatus, Entry, Winner, WinnerStatus
from app.x_client import XClient
from app.x_exceptions import XClientError
from app.entry_validation import validate_entry, validation_rules_enabled

logger = logging.getLogger(__name__)

# Matches: "@bot giveaway start Title here / prize: ... / winners: 3"
# Keep this simple on purpose — tighten it once you see real host phrasing.
START_COMMAND_RE = re.compile(r"\b(giveaway|start|begin)\b", re.IGNORECASE)


def _trusted_host_ids() -> set[str]:
    raw = os.getenv("TRUSTED_HOST_USER_IDS", "")
    return {user_id.strip() for user_id in raw.split(",") if user_id.strip()}


def handle_new_mentions(db: Session, client: XClient) -> int:
    """
    Phase 2: pull new mentions, detect giveaway-start commands from
    verified/trusted hosts, create Giveaway rows. Returns count handled.
    """
    mentions = client.get_new_mentions()
    handled = 0
    trusted_hosts = _trusted_host_ids()

    for mention in mentions:
        text = mention["text"]
        if not START_COMMAND_RE.search(text):
            client.state.mark_processed(mention["id"], context="ignored:not_a_command")
            continue

        if trusted_hosts and mention["author_id"] not in trusted_hosts:
            client.state.mark_processed(mention["id"], context="ignored:untrusted_host")
            logger.info(
                f"Ignored giveaway command from untrusted host {mention['author_id']} "
                f"(tweet {mention['id']})"
            )
            continue

        giveaway = Giveaway(
            host_tweet_id=mention["id"],
            host_user_id=mention["author_id"],
            conversation_id=mention["conversation_id"],
            title=text[:200],
            status=GiveawayStatus.ACTIVE,
        )
        db.add(giveaway)
        db.commit()

        client.state.mark_processed(mention["id"], context=f"giveaway_created:{giveaway.id}")
        logger.info(f"Created giveaway {giveaway.id} from tweet {mention['id']}")
        handled += 1

    return handled


def collect_entries(db: Session, client: XClient, giveaway: Giveaway) -> int:
    """
    Pull new replies in a giveaway's thread and store them as Entry rows.
    Safe to call repeatedly — uses the same dedup/cursor machinery as
    everything else in x_client, plus a unique constraint on tweet_id
    as a second line of defense.
    """
    if not giveaway.conversation_id:
        logger.warning(f"Giveaway {giveaway.id} has no conversation_id, skipping entry collection")
        return 0

    replies = client.get_new_thread_replies(giveaway.conversation_id)
    added = 0
    bot_user_id = str(client.get_bot_identity()["user_id"])

    for reply in replies:
        tweet_id = reply["id"]
        author_id = reply["author_id"]

        if author_id == bot_user_id:
            client.state.mark_processed(tweet_id, context="ignored:bot_reply")
            continue
        if giveaway.host_user_id and author_id == giveaway.host_user_id:
            client.state.mark_processed(tweet_id, context="ignored:host_reply")
            continue
        if giveaway.host_tweet_id and tweet_id == giveaway.host_tweet_id:
            client.state.mark_processed(tweet_id, context="ignored:host_tweet")
            continue

        existing = db.execute(
            select(Entry).where(Entry.tweet_id == tweet_id)
        ).scalar_one_or_none()
        if existing:
            client.state.mark_processed(tweet_id, context="already_in_db")
            continue

        existing_user = db.execute(
            select(Entry).where(Entry.giveaway_id == giveaway.id, Entry.user_id == author_id)
        ).scalar_one_or_none()
        if existing_user:
            client.state.mark_processed(tweet_id, context="ignored:duplicate_user")
            continue

        is_valid = True
        invalid_reason = None
        username = None

        if validation_rules_enabled():
            result = validate_entry(client, giveaway, author_id, reply.get("text", ""))
            is_valid = result.is_valid
            invalid_reason = result.reason
            username = result.username

        entry = Entry(
            giveaway_id=giveaway.id,
            tweet_id=tweet_id,
            user_id=author_id,
            username=username,
            text=reply["text"],
            is_valid=is_valid,
            invalid_reason=invalid_reason,
        )
        db.add(entry)
        context = f"entry:{giveaway.id}" if is_valid else f"invalid:{invalid_reason}"
        client.state.mark_processed(tweet_id, context=context)
        if is_valid:
            added += 1

    db.commit()
    logger.info(f"Collected {added} new valid entries for giveaway {giveaway.id}")
    return added


def revalidate_entries(db: Session, client: XClient, giveaway: Giveaway) -> tuple[int, int]:
    """Re-run validation rules on all entries for a giveaway. Returns (valid, invalid)."""
    entries = db.execute(
        select(Entry).where(Entry.giveaway_id == giveaway.id)
    ).scalars().all()

    valid_count = 0
    invalid_count = 0

    for entry in entries:
        if not validation_rules_enabled():
            entry.is_valid = True
            entry.invalid_reason = None
            valid_count += 1
            continue

        result = validate_entry(client, giveaway, entry.user_id, entry.text or "")
        entry.is_valid = result.is_valid
        entry.invalid_reason = result.reason
        if result.username:
            entry.username = result.username
        if result.is_valid:
            valid_count += 1
        else:
            invalid_count += 1

    db.commit()
    logger.info(
        f"Revalidated giveaway {giveaway.id}: {valid_count} valid, {invalid_count} invalid"
    )
    return valid_count, invalid_count


def pick_winners(db: Session, giveaway: Giveaway, seed: Optional[int] = None) -> list[Winner]:
    """
    Randomly select winners from valid, not-already-selected entries.
    Pass `seed` for reproducible/auditable picks (e.g. log the seed
    publicly before drawing, for transparency).
    """
    valid_entries = db.execute(
        select(Entry).where(Entry.giveaway_id == giveaway.id, Entry.is_valid.is_(True))
    ).scalars().all()

    already_won_user_ids = {
        w.user_id for w in db.execute(
            select(Winner).where(Winner.giveaway_id == giveaway.id)
        ).scalars().all()
    }
    candidates = [e for e in valid_entries if e.user_id not in already_won_user_ids]

    if not candidates:
        logger.warning(f"No eligible candidates for giveaway {giveaway.id}")
        return []

    rng = random.Random(seed)
    n = min(giveaway.num_winners, len(candidates))
    chosen = rng.sample(candidates, n)

    winners = []
    for entry in chosen:
        winner = Winner(
            giveaway_id=giveaway.id,
            entry_id=entry.id,
            user_id=entry.user_id,
            status=WinnerStatus.SELECTED,
        )
        db.add(winner)
        winners.append(winner)

    giveaway.status = GiveawayStatus.WINNERS_SELECTED
    db.commit()
    logger.info(f"Selected {len(winners)} winner(s) for giveaway {giveaway.id}")
    return winners


def notify_winner(client: XClient, giveaway: Giveaway, winner: Winner, db: Session, message: str) -> bool:
    """
    DM a winner. Uses x_client's built-in dedup_key so retries never
    double-send. Updates the Winner row's status based on outcome.
    """
    dedup_key = f"winner_notice:{giveaway.id}:{winner.user_id}"
    try:
        client.send_direct_message(winner.user_id, message, dedup_key=dedup_key)
        winner.status = WinnerStatus.NOTIFIED
        winner.notified_at = datetime.now(timezone.utc)
        db.commit()
        return True
    except XClientError as e:
        logger.error(f"Failed to notify winner {winner.user_id} for giveaway {giveaway.id}: {e}")
        winner.status = WinnerStatus.DM_FAILED
        winner.notes = str(e)
        db.commit()
        return False


def run_cycle(db: Session, client: XClient) -> dict:
    """
    One full pass of the bot loop: check for new commands, collect
    entries for all active giveaways. Called on a schedule (see bot.py).
    """
    summary = {"new_giveaways": 0, "entries_collected": 0}

    summary["new_giveaways"] = handle_new_mentions(db, client)

    active = db.execute(
        select(Giveaway).where(Giveaway.status == GiveawayStatus.ACTIVE)
    ).scalars().all()

    for giveaway in active:
        summary["entries_collected"] += collect_entries(db, client, giveaway)

    return summary
