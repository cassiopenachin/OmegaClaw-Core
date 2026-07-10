"""Board channel adapter (stage-2 hive binding, §4.1).

The agent's one active channel is the OmegaHive board. A polling thread holds one long-lived
port client and, whenever the board advances, renders the S-expression view (via the shared
``omegahive.port.render`` renderer, so the OmegaClaw and vanilla rungs read an identical view)
and hands it to the read-once buffer with **replace-with-latest** semantics.

Delivery: render the delta since the *last delivered* view (not the last poll), so *this
actor's* rejections since the agent last looked survive across polls until the agent consumes
them — a refusal must outlive the turn. ``getLastMessage`` returns-and-clears the buffer and
advances the delivered cursor. ``send_message`` writes a log line only: nothing consumes notes
in the spike and a note event would fold under the run lock and pollute views for nothing.

Client state (basis/cursor/generation) is the port's ``BasisStore`` under ``memory/`` (the only
writable mount; ``include_workdir: false``) in a single per-actor store shared by this reader
and each ``board`` skill-call emitter — the port write-throughs the basis on every poll,
including no-change reads.
"""

from __future__ import annotations

import os
import threading
import time

# omegahive + psycopg are imported lazily inside the functions below, not at module top:
# this module is imported at boot for every channel (via lib_omegaclaw), but its heavy deps
# are only present in the hive image and only needed when commchannel=board is active.

_lock = threading.Lock()
_buffer = ""          # rendered view, read-once, replace-with-latest
_buffer_cursor = 0    # anchor seq of the buffered view
_delivered_cursor: int | None = None  # cursor as of the last getLastMessage (None => snapshot)
_running = False
_thread: threading.Thread | None = None
_port: HiveCoordinatorPort | None = None
_run_id = ""
_actor_id = "coordinator"


def _dsn() -> str:
    dsn = os.environ.get("OMEGAHIVE_DATABASE_URL")
    if not dsn:
        raise RuntimeError("OMEGAHIVE_DATABASE_URL not set (needed by the board channel)")
    return dsn


def _connect():
    """Fresh connection; also the port's reconnect factory."""
    import psycopg
    return psycopg.connect(_dsn())


def _client_state_dir() -> str:
    base = os.environ.get("MEMORY_DIR", "memory")
    path = os.path.join(base, "hive-client-state")
    os.makedirs(path, exist_ok=True)
    return path


def _render_and_buffer(view, actor_id: str) -> None:
    """Render a changed view and place it in the read-once buffer (replace-with-latest)."""
    if not view.changed or view.board is None:
        return
    from omegahive.port.render import render_view
    # The worker roster is board state (board.roster, from worker.registered events); the
    # renderer sources it directly — the adapter does not pass a workers list.
    rendered = render_view(view.board, view.events, actor_id=actor_id)
    with _lock:
        global _buffer, _buffer_cursor
        _buffer = rendered
        _buffer_cursor = view.cursor


def _poll_once() -> None:
    """One read of the board from the last-delivered cursor; buffer the view if it advanced."""
    global _delivered_cursor, _buffer
    with _lock:
        cursor = _delivered_cursor
    view = _port.read(cursor)
    if view.generation_mismatch:
        # Restore invalidated cursors: drop cursor, re-snapshot next poll (adopts the new
        # generation). Persist nothing from a mismatch view — drop any stale buffered view
        # too, or getLastMessage would hand back an old-generation board (port spec §2).
        with _lock:
            _delivered_cursor = None
            _buffer = ""
        return
    _render_and_buffer(view, _actor_id)


def _poll_loop(poll_interval: float) -> None:
    while _running:
        try:
            _poll_once()
        except Exception as exc:  # a transient read error must not kill the reader thread
            print(f"[channels.board] poll error: {exc}", flush=True)
        time.sleep(poll_interval)


def start_board(run_id: str, actor_id: str = "coordinator",
                poll_interval: float = 1.0) -> threading.Thread:
    """Open the port for this coordinator and start the 1s polling thread. The assignable
    worker roster is board state (board.roster) surfaced by the renderer — not passed here."""
    from omegahive.events.envelope import Actor
    from omegahive.port import HiveCoordinatorPort

    # MeTTa's py-call marshals an empty argv value ("") as an empty list, not "" — coerce.
    actor_id = actor_id if isinstance(actor_id, str) and actor_id else "coordinator"
    run_id = run_id if isinstance(run_id, str) else ""

    global _running, _thread, _port, _run_id, _actor_id
    # Stop any previous poller before starting a new one (a re-init must not leak a thread).
    if _thread is not None and _thread.is_alive():
        _running = False
        _thread.join(timeout=poll_interval + 1)

    _run_id = run_id
    _actor_id = actor_id
    # A missing/unreachable DB must not crash agent boot: log and leave the channel inactive
    # (getLastMessage returns "" and the agent boots) rather than propagating out of initChannels.
    try:
        _port = HiveCoordinatorPort(
            Actor(role="coordinator", id=actor_id),
            run_id,
            _connect(),
            workdir=_client_state_dir(),
            connect=_connect,
        )
    except Exception as exc:
        print(f"[channels.board] board channel inactive — cannot open port: {exc}", flush=True)
        _port = None
        return None
    _running = True
    _thread = threading.Thread(target=_poll_loop, args=(poll_interval,), daemon=True)
    _thread.start()
    return _thread


