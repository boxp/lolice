import datetime, pathlib, sys
now = datetime.datetime.now(datetime.timezone.utc)
run = (now + datetime.timedelta(seconds=3)).strftime("%Y%m%dT%H%M%SZ") + "-" + sys.argv[1]
root = pathlib.Path("/home/.codex-task-board")
d = root / "runs" / "BOXP-428" / run
d.mkdir(parents=True)
finished = (now - datetime.timedelta(seconds=120)).isoformat().replace("+00:00", "Z")
(d / "summary.edn").write_text("{:status :done :agent \"claude\" :finished-at \"" + finished + "\"}")
# The exporter masks a line that names a secret and the nearest non-blank lines, so the
# clean marker sits two lines away: it proves the body arrives while the secret does not.
(d / "prompt.md").write_text("cleanbodymarker-" + sys.argv[1] + "\nplain line one\nplain line two\npassword=syntheticpassword428\nplain line three\nsyntheticgeminivalue428\n")
(d / "last-message.md").write_text("synthetic completion")
(d / "events.jsonl").write_text("{}\n")
project = "-home-boxp--codex-task-board-workspaces-BOXP-428-" + run
p = pathlib.Path("/home/.claude/projects") / project
p.mkdir(parents=True, exist_ok=True)
(p / "session.jsonl").write_text("{}\n")
