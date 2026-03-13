"""Add tool_calls JSON column and backfill legacy rows.

Stores the complete tool_calls array on assistant messages so each LLM
response is a single atomic row.  The backfill converts existing
role="tool_call" rows into a JSON array on the preceding assistant row,
and renames role="tool_result" to role="tool".  After migration the
only roles in the table are: user, assistant, tool.

Revision ID: 013
Revises: 012
Create Date: 2026-03-13
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add column
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(sa.Column("tool_calls", sa.Text))

    # 2. Backfill: convert tool_call/tool_result rows into the new format
    conn = op.get_bind()

    # Fetch all workstreams that have legacy tool_call rows
    ws_ids = conn.execute(
        sa.text("SELECT DISTINCT ws_id FROM conversations WHERE role = 'tool_call'")
    ).fetchall()

    for (ws_id,) in ws_ids:
        rows = conn.execute(
            sa.text(
                "SELECT id, role, content, tool_name, tool_args, "
                "tool_call_id, provider_data "
                "FROM conversations WHERE ws_id = :ws_id ORDER BY id"
            ),
            {"ws_id": ws_id},
        ).fetchall()

        # Walk the rows and collect tool_call groups
        i = 0
        last_assistant_id: int | None = None
        ids_to_delete: list[int] = []

        while i < len(rows):
            row_id, role, content, tool_name, tool_args, tc_id, pdata = rows[i]

            if role == "assistant":
                last_assistant_id = row_id
                i += 1

            elif role == "tool_call":
                # Collect consecutive tool_call rows
                tool_calls_arr: list[dict[str, object]] = []
                while i < len(rows) and rows[i][1] == "tool_call":
                    r = rows[i]
                    r_id, _, _, tn, ta, stored_tc_id, _ = r
                    call_id = stored_tc_id or f"call_{ws_id}_{r_id}"
                    tool_calls_arr.append(
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tn or "",
                                "arguments": ta or "",
                            },
                        }
                    )
                    ids_to_delete.append(r_id)
                    i += 1

                tc_json = json.dumps(tool_calls_arr)

                if last_assistant_id is not None:
                    # Merge onto the preceding assistant row
                    conn.execute(
                        sa.text("UPDATE conversations SET tool_calls = :tc WHERE id = :aid"),
                        {"tc": tc_json, "aid": last_assistant_id},
                    )
                    last_assistant_id = None
                else:
                    # No preceding assistant — turn the first tool_call
                    # into an assistant row with tool_calls.
                    first_id = ids_to_delete[-len(tool_calls_arr)]
                    conn.execute(
                        sa.text(
                            "UPDATE conversations SET role = 'assistant', "
                            "content = NULL, tool_name = NULL, tool_args = NULL, "
                            "tool_call_id = NULL, tool_calls = :tc "
                            "WHERE id = :rid"
                        ),
                        {"tc": tc_json, "rid": first_id},
                    )
                    # Remove from delete list — we promoted it
                    ids_to_delete.remove(first_id)
                    last_assistant_id = None

            else:
                if role != "assistant":
                    last_assistant_id = None
                i += 1

        # Delete consumed tool_call rows
        if ids_to_delete:
            conn.execute(
                sa.text(
                    "DELETE FROM conversations WHERE id IN ("
                    + ",".join(str(d) for d in ids_to_delete)
                    + ")"
                )
            )

    # 3. Rename tool_result → tool
    conn.execute(sa.text("UPDATE conversations SET role = 'tool' WHERE role = 'tool_result'"))


def downgrade() -> None:
    conn = op.get_bind()

    # Restore tool_result role from tool rows that have a tool_call_id
    conn.execute(
        sa.text(
            "UPDATE conversations SET role = 'tool_result' "
            "WHERE role = 'tool' AND tool_call_id IS NOT NULL"
        )
    )

    # Explode assistant rows that have tool_calls back into separate rows.
    # For each assistant row with tool_calls, insert tool_call rows after it.
    rows = conn.execute(
        sa.text(
            "SELECT id, ws_id, timestamp, tool_calls FROM conversations "
            "WHERE role = 'assistant' AND tool_calls IS NOT NULL"
        )
    ).fetchall()

    import json as _json

    for row_id, ws_id, ts, tc_json in rows:
        calls = _json.loads(tc_json)
        for call in calls:
            fn = call.get("function", {})
            conn.execute(
                sa.text(
                    "INSERT INTO conversations "
                    "(ws_id, timestamp, role, content, tool_name, tool_args, tool_call_id) "
                    "VALUES (:ws_id, :ts, 'tool_call', NULL, :tn, :ta, :tcid)"
                ),
                {
                    "ws_id": ws_id,
                    "ts": ts,
                    "tn": fn.get("name", ""),
                    "ta": fn.get("arguments", ""),
                    "tcid": call.get("id", ""),
                },
            )
        # Clear tool_calls column on the assistant row
        conn.execute(
            sa.text("UPDATE conversations SET tool_calls = NULL WHERE id = :rid"),
            {"rid": row_id},
        )

    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("tool_calls")
