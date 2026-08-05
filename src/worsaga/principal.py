"""Bind Worsaga's local stores to the Moodle account that filled them.

The sync cache, the full-text index, and the auto-sync record all live at
one fixed per-user path and are keyed by site. That is fine until two
Moodle accounts share one OS account — a student who graduates and
re-enrols, a shared lab login, a personal and a staff token for the same
site. Without a binding, the second account's sync silently diffs against
the first account's baseline, and its search hits come back from the
first account's documents.

So every store records the *verified* principal (the user id the Moodle
site itself reports, see :attr:`worsaga.client.MoodleClient.userid`)
alongside its site key, and:

- an unstamped store is adopted by the first authenticated caller that
  touches it, with a one-line notice when it already held data;
- a store stamped with a different principal for the same site refuses
  the operation and names both ids and the file to remove;
- a run that verified no identity at all — nothing was fetched, so there
  is nothing to attribute — writes nothing into a store that already
  belongs to an account, and says so once. An *unstamped* store may still
  record such a run: there is nothing there to mix it with.

The principal is read at the moment of writing, never earlier; see
:func:`known_principal` for why that ordering is the load-bearing part.

**Scope, honestly stated.** This is an interim guard, not isolation. It
stops the accidental mix-up above; it does not defend one OS user against
another, and it does not survive someone deleting the stamp. Full
per-account namespacing of the store paths is the 0.9.0 work; adopting an
unstamped store on first touch (rather than quarantining it) is the
deliberate interim behaviour, because the alternative would strand the
existing local data of every user upgrading into this release.

**Offline reads are deliberately unguarded.** Reading recorded changes or
searching the local text index makes no network request, so there is no
verified identity to check against — asking for one would either make
those paths contact Moodle (breaking their no-network contract) or invite
a check against an unverified, caller-supplied id, which guarantees
nothing. When a principal *has* already been verified in the same
process, read paths do apply the check. Otherwise the OS user boundary is
the real line, and this module does not pretend otherwise.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Metadata key prefix for the stamp. Site-qualified, so one store file
#: can legitimately hold demo data (a fixed demo principal) next to a
#: real site's data, and so a mismatch is only ever reported for the
#: same site.
PRINCIPAL_META_PREFIX = "principal_userid:"


class PrincipalMismatchError(RuntimeError):
    """Raised when a local store belongs to a different Moodle account."""


def principal_meta_key(site: str) -> str:
    """Return the metadata key holding the stamp for *site*."""
    return f"{PRINCIPAL_META_PREFIX}{site}"


def known_principal(client: Any) -> int | None:
    """Return an already-verified user id without making a request.

    This is the *only* way Worsaga obtains a principal, and it is read at
    the moment a store is written rather than earlier. Reading it early
    was a real hole: a transient ``site_info`` failure at the top of a
    sync returned ``None`` while later fetches went on to verify the
    client, so a full set of one account's data could be written into
    another account's store with no stamp to catch it.

    Read at write time the answer is trustworthy in both directions.
    Every authenticated read Worsaga makes injects the user id (the
    course list included), so a fetch that produced data has necessarily
    verified the identity; and ``None`` therefore means nothing was
    fetched at all. The demo client always knows its own. Because no
    request is ever made here, a command that promises to stay offline
    keeps that promise.
    """
    if client is None:
        return None
    try:
        userid = getattr(client, "verified_userid", None)
    except Exception:
        return None
    try:
        return int(userid) if userid else None
    except (TypeError, ValueError):
        return None


def bind_principal(
    *,
    stored: int | None,
    principal: int | None,
    site: str,
    store_label: str,
    store_path: str,
    remedy: str,
    holds_data: bool = False,
) -> int | None:
    """Return the principal to stamp on a write, or ``None`` to leave it.

    Raises :class:`PrincipalMismatchError` when *stored* names a
    different account for *site*. When the store is unstamped it is
    adopted; if it already held data for this site, one notice says so,
    once — the next touch finds the stamp and stays quiet.
    """
    if principal is None:
        return None
    if stored is None:
        if holds_data:
            logger.warning(
                "The local %s at %s already held data for %s from before "
                "Worsaga bound its stores to an account; it is now bound "
                "to Moodle user id %s.",
                store_label, store_path, site, principal,
            )
        return principal
    if stored != principal:
        raise PrincipalMismatchError(
            _mismatch_message(
                stored=stored,
                principal=principal,
                site=site,
                store_label=store_label,
                store_path=store_path,
                remedy=remedy,
            )
        )
    return None


def assert_principal(
    *,
    stored: int | None,
    principal: int | None,
    site: str,
    store_label: str,
    store_path: str,
    remedy: str,
) -> None:
    """Refuse a read against a store bound to a different account.

    Check-only: an unstamped store is not adopted here, because reads
    stay free of side effects. Adoption happens on the next write.
    """
    if principal is None or stored is None or stored == principal:
        return
    raise PrincipalMismatchError(
        _mismatch_message(
            stored=stored,
            principal=principal,
            site=site,
            store_label=store_label,
            store_path=store_path,
            remedy=remedy,
        )
    )


def _mismatch_message(
    *,
    stored: int,
    principal: int,
    site: str,
    store_label: str,
    store_path: str,
    remedy: str,
) -> str:
    return (
        f"BLOCKED: the local {store_label} at {store_path} holds data for "
        f"{site} collected as Moodle user id {stored}, but this token "
        f"authenticates as user id {principal}. Worsaga will not mix two "
        f"accounts in one local store. {remedy}"
    )


__all__ = [
    "PRINCIPAL_META_PREFIX",
    "PrincipalMismatchError",
    "assert_principal",
    "bind_principal",
    "known_principal",
    "principal_meta_key",
]
