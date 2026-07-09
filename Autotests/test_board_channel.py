"""Board channel adapter (stage-2 §4.1), fork-side / DB-free.

Two layers: (1) registration-chain consistency — pure text checks that `board` is wired into
all three channels.metta dispatch chains and imported; these always run. (2) buffer + render
behavior — needs `omegahive` (the pinned port client + shared renderer), so guarded with
importorskip: they skip in the pre-omegahive base image and run in the hive image / local dev.
The DB-required binding tests (replay-vs-repeat under the real policy) live omegahive-side.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "channels"))

import board  # channels/board.py — stdlib-only at import (omegahive is lazy)


@pytest.fixture(autouse=True)
def _reset_board_state():
    board._buffer = ""
    board._buffer_cursor = 0
    board._delivered_cursor = None
    board._port = None
    board._actor_id = "coordinator"
    yield


# --- registration-chain consistency (always runs) --------------------------------

def test_board_registered_in_all_three_dispatch_chains():
    ch = (ROOT / "src" / "channels.metta").read_text()
    assert "board.start_board" in ch, "initChannels missing board branch"
    assert "board.getLastMessage" in ch, "receive missing board branch"
    assert "board.send_message" in ch, "send missing board branch"


def test_board_module_imported_and_interface_present():
    assert "./channels/board.py" in (ROOT / "lib_omegaclaw.metta").read_text()
    for fn in ("start_board", "getLastMessage", "send_message", "stop_board"):
        assert callable(getattr(board, fn)), f"board.{fn} missing"


def test_get_last_message_empty_returns_empty_string():
    assert board.getLastMessage() == ""


# --- buffer + render behavior (needs omegahive) ----------------------------------

def _view(cursor, tasks, *, events=None, changed=True, mismatch=False):
    from omegahive.port.wire import PortView
    return PortView(cursor=cursor, generation=1, events=events or [], board=tasks,
                    changed=changed, generation_mismatch=mismatch)


def test_render_and_buffer_is_replace_with_latest_and_read_once():
    pytest.importorskip("omegahive.port.render")
    from omegahive.board.state import Board, TaskState

    board._render_and_buffer(
        _view(5, Board(tasks={"t1": TaskState("t1", "ready")})), "coordinator")
    board._render_and_buffer(
        _view(8, Board(tasks={"t1": TaskState("t1", "assigned", owner="w1")})), "coordinator")

    msg = board.getLastMessage()
    assert ":status assigned" in msg and ":status ready" not in msg  # latest only, not concatenated
    assert board.getLastMessage() == ""                              # read-once clears
    assert board._delivered_cursor == 8                              # advanced to consumed view


def test_no_change_view_is_not_buffered():
    pytest.importorskip("omegahive.port.render")
    board._render_and_buffer(_view(5, None, changed=False), "coordinator")
    assert board.getLastMessage() == ""


def test_rejections_are_rendered_from_the_event_delta():
    pytest.importorskip("omegahive.port.render")
    from uuid import uuid4

    from omegahive.board.state import Board, TaskState
    from omegahive.events.envelope import Actor, Event

    rej = Event(event_id=uuid4(), run_id="r", logical_ts=1,
                actor=Actor(role="gateway", id="gateway"), event_type="gateway.rejected",
                payload={"original_actor_role": "coordinator", "original_actor_id": "coordinator",
                         "refused_event_type": "task.assigned", "refused_task_id": "t1",
                         "refused_payload": {"worker": "w2"}, "code": "ALREADY_OWNED"})
    board._render_and_buffer(
        _view(9, Board(tasks={"t1": TaskState("t1", "assigned", owner="w1")}), events=[rej]),
        "coordinator", [])
    assert "(rejected (op assign t1) :code ALREADY_OWNED)" in board.getLastMessage()


def test_generation_mismatch_drops_the_cursor():
    pytest.importorskip("omegahive.port.render")

    class _FakePort:
        def read(self, cursor):
            return _view(cursor or 0, None, changed=False, mismatch=True)

    board._delivered_cursor = 10
    board._port = _FakePort()
    board._poll_once()
    assert board._delivered_cursor is None      # dropped; next poll re-snapshots
    assert board.getLastMessage() == ""          # nothing persisted from a mismatch view


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
