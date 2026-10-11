"""Turning what the game wrote into what Discord shows. No Discord, no network: text in, text out."""

import time


def money(amount, currency=None):
    """1250 -> "$1,250" with a symbol, "1,250 Dollars" without one. The same rule the game uses."""
    try:
        text = f"{int(amount):,}"
    except (TypeError, ValueError):
        return "?"

    currency = currency or {}
    if currency.get("symbol"):
        return str(currency["symbol"]) + text
    if currency.get("name"):
        return text + " " + str(currency["name"])
    return text


def clean(text, limit=80):
    """A name out of the game, safe to put in a message: no mentions, no markdown, not endless."""
    text = str(text if text is not None else "?")
    for char in "*_`~|>@#":
        text = text.replace(char, "")
    text = " ".join(text.split())
    return text[:limit] or "?"


def time_left(ends_ms, now_ms=None):
    """ "2d 4h", "3h 12m", "45m", "under a minute", or None once it has ended."""
    if not ends_ms:
        return None

    now_ms = now_ms if now_ms is not None else time.time() * 1000
    secs = int((ends_ms - now_ms) / 1000)
    if secs <= 0:
        return None

    days, hours, mins = secs // 86400, (secs // 3600) % 24, (secs // 60) % 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    if mins:
        return f"{mins}m"
    return "under a minute"


def _qty(record):
    qty = record.get("qty") or 1
    return f" x{qty}" if qty and qty > 1 else ""


def event_text(record, currency=None):
    """One line for the feed channel, or None for a record that isn't news."""
    kind = record.get("type")
    item = "**" + clean(record.get("item")) + "**" + _qty(record)

    if kind == "listing":
        return f"🛒 {item} listed by {clean(record.get('seller'))} for {money(record.get('price'), currency)} each"

    if kind == "sale":
        return (f"💰 {item} sold to {clean(record.get('buyer'))} by {clean(record.get('seller'))} "
                f"for {money(record.get('price'), currency)}")

    if kind == "auctionStart":
        line = (f"🔨 Auction: {item} from {clean(record.get('seller'))}, "
                f"opening at {money(record.get('price'), currency)}")
        if record.get("buyout"):
            line += f", buyout {money(record.get('buyout'), currency)}"
        left = time_left(record.get("ends"), record.get("t"))
        if left:
            line += f". Ends in {left}"
        return line

    if kind == "bid":
        return (f"📈 {clean(record.get('bidder'))} bid {money(record.get('price'), currency)} "
                f"on **{clean(record.get('item'))}**")

    if kind == "auctionEnd":
        if record.get("winner"):
            return (f"🏁 {item} went to {clean(record.get('winner'))} "
                    f"for {money(record.get('price'), currency)}")
        return f"🏁 Auction for {item} ended with no bids"

    return None


NOTICES = {
    "Sold": "Sold: **{item}**, for {amount}.",
    "Outbid": "You were outbid on **{item}**. Your {amount} is back in the bank.",
    "Won": "You won **{item}** for {amount}.",
    "AuctionSold": "Your auction of **{item}** sold for {amount}.",
    "NoBids": "No bids on **{item}**. It's on its way back to you.",
    "Expired": "Your listing of **{item}** ran out. It's on its way back to you.",
    "Mail": "zBay mail waiting in game: **{item}**.",
    "Removed": "An admin took down your listing of **{item}**.",
    "BidReturned": "The auction for **{item}** was taken down. Your {amount} is back in the bank.",
}


def notice_text(record, currency=None):
    """The direct message for something that happened to one player, or None."""
    template = NOTICES.get(record.get("code"))
    if not template:
        return None
    return template.format(item=clean(record.get("item")), amount=money(record.get("amount"), currency))


def _number(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def mailbox_text(reply, limit=15):
    """What a linked player is told is waiting in their mail: each line by name and number.

    The game sends only so many lines and this shows fewer still. The count of how many
    more is of all of them.
    """
    items = reply.get("items")
    if not isinstance(items, list):
        items = []
    items = [row for row in items if isinstance(row, dict)]

    if not items:
        return "Your mailbox is empty."

    total = _number(reply.get("total"), sum(_number(row.get("qty"), 1) for row in items))
    out = [f"**{clean(reply.get('user'))}** has {total:,} item{'' if total == 1 else 's'} in the mail."]
    out += ["**" + clean(row.get("item")) + "**" + _qty(row) for row in items[:limit]]

    more = max(_number(reply.get("lines"), len(items)), len(items)) - min(len(items), limit)
    if more > 0:
        out.append(f"and {more} more line{'' if more == 1 else 's'}.")

    if not reply.get("required"):
        out.append("Collect them in game, from the Mailbox tab of the zBay window.")
    elif reply.get("claimed"):
        out.append("Collect them at your mailbox in game.")
    else:
        out.append("You haven't claimed a mailbox in game yet. Right-click one and choose "
                   "**Set as my zBay mailbox**, then collect there.")

    return "\n".join(out)


def listing_line(row, currency=None, now_ms=None):
    """One row of a search result."""
    line = f"`{clean(row.get('id'), 12)}` **{clean(row.get('item'))}**{_qty(row)}"

    if row.get("kind") == "auction":
        line += f", bid {money(row.get('price'), currency)}"
        if row.get("bids"):
            line += f" ({row['bids']})"
        if row.get("buyout"):
            line += f", buyout {money(row.get('buyout'), currency)}"
        left = time_left(row.get("ends"), now_ms)
        line += f", ends in {left}" if left else ", ending"
    else:
        line += f", {money(row.get('price'), currency)} each"

    return line + f" · {clean(row.get('seller'), 32)}"


def search(snapshot, text=None, kind=None, limit=10, now_ms=None):
    """Lines for the listings in a snapshot that match, newest first, and how many matched."""
    if not snapshot:
        return [], 0

    needle = (text or "").strip().lower()
    currency = snapshot.get("currency")
    matched = []

    for row in snapshot.get("listings") or []:
        if kind and row.get("kind") != kind:
            continue
        if needle and needle not in str(row.get("item", "")).lower():
            continue
        matched.append(row)

    return [listing_line(row, currency, now_ms) for row in matched[:limit]], len(matched)
