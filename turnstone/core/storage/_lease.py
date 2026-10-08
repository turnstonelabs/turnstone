"""Workstream owner-lease SQL shared by both storage backends.

The lease lives on the ``workstreams`` row (migration 078). Every helper runs on
the caller's connection, inside a transaction that already serializes writers
for the row: PostgreSQL takes a row lock here (``SELECT ... FOR UPDATE``, or the
row's own ``UPDATE``), and SQLite callers hold ``BEGIN IMMEDIATE`` (SQLAlchemy
drops ``FOR UPDATE`` on SQLite). Expiry is always judged against the database
clock, never the application clock. The locking read and the clock and
liveness expressions are built once per dialect and reused with bound
parameters; the other statements are built per call.

A fence matches only the exact incarnation it was issued for. The epoch alone
cannot promise that: a hard-deleted row that is registered again under the same
id starts over at epoch 0, so the incarnation token travels with the fence and
every check compares it too.
"""

from __future__ import annotations

import functools
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from turnstone.core.storage._protocol import (
    FORK_RESERVATION_CONFIG_KEY,
    LeaseFence,
    LeaseGrant,
    WorkstreamLeaseHeldError,
    WorkstreamLeaseLostError,
)
from turnstone.core.storage._schema import watches, workstream_config, workstreams
from turnstone.core.workstream import BULK_CLOSE_STATE_VALUES, WorkstreamKind

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

#: Renewal statements carry at most this many ``(ws_id, epoch)`` pairs: two
#: bound variables each plus a few, inside the 999-variable limit of SQLite
#: builds older than 3.32.
RENEW_BATCH_SIZE = 450


@functools.cache
def db_now_ms(dialect_name: str) -> sa.ColumnElement[Any]:
    """Database-clock time in epoch milliseconds, for comparisons.

    PostgreSQL's ``now()`` is fixed at transaction start; after waiting on a
    row lock it reads early, which only makes a lease look live longer (the
    safe direction). Expiries that get written use :func:`db_write_clock_ms`.
    """
    if dialect_name == "postgresql":
        return sa.cast(sa.func.extract("epoch", sa.func.now()) * 1000, sa.BigInteger)
    return sa.cast((sa.func.julianday("now") - 2440587.5) * 86400000, sa.BigInteger)


@functools.cache
def db_write_clock_ms(dialect_name: str) -> sa.ColumnElement[Any]:
    """Database-clock time in epoch milliseconds when the statement runs.

    New expiries start from here, so a grant or renewal that waited on a lock
    still gets its full TTL. SQLite reads the clock per statement already.
    """
    if dialect_name == "postgresql":
        return sa.cast(sa.func.extract("epoch", sa.func.clock_timestamp()) * 1000, sa.BigInteger)
    return db_now_ms(dialect_name)


@functools.cache
def _incarnation_token_column() -> sa.ScalarSelect[Any]:
    return (
        sa.select(workstream_config.c.value)
        .where(
            workstream_config.c.ws_id == workstreams.c.ws_id,
            workstream_config.c.key == FORK_RESERVATION_CONFIG_KEY,
        )
        .scalar_subquery()
    )


def _incarnation_matches(ws_id: str, token: str) -> sa.ColumnElement[bool]:
    return sa.exists(
        sa.select(workstream_config.c.ws_id).where(
            workstream_config.c.ws_id == ws_id,
            workstream_config.c.key == FORK_RESERVATION_CONFIG_KEY,
            workstream_config.c.value == token,
        )
    )


@functools.cache
def _lease_state_columns(dialect_name: str) -> tuple[sa.ColumnElement[Any], ...]:
    """The lease columns every locking read selects, with the DB clock and liveness."""
    return (
        workstreams.c.ws_id,
        workstreams.c.lease_holder,
        workstreams.c.lease_node_id,
        workstreams.c.lease_expires_ms,
        db_now_ms(dialect_name).label("now_ms"),
        sa.not_(unleased_predicate(dialect_name)).label("lease_live"),
    )


