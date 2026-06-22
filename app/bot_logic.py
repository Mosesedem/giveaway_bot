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
from app.bot_replies import reply_winners_announced
from app.giveaway_lifecycle import auto_close_expired, is_collecting_entries, notify_selection_ready
from app.conversation.intake import continue_giveaway_session, handle_giveaway_mention, GIVEAWAY_TRIGGER_RE
from app.conversation.host_funding import handle_host_funding_reply
from app.conversation.payout_intake import handle_winner_dm, open_payout_session
from app.payments.payout_service import start_payout_collection

logger = logging.getLogger(__name__)

def _fintech_mode() -> bool:
    return os.getenv("FINTECH_MODE", "true").lower() == "true"


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
        if not GIVEAWAY_TRIGGER_RE.search(text):
            client.state.mark_processed(mention["id"], context="ignored:not_a_command")
            continue

        if trusted_hosts and mention["author_id"] not in trusted_hosts:
            client.state.mark_processed(mention["id"], context="ignored:untrusted_host")
            continue

        host_username = None
        try:
            host_username = client.get_user_by_id(mention["author_id"]).get("username")
        except XClientError:
            pass

        if _fintech_mode():
            reply_text, giveaway = handle_giveaway_mention(
                db,
                mention["author_id"],
                mention["id"],
                mention["conversation_id"],
                text,
                host_username,
            )
            if reply_text:
                try:
                    client.create_reply(reply_text, in_reply_to_tweet_id=mention["id"])
                except XClientError as exc:
                    logger.warning("Could not reply to host: %s", exc)
            if giveaway:
                client.state.mark_processed(mention["id"], context=f"giveaway_funding:{giveaway.id}")
                handled += 1
            else:
                client.state.mark_processed(mention["id"], context="giveaway_intake_pending")
        else:
            client.state.mark_processed(mention["id"], context="ignored:legacy_mode_disabled")

    return handled


def notify_host_funding_mismatch(db: Session, client: XClient, giveaway: Giveaway) -> None:
    from app.conversation.host_funding import mismatch_prompt, open_host_funding_session

    open_host_funding_session(db, giveaway)
    prompt = mismatch_prompt(giveaway)
    if giveaway.host_tweet_id:
        try:
            client.create_reply(prompt, in_reply_to_tweet_id=giveaway.host_tweet_id)
        except XClientError as exc:
            logger.warning("Could not reply funding mismatch on thread: %s", exc)
    if giveaway.host_user_id:
        try:
            client.send_direct_message(
                giveaway.host_user_id,
                prompt,
                dedup_key=f"funding_mismatch:{giveaway.id}",
            )
        except XClientError as exc:
            logger.warning("Could not DM host about funding mismatch: %s", exc)


def process_host_funding_replies(db: Session, client: XClient) -> int:
    from app.models import ConversationSession, ConversationKind

    sessions = db.execute(
        select(ConversationSession).where(ConversationSession.kind == ConversationKind.HOST_FUNDING)
    ).scalars().all()
    handled = 0
    for session in sessions:
        thread_id = session.thread_tweet_id
        if not thread_id:
            continue
        replies = client.get_new_thread_replies(thread_id)
        for reply in replies:
            if reply["author_id"] != session.user_id:
                continue
            reply_text, giveaway = handle_host_funding_reply(
                db, session.user_id, reply.get("text", "")
            )
            if reply_text:
                try:
                    client.create_reply(reply_text, in_reply_to_tweet_id=reply["id"])
                except XClientError:
                    pass
            if giveaway and giveaway.status == GiveawayStatus.ACTIVE:
                handled += 1
            client.state.mark_processed(reply["id"], context="host_funding_reply")
    return handled