def getLastMessage() -> str:
    """Return-and-clear the latest board view; advance the delivered cursor so the next view
    spans only what arrives after this one."""
    global _buffer, _delivered_cursor
    with _lock:
        if not _buffer:
            return ""
        msg = _buffer
        _delivered_cursor = _buffer_cursor
        _buffer = ""
    return msg


def send_message(text: str) -> bool:
    """No board event — the spike consumes no notes. Captured as a harness log line."""
    print(f"[BOARD_SEND] {text}", flush=True)
    return True


def _parse_op(payload: str):
    """Parse a single-string board op into an omegahive Op. Returns (op, None) or (None, error).
    Grammar: assign/reassign <task> <worker> | escalate/close/reopen/prune <task> [reason...]."""
    # The `board` skill is advertised on every deployment, but only the hive image installs
    # omegahive — degrade to an error string, never raise into the agent loop.
    try:
        from omegahive.port import (
            AssignOp, CloseOp, EscalateOp, PruneOp, ReassignOp, ReopenOp,
        )
    except ImportError:
        return None, "board: unavailable (no board binding in this image)"

    toks = payload.split()
    if not toks:
        return None, "board: empty op"
    verb, rest = toks[0], toks[1:]

    if verb == "assign":
        if len(rest) < 2:
            return None, "board: assign needs <task> <worker>"
        return AssignOp(task_id=rest[0], worker=rest[1]), None
    if verb == "reassign":
        # reassign <task> <to>  (from unspecified) | reassign <task> <from> <to>
        if len(rest) == 2:
            op = ReassignOp.model_validate({"task_id": rest[0], "from": "", "to": rest[1]})
            return op, None
        if len(rest) >= 3:
            op = ReassignOp.model_validate({"task_id": rest[0], "from": rest[1], "to": rest[2]})
            return op, None
        return None, "board: reassign needs <task> <worker>"
    if verb in ("escalate", "close", "reopen", "prune"):
        if not rest:
            return None, f"board: {verb} needs <task>"
        task, reason = rest[0], (" ".join(rest[1:]) or None)
        if verb == "escalate":
            return EscalateOp(task_id=task, reason=reason or "escalated by coordinator"), None
        if verb == "close":
            return CloseOp(task_id=task, reason=reason), None
        if verb == "reopen":
            return ReopenOp(task_id=task, reason=reason), None
        return PruneOp(task_id=task, reason=reason), None

    return None, (f"board: unknown op '{verb}' "
                  "(assign|reassign|escalate|close|reopen|prune)")


def board_op(payload: str) -> str:
    """The `board` skill body: parse one op and emit it through a short-lived per-call port
    client, constructor-seeded from the shared basis/cursor/generation store (so an intentional
    repeat after a fresh view re-keys instead of silently deduping). One call = one emit; key
    derivation (content+basis) is the port client's job. Returns the Accepted/Rejected outcome
    as the skill's string result. Never raises into the agent loop."""
    op, err = _parse_op(payload)
    if err:
        return err
    if not _run_id:
        return "board: channel not active (no run_id)"

    from omegahive.events.envelope import Actor
    from omegahive.port import Accepted, HiveCoordinatorPort

    # The whole emit path (connect included) is inside try: a refusal is a value, but a
    # connection loss / infra error must return a string, never propagate into the agent loop.
    conn = None
    try:
        conn = _connect()
        port = HiveCoordinatorPort(
            Actor(role="coordinator", id=_actor_id), _run_id, conn,
            workdir=_client_state_dir(), connect=_connect,
        )
        result = port.emit(op)   # idempotency_key=None -> client derives content+basis key
    except Exception as exc:
        return f"board: emit failed: {exc}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    event_type, _payload, task_id = op.to_emit()
    if isinstance(result, Accepted):
        return f"Accepted {event_type} {task_id}"
    return f"Rejected {result.code}: {result.reason}"


def stop_board() -> None:
    global _running
    _running = False