@functools.cache
def _lock_statement(dialect_name: str) -> Any:
    return (
        sa.select(
            *_lease_state_columns(dialect_name),
            workstreams.c.state,
            workstreams.c.lease_epoch,
            _incarnation_token_column().label("incarnation_token"),
        )
        .where(workstreams.c.ws_id == sa.bindparam("ws_id"))
        .with_for_update()
    )


def lock_workstream_lease_row(conn: Any, ws_id: str) -> Any | None:
    """Lock the row and read its state, lease, incarnation token and the DB clock."""
    return conn.execute(_lock_statement(conn.dialect.name), {"ws_id": ws_id}).fetchone()


def _lease_is_live(row: Any) -> bool:
    """Read the ``lease_live`` column the locking reads select.

    It is ``NOT unleased_predicate``: one derivation of liveness for
    admission, acquisition and offline maintenance alike.
    """
    return bool(row.lease_live)


def _refuse_live_lease(ws_id: str, row: Any) -> None:
    """Refuse an unfenced write to the locked ``row`` while its lease is live."""
    if row.lease_holder is not None and _lease_is_live(row):
        raise _held_error(ws_id, row)


def _held_error(ws_id: str, row: Any) -> WorkstreamLeaseHeldError:
    return WorkstreamLeaseHeldError(
        ws_id,
        holder_node_id=str(row.lease_node_id or ""),
        retry_after_ms=max(0, int(row.lease_expires_ms) - int(row.now_ms)),
    )


def _fence_matches(row: Any, lease: LeaseFence) -> bool:
    return (
        row.lease_holder == lease.holder
        and int(row.lease_epoch) == lease.epoch
        and str(row.incarnation_token or "") == lease.incarnation_token
    )


def acquire_lease_on_connection(
    conn: Any,
    *,
    ws_id: str,
    incarnation_token: str,
    holder: str,
    node_id: str | None,
    ttl_ms: int,
    allow_creating: bool,
    allow_live_takeover: bool,
) -> LeaseGrant | None:
    """Grant a new epoch for one exact incarnation, or refuse.

    Returns ``None`` when the row is missing, deleted, provisional without
    ``allow_creating``, or no longer carries ``incarnation_token``. Raises
    :class:`WorkstreamLeaseHeldError` while another holder's lease is live,
    unless the backend permits live takeover (SQLite's single-process contract).
    """
    if not ws_id or not incarnation_token or not holder:
        return None
    row = lock_workstream_lease_row(conn, ws_id)
    if row is None or str(row.incarnation_token or "") != incarnation_token:
        return None
    state = str(row.state or "")
    if state == "deleted" or (state == "creating" and not allow_creating):
        return None
    live = _lease_is_live(row)
    other_holder = row.lease_holder is not None and row.lease_holder != holder
    if live and other_holder and not allow_live_takeover:
        raise _held_error(ws_id, row)
    epoch = conn.execute(
        sa.update(workstreams)
        .where(workstreams.c.ws_id == ws_id)
        .values(
            lease_holder=holder,
            lease_node_id=node_id or None,
            lease_epoch=workstreams.c.lease_epoch + 1,
            lease_expires_ms=db_write_clock_ms(conn.dialect.name) + ttl_ms,
        )
        .returning(workstreams.c.lease_epoch)
    ).scalar_one()
    return LeaseGrant(
        fence=LeaseFence(
            ws_id=ws_id,
            holder=holder,
            epoch=int(epoch),
            incarnation_token=incarnation_token,
        ),
        previous_holder=str(row.lease_holder or "") if other_holder else "",
        previous_node_id=str(row.lease_node_id or "") if other_holder else "",
        took_over_live=bool(live and other_holder),
    )


def renewal_batches(holder: str, fences: Sequence[LeaseFence]) -> list[list[tuple[str, int]]]:
    """The ``(ws_id, epoch)`` pairs ``holder`` presents, sorted, in statement-sized batches.

    The backends renew each batch in its own transaction: one batch's
    extension stands on its own, and no batch keeps the rows it renewed
    locked while the next one runs.
    """
    pairs = sorted({(fence.ws_id, fence.epoch) for fence in fences if fence.holder == holder})
    return [
        pairs[start : start + RENEW_BATCH_SIZE] for start in range(0, len(pairs), RENEW_BATCH_SIZE)
    ]