def process_setup_thread_replies(db: Session, client: XClient) -> int:
    """Continue multi-turn giveaway setup from host replies in thread."""
    from app.models import ConversationSession, ConversationKind

    sessions = db.execute(
        select(ConversationSession).where(ConversationSession.kind == ConversationKind.GIVEAWAY_SETUP)
    ).scalars().all()
    handled = 0
    for session in sessions:
        replies = client.get_new_thread_replies(session.thread_tweet_id)
        for reply in replies:
            if reply["author_id"] != session.user_id:
                continue
            reply_text, giveaway = continue_giveaway_session(
                db, session.user_id, reply["id"], reply.get("text", "")
            )
            if reply_text:
                try:
                    client.create_reply(reply_text, in_reply_to_tweet_id=reply["id"])
                except XClientError:
                    pass
            if giveaway:
                handled += 1
            client.state.mark_processed(reply["id"], context="setup_reply")
    return handled


def process_inbound_dms(db: Session, client: XClient) -> int:
    if not _fintech_mode():
        return 0
    handled = 0
    for event in client.get_new_dm_events():
        sender = event["sender_id"]
        bot_id = str(client.get_bot_identity()["user_id"])
        if sender == bot_id:
            client.state.mark_processed(event["id"], context="ignored:own_dm")
            continue
        reply = handle_winner_dm(db, sender, event.get("text", ""))
        if reply:
            try:
                client.send_direct_message(sender, reply, dedup_key=None)
            except XClientError as exc:
                logger.warning("DM reply failed for %s: %s", sender, exc)
            handled += 1
        client.state.mark_processed(event["id"], context="dm_processed")
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

    if auto_close_expired(db, giveaway):
        logger.info(f"Giveaway {giveaway.id} auto-closed (closes_at passed)")
        return 0
    if not is_collecting_entries(giveaway):
        logger.info(f"Giveaway {giveaway.id} not accepting entries (status={giveaway.status.value})")
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


def pick_winners(
    db: Session,
    giveaway: Giveaway,
    seed: Optional[int] = None,
    announce: bool = True,
    client: Optional[XClient] = None,
) -> list[Winner]:
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

    draw_seed = seed if seed is not None else random.randint(0, 2**31 - 1)
    rng = random.Random(draw_seed)
    n = min(giveaway.num_winners, len(candidates))
    chosen = rng.sample(candidates, n)

    winners = []
    for entry in chosen:
        winner = Winner(
            giveaway_id=giveaway.id,
            entry_id=entry.id,
            user_id=entry.user_id,
            username=entry.username,
            status=WinnerStatus.SELECTED,
        )
        db.add(winner)
        winners.append(winner)

    giveaway.status = GiveawayStatus.WINNERS_SELECTED
    giveaway.pick_seed = draw_seed
    db.commit()

    if announce and client is not None:
        reply_winners_announced(client, giveaway, winners)

    logger.info(
        f"Selected {len(winners)} winner(s) for giveaway {giveaway.id} (seed={draw_seed})"
    )
    return winners


def prepare_winner_payout_dm(db: Session, giveaway: Giveaway, winner: Winner) -> str:
    """Build the bank-collection DM and open a payout conversation session."""
    message = start_payout_collection(winner, giveaway)
    open_payout_session(db, winner, giveaway)
    db.commit()
    return message


def notify_winner(client: XClient, giveaway: Giveaway, winner: Winner, db: Session, message: str) -> bool:
    """
    DM a winner. Uses x_client's built-in dedup_key so retries never
    double-send. Updates the Winner row's status based on outcome.
    """
    if _fintech_mode():
        message = prepare_winner_payout_dm(db, giveaway, winner)

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
    summary = {
        "new_giveaways": 0,
        "entries_collected": 0,
        "setup_replies": 0,
        "host_funding_replies": 0,
        "inbound_dms": 0,
    }

    summary["new_giveaways"] = handle_new_mentions(db, client)
    summary["setup_replies"] = process_setup_thread_replies(db, client)
    summary["host_funding_replies"] = process_host_funding_replies(db, client)
    summary["inbound_dms"] = process_inbound_dms(db, client)

    active = db.execute(
        select(Giveaway).where(Giveaway.status == GiveawayStatus.ACTIVE)
    ).scalars().all()

    for giveaway in active:
        if auto_close_expired(db, giveaway):
            notify_selection_ready(db, client, giveaway)
        if is_collecting_entries(giveaway):
            summary["entries_collected"] += collect_entries(db, client, giveaway)

    return summary
