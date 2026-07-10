"""The `board` single-string skill (stage-2 §4.2), fork-side / DB-free.

Covers the three-site consistency (equation <-> catalog <-> LLM_COMMANDS), the parser hazard
(a board op on the line after a send must not be swallowed into the send payload), and the
payload parser -> Op mapping (omegahive-guarded). One call = one emit; key derivation is the
port client's job (not exercised here — that's the omegahive-side DB binding test)."""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "channels"))

import board  # channels/board.py — stdlib-only at import
import helper  # src/helper.py — host-safe (no heavy imports)


# --- three-site consistency (always runs) ----------------------------------------

def test_board_registered_in_all_three_skill_sites():
    skills = (ROOT / "src" / "skills.metta").read_text()
    assert "(= (board " in skills, "board skill equation missing"
    assert ": board string" in skills, "board catalog line missing"
    assert "board" in helper.LLM_COMMANDS, "board missing from LLM_COMMANDS"


def test_board_is_not_a_two_arg_command():
    # board takes a single string payload; it must not be in the <cmd> <file> <content> set.
    src = (ROOT / "src" / "helper.py").read_text()
    two_arg = next(line for line in src.splitlines() if "special_two_arg_cmds" in line and "{" in line)
    assert "board" not in two_arg, "board must not be in special_two_arg_cmds"


# --- parser hazard: a board op after a send is its own command -------------------

def test_board_op_after_send_is_not_swallowed():
    merged = helper._merge_send_continuations(['send "hello"', 'board "assign t1 w2"'])
    assert any('board "assign t1 w2"' in m for m in merged)            # kept as its own command
    assert any(m.startswith("send ") and "assign" not in m for m in merged)  # not folded into send


def test_control_unknown_line_after_send_is_still_swallowed():
    merged = helper._merge_send_continuations(['send "hello"', 'trailing prose line'])
    assert any(m.startswith("send") and "trailing prose line" in m for m in merged)


# --- payload parser -> Op (needs omegahive) --------------------------------------

def test_parse_op_maps_each_verb():
    pytest.importorskip("omegahive.port")
    assert board._parse_op("assign t1 w2")[0].to_emit() == ("task.assigned", {"worker": "w2"}, "t1")
    prune, err = board._parse_op("prune t3")
    assert err is None and prune.to_emit()[0] == "task.pruned" and prune.task_id == "t3"
    assert board._parse_op("escalate t2 too slow")[0].to_emit() == \
        ("task.escalated", {"reason": "too slow"}, "t2")
    assert board._parse_op("close t1")[0].to_emit()[1]["status"] == "done"
    assert board._parse_op("reopen t1")[0].to_emit()[1]["status"] == "reopened"
    assert board._parse_op("reassign t1 w9")[0].to_emit() == \
        ("task.reassigned", {"from": "", "to": "w9", "reason": None}, "t1")
    assert board._parse_op("reassign t1 w1 w9")[0].to_emit()[1] == {"from": "w1", "to": "w9", "reason": None}


def test_parse_op_errors_are_strings_not_exceptions():
    pytest.importorskip("omegahive.port")
    assert board._parse_op("frobnicate t1") == (None, board._parse_op("frobnicate t1")[1])
    assert "unknown op" in board._parse_op("frobnicate t1")[1]
    assert "needs" in board._parse_op("assign t1")[1]
    assert "empty" in board._parse_op("")[1]


def test_board_op_reports_inactive_channel_without_emitting():
    pytest.importorskip("omegahive.port")
    board._run_id = ""
    assert "not active" in board.board_op("assign t1 w2")   # no run_id -> refuse before connecting
    assert "unknown op" in board.board_op("nope t1")         # parse error precedes the run_id check


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