def renew_lease_batch_on_connection(
    conn: Any,
    *,
    holder: str,
    batch: Sequence[tuple[str, int]],
    ttl_ms: int,
) -> set[str]:
    """Extend every lease of one batch that ``holder`` still owns at the presented epoch.

    Returns the ids the holder still owns; a presented id missing from the
    result is lost. No expiry predicate: an expired lease nobody took over is
    still exclusive, so its holder may revive it. The incarnation token is not
    rechecked here: a holder tracks one lease per workstream id, and a newer
    acquisition of the same id supersedes the older handle before it is
    tracked, so a renewal can only ever extend a lease the holder
    legitimately owns. Fenced writes and release still compare the token.

    On PostgreSQL the renewal never waits on a row another transaction holds
    (``SKIP LOCKED``): waiting would keep every row it already renewed locked
    behind that one, stalling those workstreams' writes. A skipped row that
    still names this holder and epoch is busy, not lost: it counts as owned
    and is extended on a later tick once the lock is gone. The bound this
    accepts: a row lock held longer than the lease's remaining TTL lets the
    lease lapse (that can be as little as the TTL minus one renewal interval,
    when the lock starts just before a tick), and another process may then
    take the workstream over; this holder's later writes are refused, never
    applied. A user who can fork the workstream can hold that lock on purpose
    (a fork locks its source row), by forking it again and again; that too is
    accepted within the bound. SQLite serializes writers with
    ``BEGIN IMMEDIATE`` and has no row locks to skip.
    """
    postgres = conn.dialect.name == "postgresql"
    presented = (
        workstreams.c.lease_holder == holder,
        sa.tuple_(workstreams.c.ws_id, workstreams.c.lease_epoch).in_(list(batch)),
    )
    target: sa.ColumnElement[bool]
    if postgres:
        claimed = (
            sa.select(workstreams.c.ws_id)
            .where(*presented)
            .with_for_update(skip_locked=True, key_share=True)
        )
        target = workstreams.c.ws_id.in_(claimed)
    else:
        target = sa.and_(*presented)
    result = conn.execute(
        sa.update(workstreams)
        .where(target)
        .values(lease_expires_ms=db_write_clock_ms(conn.dialect.name) + ttl_ms)
        .returning(workstreams.c.ws_id)
    )
    owned = {str(row[0]) for row in result}
    skipped = [pair for pair in batch if pair[0] not in owned]
    if postgres and skipped:
        # A plain read never waits on row locks: these rows are still ours.
        still_ours = conn.execute(
            sa.select(workstreams.c.ws_id).where(
                workstreams.c.lease_holder == holder,
                sa.tuple_(workstreams.c.ws_id, workstreams.c.lease_epoch).in_(skipped),
            )
        )
        owned.update(str(row[0]) for row in still_ours)
    return owned


def release_lease_on_connection(conn: Any, fence: LeaseFence) -> bool:
    """Clear one exact acquisition. The epoch stays, so it never repeats."""
    result = conn.execute(
        sa.update(workstreams)
        .where(
            workstreams.c.ws_id == fence.ws_id,
            workstreams.c.lease_holder == fence.holder,
            workstreams.c.lease_epoch == fence.epoch,
            _incarnation_matches(fence.ws_id, fence.incarnation_token),
        )
        .values(lease_holder=None, lease_node_id=None, lease_expires_ms=None)
    )
    return bool(result.rowcount)


