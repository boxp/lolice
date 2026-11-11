import html
import importlib.util
import inspect
import json
import os
import re
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

# Synthetic values, built at runtime so secret scanners do not flag this file.
ENV_SECRET = "envsecret" + "-value-" + "12345"
SECRETS = {
    "bearer": "Authorization: Bearer " + "abc.def" + "ghi12345",
    "github": "ghp_" + "a1B2" * 9,
    "openai": "sk-" + "proj" + "Zz09" * 8,
    "langfuse": "sk-lf-" + "Qw12" * 6,
    "aws": "AKIA" + "ABCDEFGH" + "IJKLMNOP",
    "google": "AIza" + "Sy" + "aB1" * 11,
    "telegram": "12345678" + "9:" + "AbC-" * 9,
    "jwt": "eyJ" + "hbGci" + ".eyJ" + "zdWIi" + ".sig" + "nature",
    "url": "postgres://user:" + "hunter2pass" + "@db:5432/x",
    "assign": "api_key=" + "notreallysecret9",
    "pem": "-----BEGIN " + "PRIVATE KEY-----\nMIIabc\n-----END " + "PRIVATE KEY-----",
    "env": ENV_SECRET,
}
MARKERS = ["abc.defghi12345", "a1B2a1B2", "Zz09Zz09", "Qw12Qw12", "ABCDEFGHIJKLMNOP", "aB1aB1", "AbC-AbC-",
           "hbGci", "hunter2pass", "notreallysecret9", "MIIabc", ENV_SECRET]

os.environ["BOXP_TEST_API_TOKEN"] = ENV_SECRET
spec = importlib.util.spec_from_file_location("langfuse_observer", Path(__file__).with_name("__init__.py"))
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class Ctx:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, cb):
        self.hooks[name] = cb


def run_turn(session="kohako-g2-abc", turn="t1", platform="api_server", body="hello", tool="terminal", args=None, result="ok"):
    plugin._on_turn_start(session_id=session, turn_id=turn, user_message=body, model="m", platform=platform)
    plugin._on_api_request(session_id=session, turn_id=turn, api_request_id="a1", system_prompt="SYS " + body,
                           request_messages=[{"role": "user", "content": body}], message_count=1, retry_count=0, started_at=time.time())
    plugin._on_api_response(session_id=session, turn_id=turn, api_request_id="a1", response={"assistant_message": {"content": "reply " + body}},
                            usage={"input_tokens": 10, "cache_read_tokens": 4, "output_tokens": 5, "reasoning_tokens": 2},
                            model="m", base_url="https://x", started_at=time.time(), ended_at=time.time())
    plugin._on_tool(session_id=session, turn_id=turn, tool_call_id="c1", tool_name=tool, args=args if args is not None else {"command": "ls"},
                    result=result, duration_ms=10, status="ok")
    plugin._on_turn_end(session_id=session, turn_id=turn, assistant_response="done " + body)
    plugin._on_session_end(session_id=session, turn_id=turn, completed=True)


def drain():
    """Queued items: each is the list of masked OTLP batches of one turn."""
    out = []
    while not plugin._queue.empty():
        out.append(plugin._queue.get_nowait())
    return out


def bodies():
    return "".join(body for item in drain() for body in item)


class capture_turns:
    """Turns as they are built, for tests that look at the buffer behind the masked batches."""
    def __enter__(self):
        self.turns = []
        original = plugin.build_spans
        self.patch = patch.object(plugin, "build_spans", side_effect=lambda turn: (self.turns.append(turn), original(turn))[1])
        self.patch.__enter__()
        return self.turns

    def __exit__(self, *exc):
        self.patch.__exit__(*exc)


