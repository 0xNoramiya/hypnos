"""Write Desktop fixtures (the JSON the plugin's REST routes return) from a Hypnos database.

Usage: python tests/desktop/make_fixtures.py DATA.db OUT.json
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from hypnos import db as dbmod  # noqa: E402
from hypnos import webapi  # noqa: E402
from hypnos.config import DEFAULTS  # noqa: E402


def main(db_path: str, out: str) -> None:
    conn = dbmod.open_db(db_path)
    turn = conn.execute("SELECT turn_id FROM routes WHERE mode = 'brain' ORDER BY id DESC LIMIT 1").fetchone()
    chat = None
    if turn:
        # The preview's chat shows the real turn behind the badge: its request, command and final answer.
        episode = conn.execute("SELECT id, user_message FROM episodes WHERE turn_id = ?", (turn[0],)).fetchone()
        if episode:
            steps = conn.execute("SELECT args_json, text FROM steps WHERE episode_id = ? ORDER BY step_index",
                                 (episode[0],)).fetchall()
            command = next((dbmod.loads(a, {}).get("command", "") for a, _ in steps if a), "")
            reply = next((t for _, t in steps if t), "")
            chat = {"user": episode[1], "command": command, "reply": reply}
    conn.close()
    fx = {"/status": webapi.status(db_path, dict(DEFAULTS)), "/night": webapi.night(db_path),
          "/nights": webapi.nights(db_path), "/report": webapi.report(db_path), "/metrics": webapi.metrics(db_path),
          "/sessions": webapi.sessions(db_path), "/habits": webapi.habits(db_path)}
    if turn:
        fx[f"/turns/{turn[0]}"] = webapi.turn(db_path, turn[0])
    if chat:
        fx["_chat"] = chat
    Path(out).write_text(json.dumps(fx, default=str))
    print(f"wrote {out} ({', '.join(fx)})")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