def enforce_workstream_lease(
    conn: Any,
    ws_id: str,
    row: Any,
    lease: LeaseFence | None,
) -> None:
    """Apply the owner-lease rule to a row :func:`lock_workstream_lease_row` locked.

    * A fenced write must match the row's holder, epoch and incarnation token,
      otherwise :class:`WorkstreamLeaseLostError`. Expiry does not matter:
      until another holder takes over, the presenting holder still has
      exclusive access.
    * An unfenced write is refused with :class:`WorkstreamLeaseHeldError` while
      any lease is live. On an expired lease it proceeds, and this transaction
      clears the holder and advances the epoch so a paused former holder can
      never write after it.

    Token-guarded lifecycle methods call this after their own incarnation
    check, so a stale incarnation keeps its documented ``False`` result rather
    than reporting the new incarnation's owner.
    """
    if lease is not None:
        if lease.ws_id != ws_id:
            raise ValueError("lease fence belongs to a different workstream")
        if not _fence_matches(row, lease):
            raise WorkstreamLeaseLostError(ws_id)
        return
    _refuse_live_lease(ws_id, row)
    if row.lease_holder is not None:
        conn.execute(
            sa.update(workstreams).where(workstreams.c.ws_id == ws_id).values(**fence_out_values())
        )


def admit_workstream_write_on_connection(
    conn: Any,
    ws_id: str,
    lease: LeaseFence | None,
) -> Any | None:
    """Lock the parent row and enforce the owner lease for one session-owned write.

    Returns the locked row (``state``, the lease columns and the incarnation
    token), or ``None`` when the row is missing so each caller keeps its
    existing missing-row behavior. See :func:`enforce_workstream_lease`.
    """
    row = lock_workstream_lease_row(conn, ws_id)
    if row is not None:
        enforce_workstream_lease(conn, ws_id, row, lease)
    return row


def admit_offline_writes_on_connection(conn: Any, ws_ids: Iterable[str]) -> set[str]:
    """Lock existing parents in sorted id order and admit an unfenced write to each.

    The batched twin of :func:`admit_workstream_write_on_connection` for
    multi-workstream offline imports: one locking statement for the whole set
    (sorted order keeps concurrent batch writers from deadlocking), a
    :class:`WorkstreamLeaseHeldError` before any mutation when any parent has a
    live lease, and one statement fencing out every expired lease. Returns the
    ids whose parent row exists.
    """
    ordered = sorted(set(ws_ids))
    rows = conn.execute(
        sa.select(*_lease_state_columns(conn.dialect.name))
        .where(workstreams.c.ws_id.in_(ordered))
        .order_by(workstreams.c.ws_id)
        .with_for_update()
    ).fetchall()
    for row in rows:
        _refuse_live_lease(str(row.ws_id), row)
    expired = [str(row.ws_id) for row in rows if row.lease_holder is not None]
    if expired:
        conn.execute(
            sa.update(workstreams)
            .where(workstreams.c.ws_id.in_(expired))
            .values(**fence_out_values())
        )
    return {str(row.ws_id) for row in rows}


def update_workstream_row_on_connection(
    conn: Any,
    ws_id: str,
    lease: LeaseFence | None,
    values: dict[str, Any],
) -> None:
    """Admit, then update columns of one ``workstreams`` row; a missing row is a no-op."""
    if admit_workstream_write_on_connection(conn, ws_id, lease) is not None:
        conn.execute(sa.update(workstreams).where(workstreams.c.ws_id == ws_id).values(**values))