class ObserverTest(unittest.TestCase):
    def setUp(self):
        drain()
        plugin._turns.clear()
        plugin._stats.clear()
        plugin._sessions.clear()
        getattr(plugin, "_pins", {}).clear()
        getattr(plugin, "_parents", {}).clear()
        getattr(plugin, "_pinned_digests", {}).clear()
        plugin._delegated.clear()
        getattr(plugin, "_scanned", {}).clear()
        plugin._creds = ("pk-lf-" + "test" * 4, "sk-lf-" + "test" * 4)
        plugin._secret_values = None

    def test_td12_sources_and_mentions(self):
        mentions = [
            {'command': "kubectl logs -n x pod | grep -viE 'authorization|token|secret|key=' | tail"},
            {'command': "python3 /opt/x/delegate.py run <<'EOF'\n.env kubectl get secret printenv\nEOF"},
            {'command': 'bb task-board.bb append-note X --note "ID token .env kubectl get secret printenv"'},
            {'command': "git commit -m 'stop reading .env and gh auth token in tests'"},
            {'command': 'grep -c -i token out.log'}, {'command': "grep -il 'token\\|bearer' a.json"},
            {'command': "rg -n '\"op\": \"set\"' src"},
            {'command': "python3 - <<'EOF'\nimport os\nlayout = os.environ.get('LAYOUT', '/tmp/x')\nc = Client(token=os.environ['MCP_TOKEN'])\nchanges = [{'op': 'set'}]\nEOF"},
            {'command': 'node -e "const H = process.env.H || \'http://x\'"'},
            {'command': '/usr/bin/env python3 x.py'}, {'command': 'env LANG=C sort a.txt'}, {'command': 'env -u HOME make test'}, {'command': 'set -e; make'}, {'command': 'kubectl exec pod -- ls /app'}, {'command': "kubectl exec pod -- sh -c 'make test'"},
            {'path': '/tmp/notes.md', 'old_string': 'a', 'new_string': 'the token is in .env; run printenv; kubectl get secret'},
            {'path': '/tmp/x.md', 'content': 'cat ~/.kube/config'}, {'goal': 'read .env and print the token'},
            {'cmd': ['bash', '-lc', 'grep -rn token src']}, {'command': 'echo the ID token is checked'},
            {'code': "print(os.environ.get('LAYOUT'))"},
            {'command': "cat <<'EOF'\n$(cat .env) printenv\nEOF"}, {'command': 'cat <<EOF\nread .env and printenv $(date)\nEOF'},
            {'command': "bash -c 'make'"}, {'command': 'bash -c true'}, {'command': 'grep -c token out.log'},
            {'command': "echo '<<x'; cat <<'EOF'\ncat .env\nEOF"},
            {'command': "python3 - <<'EOF'\nimport os, subprocess\nenv = os.environ.copy()\nenv['LANG'] = 'C'\nsubprocess.run(['make'], env=os.environ.copy())\nEOF"},
        ]
        sources = [{'command': s} for s in [
            'cat .env', "bash -lc 'cat ~/.kube/config'", 'kubectl get secret x -o yaml | base64 -d',
            'env | grep -i grafana', 'printenv', 'export', 'set', 'declare -p',
            "python3 - <<'EOF'\nimport os\nprint(dict(os.environ))\nEOF",
            "python3 -c 'import os; print(os.environ.copy())'",
            "python3 - <<'EOF'\nimport os, json\nprint(json.dumps(dict(os.environ.copy())))\nEOF",
            "python3 -c 'import os; x = os.environ.copy().items(); print(list(x))'",
            'python3 -c "import os; print(os.environ[\'GITHUB_TOKEN\'])"',
            "node -e 'console.log(JSON.stringify(process.env))'", 'X=$(gh auth token) cmd',
            'echo "$(cat .env)"', 'git commit -m "$(cat ~/.netrc)"', 'cat mytoken', 'head < access_token',
            "ssh host bash <<'EOF'\ncat .env\nEOF", 'cat > /tmp/x <<EOF\nhello\nEOF\ncat .env',
            'grep -r x src; cat secrets/db',
            "bash -c 'printenv'", 'bash -c printenv', 'sh -lc env', "bash -c 'export'", 'sudo bash -c printenv',
            "bash -c 'cat mytoken'", 'zsh -c "bash -c printenv"',
            # env with assignments/options before the command runs that command (BOXP-428 TD-12 P1)
            'env LANG=C printenv', 'env -u HOME printenv', 'env -i LANG=C printenv PATH', 'sudo env LANG=C printenv',
            'env LANG=C bash -c printenv', 'env LANG=C cat mytoken', 'env -S printenv', 'env LANG=C env',
            # an unquoted heredoc body still runs command substitutions (BOXP-428 TD-12 P0)
            'cat <<EOF\n$(cat .env)\nEOF', 'cat <<EOF\nv=$(printenv)\nEOF', 'cat <<-EOF\n\t`cat mytoken`\nEOF',
            'tee /tmp/x <<"EOF2"\nok\nEOF2\ncat <<EOF\n$(env)\nEOF',
            # a quoted or commented <<EOF is not a heredoc operator, so the next lines still run (BOXP-428 TD-12 P0)
            "echo '<<EOF'\ncat .env\nEOF", 'echo "x <<EOF"\ncat .env\nEOF', 'echo a # <<EOF\ncat .env\nEOF',
            "printf '%s' 'a\n<<EOF'\nprintenv\nEOF", 'echo \\<<EOF\ncat .env\nEOF']] + [{'path': '/app/.env'}, {'path': 'mytoken'},
            {'cmd': ['bash', '-lc', 'printenv']},
            {'cmd': ['bash', '-lc', 'cat ~/.npmrc']}, {'code': "print(os.environ['GITHUB_TOKEN'])"}]
        for value in mentions:
            with self.subTest(mention=value):
                self.assertFalse(plugin._withhold(json.dumps(value)))
        for value in sources:
            with self.subTest(source=value):
                self.assertTrue(plugin._withhold(json.dumps(value)))

    def test_td12_documentation_placeholders(self):
        real = 'abcd1234' + 'efgh5678' + 'ijkl9012'
        for i, result in enumerate(['Authorization: Bearer <token>\nBearer YOUR_API_TOKEN\npostgres://user:password@db/app',
                                    'Authorization: Bearer ' + real, 'postgres://app:' + real + '@db/app']):
            with self.subTest(i=i):
                session = 'kohako-g2-td12-doc-' + str(i)
                self._value_only_reply(session, 'read_file', {'path': '/tmp/a'}, result=result)
                body = self._bodies()
                self.assertNotIn(real, body)
                self.assertEqual(i == 0, 'plainconfigvalue' in body)

    def test_td12_continued_session(self):
        for i, command in enumerate(['kubectl logs pod | grep -viE "authorization|token|secret"', 'cat /app/.env']):
            with self.subTest(command=command):
                session = 'kohako-g2-td12-continued-' + str(i)
                plugin._on_turn_start(session_id=session, turn_id='t', user_message='td12requestmarker')
                plugin._on_tool(session_id=session, turn_id='t', tool_call_id='c1', tool_name='terminal', args={'command': command}, result='td12tooloutputmarker')
                plugin._on_tool(session_id=session, turn_id='t', tool_call_id='c2', tool_name='read_file', args={'path': '/work/README.md'}, result='td12readmemarker')
                plugin._on_turn_end(session_id=session, turn_id='t', assistant_response='firstreply')
                plugin._on_session_end(session_id=session, turn_id='t', completed=True)
                run_turn(session=session, turn='t2', body='td12nextturnmarker', tool='todo', args={}, result='td12replymarker')
                body = self._bodies()
                self.assertIn('td12requestmarker', body)
                for marker in ('td12tooloutputmarker', 'td12readmemarker', 'td12nextturnmarker', 'td12replymarker'):
                    self.assertEqual(i == 0, marker in body, marker)

    def test_named_value_restatements(self):
        value = "hunterhorsebattery"
        for result in ("password: " + value, "password: '" + value + "'", '{"api_key": "' + value + '"}',
                       "--token " + value, "curl -u admin:" + value,
                       '{"name": "APP_SECRET", "value": "' + value + '"}', "secret: |\n  " + value,
                       "<password>" + value + "</password>", '<config><db_password type="x">\n  ' + value + "\n</db_password></config>",
                       "&lt;apiToken&gt;" + value + "&lt;/apiToken&gt;"):
            with self.subTest(result=result):
                session = "kohako-g2-" + str(abs(hash(result)))
                plugin._on_turn_start(session_id=session, turn_id="t", user_message="hello")
                plugin._on_tool(session_id=session, turn_id="t", tool_name="read_file", args={"path": "/tmp/a"}, result=result)
                plugin._on_turn_end(session_id=session, turn_id="t", assistant_response="it is " + value + " and laterreplymarker")
                plugin._on_session_end(session_id=session, turn_id="t", completed=True)
                body = bodies()
                self.assertNotIn(value, body)
                self.assertIn("laterreplymarker", body)

    def test_named_value_from_a_structured_body(self):
        value = "hunterhorsebattery"
        for args, result in (({"path": "/tmp/a"}, {"output": "password: " + value + "\nnext line"}),
                             ({"command": "mysql -p" + value}, "ok")):
            with self.subTest(args=args, result=result):
                session = "kohako-g2-structured-" + str(abs(hash(str(args))))
                plugin._on_turn_start(session_id=session, turn_id="t", user_message="hello")
                plugin._on_tool(session_id=session, turn_id="t", tool_name="terminal", args=args, result=result)
                plugin._on_turn_end(session_id=session, turn_id="t", assistant_response=value + " laterreplymarker")
                plugin._on_session_end(session_id=session, turn_id="t", completed=True)
                body = bodies()
                self.assertNotIn(value, body)
                self.assertIn("laterreplymarker", body)

    def test_surrogate_pair_value_is_masked_in_its_decoded_form(self):
        # A JSON body writes U+1F600 as a surrogate pair; the reply restates the joined character.
        session = "kohako-g2-surrogate"
        plugin._on_turn_start(session_id=session, turn_id="t", user_message="hello")
        plugin._on_tool(session_id=session, turn_id="t", tool_name="read_file", args={"path": "/tmp/a"},
                        result='password: "\\ud83d\\ude00hunterhorseloft"')
        plugin._on_turn_end(session_id=session, turn_id="t",
                            assistant_response="it is \U0001F600hunterhorseloft laterreplymarker")
        plugin._on_session_end(session_id=session, turn_id="t", completed=True)
        body = bodies()
        self.assertNotIn("hunterhorseloft", body)
        self.assertIn("laterreplymarker", body)
        self.assertEqual(plugin._join_surrogates("\ud83d"), "\ud83d")  # a lone surrogate stays

    def test_evicted_pins_are_rebuilt_from_resent_history(self):
        session = "kohako-g2-evicted"
        self.assertFalse(plugin._pin_body(session, None, "password: hunterhorseloft"))
        self.assertTrue(plugin._pin_pattern(session).search("hunterhorseloft"))
        with patch.object(plugin, "_MAX_SESSIONS", 4):
            for i in range(8):
                plugin._pin_body("kohako-g2-other-%d" % i, None, "password: othervalue%dzz" % i)
        self.assertNotIn(session, plugin._pins)
        self.assertIsNone(plugin._pin_pattern(session))
        plugin._pin_body(session, None, "password: hunterhorseloft")  # the same history, resent
        pattern = plugin._pin_pattern(session)
        self.assertTrue(pattern and pattern.search("hunterhorseloft"))

    def test_pins_leave_ids_and_keys_alone(self):
        spans = [{"traceId": "abcd1234", "spanId": "1234", "name": "x 1234",
                  "attributes": [{"key": "langfuse.observation.input", "value": {"stringValue": "1234 here"}}]}]
        masked = plugin._mask_pins(spans, plugin.re.compile("1234|input"))
        self.assertEqual(masked[0]["traceId"], "abcd1234")
        self.assertEqual(masked[0]["spanId"], "1234")
        self.assertEqual(masked[0]["attributes"][0]["key"], "langfuse.observation.input")
        self.assertNotIn("1234", json.dumps([masked[0]["name"], masked[0]["attributes"][0]["value"]]))

    def test_parent_pinned_value_masks_child(self):
        plugin._on_turn_start(session_id="kohako-g2-parent", turn_id="t", user_message="password: hunterhorsebattery")
        plugin._on_subagent_start(parent_session_id="kohako-g2-parent", child_session_id="kohako-g2-child")
        plugin._on_turn_start(session_id="kohako-g2-child", turn_id="t", parent_session_id="kohako-g2-parent", user_message="hunterhorsebattery childmarker")
        plugin._on_turn_end(session_id="kohako-g2-child", turn_id="t", assistant_response="hunterhorsebattery childmarker")
        plugin._on_session_end(session_id="kohako-g2-child", turn_id="t", completed=True)
        self.assertNotIn("hunterhorsebattery", bodies())

    def test_short_tool_value_is_pinned_as_a_word(self):
        # BOXP-428 TD-12 (production 2026-11-10): a 1-3 character named value withheld the whole session, and the
        # triggers were test logs (PASS: old) and config files. It is pinned as a whole word instead.
        for i, (result, value) in enumerate((("password: q7z", "q7z"), ("PASS: old\nPASS: pin", "old"),
                                             ('same_site_cookie_attribute = "lax"', "lax"), ('{"auth":38}', "38"),
                                             ("password: 5", "5"), ("<password>abc</password>", "abc"),
                                             # A printf directive or digits with a colon can be a real password.
                                             ("password: %d", "%d"), ("password: 1234:", "1234:"))):
            with self.subTest(result):
                session = "kohako-g2-short-%d" % i
                plugin._on_turn_start(session_id=session, turn_id="t", user_message="hello")
                plugin._on_tool(session_id=session, turn_id="t", tool_name="read_file", args={}, result=result)
                plugin._on_turn_end(session_id=session, turn_id="t", assistant_response="it is %s, laterbodymarker" % value)
                plugin._on_session_end(session_id=session, turn_id="t", completed=True)
                body = bodies()
                self.assertIn("laterbodymarker", body)
                self.assertIsNone(plugin._tainted(session))
                self.assertIsNone(re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % value, body.replace("\\n", " ")))

    def test_word_values_mask_whole_words_only(self):
        pattern = re.compile(plugin._WORD_BEFORE + "(?:" + plugin._value_pattern("q7z") + ")" + plugin._WORD_AFTER)
        for text in ("q7z", "x\\nq7z", "x\\u0020q7z", "a%20q7z", "(q7z)", '"q7z"'):
            self.assertNotIn("q7z", pattern.sub("[x]", text), text)
        for text in ("aq7zb", "q7z_x", "xq7z"):
            self.assertIn("q7z", pattern.sub("[x]", text), text)
        # Only ASCII letters, digits and _ join a word: in Japanese (or other non-ASCII) text the value stands alone
        # (lolice#1157 review: 秘密はq7zです).
        for text in ("秘密はq7zです", "値:q7z。", "éq7zé", "q7z１"):
            self.assertNotIn("q7z", pattern.sub("[x]", text), text)

    def test_short_value_in_japanese_reply_is_masked(self):
        session = "kohako-g2-short-ja"
        plugin._on_turn_start(session_id=session, turn_id="t", user_message="hello")
        plugin._on_tool(session_id=session, turn_id="t", tool_name="read_file", args={}, result="password: q7z")
        plugin._on_turn_end(session_id=session, turn_id="t", assistant_response="秘密はq7zです。jareplymarker")
        plugin._on_session_end(session_id=session, turn_id="t", completed=True)
        body = bodies()
        self.assertIn("jareplymarker", body)
        self.assertNotIn("q7z", body)

    def test_reference_shaped_values_pin_nothing(self):
        for text in ("password: ${{ secrets.DB_PASSWORD }}", "CLOUDFLARE_API_TOKEN: ${{ secrets.CF }}",
                     "key=${ANTHROPIC_API_KEY:-}", "runtime-authorization.yaml:4:  kind: Role",
                     "src/secret_store.py:12:  kind: Role", 'echo "${ANTHROPIC_API_KEY:-}"', "${PASSWORD:-}"):
            self.assertEqual(plugin._named_values(text), (set(), False), text)

    def test_template_like_values_are_pinned(self):
        # Regression (arch#14742 review): only a whole template expression is skipped. A value that merely starts like
        # a template is a password: it is pinned and a later restatement is masked; a quoted opener withholds.
        for text, value in (('password: "{{hunter123"', "{{hunter123"), ("password: '{%secretpw'", "{%secretpw"),
                            ('password: "${{hunter123"', "${{hunter123"), ('{"password": "{{hunter123"}', "{{hunter123")):
            values, _ = plugin._named_values(text)
            self.assertIn(value, values, text)
        for text in ('password: "{{"', "password: '${{'", 'password: "{%"'):
            self.assertTrue(plugin._named_values(text)[1], text)
        for text in ("password: ${{ secrets.X }}", "PASSWORD: {{ .Values.db.password }} # note"):
            values, unpinned = plugin._named_values(text)
            self.assertFalse(unpinned, text)
            self.assertFalse(any(re.match(r"\$?\{[{%]", v) for v in values), text)
        session = "kohako-g2-template"
        plugin._on_turn_start(session_id=session, turn_id="t", user_message="hello")
        plugin._on_tool(session_id=session, turn_id="t", tool_name="read_file", args={},
                        result='password: "{{hunter123"\nconfigline')
        plugin._on_turn_end(session_id=session, turn_id="t", assistant_response="it is {{hunter123 , laterbodymarker")
        plugin._on_session_end(session_id=session, turn_id="t", completed=True)
        body = bodies()
        self.assertIn("laterbodymarker", body)
        self.assertNotIn("hunter123", body)

    def test_unclosed_template_opener_is_the_value(self):
        # Regression (arch#14742 review): an unquoted opener with no closer on its line (password: {{) is not an
        # object or a template: it is a short symbol password and withholds, even when the same name later has a
        # closed template (password: {{ secrets.X }}). A closed template on its own still does not withhold.
        for text in ("password: {{", "password: {{\npassword: {{ secrets.X }}", "--password {{",
                     "password: ${{\nnote\npassword: ${{ secrets.X }}", "password: {%\npassword: {% x %}"):
            self.assertTrue(plugin._named_values(text)[1], text)
        for text in ("password: {{ secrets.X }}", "password: {}", "password: {a: hunterhorse}"):
            self.assertFalse(plugin._named_values(text)[1], text)
        self.assertIn("{{hunter123", plugin._named_values("password: {{hunter123")[0])
        session = "kohako-g2-open-template"
        plugin._on_turn_start(session_id=session, turn_id="t", user_message="hello")
        plugin._on_tool(session_id=session, turn_id="t", tool_name="read_file", args={},
                        result="password: {{\npassword: {{ secrets.X }}")
        plugin._on_turn_end(session_id=session, turn_id="t", assistant_response="it is {{ , laterbodymarker")
        plugin._on_session_end(session_id=session, turn_id="t", completed=True)
        self.assertIsNone(plugin._tainted(session))  # turn-scoped since TD-13: the rest of this turn is withheld
        self.assertNotIn("laterbodymarker", bodies())

    def test_unclosed_template_opener_on_same_line_as_closed_template(self):
        # Regression (arch#14742 review): a closer belongs to the nearest opener before it. An unclosed opener on the
        # same line as a later closed template (password: {{ note password: {{ secrets.X }}) is a short symbol
        # password and withholds; the restatement is never sent.
        for text in ("password: {{ note password: {{ secrets.X }}", "password: ${{ x password: ${{ secrets.X }}",
                     "password: {% a {% b %}", "token: {{ note {{ x }}"):
            self.assertTrue(plugin._named_values(text)[1], text)
        for text in ("password: ${{ secrets.X }} and token: ${{ secrets.Y }}",
                     "PASSWORD: {{ .Values.db.password }} # note"):
            self.assertFalse(plugin._named_values(text)[1], text)
        session = "kohako-g2-open-template-same-line"
        plugin._on_turn_start(session_id=session, turn_id="t", user_message="hello")
        plugin._on_tool(session_id=session, turn_id="t", tool_name="read_file", args={},
                        result="password: {{ note password: {{ secrets.X }}")
        plugin._on_turn_end(session_id=session, turn_id="t", assistant_response="it is {{ , laterbodymarker")
        plugin._on_session_end(session_id=session, turn_id="t", completed=True)
        self.assertIsNone(plugin._tainted(session))  # turn-scoped since TD-13
        self.assertNotIn("laterbodymarker", bodies())

    def test_short_symbol_only_values_withhold(self):
        # Regression (arch#14742 review): a value of symbols only can be a real password (password: ==). A short one
        # has no word boundary to pin it by, so it withholds the lineage; the restatement is never sent.
        for text in ("password: ==", 'password: "=="', "token=***", "password: -", "<password>==</password>",
                     "password: -_", "password: _-", "token=_=*", 'password: "-_"'):
            self.assertEqual(plugin._named_values(text), (set(), True), text)
        self.assertEqual(plugin._named_values("password: !@#$"), ({"!@#$"}, False))
        # Mixed underscores and symbols are still symbols only (lolice#1157 review): password: -_ withholds, so a
        # later a-_b restatement is never sent.
        self.assertEqual(plugin._named_values("password: -_=_"), ({"-_=_"}, False))
        session = "kohako-g2-symbols"
        plugin._on_turn_start(session_id=session, turn_id="t", user_message="hello")
        plugin._on_tool(session_id=session, turn_id="t", tool_name="read_file", args={}, result="password: ==\nconfigline")
        plugin._on_turn_end(session_id=session, turn_id="t", assistant_response="it is == , laterbodymarker")
        plugin._on_session_end(session_id=session, turn_id="t", completed=True)
        self.assertIsNone(plugin._tainted(session))  # turn-scoped since TD-13
        body = bodies()
        self.assertNotIn("laterbodymarker", body)
        self.assertNotIn("it is ==", body)
        session = "kohako-g2-symbols-mixed"
        plugin._on_turn_start(session_id=session, turn_id="t", user_message="hello")
        plugin._on_tool(session_id=session, turn_id="t", tool_name="read_file", args={}, result="password: -_\nconfigline")
        plugin._on_turn_end(session_id=session, turn_id="t", assistant_response="use a-_b , mixedlatermarker")
        plugin._on_session_end(session_id=session, turn_id="t", completed=True)
        self.assertIsNone(plugin._tainted(session))  # turn-scoped since TD-13
        body = bodies()
        self.assertNotIn("mixedlatermarker", body)
        self.assertNotIn("a-_b", body)

    def test_expansion_defaults_are_values(self):
        self.assertEqual(plugin._named_values('echo "${API_TOKEN:-hunterzz9}"'), ({"hunterzz9"}, False))
        self.assertEqual(plugin._named_values("x=${API_TOKEN:=ab}"), ({"ab"}, False))

    def test_provider_prefix_on_names_does_not_withhold(self):
        self.assertEqual(plugin._exposure("npm_config_cache=/home/u/.npm\nsk-inet ok"), set())
        self.assertEqual(plugin._exposure("key: ghp_" + "plainwordvalue"), {"provider_key"})

    def test_named_values_in_xml_elements(self):
        self.assertEqual(plugin._named_values("<password>quietmeadowstone</password>"), ({"quietmeadowstone"}, False))
        self.assertEqual(plugin._named_values('{"output":"\\u003cpassword\\u003equietmeadowstone\\u003c/password\\u003e"}'),
                         ({"quietmeadowstone"}, False))
        self.assertEqual(plugin._named_values("<secret><value>outerkeyvalue</value></secret>"), ({"outerkeyvalue"}, False))
        self.assertEqual(plugin._named_values("<password>abc</password>"), ({"abc"}, False))
        self.assertEqual(plugin._named_values("<password>${DB_PASSWORD}</password> <password/>"), ({"${DB_PASSWORD}"}, False))

    def test_named_values_in_entity_quotes(self):
        for text, value in (("password: &quot;hunterhorse&quot;", "hunterhorse"),
                            ("password: &#34;hunterhorse&#34;", "hunterhorse"),
                            ("password: &#x22;hunterhorse&#x22;", "hunterhorse"),
                            ("password=&apos;hunterhorse&apos;", "hunterhorse"),
                            ("password: \\u0022hunterhorse\\u0022", "hunterhorse"),
                            ("password: hunterhorse&quot;", "hunterhorse")):
            with self.subTest(text=text):
                found, unpinned = plugin._named_values(text)
                self.assertFalse(unpinned)
                # An unquoted ${VAR} stops at the brace (${VAR is pinned), which still covers ${VAR}.
                self.assertTrue(found and all(value.startswith(v) for v in found), found)

    def test_named_values_after_self_closing_elements(self):
        for text in ("<password/><password>hunterhorse</password>",
                     "<password /> text <password>hunterhorse</password>",
                     "&lt;password/&gt;&lt;password&gt;hunterhorse&lt;/password&gt;"):
            with self.subTest(text=text):
                self.assertEqual(plugin._named_values(text), ({"hunterhorse"}, False))
        self.assertEqual(plugin._named_values("<password/><password/>"), (set(), False))
        started = time.monotonic()
        self.assertEqual(plugin._named_values("<password/>" * 20000 + "<password>hunterhorse</password>"),
                         ({"hunterhorse"}, False))
        self.assertLess(time.monotonic() - started, 5)

    def test_entity_quoted_and_self_closing_values_are_masked_in_replies(self):
        for result in ("password: &quot;hunterhorseloft&quot;", "<password/><password>hunterhorseloft</password>"):
            with self.subTest(result=result):
                session = "kohako-g2-entity-" + str(abs(hash(result)))
                plugin._on_turn_start(session_id=session, turn_id="t", user_message="hello")
                plugin._on_tool(session_id=session, turn_id="t", tool_name="terminal", args={"command": "cat notes.txt"}, result=result)
                plugin._on_turn_end(session_id=session, turn_id="t", assistant_response="it is hunterhorseloft laterreplymarker")
                plugin._on_session_end(session_id=session, turn_id="t", completed=True)
                body = bodies()
                self.assertNotIn("hunterhorseloft", body)
                self.assertIn("laterreplymarker", body)

    def test_named_values_include_decoded_forms(self):
        self.assertEqual(plugin._named_values("password: &quot;hunter&amp;horse&quot;"), ({"hunter&amp;horse", "hunter&horse"}, False))
        self.assertEqual(plugin._named_values("password: &quot;hunter&#x26;horse&quot;"), ({"hunter&#x26;horse", "hunter&horse"}, False))
        self.assertEqual(plugin._named_values('{"password": "amber\\u0026pond"}'), ({"amber\\u0026pond", "amber&pond"}, False))
        self.assertEqual(plugin._named_values("password: &quot;hunter&#99999999;horse&quot;"), ({"hunter&#99999999;horse"}, False))

    def test_decoded_named_values_are_masked_in_replies(self):
        for result, value in (("password: &quot;hunter&amp;horsefield&quot;", "hunter&horsefield"),
                              ("<password>pine&lt;hollowgate</password>", "pine<hollowgate"),
                              ('{"password": "amber\\u0026willowpond"}', "amber&willowpond"),
                              ('password: "hunter\\"horsefield"', 'hunter"horsefield'),
                              ('{"output": "password: \\"maple\\\\\\"brookstone\\""}', 'maple"brookstone'),
                              ("password: \\mylakeviewpine", "\\mylakeviewpine"),
                              ('{"output": "password: \\\\coldbackslashpine\\nnext"}', "\\coldbackslashpine"),
                              ("password: amber\\slashgrove", "amber\\slashgrove")):
            with self.subTest(result=result):
                session = "kohako-g2-decoded-" + str(abs(hash(result)))
                plugin._on_turn_start(session_id=session, turn_id="t", user_message="hello")
                plugin._on_tool(session_id=session, turn_id="t", tool_name="terminal", args={"command": "cat notes.txt"}, result=result)
                plugin._on_turn_end(session_id=session, turn_id="t", assistant_response="it is " + value + " laterreplymarker")
                plugin._on_session_end(session_id=session, turn_id="t", completed=True)
                body = bodies()
                self.assertNotIn(value, body)
                self.assertNotIn(json.dumps(value)[1:-1], body)
                self.assertIn("laterreplymarker", body)

    def test_named_values_with_escaped_quotes(self):
        # An escaped quote inside a quoted value does not end it (the in-place mask's (?<!\\) rule).
        self.assertEqual(plugin._named_values('password: "hunter\\"horsefield"'), ({'hunter\\"horsefield', 'hunter"horsefield'}, False))
        self.assertEqual(plugin._named_values("password: 'it\\'s-quietpond'"), ({"it\\'s-quietpond", "it's-quietpond"}, False))
        self.assertEqual(plugin._named_values('password: "hunterhorse" next: "x"'), ({"hunterhorse"}, False))
        self.assertEqual(plugin._named_values('{"password": "hunterhorse", "note": "a\\"b"}'), ({"hunterhorse"}, False))

    def test_named_values_ending_in_escaped_backslash(self):
        # Regression (lolice#1107 review): a value ending in an escaped backslash ("hunterhorse\\") closes at the
        # quote after it, so the value and its decoded form (hunterhorse\) are pinned, not the rest of the text.
        self.assertEqual(plugin._named_values('password: "hunterhorse\\\\"'), ({"hunterhorse\\\\", "hunterhorse\\"}, False))
        self.assertEqual(plugin._named_values('password: "hunterhorse\\\\" next: "x"'), ({"hunterhorse\\\\", "hunterhorse\\"}, False))
        self.assertEqual(plugin._named_values("password: 'pond\\\\' note"), ({"pond\\\\", "pond\\"}, False))
        # One layer deeper (inside a JSON string): \\\\\\" is an escaped backslash, then the closing \".
        found, _ = plugin._named_values('{"output": "password: \\"maplebrook\\\\\\\\\\" next"}')
        self.assertIn("maplebrook\\", found)
        # An escaped quote still does not end the value.
        self.assertEqual(plugin._named_values('password: "hunter\\"horsefield"'), ({'hunter\\"horsefield', 'hunter"horsefield'}, False))
        # A list element ending in an escaped backslash is one element.
        self.assertEqual(plugin._named_values('password: ["hunterhorse\\\\", "meadowlake"]')[0] >= {"hunterhorse\\", "meadowlake"}, True)

    def test_value_ending_in_backslash_is_masked_in_replies(self):
        session = "kohako-g2-trailing-backslash"
        plugin._on_turn_start(session_id=session, turn_id="t", user_message="hello")
        plugin._on_tool(session_id=session, turn_id="t", tool_name="terminal", args={"command": "cat notes.txt"},
                        result='password: "hunterhorsefield\\\\"')
        plugin._on_turn_end(session_id=session, turn_id="t", assistant_response="it is hunterhorsefield\\ laterreplymarker")
        plugin._on_session_end(session_id=session, turn_id="t", completed=True)
        body = bodies()
        self.assertNotIn("hunterhorsefield", body)
        self.assertIn("laterreplymarker", body)

    def test_named_values_with_backslashes(self):
        # A backslash other than a quote/line escape is part of an unquoted value, including at its start.
        self.assertEqual(plugin._named_values("password: \\mysecret123"), ({"\\mysecret123"}, False))
        self.assertEqual(plugin._named_values('{"output": "password: \\\\coldpine\\nnext"}'), ({"\\\\coldpine", "\\coldpine"}, False))
        self.assertEqual(plugin._named_values("password: amber\\slash"), ({"amber\\slash"}, False))
        self.assertEqual(plugin._named_values('{"output": "password: hunterhorse\\nnext"}'), ({"hunterhorse"}, False))

    def test_named_values_in_quoted_cli_passwords(self):
        # Regression (arch#14548 review, same rule): a quoted CLI password with spaces is one shell word, so the
        # whole value is pinned and masked in place, not only the part before the space.
        cases = [("sshpass -p 'apple orchard' ssh host", "apple orchard"),
                 ('sshpass -p "pear grove stone" ssh host', "pear grove stone"),
                 ("mysql -h db -p'long meadow pass' app", "long meadow pass"),
                 ('mysql -h db -p"river bend" app', "river bend"),
                 ("curl -u admin:'quiet harbor' https://x", "quiet harbor"),
                 ("curl -u 'admin:cedar hollow' https://x", "cedar hollow"),
                 ('curl --user "admin:amber cove" https://x', "amber cove"),
                 ('{"command":"sshpass -p \\"birch lane\\" ssh host"}', "birch lane")]
        for text, value in cases:
            with self.subTest(text=text):
                self.assertIn(value, plugin._named_values(text)[0])
                self.assertNotIn(value.split()[-1], plugin._redact(text)[0])
        self.assertEqual(plugin._named_values("mysql -p && ls -p 'some dir'"), (set(), False))
        self.assertEqual(plugin._named_values("date -u +%H:%M")[0], set())
        # Regression (lolice#1157 review): only the date command takes a date format after -u. A curl -u value that
        # looks like a date format (+%H:%M) is user:password and its password is pinned.
        for text in ("date -u +%FT%TZ", "TZ=UTC date -u '+%H:%M'", "echo $(date -u +%H:%M)", "/bin/date -u +%T"):
            with self.subTest(text=text):
                self.assertEqual(plugin._named_values(text)[0], set())
        for text in ("curl -u +%H:%M https://example.com", "date; curl -u +%H:%M https://x",
                     "curl --user +%H:%M https://x"):
            with self.subTest(text=text):
                self.assertIn("%M", plugin._named_values(text)[0])

    def test_named_values_in_backslash_escaped_cli_passwords(self):
        # Regression (lolice#1107 review): a shell word may escape its spaces (sshpass -p apple\\ orchard). The whole
        # word is masked in place, and both its written form and the shell-decoded password are pinned, so neither the
        # rest of the word nor a restatement of the decoded password is sent.
        cases = [("sshpass -p apple\\ orchard ssh host", "apple orchard"),
                 ("mysql -h db -priver\\ bend\\ stone app", "river bend stone"),
                 ("curl -u admin:quiet\\ harbor https://x", "quiet harbor"),
                 ("tool --password cedar\\ hollow run", "cedar hollow"),
                 ('{"command":"sshpass -p birch\\\\ lane ssh host"}', "birch lane")]
        for text, value in cases:
            with self.subTest(text=text):
                pins = plugin._named_values(text)[0]
                self.assertIn(value, pins)
                self.assertNotIn(value.split()[-1], plugin._redact(text)[0])
                pattern = plugin.re.compile("|".join(map(plugin._value_pattern, sorted(pins, key=len, reverse=True))))
                self.assertNotIn(value.split()[-1], pattern.sub("[REDACTED:pinned]", "restated: " + value))
        self.assertEqual(plugin._named_values("sshpass -p plainpass ssh host")[0], {"plainpass"})

    def test_named_values_in_unclosed_quoted_cli_passwords(self):
        # Regression (arch#14548 review, same rule): a quote that never closes runs to the end of the line, so
        # `curl -u 'admin:apple orchard` masks and pins the password.
        cases = [("curl -u 'admin:apple orchard", "apple orchard"),
                 ('curl --user "admin:pear grove', "pear grove"),
                 ("curl -u admin:'quiet harbor", "quiet harbor"),
                 ("sshpass -p 'birch lane", "birch lane"),
                 ("mysql -h db -p'river bend", "river bend")]
        for text, value in cases:
            with self.subTest(text=text):
                self.assertIn(value, plugin._named_values(text)[0])
                self.assertNotIn(value.split()[-1], plugin._redact(text)[0])

    def test_named_values_in_list_and_object_values(self):
        # Regression (arch#14548 review, same rule): a list or object value (password: [a, b], token: {primary: a})
        # is masked in place as a whole, so every element is pinned too, not only the text up to the first delimiter.
        cases = [("password: [hunterhorse, meadowlake]", ["hunterhorse", "meadowlake"]),
                 ("password = [\"hunterhorse\", 'meadowlake']", ["hunterhorse", "meadowlake"]),
                 ('{"api_key": ["hunterhorse", "meadowlake"]}', ["hunterhorse", "meadowlake"]),
                 ("secret: {primary: hunterhorse, backup: meadowlake}", ["hunterhorse", "meadowlake"]),
                 ('{\\"token\\": [\\"hunterhorse\\", \\"mead,owlake\\"]}', ["hunterhorse", "mead,owlake"]),
                 ("password: [[hunterhorse], (meadowlake)]", ["hunterhorse", "meadowlake"]),
                 ("password: [hunterhorse, meadowlake", ["hunterhorse", "meadowlake"]),
                 # The flag form (--password [a, b]) is masked in place as a whole too.
                 ("app --password [hunterhorse, meadowlake]", ["hunterhorse", "meadowlake"]),
                 ("app --api-key {primary: hunterhorse, backup: meadowlake} --verbose", ["hunterhorse", "meadowlake"]),
                 ("app --token\t[\"hunterhorse\", 'meadowlake']", ["hunterhorse", "meadowlake"]),
                 ("app --password [hunterhorse, meadowlake", ["hunterhorse", "meadowlake"])]
        for text, values in cases:
            with self.subTest(text=text):
                found = plugin._named_values(text)[0]
                for value in values:
                    self.assertIn(value, found)
                    self.assertNotIn(value, plugin._redact(text)[0])
                self.assertFalse(any(v.startswith("[") for v in found))
        found = plugin._named_values("secret: {primary: hunterhorse, backup: meadowlake}")[0]
        self.assertFalse({"primary", "backup"} & found)
        self.assertEqual(plugin._named_values("password: [hunterhorse, ab]"), ({"hunterhorse", "ab"}, False))
        # Regression (lolice#1107 review): an object nested in a list ([{primary:a}]) with no space after the
        # colon pins the value itself, not only key:value as one token.
        for text in ["password: [{primary:hunterhorse}]", "password: [{primary=hunterhorse}, {backup:meadowlake}]",
                     "app --token [{primary:hunterhorse}]", "password: [({primary:hunterhorse})]"]:
            with self.subTest(text=text):
                found = plugin._named_values(text)[0]
                self.assertIn("hunterhorse", found)
                self.assertNotIn("primary", found)
                self.assertNotIn("hunterhorse", plugin._redact(text)[0])
        self.assertIn("meadowlake", plugin._named_values("password: [{primary=hunterhorse}, {backup:meadowlake}]")[0])

    def test_named_values_in_env_entry_list_and_object_values(self):
        # Regression (arch#14548 review, same rule): an env entry whose value is a list or object
        # ({"name":"API_SECRET","value":["a","b"]}, either key order) is masked in place as a whole, so every element is
        # pinned too, not only the text up to the first delimiter.
        cases = ['{"name":"API_SECRET","value":["hunterhorse","meadowlake"]}',
                 '{"name": "DB_PASSWORD", "value": [hunterhorse, meadowlake]}',
                 '{"name":"API_TOKEN","value":{"primary":"hunterhorse","backup":"meadowlake"}}',
                 '{\\"name\\":\\"API_SECRET\\",\\"value\\":[\\"hunterhorse\\",\\"meadowlake\\"]}',
                 '{&quot;name&quot;:&quot;API_SECRET&quot;,&quot;value&quot;:[&quot;hunterhorse&quot;,&quot;meadowlake&quot;]}',
                 '{"name":"API_SECRET","value":["hunterhorse","meadowlake"',
                 '{"value":["hunterhorse","meadowlake"],"name":"API_SECRET"}',
                 '{"value": {"primary": "hunterhorse", "backup": "meadowlake"}, "name": "DB_PASSWORD"}']
        for text in cases:
            with self.subTest(text=text):
                found = plugin._named_values(text)[0]
                for value in ["hunterhorse", "meadowlake"]:
                    self.assertIn(value, found)
                    self.assertNotIn(value, plugin._redact(text)[0])
                self.assertFalse(any(v.startswith("[") for v in found))
                self.assertFalse({"primary", "backup"} & found)
        self.assertFalse(plugin._named_values('{"name":"API_SECRET","value":["hunterhorse","ab"]}')[1])
        self.assertIn("ab", plugin._named_values('{"value":["hunterhorse","ab"],"name":"API_SECRET"}')[0])
        self.assertFalse(plugin._named_values('{"name":"PATH_LIST","value":["alphabeta","gammadelta"]}')[0])

    def test_named_values_in_uppercase_cli_flags(self):
        # Regression (arch#14548 review, same rule): CLI password flags are matched case-insensitively for both
        # masking and pinning, so `curl --USER admin:x` pins x and a later bare restatement is masked.
        cases = [("curl --USER admin:secretvalue https://x", "secretvalue"),
                 ("curl -U admin:lanternfield https://x", "lanternfield"),
                 ("curl --User 'admin:copper ridge' https://x", "copper ridge"),
                 ("SSHPASS -P marblestone ssh host", "marblestone"),
                 ("MYSQL -h db -Pquartzvalley app", "quartzvalley")]
        for text, value in cases:
            with self.subTest(text=text):
                self.assertIn(value, plugin._named_values(text)[0])
                self.assertNotIn(value.split()[-1], plugin._redact(text)[0])

    def test_named_values_in_encoded_env_entries(self):
        # Regression (arch#14548 review, same rule): the env entry value key takes every serialized quote form
        # (&quot;, &#34;, &#x22;, &apos;, \\u0022, \\"), so the value of an HTML/JSON-encoded entry is pinned.
        cases = ['&quot;name&quot;: &quot;API_SECRET&quot;, &quot;value&quot;: &quot;quietmeadowstone&quot;',
                 '&#34;name&#34;: &#34;API_SECRET&#34;, &#34;value&#34;: &#34;quietmeadowstone&#34;',
                 '&#x22;value&#x22;: &#x22;quietmeadowstone&#x22;, &#x22;name&#x22;: &#x22;API_SECRET&#x22;',
                 '{\\u0022name\\u0022: \\u0022API_SECRET\\u0022, \\u0022value\\u0022: \\u0022quietmeadowstone\\u0022}',
                 '{\\"name\\": \\"API_SECRET\\", \\"value\\": \\"quietmeadowstone\\"}',
                 "&apos;name&apos;: &apos;API_SECRET&apos;, &apos;value&apos;: &apos;quietmeadowstone&apos;"]
        for text in cases:
            with self.subTest(text=text):
                self.assertIn("quietmeadowstone", plugin._named_values(text)[0])
                self.assertNotIn("quietmeadowstone", plugin._redact(text)[0])

    def test_named_values_skip_placeholders_and_settings(self):
        for text in ("password: [REDACTED:assignment]", "AUTH_ENABLED=false", "password: $PW"):
            with self.subTest(text=text):
                self.assertEqual(plugin._named_values(text), (set(), False))

    def test_dollar_values_are_pinned(self):
        # Regression (lolice#1107 review): a real password starting with $ is not a variable reference to skip.
        # $VAR / ${VAR} forms are pinned (masking a later ${VAR} is a harmless over-mask); a short one does not
        # withhold, since it is most likely a reference.
        for text, value in (("password: $hunterhorse", "$hunterhorse"), ("password: ${VAR}", "${VAR}"),
                            ("--token $TOKEN", "$TOKEN"), ('{"api_key": "$quietmeadow"}', "$quietmeadow"),
                            ("<password>${DB_PASSWORD}</password>", "${DB_PASSWORD}")):
            with self.subTest(text=text):
                found, unpinned = plugin._named_values(text)
                self.assertFalse(unpinned)
                # An unquoted ${VAR} stops at the brace (${VAR is pinned), which still covers ${VAR}.
                self.assertTrue(found and all(value.startswith(v) for v in found), found)

    def test_long_dollar_value_withholds(self):
        # Regression (lolice#1107 review): a $-prefixed value past the pin length limit is not exempt as a
        # variable reference; it cannot be pinned, so the field reports unpinned (withheld) like any long value.
        for text in ("password: $" + "a" * 300, "password: ${" + "b" * 300 + "}"):
            with self.subTest(text=text[:20]):
                self.assertTrue(plugin._named_values(text)[1])
        self.assertEqual(plugin._named_values("password: $PW"), (set(), False))

    def test_dollar_value_restatement_is_masked(self):
        value = "$hunterhorse"
        for result in ("password: " + value, "password: '" + value + "'", "--password " + value):
            with self.subTest(result=result):
                session = "kohako-g2-dollar-" + str(abs(hash(result)))
                plugin._on_turn_start(session_id=session, turn_id="t", user_message="hello")
                plugin._on_tool(session_id=session, turn_id="t", tool_name="read_file", args={"path": "/tmp/a"}, result=result)
                plugin._on_turn_end(session_id=session, turn_id="t", assistant_response="it is " + value + " and laterreplymarker")
                plugin._on_session_end(session_id=session, turn_id="t", completed=True)
                body = bodies()
                self.assertNotIn("hunterhorse", body)
                self.assertIn("laterreplymarker", body)
                self.assertIsNone(plugin._tainted(session))

    def test_pinned_escapes_and_guard(self):
        session = "kohako-g2-encoded"
        value = "hunterhorsebattery"
        plugin._on_turn_start(session_id=session, turn_id="t", user_message="password: " + value)
        encoded = "%68unterhorsebattery " + "&#104;unterhorsebattery " + "\\u0068unterhorsebattery"
        plugin._on_turn_end(session_id=session, turn_id="t", assistant_response=encoded + " keptmarker")
        plugin._on_session_end(session_id=session, turn_id="t", completed=True)
        body = bodies()
        self.assertIn("keptmarker", body)
        for marker in ("%68unterhorsebattery", "&#104;unterhorsebattery", "u0068unterhorsebattery"):
            self.assertNotIn(marker, body)
        self.assertEqual(list(plugin.guarded_payloads([{"name": value}], plugin._pin_pattern(session))), [])

    def test_pinned_limit_withholds(self):
        session = "kohako-g2-limit"
        plugin._on_turn_start(session_id=session, turn_id="t", user_message="hello")
        for i in range(257):
            plugin._pin_body(session, None, "password: uniquevalue%03d" % i)
        self.assertEqual(plugin._tainted(session), "pinned_values_over_limit")

    def test_hook_callbacks_declare_no_kwargs_and_no_history(self):
        ctx = Ctx()
        with patch.object(plugin, "_start_worker"):
            plugin.register(ctx)
        self.assertNotIn("pre_transcription", ctx.hooks)
        for cb in ctx.hooks.values():
            params = inspect.signature(cb).parameters
            self.assertFalse(any(p.kind == p.VAR_KEYWORD for p in params.values()))
            self.assertNotIn("conversation_history", params)

    def test_hooks_idle_without_key_and_start_when_key_appears(self):
        with tempfile.TemporaryDirectory() as key_dir, patch.object(plugin, "_KEY_DIR", key_dir), \
                patch.object(plugin, "_start_worker") as start:
            ctx = Ctx()
            plugin.register(ctx)
            start.assert_called_once()
            self.assertIsNone(plugin._creds)
            run_turn(args={"path": "/a/.env"}, result="V")  # state is tracked, nothing is buffered or sent
            self.assertEqual(plugin._turns, {})
            self.assertEqual(drain(), [])
            self.assertEqual(plugin.stats().get("turns_skipped_no_key"), 1)
            self.assertEqual(plugin._tainted("kohako-g2-abc"), "known_source")
            Path(key_dir, "LANGFUSE_PUBLIC_KEY").write_text("pk-lf-later\n")
            self.assertIsNone(plugin._refresh_credentials())  # half-written Secret stays idle
            Path(key_dir, "LANGFUSE_SECRET_KEY").write_text("sk-lf-later\n")
            self.assertEqual(plugin._refresh_credentials(), ("pk-lf-later", "sk-lf-later"))
            run_turn(turn="t2", body="plainwordvalue")  # what the key-less period saw still withholds
            body = bodies()
            self.assertNotIn("plainwordvalue", body)
            self.assertIn("withheld_after_known_source", body)
            run_turn(session="kohako-g2-fresh")
            self.assertEqual(len(drain()), 1)

    def test_worker_picks_up_key_while_running(self):
        with tempfile.TemporaryDirectory() as key_dir, patch.object(plugin, "_KEY_DIR", key_dir), \
                patch.object(plugin, "_KEY_POLL_SECONDS", 0.01), patch.object(plugin, "_start_worker"):
            plugin.register(Ctx())
            threading.Thread(target=plugin._work, daemon=True).start()
            Path(key_dir, "LANGFUSE_PUBLIC_KEY").write_text("pk-lf-later")
            Path(key_dir, "LANGFUSE_SECRET_KEY").write_text("sk-lf-later")
            deadline = time.time() + 2
            while plugin._creds is None and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(plugin._creds, ("pk-lf-later", "sk-lf-later"))

    def test_redacts_every_synthetic_secret(self):
        for name, secret in SECRETS.items():
            with self.subTest(name):
                out = plugin.redact("before " + secret + " after")
                for marker in MARKERS:
                    self.assertNotIn(marker, out)

    def test_assignment_inside_nested_serialization_is_masked(self):
        value = "plainwordvalue"
        for name, text in (
            ("json in json", json.dumps({"output": json.dumps({"password": value})})),
            ("compact json in json", '{"output":"{\\"password\\":\\"' + value + '\\"}"}'),
            ("three levels", json.dumps({"a": json.dumps({"b": json.dumps({"api_key": value})})})),
            ("pretty printed", json.dumps({"output": json.dumps({"token": value}, indent=2)})),
            ("spaces in value", json.dumps({"output": json.dumps({"secret": "x " + value})})),
            ("quote in value", json.dumps({"output": json.dumps({"password": 'x" ' + value})})),
            ("quote in plain json", json.dumps({"password": 'x" ' + value})),
            ("unicode escaped quotes", '{\\u0022password\\u0022:\\u0022x ' + value + '\\u0022}'),
            ("html quotes", '{&quot;password&quot;:&quot;x ' + value + '&quot;}'),
            ("cut off value", '{\\"password\\":\\"x ' + value),
        ):
            with self.subTest(name):
                self.assertNotIn(value, plugin.redact(text))

    def test_secret_forms_without_a_plain_assignment_are_masked(self):
        value = "plainwordvalue"
        for name, text in (
            ("cli flag", "mytool --password " + value + " run"),
            ("cli flag with quotes", 'mytool --api-key "x ' + value + '" run'),
            ("cli flag in json", json.dumps({"cmd": "mytool --token " + value})),
            ("curl user", "curl -u admin:" + value + " https://x"),
            ("curl long user", "curl --user admin:" + value + " https://x"),
            ("mysql", "mysql -h db -p" + value + " app"),
            ("sshpass", "sshpass -p " + value + " ssh host"),
            ("quoted authorization", json.dumps({"authorization": value})),
            ("docker auth", json.dumps({"auths": {"r": {"auth": value}}})),
            ("short pass", "DB_PASS=" + value),
            ("quoted pass", json.dumps({"pass": value})),
            ("passphrase", "passphrase: " + value),
            ("env entry yaml", "- name: DB_PASSWORD\n  value: " + value),
            ("env entry json", json.dumps({"name": "DB_PASSWORD", "value": value})),
            ("env entry reversed", json.dumps({"value": value, "name": "DB_PASSWORD"})),
            ("env entry nested", json.dumps({"o": json.dumps({"name": "API_TOKEN", "value": value})})),
            ("block scalar", "password: |\n  x\n  " + value + "\n"),
            ("block scalar nested", json.dumps({"o": "token: >-\n  " + value + "\n"})),
            ("unicode escaped separator", "password\\u003d" + value),
        ):
            with self.subTest(name):
                self.assertNotIn(value, plugin.redact(text))

    def test_every_element_of_a_structured_value_is_masked(self):
        first, second = "firstplainvalue", "secondplainvalue"
        for name, text in (
            ("json array", json.dumps({"password": [first, second]})),
            ("json array compact", '{"password":["' + first + '","' + second + '"]}'),
            ("json object", json.dumps({"credentials": {"user": first, "pw": second}})),
            ("deep", json.dumps({"auth": [[first], {"x": [second]}]})),
            ("bracket inside element", json.dumps({"password": ["a]" + first, "b}" + second]})),
            ("backslash at element end", json.dumps({"password": ["a\\", "]" + first, second]})),
            ("pretty", json.dumps({"password": [first, second]}, indent=2)),
            ("nested json", json.dumps({"output": json.dumps({"password": [first, second]})})),
            ("nested twice", json.dumps({"o": json.dumps({"o": json.dumps({"token": [first, second]})})})),
            ("python repr", str({"password": [first, second]})),
            ("python tuple", "password = ('" + first + "', '" + second + "')"),
            ("shell array", "PASSWORDS=(" + first + " " + second + ")"),
            ("yaml flow", "password: [" + first + ", " + second + "]"),
            ("yaml flow map", "auth: {user: " + first + ", pw: " + second + "}"),
            ("yaml list", "password:\n  - " + first + "\n  - " + second + "\n"),
            ("yaml list same indent", "password:\n- " + first + "\n- " + second + "\n"),
            ("yaml map", "  credentials:\n    user: " + first + "\n    pw: " + second + "\n"),
            ("yaml nested in json", json.dumps({"o": "password:\n  - " + first + "\n  - " + second + "\n"})),
            ("unclosed", '{"password":["' + first + '","' + second),
            ("entry array", json.dumps({"name": "DB_PASSWORD", "value": [first, second]})),
            ("entry array reversed", json.dumps({"value": [first, second], "name": "DB_PASSWORD"})),
            ("entry yaml list", "- name: DB_PASSWORD\n  value:\n    - " + first + "\n    - " + second + "\n"),
            ("flag array", "mytool --password [" + first + "," + second + "]"),
        ):
            with self.subTest(name):
                out = plugin.redact(text)
                self.assertNotIn(first, out)
                self.assertNotIn(second, out)

    def test_structured_masking_stops_at_the_end_of_the_value(self):
        for text in (json.dumps({"password": ["plainwordvalue", "]"], "name": "keptvalue"}),
                     json.dumps({"output": json.dumps({"password": ["plainwordvalue"], "name": "keptvalue"})}),
                     json.dumps({"auth": {"a": {"b": "plainwordvalue"}}, "name": "keptvalue"}),
                     "password:\n  - plainwordvalue\nname: keptvalue\n",
                     "a:\n  password:\n  - plainwordvalue\n  name: keptvalue\n"):
            with self.subTest(text):
                out = plugin.redact(text)
                self.assertNotIn("plainwordvalue", out)
                self.assertIn("keptvalue", out)
        self.assertNotIn("plainwordvalue", plugin.redact(json.dumps({"value": ["plainwordvalue"], "name": "DB_PASSWORD", "other": "x"})))

    def test_ordinary_arrays_and_blocks_are_kept(self):
        for text in (json.dumps({"items": ["keptvalue", "keptother"]}), "names:\n  - keptvalue\n",
                     json.dumps({"name": "DB_HOST", "value": ["keptvalue"]}),
                     json.dumps({"value": ["keptvalue"], "name": "DB_HOST"})):
            with self.subTest(text):
                self.assertEqual(plugin.redact(text), text)

    def test_structured_masking_stays_fast_on_large_input(self):
        for text in ('{"password":[' + '"a",' * 200000, "password:\n" + "  - a\n" * 200000, '{"password":[' * 50000):
            started = time.time()
            plugin.redact(text)
            self.assertLess(time.time() - started, 5)

    def test_structured_secret_in_tool_result_never_reaches_payload(self):
        run_turn(result={"output": '{"password":["first","secondsecretvalue"]}'})
        self.assertNotIn("secondsecretvalue", bodies())

    def test_ordinary_words_near_secret_forms_are_kept(self):
        for text in ("bypass=keptvalue", "compass: keptvalue", "tests passed: keptvalue", "author: keptvalue",
                     '{"name": "DB_HOST", "value": "keptvalue"}', "mytool --name keptvalue", "mysql -h keptvalue",
                     "curl -u keptvalue https://x", "grep -p keptvalue"):
            with self.subTest(text):
                self.assertEqual(plugin.redact(text), text)

    def test_masked_flag_does_not_trip_the_final_guard(self):
        run_turn(tool="delegate_task", result="mytool --password plainwordvalue\nnext\ncleanbodymarker")
        self.assertIn("cleanbodymarker", bodies())

    def test_nested_masking_stops_at_the_end_of_the_value(self):
        out = plugin.redact(json.dumps({"output": json.dumps({"password": "plainwordvalue", "name": "keptvalue"})}))
        self.assertNotIn("plainwordvalue", out)
        text = json.dumps({"output": json.dumps({"name": "keptvalue"})})
        self.assertEqual(plugin.redact(text), text)

    def test_nested_secret_in_tool_result_never_reaches_payload(self):
        for result in ({"output": '{"password":"plainwordvalue"}'}, '{"output":"{\\"password\\":\\"plainwordvalue\\"}"}'):
            with self.subTest(result):
                run_turn(result=result)
                self.assertNotIn("plainwordvalue", bodies())

    def test_keeps_ordinary_text_and_git_sha(self):
        text = "git rev 0123456789abcdef0123456789abcdef01234567 path /a/b/c.txt 日本語"
        self.assertEqual(plugin.redact(text), text)

    def test_secrets_never_reach_payload_from_any_field(self):
        corpus = " ".join(SECRETS.values())
        with capture_turns() as turns:
            run_turn(body=corpus, args={"command": "echo " + corpus}, result=corpus)
        turn = turns[0]
        turn["exit_reason"] = corpus
        turn["spans"][0]["name"] = "name " + SECRETS["github"]
        turn["spans"][1]["metadata"]["provider"] = "https://h/?token=" + SECRETS["langfuse"]
        batches = list(plugin.guarded_payloads(plugin.build_spans(turn)))
        self.assertTrue(batches)
        for marker in MARKERS:
            self.assertNotIn(marker, "".join(batches) + bodies())

    def test_sensitive_tool_output_is_withheld(self):
        for args in ({"path": "/home/x/.codex/auth.json"}, {"command": "kubectl get secret foo -o yaml"}, {"command": "env"},
                     {"command": "cat /proc/1/environ"}, {"path": "/a/.ssh/id_rsa"}):
            with self.subTest(args):
                run_turn(args=args, result="UNIQUEOUTPUT")
                body = bodies()
                self.assertNotIn("UNIQUEOUTPUT", body)
                self.assertIn("withheld", body)

    def test_sensitive_commands_are_found_wherever_the_flags_are(self):
        for command in ("kubectl -n foo get secret bar -o jsonpath='{.data.x}'", "kubectl --context a get secrets/bar",
                        "kubectl get -n foo secret bar", "kubectl get pods,secrets -A -o yaml", "oc -n foo extract secret/bar",
                        "aws --region ap-northeast-1 ssm get-parameter --name x --with-decryption",
                        "aws secretsmanager get-secret-value --secret-id x", "printenv FOO", "env > /tmp/x", "export -p",
                        "helm get values foo", "python3 -c 'import os; print(os.environ)'"):
            with self.subTest(command):
                plugin._sessions.clear()
                run_turn(args={"command": command}, result="UNIQUEOUTPUT")
                body = self._bodies()
                self.assertNotIn("UNIQUEOUTPUT", body)
                self.assertIn("withheld", body)

    def test_known_source_table_additions_are_withheld(self):
        # The K0 table of THREAT_MODEL.md: credential files and credential-printing commands, fixed list.
        for args in ({"path": "/home/x/.kube/config"}, {"path": "/home/x/.npmrc"}, {"path": "/home/x/.pypirc"}, {"path": "/home/x/.pgpass"},
                     {"path": "/home/x/.docker/config.json"}, {"path": "/home/x/.config/gh/hosts.yml"}, {"path": "/home/x/.config/gcloud/credentials.db"},
                     {"path": "/home/x/.azure/msal_token_cache.json"}, {"path": "/home/x/.cargo/credentials.toml"}, {"path": "/home/x/.vault-token"},
                     {"path": "/etc/app/private.key"}, {"path": "/etc/ssl/server.pem"}, {"path": "/home/x/keystore.p12"}, {"path": "/home/x/.ssh/id_ecdsa_sk"},
                     {"path": "/home/x/.gnupg/secring.gpg"}, {"path": "/var/lib/app/tokens.json"}, {"path": "/var/lib/app/refresh_token.txt"},
                     {"path": "/tmp/tokens"}, {"path": "/var/lib/app/tokens/current"},
                     # id_*: any SSH-style key name, not only the four algorithms spelled out before
                     {"path": "/backup/id_xmss"}, {"command": "cat /tmp/id_ed448_sk"}, {"command": "cat id_rsa"}, {"path": "/home/x/keys/id_falcon-sk"},
                     # token files named without an extension or a directory, in a reading position
                     {"command": "cat tokens"}, {"path": "tokens"}, {"path": "./tokens"}, {"command": "cat ./tokens"}, {"command": "tail -n1 tokens"},
                     {"command": "sed -n 1p mytoken"}, {"command": "cat access_tokens_backup"}, {"command": "wc -l < tokens"},
                     {"command": "bash -c 'cat tokens'"}, {"command": "git show HEAD:tokens"}, {"command": "sudo cat tokens"},
                     {"command": "cat ~/.kube/config"}, {"command": "gcloud auth print-access-token"}, {"command": "gcloud auth application-default print-access-token"},
                     {"command": "az account get-access-token --resource x"}, {"command": "aws sts get-session-token"}, {"command": "aws ecr get-login-password --region x"},
                     {"command": "kubectl -n foo create token sa-x"}, {"command": "oc whoami -t"}, {"command": "vault read secret/x"}, {"command": "vault kv get kv/x"},
                     {"command": "op read op://vault/item/field"}, {"command": "docker-credential-gcr get"}, {"command": "env -u HOME"}):
            with self.subTest(args):
                plugin._sessions.clear()
                run_turn(args=args, result="UNIQUEOUTPUT")
                body = self._bodies()
                self.assertNotIn("UNIQUEOUTPUT", body)
                self.assertIn("withheld_known_source", body)

    def test_words_that_look_like_the_table_are_not_withheld(self):
        for args in ({"path": "/a/keyboard.md"}, {"path": "/a/api-keys.md"}, {"path": "/a/keymap.py"}, {"path": "/a/package.json"}, {"path": "/a/.bashrc"},
                     {"path": "/a/kube-config.md"}, {"path": "/a/.kubeignore"}, {"path": "/a/user_id_map.json"},
                     {"command": "grep -rn id_12345 src"}, {"query": "the session_id_format"}, {"command": "ls ~/.config"}, {"command": "gcloud auth list"},
                     {"command": "az account show"}, {"command": "aws sts get-caller-identity"}, {"command": "oc whoami"}, {"command": "vault status"},
                     {"command": "kubectl get pods -n foo"}, {"command": "git commit -m 'count tokens per request'"},
                     {"command": "codex exec --max-output-tokens 5 x"}, {"model_settings": {"max_output_tokens": 5}}, {"query": "the token refresh flow"},
                     # the word token in a mention, not a read: listing, searching, naming, deleting
                     {"command": "grep -rn tokens src"}, {"command": "rg -w token src/"}, {"command": "ls tokens"}, {"command": "echo tokens"},
                     {"command": "rm -f tokens"}, {"command": "git add tokens"}, {"command": "git log --oneline -- tokens"}, {"command": "find . -name tokens"},
                     {"command": "python3 -c 'print(1)' --tokens 5"}):
            with self.subTest(args):
                plugin._sessions.clear()
                run_turn(args=args, result="UNIQUEOUTPUT")
                body = self._bodies()
                self.assertIn("UNIQUEOUTPUT", body)
                self.assertNotIn("[withheld", body)

    def test_queue_only_holds_masked_batches(self):
        # Hooks keep raw bodies in the turn buffer; on_session_end masks them, so no raw value sits in the queue.
        run_turn(body="the value is " + SECRETS["github"], args={"command": "cat x"}, result="api_key=" + "plainword" + "value2\nnext")
        for item in drain():
            self.assertIsInstance(item, list)
            for batch in item:
                self.assertIsInstance(batch, str)
                self.assertNotIn("a1B2a1B2", batch)
                self.assertNotIn("plainwordvalue2", batch)
        self.assertEqual(plugin.stats().get("hook_errors"), None)

    def test_ordinary_commands_are_not_withheld(self):
        for command in ("kubectl get pods -n foo", "git status", "ls -la", "aws s3 ls", "environment-check", "reset --hard"):
            with self.subTest(command):
                plugin._sessions.clear()
                run_turn(args={"command": command}, result="UNIQUEOUTPUT")
                self.assertIn("UNIQUEOUTPUT", self._bodies())  # not withheld

    def test_value_is_masked_in_every_serialized_form(self):
        values = {"A_TOKEN": 'ab"1', "B_TOKEN": "q'z", "C_TOKEN": "l1\nl2", "D_TOKEN": "b\\s", "E_TOKEN": "é7",
                  "F_TOKEN": "😀k", "G_TOKEN": "s p/c", "H_TOKEN": "t\tu&v<w>", "LONG_PASSWORD": 'long"value\nwith\\all'}
        def nested(value, depth, ascii_only=False):
            for _ in range(depth):
                value = json.dumps(value, ensure_ascii=ascii_only)[1:-1]
            return value
        with patch.dict(os.environ, values):
            plugin._secret_values = None
            for value in values.values():
                forms = [value, repr(value)[1:-1], html.escape(value), urllib.parse.quote(value, safe=""),
                         urllib.parse.quote_plus(value), value.replace("/", "\\/")]
                forms += [nested(value, depth, ascii_only) for depth in (1, 2, 3, 4) for ascii_only in (False, True)]
                for form in forms:
                    out = plugin.redact("answer " + form + " end")
                    self.assertEqual("answer [REDACTED:env] end", out, form)
                    self.assertTrue(plugin._unsafe("answer " + form + " end"), form)
                # What the review found: a structure goes through JSON before it reaches the mask.
                for structure in ({"k": [value]}, {"k": {"n": value + "x"}}):
                    for fragment in (nested(value, 1), value):
                        self.assertNotIn(fragment, plugin._field(structure))
                    self.assertIn("[REDACTED:env]", plugin._field(structure))
                    # The guard alone stops it even when the field pass is skipped.
                    self.assertTrue(plugin._unsafe(json.dumps(structure)))
                    self.assertTrue(plugin._unsafe(json.dumps(structure, ensure_ascii=False)))
        # A value read from a file keeps its newline; the text shows it without.
        with patch.dict(os.environ, {"FILE_TOKEN": "zq7mw\n"}):
            plugin._secret_values = None
            self.assertEqual("answer [REDACTED:env]", plugin.redact("answer zq7mw"))
        # Neighbours of a mask survive, and an ordinary sentence is not touched.
        with patch.dict(os.environ, {"A_TOKEN": 'ab"1'}):
            plugin._secret_values = None
            self.assertEqual('ab"2 ab 1', plugin.redact('ab"2 ab 1'))
        plugin._secret_values = None

    def test_alphanumerics_are_masked_when_encoded_by_code_point(self):
        for value in ("abc", "aB7", 'x"9'):
            codes = [ord(ch) for ch in value]
            forms = ["".join("\\u%04x" % c for c in codes), "".join("\\u%04X" % c for c in codes),
                     "".join("\\\\\\u%04x" % c for c in codes), "".join("\\x%02x" % c for c in codes),
                     "".join("%%%02x" % c for c in codes), "".join("%%%02X" % c for c in codes),
                     "".join("%%25%02x" % c for c in codes), "".join("%%2525%02x" % c for c in codes),
                     "".join("&#%d;" % c for c in codes), "".join("&#x%x;" % c for c in codes),
                     value[0] + "".join("%%%02x" % c for c in codes[1:]),
                     "\\u%04x" % codes[0] + value[1:]]
            with patch.dict(os.environ, {"API_TOKEN": value}):
                plugin._secret_values = None
                for form in forms:
                    self.assertEqual("answer [REDACTED:env] end", plugin.redact("answer " + form + " end"), form)
                    self.assertTrue(plugin._unsafe("answer " + form + " end"), form)
        # A long run of backslashes neither hides a value nor slows the scan down.
        with patch.dict(os.environ, {"API_TOKEN": "abc", "B_TOKEN": "x\"y"}):
            plugin._secret_values = None
            self.assertEqual("[REDACTED:env]", plugin.redact("\\" * 300 + "u0061\\u0062\\u0063"))
            self.assertEqual("[REDACTED:env]", plugin.redact("\\" * 300 + "u0078\\\"y"))
            started = time.monotonic()
            self.assertIsNone(plugin._env_pattern().search("\\" * 100000))
            self.assertLess(time.monotonic() - started, 5)
        # Case stays exact for the literal form, and other code points are left alone.
        with patch.dict(os.environ, {"API_TOKEN": "abc"}):
            plugin._secret_values = None
            self.assertEqual("ABC %61%62%64 \\u0061bd", plugin.redact("ABC %61%62%64 \\u0061bd"))
        plugin._secret_values = None

    def test_short_values_and_passphrase_are_masked_without_their_name(self):
        short, phrase = "ab" + "1!", "horse" + "77"
        env = {"DB_PASS": short, "GPG_PASSPHRASE": phrase, "API_TOKEN": "zq7", "PIN_PASSWORD": "4821",
               "AUTH_ENABLED": "true", "TOKENIZERS_PARALLELISM": "false", "SECRET_ROTATION": "1",
               "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000", "PWD": "/tmp", "OLDPWD": "/",
               "GIT_AUTHOR_NAME": "boxp", "UNRELATED": "visible", "EMPTY_TOKEN": ""}
        with patch.dict(os.environ, env):
            plugin._secret_values = None
            for text in ("answer: " + short, "答えは" + short + "です", "(" + phrase + ")", "user:" + short + "@host",
                         "zq7", "pin 4821.", '{"a":"x\\nzq7"}', '{"a":"\\u3048zq7"}'):
                out = plugin.redact(text)
                self.assertIn("[REDACTED:env]", out, text)
                for value in (short, phrase, "zq7", "4821"):
                    self.assertNotIn(value, out, text)
                self.assertTrue(plugin._unsafe(json.dumps({"v": text})), text)
            # A value can be part of a longer word, so it is masked there too.
            for text in ("zq77", "xzq7", "horse777", "abzq7cd"):
                out = plugin.redact(text)
                self.assertNotIn("zq7", out, text)
                self.assertNotIn(phrase, out, text)
                self.assertIn("[REDACTED:env]", out, text)
                self.assertTrue(plugin._unsafe(json.dumps({"v": text})), text)
            # Flags, counters, working directories and authors are not credentials.
            kept = "true false 1 64000 /tmp boxp/lolice"
            self.assertEqual(kept, plugin.redact(kept))
            self.assertFalse(plugin._unsafe(json.dumps({"v": kept})))
        # A later value does not rewrite the mark of an earlier one, and the guard skips marks.
        with patch.dict(os.environ, {"A_TOKEN": "longsecretvalue", "B_TOKEN": "env"}):
            plugin._secret_values = None
            self.assertEqual("[REDACTED:env] [REDACTED:env]", plugin.redact("longsecretvalue env"))
            self.assertFalse(plugin._unsafe(json.dumps({"v": "[REDACTED:env] done"})))
        plugin._secret_values = None

    def test_a_value_counts_as_a_setting_only_under_a_setting_name(self):
        # Secret-looking values are written out of pieces so that the file itself holds none.
        credentials = {"PASSWORD": "dis" + "abled", "DB_PASSWORD": "tr" + "ue", "AUTHORIZATION": "xy" + "z9",
                       "HTTP_AUTHORIZATION": "qw" + "e8", "MINIO_ROOT_PASSWORD": "1234" + "5678",
                       "ADMIN_TOKEN": "48" + "21", "SUPPORT_SECRET": "51" + "50", "EXPORT_KEY_TOKEN": "77" + "31",
                       "AUTH_PORT": "hun" + "ter2", "API_TOKENS": "tr" + "ue2"}
        settings = {"AUTH_ENABLED": "disabled", "PASSWORD_REQUIRED": "false", "NO_AUTH": "true", "USE_TOKEN": "yes",
                    "AUTH_TIMEOUT": "30", "TOKEN_TTL": "3600", "SESSION_COOKIE_MAX_AGE": "86400",
                    "MAX_OUTPUT_TOKENS": "64000", "GIT_AUTHOR_NAME": "boxp", "AUTHORS": "boxp"}
        for name, value in credentials.items():
            self.assertTrue(plugin._is_secret_value(name, value), name)
        for name, value in settings.items():
            self.assertFalse(plugin._is_secret_value(name, value), name)
        with patch.dict(os.environ, credentials):
            plugin._secret_values = None
            for value in credentials.values():
                text = "answer: " + value + " done"
                self.assertEqual("answer: [REDACTED:env] done", plugin.redact(text))
                self.assertTrue(plugin._unsafe(json.dumps({"v": text})), value)
        plugin._secret_values = None

    def test_environment_dumps_are_withheld_with_any_arguments(self):
        for command in ("env LANG=C", "env LANG=C | grep FOO", "env A=1 B='x y' > /tmp/x", "env -i", "env -u HOME", "env --null",
                        "/usr/bin/env LANG=C", "sudo env LANG=C", "kubectl exec pod -- env A=1", "kubectl exec pod -- printenv GITHUB_TOKEN", "oc exec pod -- printenv",
                        "kubectl exec -n ns pod -c app -- env LANG=C printenv", "kubectl exec pod -- sh -c 'printenv'",
                        "kubectl exec pod -- export", "ls\nenv LANG=C\necho done",
                        "echo $(env LANG=C)", "(env LANG=C)", "env A=1 2>&1", "bash -c \"env A=1\"", "env A=1 # note", "python3 -c \"print(__import__('os').environ)\"", "set", "(set -o posix; set)", "export", "declare -p",
                        "typeset -x", "awk 'BEGIN{for(k in ENVIRON)print k}'", "perl -e 'print %ENV'", "ruby -e 'p ENV.to_h'"):
            with self.subTest(command):
                plugin._sessions.clear()
                run_turn(args={"command": command}, result="UNIQUEOUTPUT")
                body = self._bodies()
                self.assertNotIn("UNIQUEOUTPUT", body)
                self.assertIn("withheld", body)

    def test_output_shaped_like_an_environment_dump_is_withheld(self):
        for result in ("PATH=/usr/bin\nHOME=/root\nOPAQUE=UNIQUEOUTPUT\n", "PATH=/usr/bin\0HOME=/root\0OPAQUE=UNIQUEOUTPUT\0",
                       {"output": "HOME=/root\nPATH=/usr/bin\nOPAQUE=UNIQUEOUTPUT"}):
            with self.subTest(result):
                plugin._sessions.clear()
                run_turn(args={"command": "python3 show.py"}, result=result)
                body = self._bodies()
                self.assertNotIn("UNIQUEOUTPUT", body)
                self.assertIn("withheld", body)

    def test_commands_merely_run_through_env_are_not_withheld(self):
        for command in ("env LANG=C ls", "env A=1 B=2 make test", "/usr/bin/env python3 x.py", "docker run --env A=1 img",
                        "cat .venv/README", "git config --unset a.b", "reset", "exporter --help"):
            with self.subTest(command):
                plugin._sessions.clear()
                run_turn(args={"command": command}, result="UNIQUEOUTPUT")
                self.assertIn("UNIQUEOUTPUT", self._bodies())  # not withheld

    def test_arguments_of_a_sensitive_call_are_withheld(self):
        for args in ({"command": "echo plainwordvalue > /a/.env"}, {"path": "/a/.env", "content": "plainwordvalue"},
                     {"command": "kubectl -n foo create secret generic x --from-literal=k=plainwordvalue"}):
            with self.subTest(args):
                plugin._sessions.clear()
                run_turn(args=args, result="ok")
                self.assertNotIn("plainwordvalue", self._bodies())

    def _child_turn(self, session, parent, body="child work"):
        plugin._on_turn_start(session_id=session, turn_id="c", user_message=body, model="m", platform="", parent_session_id=parent)
        plugin._on_turn_end(session_id=session, turn_id="c", assistant_response="done")
        plugin._on_session_end(session_id=session, turn_id="c", completed=True)

    def test_delegated_child_is_in_scope_without_a_ticket(self):
        with capture_turns() as turns:
            plugin._on_subagent_start(parent_session_id="tg-1", parent_turn_id="t", child_session_id="child-1")
            self._child_turn("child-1", "tg-1")
            self.assertEqual([t["session"] for t in turns], ["child-1"])
            plugin._on_subagent_start(parent_session_id="child-1", parent_turn_id="c", child_session_id="grandchild-1")
            self._child_turn("grandchild-1", "child-1")
            self.assertEqual([t["session"] for t in turns], ["child-1", "grandchild-1"])
        self.assertEqual(len(drain()), 2)

    def test_rotated_session_of_a_child_stays_in_scope(self):
        plugin._on_subagent_start(parent_session_id="tg-1", parent_turn_id="t", child_session_id="child-1")
        self._child_turn("child-1-rotated", "child-1")
        self.assertEqual(len(drain()), 1)

    def test_compression_lineage_alone_is_not_delegation(self):
        self._child_turn("tg-rotated", "tg-chat", body="dinner plans")
        self.assertEqual(drain(), [])
        self.assertEqual(plugin.stats().get("turns_out_of_scope"), 1)

    def _bodies(self):
        return bodies()

    def test_withheld_output_stays_out_of_later_requests_and_replies(self):
        # The value matches no pattern, so only the withholding can keep it out.
        unknown = "plainwordvalue"
        histories = {
            "chat": [{"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}]},
                     {"role": "tool", "tool_call_id": "c1", "content": unknown}],
            "anthropic": [{"role": "assistant", "content": [{"type": "text", "text": "reading"}]},
                          {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": unknown}]}],
            "responses": [{"type": "function_call", "call_id": "c1", "name": "read_file", "arguments": "{}"},
                          {"type": "function_call_output", "call_id": "c1", "output": unknown}],
        }
        for name, history in histories.items():
            with self.subTest(name):
                session = "kohako-g2-" + name
                plugin._on_turn_start(session_id=session, turn_id="t", user_message="read it")
                plugin._on_tool(session_id=session, turn_id="t", tool_call_id="c1", tool_name="read_file",
                                args={"path": "/a/.env"}, result=unknown)
                plugin._on_api_request(session_id=session, turn_id="t", api_request_id="a2", request_messages=history,
                                       system_prompt="SYS", message_count=2)
                plugin._on_api_response(session_id=session, turn_id="t", api_request_id="a2",
                                        response={"assistant_message": {"role": "assistant", "content": "it is " + unknown}})
                plugin._on_tool(session_id=session, turn_id="t", tool_call_id="c2", tool_name="terminal",
                                args={"command": "curl -u " + unknown}, result=unknown)
                plugin._on_turn_end(session_id=session, turn_id="t", assistant_response="it is " + unknown)
                plugin._on_session_end(session_id=session, turn_id="t", completed=True)
                body = self._bodies()
                self.assertNotIn(unknown, body)
                self.assertIn("withheld_after_known_source", body)
                # The following turn of the session still carries the value in its history.
                run_turn(session=session, turn="t2", body="next", result=unknown)
                body = self._bodies()
                self.assertNotIn(unknown, body)
                self.assertNotIn("reply next", body)

    def test_user_message_after_sensitive_tool_is_withheld(self):
        # A credential the user pastes again matches no pattern, so only the withholding can keep it out.
        unknown = "plainwordvalue"
        run_turn(session="kohako-g2-u", turn="t1", args={"path": "/a/.env"}, result="V")
        drain()
        run_turn(session="kohako-g2-u", turn="t2", body=unknown)
        body = self._bodies()
        self.assertNotIn(unknown, body)
        self.assertIn("withheld_after_known_source", body)

    def test_user_message_of_resumed_tainted_session_is_withheld(self):
        history = [{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read_file", "arguments": '{"path": "/a/.env"}'}}]}]
        plugin._on_turn_start(session_id="kohako-g2-v", turn_id="t", user_message="plainwordvalue")
        plugin._on_api_request(session_id="kohako-g2-v", turn_id="t", api_request_id="a", request_messages=history)
        plugin._on_session_end(session_id="kohako-g2-v", turn_id="t", completed=True)
        self.assertNotIn("plainwordvalue", self._bodies())

    def test_user_message_before_the_sensitive_tool_is_kept(self):
        run_turn(session="kohako-g2-w", body="please read it", args={"path": "/a/.env"}, result="V")
        self.assertIn("please read it", self._bodies())

    def test_tool_results_in_history_are_never_copied_even_without_sensitive_call(self):
        view = [plugin._message_view(m) for m in (
            {"role": "tool", "content": "TOOLBODY"},
            {"type": "function_call_output", "output": "TOOLBODY"},
            {"role": "user", "content": [{"type": "tool_result", "content": "TOOLBODY"}, {"type": "text", "text": "keep"}]},
            {"role": "assistant", "content": "say", "reasoning": "THINK",
             "tool_calls": [{"function": {"name": "terminal", "arguments": "ARGBODY"}}]})]
        text = json.dumps(view)
        for hidden in ("TOOLBODY", "ARGBODY", "THINK"):
            self.assertNotIn(hidden, text)
        for kept in ("keep", "say", "terminal"):
            self.assertIn(kept, text)

    def test_taint_reaches_parent_and_child(self):
        plugin._on_turn_start(session_id="kohako-g2-p", turn_id="t", user_message="go")
        plugin._on_turn_start(session_id="child", turn_id="c", user_message="goal", parent_session_id="kohako-g2-p")
        plugin._on_tool(session_id="child", turn_id="c", tool_call_id="x", tool_name="read_file", args={"path": "/a/.env"}, result="V")
        plugin._on_subagent(parent_session_id="kohako-g2-p", parent_turn_id="t", child_session_id="child", child_summary="CHILDSAYS")
        plugin._on_turn_end(session_id="kohako-g2-p", turn_id="t", assistant_response="PARENTSAYS")
        plugin._on_session_end(session_id="kohako-g2-p", turn_id="t", completed=True)
        body = self._bodies()
        self.assertNotIn("CHILDSAYS", body)
        self.assertNotIn("PARENTSAYS", body)
        plugin._on_turn_start(session_id="child2", turn_id="c", user_message="GOALTEXT BOXP-1", parent_session_id="kohako-g2-p")
        plugin._on_session_end(session_id="child2", turn_id="c", completed=True)
        self.assertNotIn("GOALTEXT", self._bodies())

    def test_resumed_session_rebuilds_taint_from_history(self):
        history = [{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read_file", "arguments": '{"path": "/a/.env"}'}}]},
                   {"role": "tool", "content": "V"}, {"role": "assistant", "content": "ECHOED"}, {"role": "user", "content": "more"}]
        plugin._on_turn_start(session_id="kohako-g2-r", turn_id="t", user_message="more")
        plugin._on_api_request(session_id="kohako-g2-r", turn_id="t", api_request_id="a", request_messages=history)
        plugin._on_api_response(session_id="kohako-g2-r", turn_id="t", api_request_id="a", response={"assistant_message": {"role": "assistant", "content": "LATER"}})
        plugin._on_session_end(session_id="kohako-g2-r", turn_id="t", completed=True)
        body = self._bodies()
        self.assertNotIn("ECHOED", body)
        self.assertNotIn("LATER", body)
        # A question that only mentions a sensitive word does not taint a new session.
        run_turn(session="kohako-g2-q", body="what is .env for?", args={"command": "ls"})
        self.assertIn("reply what is .env for?", self._bodies())

    def test_resumed_session_rebuilds_taint_from_environment_output(self):
        dump = "PATH=/usr/bin\nHOME=/root\nOPAQUE=plainresumevalue"
        shapes = {
            "tool": {"role": "tool", "content": dump},
            "tool_blocks": {"role": "tool", "content": [{"type": "text", "text": dump}]},
            "wrapped": {"role": "tool", "content": json.dumps({"output": dump})},
            "nul": {"role": "tool", "content": dump.replace("\n", "\0")},
            "responses": {"type": "function_call_output", "output": dump},
            "anthropic": {"role": "user", "content": [{"type": "tool_result", "content": dump}]},
        }
        for name, result in shapes.items():
            with self.subTest(name):
                session = "kohako-g2-resume-" + name
                history = [{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "terminal", "arguments": '{"command": "python3 show.py"}'}}]},
                           result, {"role": "assistant", "content": "ECHOED plainresumevalue"}, {"role": "user", "content": "plainresumevalue again"}]
                plugin._on_turn_start(session_id=session, turn_id="t", user_message="plainresumevalue again")
                plugin._on_api_request(session_id=session, turn_id="t", api_request_id="a", request_messages=history)
                plugin._on_api_response(session_id=session, turn_id="t", api_request_id="a", response={"assistant_message": {"role": "assistant", "content": "LATER plainresumevalue"}})
                plugin._on_tool(session_id=session, turn_id="t", tool_call_id="x", tool_name="terminal", args={"command": "echo plainresumevalue"}, result="plainresumevalue")
                plugin._on_turn_end(session_id=session, turn_id="t", assistant_response="plainresumevalue")
                plugin._on_session_end(session_id=session, turn_id="t", completed=True)
                self.assertNotIn("plainresumevalue", self._bodies())

    def test_a_session_without_earlier_messages_is_not_withheld(self):
        histories = {
            "first_message": [{"role": "user", "content": "go"}],
            "instructions": [{"role": "system", "content": "Set PATH=/usr/bin and HOME=/root in the sandbox."}, {"role": "user", "content": "go"}],
        }
        for name, history in histories.items():
            with self.subTest(name):
                session = "kohako-g2-plain-" + name
                plugin._on_turn_start(session_id=session, turn_id="t", user_message="visible " + name)
                plugin._on_api_request(session_id=session, turn_id="t", api_request_id="a", request_messages=history)
                plugin._on_session_end(session_id=session, turn_id="t", completed=True)
                self.assertIn("visible " + name, self._bodies())

    def _value_only_reply(self, session, tool, args, result="plainconfigvalue", first="read it"):
        # The value has no known format and no secret word near it, so nothing about it can be recognised.
        plugin._on_turn_start(session_id=session, turn_id="t", user_message=first)
        plugin._on_api_request(session_id=session, turn_id="t", api_request_id="a1", request_messages=[{"role": "user", "content": first}])
        plugin._on_tool(session_id=session, turn_id="t", tool_call_id="c1", tool_name=tool, args=args, result=result)
        plugin._on_api_request(session_id=session, turn_id="t", api_request_id="a2", request_messages=[{"role": "user", "content": first}])
        plugin._on_api_response(session_id=session, turn_id="t", api_request_id="a2", response={"assistant_message": {"role": "assistant", "content": "plainconfigvalue"}})
        plugin._on_turn_end(session_id=session, turn_id="t", assistant_response="plainconfigvalue")
        plugin._on_session_end(session_id=session, turn_id="t", completed=True)

    def test_reply_after_an_ordinary_tool_is_sent(self):
        # Using a tool is not evidence. A value without a secret word or known format near it is the accepted gap (THREAT_MODEL.md).
        for tool, args in (("terminal", {"command": "cat /etc/app/config.yaml"}), ("read_file", {"path": "/etc/app/config.yaml"}),
                           ("search_files", {"pattern": "db"}), ("session_search", {"query": "db"}), ("memory", {"action": "read"}),
                           ("browser_vault", {}), ("a_tool_added_later", {}), ("todo", {}), ("web_search", {})):
            with self.subTest(tool):
                session = "kohako-g2-config-" + tool
                self._value_only_reply(session, tool, args)
                body = self._bodies()
                self.assertIn("plainconfigvalue", body)
                self.assertIn("read it", body)
                self.assertNotIn("[withheld", body)
                self.assertNotIn('"withheld_', body)
                self.assertIn('"stringValue": "sent"', body)
                # The next turn of the same session stays readable too.
                run_turn(session=session, turn="t2", body="nextturnmarker", tool="todo", args={}, result="ok")
                self.assertIn("nextturnmarker", self._bodies())

    def test_reply_after_a_child_used_a_tool_is_sent(self):
        plugin._on_turn_start(session_id="kohako-g2-pc", turn_id="t", user_message="go")
        plugin._on_subagent_start(parent_session_id="kohako-g2-pc", parent_turn_id="t", child_session_id="pc-child")
        plugin._on_turn_start(session_id="pc-child", turn_id="c", user_message="childgoalmarker", parent_session_id="kohako-g2-pc")
        plugin._on_tool(session_id="pc-child", turn_id="c", tool_call_id="x", tool_name="terminal", args={"command": "cat /etc/app/config.yaml"}, result="childtoolmarker")
        plugin._on_turn_end(session_id="pc-child", turn_id="c", assistant_response="childreplymarker")
        plugin._on_session_end(session_id="pc-child", turn_id="c", completed=True)
        plugin._on_subagent(parent_session_id="kohako-g2-pc", parent_turn_id="t", child_session_id="pc-child", child_summary="childsummarymarker")
        plugin._on_turn_end(session_id="kohako-g2-pc", turn_id="t", assistant_response="parentreplymarker")
        plugin._on_session_end(session_id="kohako-g2-pc", turn_id="t", completed=True)
        body = self._bodies()
        for marker in ("childgoalmarker", "childtoolmarker", "childreplymarker", "childsummarymarker", "parentreplymarker"):
            self.assertIn(marker, body)
        self.assertNotIn("[withheld", body)
        self.assertNotIn('"withheld_', body)

    def test_known_format_in_a_tool_result_masks_it_and_withholds_the_rest_of_the_turn(self):
        # The model saw a credential-shaped value: it is masked and the rest of that turn is withheld. Later turns are
        # sent through the mask, with the credential pinned, so a bare restatement stays masked (BOXP-428 TD-13: this
        # withheld a long chat session for good).
        for result, credential in (("Authorization: Bearer " + "plainword" + "value1", "plainwordvalue1"),
                                   ("url: postgres://app:" + "plainwordvalue@db/x", "plainwordvalue"),
                                   ("key: " + "ghp_" + "plainwordvalue", "plainwordvalue"),
                                   # lolice#1179 review: a cookie header pins the value of each pair, not only name=value
                                   ("Set-Cookie: session=" + "plainwordvalue; Path=/; HttpOnly", "plainwordvalue"),
                                   ("Cookie: a=1; sid=" + "plainwordvalue", "plainwordvalue"),
                                   ('Set-Cookie: sid="' + 'plainwordvalue"; Max-Age=3600', "plainwordvalue"),
                                   # lolice#1179 review: a cookie may be named like an attribute (path, domain)
                                   ("Cookie: path=" + "plainwordvalue", "plainwordvalue"),
                                   ("Cookie: a=1; domain=" + "plainwordvalue", "plainwordvalue"),
                                   ("Set-Cookie: expires=" + "plainwordvalue; Path=/", "plainwordvalue")):
            with self.subTest(result):
                session = "kohako-g2-fmt-" + str(abs(hash(str(result))))
                self._value_only_reply(session, "terminal", {"command": "cat app.yaml"}, result=result)
                body = self._bodies()
                self.assertNotIn("plainwordvalue", body)
                self.assertIn("read it", body)
                self.assertIn("withheld_after_known_format_in_tool_output", body)
                self.assertIsNone(plugin._tainted(session))
                run_turn(session=session, turn="t2", body="next turn " + credential + " nextturnmarker", tool="todo",
                         args={}, result="ok")
                body = self._bodies()
                self.assertNotIn(credential, body)
                self.assertIn("nextturnmarker", body)

    def test_a_credential_with_separators_is_pinned_whole(self):
        # lolice#1179 review: a URL password abc,def was pinned only as the short words abc and def, which match on word
        # boundaries, so prefixabc,defsuffix in a later turn was sent.
        for result, restated in (("url: https://u:" + "abc,def@example.com", "prefixabc,defsuffix"),
                                 ("url: https://u:" + "hunter123:q7z@example.com", "prefixhunter123:q7zsuffix"),
                                 ("Authorization: Bearer " + "abc;def", "prefixabc;defsuffix")):
            with self.subTest(result):
                session = "kohako-g2-sep-" + str(abs(hash(str(result))))
                self._value_only_reply(session, "terminal", {"command": "cat app.yaml"}, result=result)
                run_turn(session=session, turn="t2", body="next turn " + restated + " nextturnmarker", tool="todo",
                         args={}, result="ok")
                body = self._bodies()
                self.assertNotIn(restated, body)
                self.assertNotIn(restated[6:13], body)
                self.assertIn("nextturnmarker", body)

    def test_a_header_after_a_line_break_in_a_structured_result_withholds_the_turn(self):
        # lolice#1179 review: a structured tool result serializes its line break as \\n, so "\\nAuthorization:" had no
        # word boundary and the credential was neither evidence nor pinned.
        for result in ({"output": "line one\nAuthorization: Bearer " + "plainwordvalue1\nline three"},
                       {"stdout": "x\nCookie: sid=" + "plainwordvalue1"},
                       ["a", "b\n" + "ghp_" + "plainwordvalue1"]):
            with self.subTest(result):
                session = "kohako-g2-nl-" + str(abs(hash(str(result))))
                self._value_only_reply(session, "terminal", {"command": "cat app.yaml"}, result=result)
                body = self._bodies()
                self.assertNotIn("plainwordvalue1", body)
                self.assertIn("withheld_after_known_format_in_tool_output", body)
                run_turn(session=session, turn="t2", body="next turn plainwordvalue1 nextturnmarker", tool="todo",
                         args={}, result="ok")
                body = self._bodies()
                self.assertNotIn("plainwordvalue1", body)
                self.assertIn("nextturnmarker", body)

    def test_a_symbol_only_known_format_credential_is_pinned(self):
        # lolice#1179 review: a credential of a known format made only of symbols (https://u:!!!@host) was not pinned,
        # so a bare restatement of it in a later turn was sent.
        for result, credential in (("url: https://u:" + "!!!@example.com", "!!!"),
                                   ("url: https://u:" + "!@example.com", "!"),
                                   ("url: https://u:" + "#$%&@example.com", "#$%&"),
                                   ("Authorization: Bearer " + "----", "----"),
                                   ("Cookie: a=" + "!!; b=1", "!!")):
            with self.subTest(result):
                session = "kohako-g2-sym-" + str(abs(hash(str(result))))
                self._value_only_reply(session, "terminal", {"command": "cat app.yaml"}, result=result)
                self.assertIn("withheld_after_known_format_in_tool_output", self._bodies())
                run_turn(session=session, turn="t2", body="next turn " + credential + " nextturnmarker", tool="todo",
                         args={}, result="ok")
                body = self._bodies()
                self.assertNotIn(" " + credential + " ", body)
                self.assertIn("nextturnmarker", body)
                self.assertIsNone(plugin._tainted(session))

    def test_a_name_based_hit_masks_in_place_and_keeps_later_bodies(self):
        # A secret name with a value is masked where it is, but is no evidence: ordinary code and logs match it too.
        # A bare restatement of the masked value elsewhere is the accepted residual (THREAT_MODEL.md 2).
        for result in ("host: db\npassword: plainwordvalue\nport: 5432", {"output": '{"api_key":"plainwordvalue"}'},
                       "<password>plainwordvalue</password>", "mytool --password plainwordvalue"):
            with self.subTest(result):
                session = "kohako-g2-name-" + str(abs(hash(str(result))))
                self._value_only_reply(session, "terminal", {"command": "cat app.yaml"}, result=result)
                body = self._bodies()
                self.assertNotIn("plainwordvalue", body)  # masked in the tool result
                self.assertIn("cat app.yaml", body)
                self.assertIn("masked_known_format", body)
                self.assertNotIn("withheld_after_known_format_in_tool_output", body)
                self.assertIn("plainconfigvalue", body)  # the reply after the tool stays readable

    def test_ordinary_code_naming_a_secret_does_not_withhold(self):
        # Regression (production 2026-11-07): these withheld every later body of the session.
        for result in ("TOKEN_FILE=/work/.cache/state", "token = os.environ['API']", "fetch(url, { credentials: 'same-origin' })",
                       "PASS: disk check", "mytool --token $TOKEN run"):
            with self.subTest(result):
                session = "kohako-g2-code-" + str(abs(hash(result)))
                self._value_only_reply(session, "read_file", {"path": "/src/app.py"}, result=result)
                self.assertIsNone(plugin._tainted(session))
                body = self._bodies()
                self.assertIn("plainconfigvalue", body)
                self.assertNotIn("withheld_after_", body)

    def test_a_secret_word_alone_in_a_tool_result_does_not_withhold(self):
        self._value_only_reply("kohako-g2-word", "terminal", {"command": "cat README.md"},
                               result="Set the password in the settings page.\nneighbour line\nkeptlinemarker", first="read it")
        body = self._bodies()
        self.assertIn("keptlinemarker", body)
        self.assertIn("plainconfigvalue", body)  # the reply stays
        self.assertNotIn("[withheld", body)
        self.assertNotIn('"withheld_', body)

    def test_oversized_tool_result_is_withheld_unscanned(self):
        big = "x" * (plugin._MAX_SCAN_BYTES + 1)
        self._value_only_reply("kohako-g2-big", "terminal", {"command": "cat big.log"}, result=big)
        body = self._bodies()
        self.assertNotIn("xxxxxxxx", body)
        self.assertIn("withheld_unscanned_output", body)
        self.assertNotIn("plainconfigvalue", body)

    def test_a_harmless_turn_is_not_all_withheld(self):
        # Regression guard: an empty trace would also hold no secret. At least one body has to be sent.
        run_turn(session="kohako-g2-harmless", body="harmlessrequestmarker", args={"command": "ls -la"}, result="harmlesslistingmarker")
        body = self._bodies()
        for marker in ("harmlessrequestmarker", "harmlesslistingmarker", "reply harmlessrequestmarker", "done harmlessrequestmarker", "ls -la"):
            self.assertIn(marker, body)
        self.assertIn('"stringValue": "sent"', body)
        self.assertNotIn("[withheld", body)
        self.assertNotIn('"withheld_', body)
        self.assertNotIn("omitted", body)
        self.assertGreaterEqual(plugin.stats().get("bodies_sent", 0), 3)

    def _resumed(self, session, history, parent=None):
        plugin._on_turn_start(session_id=session, turn_id="t", user_message="plainconfigvalue", parent_session_id=parent)
        plugin._on_api_request(session_id=session, turn_id="t", api_request_id="a", request_messages=history)
        plugin._on_api_response(session_id=session, turn_id="t", api_request_id="a", response={"assistant_message": {"role": "assistant", "content": "plainconfigvalue"}})
        plugin._on_turn_end(session_id=session, turn_id="t", assistant_response="plainconfigvalue")
        plugin._on_session_end(session_id=session, turn_id="t", completed=True)
        return self._bodies()

    def test_history_this_process_did_not_observe_withholds_nothing_by_itself(self):
        # After a restart, an ordinary tool in the history or the fact that it was not observed is no evidence.
        histories = {
            "tool_result": [{"role": "tool", "content": "README.md"}, {"role": "user", "content": "go"}],
            "tool_call": [{"role": "assistant", "tool_calls": [{"function": {"name": "read_file", "arguments": '{"path": "/a/notes.txt"}'}}]}, {"role": "user", "content": "go"}],
            "compacted": [{"role": "user", "content": "[CONTEXT COMPACTION] the file held plainconfigvalue"}, {"role": "user", "content": "go"}],
            "reply_only": [{"role": "assistant", "content": "plainconfigvalue"}, {"role": "user", "content": "go"}],
            "unknown_parent": [{"role": "user", "content": "[CONTEXT COMPACTION] summary"}, {"role": "user", "content": "go"}],
        }
        for name, history in histories.items():
            with self.subTest(name):
                body = self._resumed("kohako-g2-unseen-" + name, history, parent="kohako-g2-never-seen" if name == "unknown_parent" else None)
                self.assertIn("plainconfigvalue", body)
                self.assertNotIn("[withheld", body)
                self.assertNotIn('"withheld_', body)

    def test_evidence_in_unobserved_history_withholds_without_its_hook(self):
        # A call whose post_tool_call never fired, for example after an interrupt or a restart: the history still shows it.
        shapes = {
            "chat_args": {"role": "assistant", "tool_calls": [{"function": {"name": "read_file", "arguments": '{"path": "/a/.env"}'}}]},
            "anthropic_args": {"role": "assistant", "content": [{"type": "tool_use", "name": "read_file", "input": {"path": "/a/.env"}}]},
            "responses_args": {"type": "function_call", "name": "terminal", "arguments": '{"command": "kubectl get secret x -o yaml"}'},
            "responses_result": {"type": "function_call_output", "output": "PATH=/usr/bin\nHOME=/root\nOPAQUE=plainwordvalue"},
        }
        for name, shape in shapes.items():
            with self.subTest(name):
                session = "kohako-g2-nohook-" + name
                plugin._on_turn_start(session_id=session, turn_id="t", user_message="go")
                plugin._on_api_request(session_id=session, turn_id="t", api_request_id="a", request_messages=[{"role": "user", "content": "go"}])
                self.assertIsNone(plugin._tainted(session))
                plugin._on_api_request(session_id=session, turn_id="t", api_request_id="b", request_messages=[shape, {"role": "user", "content": "go"}])
                self.assertTrue(plugin._tainted(session))
                plugin._on_turn_end(session_id=session, turn_id="t", assistant_response="plainwordvalue")
                plugin._on_session_end(session_id=session, turn_id="t", completed=True)
                self.assertNotIn("plainwordvalue", self._bodies())

    def test_known_format_in_unobserved_history_is_pinned_not_withheld(self):
        # BOXP-428 TD-13: a resumed long session rescans its history after every restart. A known format in an old
        # tool result is masked and its credential pinned; it does not withhold every later turn of the session.
        shapes = {
            "format_in_result": ({"role": "tool", "content": "Authorization: Bearer " + "plainword" + "value9"}, "plainwordvalue9"),
            "anthropic_result": ({"role": "user", "content": [{"type": "tool_result", "content": "key: ghp_" + "plainwordvalue"}]},
                                 "plainwordvalue"),
            # Serialized, a header after a line break follows the two characters \\n (production shape after a restart).
            "header_after_newline": ({"role": "tool", "content": "GET /api 200\nAuthorization: token " + "plainword" + "value7\nok"},
                                     "plainwordvalue7"),
        }
        for name, (shape, credential) in shapes.items():
            with self.subTest(name):
                session = "kohako-g2-nohook-" + name
                plugin._on_turn_start(session_id=session, turn_id="t", user_message="go")
                plugin._on_api_request(session_id=session, turn_id="t", api_request_id="b", request_messages=[shape, {"role": "user", "content": "go"}])
                self.assertIsNone(plugin._tainted(session))
                plugin._on_turn_end(session_id=session, turn_id="t", assistant_response="it was " + credential + " historyreadablemarker")
                plugin._on_session_end(session_id=session, turn_id="t", completed=True)
                body = self._bodies()
                self.assertNotIn(credential, body)
                self.assertIn("historyreadablemarker", body)

    def test_ordinary_tool_names_in_history_are_no_evidence(self):
        plugin._on_turn_start(session_id="kohako-g2-names", turn_id="t", user_message="go")
        for shape in ({"role": "assistant", "tool_calls": [{"function": {"name": "terminal", "arguments": '{"command": "ls"}'}}]},
                      {"role": "assistant", "content": [{"type": "tool_use", "name": "terminal", "input": {"command": "ls"}}]},
                      {"type": "function_call", "name": "terminal", "arguments": '{"command": "ls"}'}, {"role": "tool", "content": "a.txt b.txt"}):
            plugin._on_api_request(session_id="kohako-g2-names", turn_id="t", api_request_id="b", request_messages=[shape])
        self.assertIsNone(plugin._tainted("kohako-g2-names"))

    def test_final_guard_drops_span_when_masking_misses(self):
        with capture_turns() as turns:
            run_turn()
        spans = plugin.build_spans(turns[0])
        spans[1]["name"] = SECRETS["github"]  # bypasses field masking
        bodies = list(plugin.guarded_payloads(spans))
        self.assertNotIn("a1B2a1B2", "".join(bodies))
        self.assertEqual(plugin.stats().get("spans_dropped_secret_guard"), 1)

    def test_xml_elements_with_a_secret_name_are_masked(self):
        for text in ("<password>plainwordvalue</password>", "<db-password attr=\"x\">plainwordvalue</db-password>",
                     "<ns:apiKey>plainwordvalue</ns:apiKey>", "<password>\n  plainwordvalue\n\n\n</password>",
                     "<password>plainwordvalue", "&lt;password&gt;plainwordvalue&lt;/password&gt;",
                     "\\u003cpassword\\u003eplainwordvalue\\u003c/password\\u003e",
                     json.dumps({"output": "<password>\n\n\nplainwordvalue\n\n\n</password>"})):
            with self.subTest(text):
                self.assertNotIn("plainwordvalue", plugin.redact(text))

    def test_a_secret_word_masks_its_line_whatever_the_format(self):
        for text in ("the password is plainwordvalue", "plainwordvalue is the password", "パスワードは plainwordvalue です",
                     "Password:\nplainwordvalue", "plainwordvalue\n<key>password</key>", "secret plainwordvalue",
                     "[db]\npassword plainwordvalue", "username,password\nadmin,plainwordvalue",
                     "認証情報。plainwordvalue", "password: first plainwordvalue", "--password first plainwordvalue", "token=first plainwordvalue", '{"key":"password","data":"plainwordvalue"}'):
            with self.subTest(text):
                self.assertNotIn("plainwordvalue", plugin.redact(text))

    def test_a_secret_word_leaves_distant_lines_and_ordinary_words(self):
        out = plugin.redact("keptfirst\nkeptsecond\npassword plainwordvalue\nmasked neighbour\nkeptlast")
        self.assertIn("keptfirst", out)
        self.assertIn("keptlast", out)
        self.assertNotIn("plainwordvalue", out)
        for text in ("input_tokens 12", "Cloudflare Access の設定", "author: keptvalue", "tests passed"):
            with self.subTest(text):
                self.assertEqual(plugin.redact(text), text)

    def test_tool_bodies_are_sent_through_the_mask(self):
        for tool, args, result in (
                ("read_file", {"path": "/a/notes.txt"}, "harmlessbodymarker"),
                ("terminal", {"command": "mycli login harmlessbodymarker"}, "ok"),
                ("write_file", {"path": "/a/b", "content": "harmlessbodymarker"}, "ok"),
                ("execute_code", {"code": "x = 'harmlessbodymarker'"}, "harmlessbodymarker"),
                ("unknown_tool", {"anything": "harmlessbodymarker"}, {"nested": "harmlessbodymarker"})):
            with self.subTest(tool):
                run_turn(tool=tool, args=args, result=result)
                body = bodies()
                self.assertIn("harmlessbodymarker", body)
                self.assertNotIn("omitted", body)
        run_turn(tool="read_file", args={"path": "/a/notes.txt", "offset": 3}, result="line1\nline2")
        body = bodies()
        self.assertIn("/a/notes.txt", body)
        self.assertIn("line2", body)
        run_turn(session="kohako-g2-program", tool="terminal", args={"command": "LANG=C /usr/bin/kubectl get pods"})
        self.assertIn("get pods", bodies())

    def test_tool_bodies_with_a_known_format_are_masked_not_omitted(self):
        run_turn(tool="read_file", args={"path": "/a/app.yaml"}, result="host: db\npassword: plainwordvalue\nneighbour\nkeptdistantmarker\n")
        body = bodies()
        self.assertNotIn("plainwordvalue", body)
        self.assertIn("REDACTED", body)
        self.assertIn("masked_known_format", body)
        self.assertIn("keptdistantmarker", body)  # the line next to the secret word is masked, the one after it is kept

    def test_delegation_tool_bodies_are_sent_masked(self):
        run_turn(tool="delegate_task", args={"goal": "delegatedgoalmarker"}, result="delegatedresultmarker")
        body = bodies()
        self.assertIn("delegatedgoalmarker", body)
        self.assertIn("delegatedresultmarker", body)
        run_turn(tool="delegate_task", args={"goal": "<password>plainwordvalue</password>"}, result="the password is plainwordvalue")
        self.assertNotIn("plainwordvalue", bodies())

    def test_final_guard_drops_span_with_a_secret_word(self):
        with capture_turns() as turns:
            run_turn()
        spans = plugin.build_spans(turns[0])
        spans[1]["name"] = "<password>plainwordvalue</password>"  # bypasses field masking
        self.assertNotIn("plainwordvalue", "".join(plugin.guarded_payloads(spans)))
        self.assertEqual(plugin.stats().get("spans_dropped_secret_guard"), 1)

    def test_scope(self):
        run_turn(session="kohako-g2-1")
        self.assertEqual(len(drain()), 1)
        run_turn(session="tg-1", platform="telegram", body="dinner plans")
        self.assertEqual(drain(), [])
        self.assertEqual(plugin.stats().get("turns_out_of_scope"), 1)
        with capture_turns() as turns:
            run_turn(session="tg-2", platform="telegram", body="BOXP-428 を作って")
        self.assertEqual(turns[0]["tickets"], {"BOXP-428"})
        self.assertIn("ticket:BOXP-428", bodies())
        run_turn(session="tg-3", platform="telegram", tool="delegate_task")
        self.assertEqual(len(drain()), 1)
        run_turn(session="wh", platform="webhook")
        self.assertEqual(len(drain()), 1)

    def test_spans_have_trace_attrs_exclusive_usage_and_no_public(self):
        with capture_turns() as turns:
            run_turn(body="BOXP-1 x")
        spans = plugin.build_spans(turns[0])
        ids = {s["traceId"] for s in spans}
        self.assertEqual(len(ids), 1)
        root = spans[0]["spanId"]
        for s in spans:
            keys = {a["key"] for a in s["attributes"]}
            self.assertIn("langfuse.session.id", keys)
            self.assertIn("langfuse.trace.tags", keys)
            self.assertNotIn("langfuse.trace.public", keys)
            self.assertEqual(s.get("parentSpanId", root), root)
        gen = next(s for s in spans if s["name"] == "llm-call")
        usage = next(a for a in gen["attributes"] if a["key"] == "langfuse.observation.usage_details")
        self.assertEqual(json.loads(usage["value"]["stringValue"]),
                         {"input": 6, "output": 3, "input_cached_tokens": 4, "output_reasoning_tokens": 2})

    def test_ids_are_deterministic(self):
        with capture_turns() as turns:
            run_turn()
            run_turn()
        a, b = plugin.build_spans(turns[0]), plugin.build_spans(turns[1])
        self.assertEqual([s["spanId"] for s in a], [s["spanId"] for s in b])

    def test_turn_limits_and_queue_full_never_raise(self):
        plugin._on_turn_start(session_id="kohako-g2-big", turn_id="t")
        for i in range(plugin._MAX_TURN_SPANS + 5):
            plugin._on_tool(session_id="kohako-g2-big", turn_id="t", tool_call_id=str(i), tool_name="x", args={}, result="r")
        self.assertEqual(plugin.stats()["spans_dropped_turn_limit"], 5)
        plugin._turns.clear()
        for i in range(plugin._QUEUE_MAX + 3):
            run_turn(turn=str(i))
        self.assertEqual(plugin.stats()["turns_dropped_queue_full"], 3)
        for i in range(plugin._MAX_TURNS + 2):
            plugin._on_turn_start(session_id=f"s{i}", turn_id="t")
        self.assertEqual(plugin.stats()["turns_evicted"], 2)

    def test_hook_exception_is_swallowed(self):
        with patch.object(plugin, "_turn", side_effect=RuntimeError):
            self.assertIsNone(plugin._on_turn_start(session_id="x", turn_id="y"))
        self.assertEqual(plugin.stats()["hook_errors"], 1)

    def test_send_is_bounded_when_server_fails_and_posts_when_up(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            status = 500

            def do_POST(self):
                seen.append((self.path, self.headers.get("x-langfuse-ingestion-version"),
                             self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(Handler.status)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        run_turn()
        batches = drain()[0]
        with patch.object(plugin, "_URL", f"http://127.0.0.1:{server.server_port}"), patch("time.sleep"):
            plugin.export_bodies(batches)
            self.assertEqual(len(seen), 2)
            self.assertEqual(plugin.stats()["batches_dropped"], 1)
            Handler.status = 200
            plugin.export_bodies(batches)
            self.assertEqual(len(seen), 3)
            self.assertEqual(seen[-1][0], "/api/public/otel/v1/traces")
            self.assertEqual(seen[-1][1], "4")
        server.shutdown()
        # Nothing listening: bounded failure, no exception.
        with patch.object(plugin, "_URL", "http://127.0.0.1:9"), patch("time.sleep"):
            plugin.export_bodies(batches)
        self.assertEqual(plugin.stats()["batches_dropped"], 2)



def _restart():
    """A Hermes restart: the observer's in-memory state is gone, the session's history is not."""
    plugin._turns.clear()
    plugin._sessions.clear()
    plugin._pins.clear()
    plugin._parents.clear()
    plugin._pinned_digests.clear()
    plugin._scanned.clear()


def home_turn(session, turn, user, tool, args, result, reply, history=()):
    """One turn of a long-lived home chat session (Telegram, ticket mentioned so that it is in scope), with the
    resent history of earlier turns as Hermes sends it on every request."""
    plugin._on_turn_start(session_id=session, turn_id=turn, user_message=user, model="gpt", platform="telegram")
    messages = list(history) + [{"role": "user", "content": user}]
    plugin._on_api_request(session_id=session, turn_id=turn, api_request_id=turn + "-a", system_prompt="あなたはコハコです。",
                           request_messages=messages, message_count=len(messages), retry_count=0, started_at=time.time())
    plugin._on_api_response(session_id=session, turn_id=turn, api_request_id=turn + "-a",
                            response={"assistant_message": {"content": "確認します", "tool_calls": [
                                {"function": {"name": tool, "arguments": json.dumps(args, ensure_ascii=False)}}]}},
                            usage={"input_tokens": 10, "output_tokens": 5}, model="gpt", provider="openai-codex",
                            api_call_count=1, finish_reason="tool_calls", started_at=time.time(), ended_at=time.time())
    plugin._on_tool(session_id=session, turn_id=turn, tool_call_id=turn + "-c", tool_name=tool, args=args, result=result,
                    duration_ms=5, status="ok")
    tool_messages = [{"role": "assistant", "tool_calls": [{"function": {"name": tool, "arguments": json.dumps(args)}}]},
                     {"role": "tool", "content": result}]
    plugin._on_api_request(session_id=session, turn_id=turn, api_request_id=turn + "-b", system_prompt="あなたはコハコです。",
                           request_messages=messages + tool_messages, message_count=len(messages) + 2, retry_count=0,
                           started_at=time.time())
    plugin._on_api_response(session_id=session, turn_id=turn, api_request_id=turn + "-b",
                            response={"assistant_message": {"content": reply}}, usage={"input_tokens": 10, "output_tokens": 5},
                            model="gpt", provider="openai-codex", api_call_count=2, finish_reason="stop",
                            started_at=time.time(), ended_at=time.time())
    plugin._on_turn_end(session_id=session, turn_id=turn, assistant_response=reply)
    plugin._on_session_end(session_id=session, turn_id=turn, completed=True)
    return messages + tool_messages + [{"role": "assistant", "content": reply}]


def observations(body_text):
    """(name, attributes) of every span in the drained batches."""
    out = []
    for body in body_text:
        for rs in json.loads(body)["resourceSpans"]:
            for ss in rs["scopeSpans"]:
                for span in ss["spans"]:
                    attrs = {a["key"]: a["value"].get("stringValue", a["value"]) for a in span["attributes"]}
                    out.append((span["name"], attrs))
    return out


class HomeSessionMaskRegressionTest(unittest.TestCase):
    """BOXP-428 TD-13. Production 2026-11-11: every turn of the long Telegram home session (20261026_...) was sent as
    withheld_after_known_format_in_tool_output, from turn start, for days, while the short webhook/cron sessions of the
    same pod were readable. One credential-shaped known format in an earlier tool result withheld the session for good,
    and every restart rescanned the history and withheld it again. Metadata the observer writes itself (retry "0",
    release "0.1.0", "delegated") came out as [REDACTED:pinned]. Fixture reduced to that structure, synthetic values."""

    CREDENTIAL = "ghp_" + "Td13Home" + "Q7w8E9r0"
    SESSION = "20261026_050737_td13home"

    def setUp(self):
        ObserverTest.setUp(self)

    def _first_turn(self):
        # A tool result printing a credential-shaped header (synthetic), plus a short named value pinned as a word.
        return home_turn(self.SESSION, "t1", "BOXP-505 のログを見て", "terminal", {"command": "tail app.log"},
                         "GET /api 200\nAuthorization: token " + self.CREDENTIAL + "\nretry: 0 password: q7z\nok",
                         "ログを確認しました。firstturnreplymarker")

    def test_later_turns_of_the_session_are_readable_and_the_credential_stays_masked(self):
        history = self._first_turn()
        first = bodies()
        self.assertNotIn(self.CREDENTIAL, first)
        self.assertNotIn("firstturnreplymarker", first)  # the rest of that turn is withheld
        self.assertIn("withheld_after_known_format_in_tool_output", first)
        home_turn(self.SESSION, "t2", "BOXP-505 今日の天気とメモを確認して。なでなで", "read_file", {"path": "/notes/today.md"},
                  "secondtoolmarker 今日のメモ\n買い物\n認証設定の説明: パスワードは管理画面で変更します\n散歩",
                  "晴れです。さっきのキーは " + self.CREDENTIAL + " でした。secondreplymarker", history=history)
        second = bodies()
        for marker in ("今日の天気とメモ", "secondtoolmarker", "secondreplymarker", "あなたはコハコです", "/notes/today.md"):
            self.assertIn(marker, second)
        self.assertNotIn(self.CREDENTIAL, second)
        self.assertNotIn("Td13HomeQ7w8E9r0", second)
        self.assertNotIn("withheld_after_known_format_in_tool_output", second)
        self.assertIsNone(plugin._tainted(self.SESSION))

    def test_a_restart_that_rescans_the_history_does_not_withhold_the_session_again(self):
        history = self._first_turn()
        bodies()
        _restart()
        home_turn(self.SESSION, "t3", "BOXP-505 続きをお願い", "terminal", {"command": "ls /notes"}, "today.md\nrestarttoolmarker",
                  "続けます。キーは " + self.CREDENTIAL + "。restartreplymarker", history=history)
        body = bodies()
        for marker in ("続きをお願い", "restarttoolmarker", "restartreplymarker"):
            self.assertIn(marker, body)
        self.assertNotIn(self.CREDENTIAL, body)
        self.assertNotIn("withheld_after_", body)

    def test_another_session_is_not_affected(self):
        self._first_turn()
        bodies()
        home_turn("20261111_000016_td13other", "o1", "BOXP-505 別の会話", "terminal", {"command": "date"}, "othertoolmarker",
                  "別セッションの返答 othersessionmarker")
        body = bodies()
        self.assertIn("othertoolmarker", body)
        self.assertIn("othersessionmarker", body)
        self.assertNotIn("withheld_after_", body)

    def test_short_and_long_credentials_are_pinned(self):
        # Review of lolice#1179: a credential of 1-3 or over 256 characters was neither pinned nor withheld.
        for secret in ("q7z", "abcde12345" * 27):
            with self.subTest(len(secret)):
                session = "kohako-g2-credlen-" + str(len(secret))
                plugin._on_turn_start(session_id=session, turn_id="1", user_message="hello")
                plugin._on_tool(session_id=session, turn_id="1", tool_name="read_file", args={},
                                result="Authorization: Bearer " + secret)
                plugin._on_session_end(session_id=session, turn_id="1", completed=True)
                drain()
                run_turn(session=session, turn="2", body="reply " + secret + " credlenmarker")
                body = bodies()
                self.assertNotIn(secret, body.replace("q7zz", ""))
                self.assertIn("credlenmarker", body)

    def test_oversized_credential_withholds_the_lineage(self):
        # Review of lolice#1179: a known-format credential over 65,536 characters was not pinned, and the turn-scoped
        # hold ended with the turn, so a later restatement was sent. Too long to pin: the lineage is withheld.
        secret = "Q7w8E9r0" * 8193
        session = "kohako-g2-credhuge"
        plugin._on_turn_start(session_id=session, turn_id="1", user_message="hello")
        plugin._on_tool(session_id=session, turn_id="1", tool_name="read_file", args={},
                        result="Authorization: Bearer " + secret)
        plugin._on_session_end(session_id=session, turn_id="1", completed=True)
        drain()
        run_turn(session=session, turn="2", body="reply " + secret[:70000] + " hugemarker")
        self.assertNotIn("Q7w8E9r0Q7w8E9r0", bodies())
        self.assertEqual(plugin._tainted(session), "credential_over_limit")

    def test_open_child_turn_is_withheld_when_the_parent_sees_a_credential(self):
        # Review of lolice#1179: a child turn already open when its parent's tool printed a credential kept sending the
        # rest of its own turn (the hold walked ancestors only).
        parent, child = "kohako-g2-openparent", "kohako-g2-openchild"
        plugin._on_turn_start(session_id=parent, turn_id="p", user_message="hello")
        plugin._on_turn_start(session_id=child, turn_id="c", user_message="child goal", parent_session_id=parent)
        plugin._on_tool(session_id=parent, turn_id="p", tool_name="read_file", args={},
                        result="Authorization: Bearer " + "ghp_" + "OpenChild" + "Q7w8E9r0zz")
        plugin._on_turn_end(session_id=child, turn_id="c", assistant_response="openchildreplymarker")
        plugin._on_session_end(session_id=child, turn_id="c", completed=True)
        plugin._on_session_end(session_id=parent, turn_id="p", completed=True)
        body = bodies()
        self.assertNotIn("openchildreplymarker", body)
        self.assertNotIn("OpenChildQ7w8E9r0zz", body)
        # a sibling of the parent's lineage that is not under the parent is untouched
        self.assertIsNone(plugin._open_withheld("kohako-g2-unrelated"))

    def test_child_credential_does_not_withhold_open_sibling_turns(self):
        # Review of lolice#1179: a child's credential withheld its parent and, through the parent, every open sibling.
        parent, child, sibling = "kohako-g2-sibparent", "kohako-g2-sibchild", "kohako-g2-sibling"
        plugin._on_turn_start(session_id=parent, turn_id="p", user_message="hello")
        plugin._on_turn_start(session_id=child, turn_id="c", user_message="child goal", parent_session_id=parent)
        plugin._on_turn_start(session_id=sibling, turn_id="s", user_message="sibling goal", parent_session_id=parent)
        plugin._on_tool(session_id=child, turn_id="c", tool_name="read_file", args={},
                        result="Authorization: Bearer " + "ghp_" + "SibChild" + "Q7w8E9r0zz")
        self.assertIsNotNone(plugin._open_withheld(child))
        self.assertIsNotNone(plugin._open_withheld(parent))
        self.assertIsNone(plugin._open_withheld(sibling))
        plugin._on_turn_end(session_id=sibling, turn_id="s", assistant_response="siblingreplymarker")
        plugin._on_session_end(session_id=sibling, turn_id="s", completed=True)
        plugin._on_session_end(session_id=child, turn_id="c", completed=True)
        plugin._on_session_end(session_id=parent, turn_id="p", completed=True)
        body = bodies()
        self.assertIn("siblingreplymarker", body)
        self.assertNotIn("SibChildQ7w8E9r0zz", body)

    def test_caller_metadata_is_still_masked(self):
        # Review of lolice#1179: only the observer's own constants, ids and numbers are exempt from the pin mask.
        session = "kohako-g2-callermeta"
        plugin._on_turn_start(session_id=session, turn_id="1", user_message="hello")
        plugin._on_tool(session_id=session, turn_id="1", tool_name="read_file", args={},
                        result="Authorization: Bearer " + "plainword" + "value1")
        plugin._on_session_end(session_id=session, turn_id="1", completed=True)
        drain()
        plugin._on_turn_start(session_id=session, turn_id="2", user_message="hello")
        plugin._on_api_response(session_id=session, turn_id="2", api_request_id="a",
                                response={"assistant_message": {"content": "ok"}}, finish_reason="plainwordvalue1",
                                provider="plainwordvalue1", response_model="plainwordvalue1")
        plugin._on_session_end(session_id=session, turn_id="2", completed=True)
        self.assertNotIn("plainwordvalue1", bodies())

    def test_observer_metadata_is_not_masked_by_pinned_short_values(self):
        history = self._first_turn()
        bodies()
        home_turn(self.SESSION, "t2", "BOXP-505 もう一度", "todo", {"todos": []}, "ok", "q7z ではありません。metadatamarker",
                  history=history)
        spans = observations([b for item in drain() for b in item])
        self.assertTrue(spans)
        text = json.dumps(spans, ensure_ascii=False)
        self.assertIn("metadatamarker", text)
        self.assertNotIn("q7z", text)  # the pinned short value is still masked in the reply
        for name, attrs in spans:
            self.assertEqual(attrs.get("langfuse.release"), plugin.VERSION)
            self.assertEqual(attrs.get("langfuse.trace.metadata.coverage.g2_realtime"),
                             "delegated text only; realtime voice session not observed")
            if name == "llm-call":
                self.assertEqual(attrs.get("langfuse.observation.metadata.retry"), "0")

if __name__ == "__main__":
    unittest.main()
