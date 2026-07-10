"""Agent-driven board end-to-end smoke (stage-2 hive binding).

Closes the loop the plumbing tests skip: the *real* agent, booted on `commchannel=board`
against a *real* Postgres, reads the seeded board, asks its LLM, and — on the mock LLM's canned
`board "assign t1 w1"` reply — parses and dispatches the `board` skill, which emits through the
port; the gateway accepts and the board mutates. We then assert Postgres shows `t1` assigned. So
this exercises receive(board view) → LLM → parse → skill → port emit → board change, in one live
process. (An illegal op like `prune t1` here is correctly *rejected* by the port's legality
engine and the refusal folds back into the next view — the reject path also works.)

Needs podman + the `omegaclaw-hive` image + network access to pull `postgres:16`; it is a
manual/local smoke (like the deployment #0 acceptance run), not a fork-CI unit test — run with:

    RUN_BOARD_E2E=1 python3 Autotests/test_board_e2e.py

The mock returns a fixed op (it does not reason); the point is that the agent's machinery
carries a board decision all the way to a persisted board mutation.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FORK = os.path.dirname(HERE)
WORKTREE = "/home/cassio/src/SNET/omegahive-wt/render"
sys.path.insert(0, os.path.join(HERE, "mock"))

IMAGE = "localhost/omegaclaw-hive:0.1"
RUN_ID = "hive-e2e"
NET = "hivenet"
PG = "hive-pg"
CTR = "board-e2e"
DSN_HOST = "postgresql://omegahive:omegahive@localhost:5433/omegahive"
DSN_CONT = "postgresql://omegahive:omegahive@%s:5432/omegahive" % PG
VENV_PY = "/home/cassio/src/SNET/omegahive/.venv/bin/python"


def sh(*args, **kw):
    return subprocess.run(list(args), capture_output=True, text=True, **kw)


def _pg_up_and_seeded():
    sh("podman", "network", "create", NET)   # ignore "already exists"
    sh("podman", "rm", "-f", PG)              # always start from a clean, fresh DB (also resets
    #                                           the run so the precondition holds on re-runs)
    r = sh("podman", "run", "-d", "--name", PG, "--network", NET, "-p", "5433:5432",
           "-e", "POSTGRES_USER=omegahive", "-e", "POSTGRES_PASSWORD=omegahive",
           "-e", "POSTGRES_DB=omegahive", "postgres:16")
    assert r.returncode == 0, f"postgres run failed: {r.stderr}"
    for _ in range(60):
        if sh("podman", "exec", PG, "pg_isready", "-U", "omegahive", "-d", "omegahive").returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError("postgres never became ready")
    env = {**os.environ, "OMEGAHIVE_DATABASE_URL": DSN_HOST, "PYTHONPATH": WORKTREE + "/src"}
    m = sh(VENV_PY, "-m", "omegahive.cli", "db-migrate", cwd=WORKTREE, env=env)
    assert m.returncode == 0, f"db-migrate failed: {m.stderr}"
    s = sh(VENV_PY, "-m", "omegahive.cli", "seed-demo", "--run-id", RUN_ID,
           "--plan", "scenarios/demo_plan.yaml", cwd=WORKTREE, env=env)
    assert s.returncode == 0, f"seed-demo failed: {s.stderr}"


def _t1_assigned() -> bool:
    # A legal coordinator op that mutates the board: assign a worker to t1 -> task.assigned.
    # (prune t1 is legitimately illegal here — it would drop t2's k=1 join below k.)
    q = ("select count(*) from events where run_id='%s' and event_type='task.assigned' "
         "and task_id='t1';" % RUN_ID)
    out = sh("podman", "exec", PG, "psql", "-U", "omegahive", "-d", "omegahive",
             "-tAc", q).stdout.strip()
    return out.isdigit() and int(out) > 0


def run_board_e2e() -> bool:
    from llm import LLM_MOCK_PORT, LlmMockController

    _pg_up_and_seeded()
    assert not _t1_assigned(), "precondition: t1 not assigned before the run"

    ctrl = LlmMockController(("0.0.0.0", LLM_MOCK_PORT))
    sh("podman", "rm", "-f", CTR)
    sh("podman", "run", "-d", "-it", "--name", CTR, "--network", NET,
       "--add-host=host.docker.internal:host-gateway",
       "--security-opt", "no-new-privileges:true", "--init",
       "--tmpfs", "/tmp:size=64m,mode=1777", "--tmpfs", "/var/tmp:size=64m,mode=1777",
       "--tmpfs", "/run:size=16m,mode=0755",
       "-e", "OMEGAHIVE_DATABASE_URL=" + DSN_CONT,
       "-e", "TEST_SERVER_IP=host.docker.internal",
       "-e", "ANTHROPIC_API_KEY=dummy",
       IMAGE,
       "commchannel=board", "provider=Test", "embeddingprovider=Local",
       "run_id=" + RUN_ID, "actor_id=coordinator",  # w1 is auto-registered by the demo_plan seed
       "securityPolicyPath=/PeTTa/repos/OmegaClaw-Core/profile/policy.yaml")

    # Register the canned decision once the in-container mock LLM has connected.
    for _ in range(60):
        if ctrl.set_answer("__default__", 'board "assign t1 w1"'):
            print("[e2e] default answer registered (mock LLM connected)", flush=True)
            break
        time.sleep(1)

    # Wait for the agent to carry the decision to a persisted board mutation.
    ok = False
    for _ in range(60):
        logs = sh("podman", "logs", CTR).stdout + sh("podman", "logs", CTR).stderr
        if "Accepted task.assigned t1" in logs or _t1_assigned():
            ok = True
            break
        time.sleep(2)

    logs = sh("podman", "logs", CTR).stdout + sh("podman", "logs", CTR).stderr
    delivered = "(task t1" in logs  # the rendered board view reached the agent's prompt
    ctrl.stop()
    print("[e2e] board view delivered to agent:", delivered, flush=True)
    print("[e2e] agent emitted assign (log):", "Accepted task.assigned t1" in logs, flush=True)
    print("[e2e] t1 assigned in Postgres:", _t1_assigned(), flush=True)
    return ok and delivered and _t1_assigned()


def test_board_e2e():
    import pytest
    if os.environ.get("RUN_BOARD_E2E") != "1":
        pytest.skip("board E2E needs podman + postgres; set RUN_BOARD_E2E=1")
    assert run_board_e2e()


if __name__ == "__main__":
    os.environ["RUN_BOARD_E2E"] = "1"
    result = run_board_e2e()
    print("\n=== BOARD E2E:", "PASS" if result else "FAIL", "===")
    sys.exit(0 if result else 1)
