import html
import importlib.util
import inspect
import json
import os
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
        plugin._delegated.clear()
        getattr(plugin, "_scanned", {}).clear()
        plugin._creds = ("pk-lf-" + "test" * 4, "sk-lf-" + "test" * 4)
        plugin._secret_values = None

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
                     {"command": "codex exec --max-output-tokens 5 x"}, {"model_settings": {"max_output_tokens": 5}}, {"query": "the token refresh flow"}):
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
                        "/usr/bin/env LANG=C", "sudo env LANG=C", "kubectl exec pod -- env A=1", "ls\nenv LANG=C\necho done",
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

    def test_known_format_in_a_tool_result_masks_it_and_withholds_later_bodies(self):
        # The model saw a credential: the line is masked and what follows in the session is withheld.
        for result in ("host: db\npassword: plainwordvalue\nport: 5432", {"output": '{"api_key":"plainwordvalue"}'},
                       "<password>plainwordvalue</password>", "Authorization: Bearer " + "plainword" + "value1"):
            with self.subTest(result):
                session = "kohako-g2-fmt-" + str(abs(hash(str(result))))
                self._value_only_reply(session, "terminal", {"command": "cat app.yaml"}, result=result)
                body = self._bodies()
                self.assertNotIn("plainwordvalue", body)
                self.assertIn("read it", body)
                self.assertIn("withheld_after_known_format_in_tool_output", body)
                run_turn(session=session, turn="t2", body="plainwordvalue", tool="todo", args={}, result="ok")
                self.assertNotIn("plainwordvalue", self._bodies())

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
            "format_in_result": {"role": "tool", "content": "api_key=" + "plainword" + "value9"},
            "anthropic_result": {"role": "user", "content": [{"type": "tool_result", "content": "<password>plainwordvalue</password>"}]},
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


if __name__ == "__main__":
    unittest.main()