def end_foreign_node_watches_on_connection(
    conn: Any,
    ws_id: str,
    node_id: str,
    lease: LeaseFence | None,
) -> list[dict[str, Any]]:
    """Admit, then deactivate ``ws_id``'s active watches bound to a node other than ``node_id``.

    Returns the ended watches in ``watch_id`` order; a missing row ends nothing.
    """
    if admit_workstream_write_on_connection(conn, ws_id, lease) is None:
        return []
    ended = conn.execute(
        sa.update(watches)
        .where(
            watches.c.ws_id == ws_id,
            watches.c.active == 1,
            sa.func.coalesce(watches.c.node_id, "").not_in(["", node_id]),
        )
        .values(active=0, next_poll="", updated=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"))
        .returning(watches.c.watch_id, watches.c.name, watches.c.command, watches.c.node_id)
    ).fetchall()
    return sorted((dict(row._mapping) for row in ended), key=lambda watch: watch["watch_id"])


@functools.cache
def unleased_predicate(dialect_name: str) -> sa.ColumnElement[bool]:
    """Rows offline maintenance may touch: no holder, or an expired lease."""
    return sa.or_(
        workstreams.c.lease_holder.is_(None),
        workstreams.c.lease_expires_ms.is_(None),
        workstreams.c.lease_expires_ms <= db_now_ms(dialect_name),
    )


def unheld_since_predicate(dialect_name: str, cutoff: str) -> sa.ColumnElement[bool]:
    """Rows whose owner lease, if any, ran out by *cutoff* (UTC ``YYYY-MM-DDTHH:MM:SS``).

    Release and fence-out clear the expiry, so an expiry that remains belongs to a holder that
    stopped renewing (it crashed, or its renewals are failing) and falls one lease TTL after its
    last renewal. Maintenance that judges a row by its age also waits for that expiry, so a row
    renewed up to one TTL before *cutoff* is still kept. A workstream someone reopened to read
    keeps an old ``updated``, and a renewal outage must not let cleanup close or prune it while
    it is still open. The window is measured back from the database clock, like every other
    lease comparison.
    """
    parsed = datetime.strptime(cutoff, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    window_ms = max(0, int((datetime.now(UTC) - parsed).total_seconds() * 1000))
    return sa.or_(
        workstreams.c.lease_expires_ms.is_(None),
        workstreams.c.lease_expires_ms <= db_now_ms(dialect_name) - window_ms,
    )


@functools.cache
def live_lease_node_id(dialect_name: str) -> sa.ColumnElement[Any]:
    """``lease_node_id`` while the lease is live by the database clock, else NULL.

    Row reads expose the holder's node as a routing hint. A crashed holder's
    expired lease must not keep attracting traffic: the next opener takes
    it over, so routing falls through to the usual placement instead.
    """
    return sa.case(
        (sa.not_(unleased_predicate(dialect_name)), workstreams.c.lease_node_id),
        else_=sa.null(),
    )


def fence_out_values() -> dict[str, Any]:
    """Column values that retire any lease during an offline write.

    Callers apply them only to rows whose lease is absent or expired: rows
    :func:`unleased_predicate` admits, or rows whose ``lease_live`` column
    (selected from it by the locking reads) is false. Advancing the epoch even
    when no holder is set is harmless and keeps the statement unconditional.
    """
    return {
        "lease_holder": None,
        "lease_node_id": None,
        "lease_expires_ms": None,
        "lease_epoch": workstreams.c.lease_epoch + 1,
    }


def close_stale_orphans_on_connection(
    conn: Any,
    kind: WorkstreamKind | str,
    cutoff: str,
    exclude_ws_ids: Sequence[str],
) -> list[str]:
    """Both backends' ``bulk_close_stale_orphans``: close the rows and return their ids.

    One UPDATE ... RETURNING, so eligibility is judged and the closed rows are
    reported by the same statement: a row a new message freshened or
    ``set_state`` moved out of the bulk-close set is neither closed nor
    reported. A row loaded by any live process carries a live owner lease, so
    the lease predicate protects it (PostgreSQL re-evaluates it against a
    concurrent acquisition's committed version of each row the UPDATE locks),
    and the same statement fences out an expired lease. ``updated`` keeps the
    session's last real use (see the protocol docstring). The caller commits.
    """
    dialect_name = conn.dialect.name
    stmt = (
        sa.update(workstreams)
        .where(
            workstreams.c.kind == WorkstreamKind(kind).value,
            workstreams.c.state.in_(BULK_CLOSE_STATE_VALUES),
            workstreams.c.updated < cutoff,
            unleased_predicate(dialect_name),
            unheld_since_predicate(dialect_name, cutoff),
        )
        .values(state="closed", **fence_out_values())
        .returning(workstreams.c.ws_id)
    )
    if exclude_ws_ids:
        # Nothing to exclude leaves out ``NOT IN ()`` and SQLAlchemy's
        # empty-collection warning.
        stmt = stmt.where(~workstreams.c.ws_id.in_(exclude_ws_ids))
    return [row[0] for row in conn.execute(stmt)]
