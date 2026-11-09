"""Read runtime status and explicitly acknowledge verified uncertain operations."""

import json
import sqlite3
from contextlib import closing
from marlow_constants import get_marlow_home


def command(args):
    from agent.codex_state import RuntimeState

    path = get_marlow_home() / "runtime/codex-runs.sqlite3"
    if path.is_symlink():
        raise ValueError("Runtime status database must not be a symlink")
    if not path.exists():
        print("No Codex runtime executions recorded.")
        return
    with closing(sqlite3.connect(path)) as db:
        rows = db.execute(
            "SELECT scope,thread_id,turn_id,status,updated FROM threads ORDER BY updated DESC"
        ).fetchall()
    if args.runtime_action == "status":
        print(
            json.dumps(
                [
                    dict(
                        scope=json.loads(scope),
                        thread=thread,
                        turn=turn,
                        status=status,
                        updated=updated,
                    )
                    for scope, thread, turn, status, updated in rows
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if not args.checked:
        raise ValueError(
            "Verify the actual side effects first, then pass --checked; this does not retry the task"
        )
    turn = None if args.turn == "unknown" else args.turn
    matches = [
        row
        for row in rows
        if json.loads(row[0]).get("session") == args.session
        and row[1] == args.thread
        and row[2] == turn
    ]
    if len(matches) != 1:
        raise ValueError(
            "Specify one exact current session, thread and turn from runtime status"
        )
    state = RuntimeState(get_marlow_home(), json.loads(matches[0][0]))
    state.acknowledge(thread_id=args.thread, turn_id=turn)
    print(
        "Verification recorded. The previous run remains in audit history. No task was replayed."
    )
