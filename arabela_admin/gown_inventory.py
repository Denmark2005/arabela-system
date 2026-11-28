"""The Gown Inventory figures -- ONE rule, used by the dashboard tile and by the strip at the top of Gown Catalog.

Every gown is counted in exactly ONE box, so the boxes always add up to the total:

  1. Out-of-Stock  -- the gown is off the website (needs repair, or set by staff)
  2. Reserved      -- an approved booking holds it
  3. Blocked       -- it has a block covering today (the rest days after a rental, or a block staff added)
  4. Available     -- everything else: it can be rented today

A block is a date range laid over a gown, not a status, so before this rule a blocked gown was also counted as Available
(or Reserved) and the four boxes added up to more than the total, while an Out-of-Stock gown was in no box at all.

Pure arithmetic on what the caller already loaded: no queries and no imports of models, so it can be used anywhere and tested without a database.
"""

AVAILABLE = "Available"
RESERVED = "Reserved"
BLOCKED = "Blocked"


def box_of(status, blocked_today):
    """The name the catalog's status filter lists a gown under: "Blocked" for an Available gown with a block today, else its own status."""
    return BLOCKED if status == AVAILABLE and blocked_today else status


def split(statuses, blocked_ids):
    """`statuses`: (gown id, status) for EVERY gown. `blocked_ids`: ids of the gowns with a block covering today.
    Returns {"total", "available", "reserved", "blocked", "out_of_stock"}; the last four always add up to "total"."""
    blocked_ids = set(blocked_ids)
    out = {"total": 0, "available": 0, "reserved": 0, "blocked": 0, "out_of_stock": 0}
    for gown_id, status in statuses:
        out["total"] += 1
        if status == RESERVED:
            out["reserved"] += 1
        elif status != AVAILABLE:
            out["out_of_stock"] += 1          # Out-of-Stock, and anything unexpected: off the website either way
        elif gown_id in blocked_ids:
            out["blocked"] += 1
        else:
            out["available"] += 1
    return out
