"""Core buy logic — getPaymentForm + sendStarsForm."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telethon import functions, types

if TYPE_CHECKING:
    from telethon import TelegramClient

logger = logging.getLogger(__name__)


async def buy_gift(
    client: TelegramClient,
    slug: str,
    price: int,
    gift_title: str,
    dry_run: bool = True,
) -> bool:
    """Attempt to purchase a resale gift.

    Returns True on success, False on failure.
    """
    me = await client.get_me()
    to_peer = types.InputPeerUser(user_id=me.id, access_hash=me.access_hash)
    invoice = types.InputInvoiceStarGiftResale(slug=slug, to_id=to_peer)

    if dry_run:
        logger.info(
            "[DRY-RUN] Would buy '%s' (slug=%s) for %d Stars",
            gift_title,
            slug,
            price,
        )
        return True

    try:
        form = await client(functions.payments.GetPaymentFormRequest(invoice=invoice))
        form_id = form.form_id
        logger.debug("Got payment form: form_id=%d", form_id)

        result = await client(
            functions.payments.SendStarsFormRequest(form_id=form_id, invoice=invoice)
        )

        if isinstance(result, types.payments.PaymentResult):
            logger.info(
                "Bought '%s' (slug=%s) for %d Stars",
                gift_title,
                slug,
                price,
            )
            return True

        logger.warning("Unexpected payment result: %s", result)
        return False

    except Exception:
        logger.exception("Failed to buy '%s' (slug=%s)", gift_title, slug)
        return False
