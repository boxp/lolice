import importlib.util
import base64
import os
import time

spec = importlib.util.spec_from_file_location("observer", os.environ.get("PLUGIN_PATH", "/plugin/__init__.py"))
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

class Context:
    def __init__(self):
        self.hooks = {}
    def register_hook(self, name, callback):
        self.hooks[name] = callback

ctx = Context()
plugin.register(ctx)

def fire(session, clean):
    hooks = ctx.hooks
    common = {"session_id": session, "turn_id": "turn"}
    body = "cleanbodymarker" if clean else "plainwordvalue"
    hooks["pre_llm_call"](**common, user_message="hello")
    hooks["post_tool_call"](**common, tool_call_id="tool", tool_name="todo" if clean else "terminal", args={"todos": []} if clean else {"command": "echo plainwordvalue > /a/.env"}, result=body)
    hooks["pre_api_request"](**common, api_request_id="api", request_messages=[{"role": "user" if clean else "tool", "content": body}])
    hooks["post_api_request"](**common, api_request_id="api", response={"assistant_message": {"content": body}})
    hooks["post_llm_call"](**common, assistant_response=body)
    hooks["on_session_end"](**common, completed=True)
    # The user repeats a value in the next turn of the same session.
    hooks["pre_llm_call"](session_id=session, turn_id="next", user_message=body)
    hooks["on_session_end"](session_id=session, turn_id="next", completed=True)

def fire_flagged_secret_read(session):
    hooks = ctx.hooks
    common = {"session_id": session, "turn_id": "turn"}
    hooks["pre_llm_call"](**common, user_message="hello")
    hooks["post_tool_call"](**common, tool_call_id="tool", tool_name="terminal",
                            args={"command": "kubectl -n foo get secret bar -o jsonpath='{.data.x}'"}, result="plainkubevalue")
    hooks["on_session_end"](**common, completed=True)

def fire_env_dump(session, command, result):
    # The value matches no secret pattern, so only withholding the output can keep it out.
    hooks = ctx.hooks
    common = {"session_id": session, "turn_id": "turn"}
    hooks["pre_llm_call"](**common, user_message="hello")
    hooks["post_tool_call"](**common, tool_call_id="tool", tool_name="terminal", args={"command": command}, result=result)
    hooks["on_session_end"](**common, completed=True)

def fire_resumed_env_dump(session):
    # A resumed session: the only evidence is the output of an ordinary command in its history.
    hooks = ctx.hooks
    common = {"session_id": session, "turn_id": "turn"}
    history = [{"role": "tool", "content": "PATH=/usr/bin\nHOME=/root\nOPAQUE=plainresumevalue"}, {"role": "user", "content": "plainresumevalue"}]
    hooks["pre_llm_call"](**common, user_message="plainresumevalue")
    hooks["pre_api_request"](**common, api_request_id="api", request_messages=history)
    hooks["post_api_request"](**common, api_request_id="api", response={"assistant_message": {"content": "plainresumevalue"}})
    hooks["post_llm_call"](**common, assistant_response="plainresumevalue")
    hooks["on_session_end"](**common, completed=True)

def fire_ordinary_read(session, result, reply, path="/repo/README.md"):
    # TD-05: an ordinary read and the reply after it stay readable; only evidence of a secret withholds.
    hooks = ctx.hooks
    common = {"session_id": session, "turn_id": "turn"}
    hooks["pre_llm_call"](**common, user_message="readme askmarker")
    hooks["pre_api_request"](**common, api_request_id="api", request_messages=[{"role": "user", "content": "readme askmarker"}])
    hooks["post_tool_call"](**common, tool_call_id="tool", tool_name="read_file", args={"path": path}, result=result)
    hooks["pre_api_request"](**common, api_request_id="api2", request_messages=[{"role": "user", "content": "readme askmarker"}, {"role": "tool", "content": result}])
    hooks["post_api_request"](**common, api_request_id="api2", response={"assistant_message": {"content": reply}})
    hooks["post_llm_call"](**common, assistant_response=reply)
    hooks["on_session_end"](**common, completed=True)

def fire_read_then_reply(session, result, reply):
    # The reply comes straight after the tool, before the history carrying the tool result is sent again.
    hooks = ctx.hooks
    common = {"session_id": session, "turn_id": "turn"}
    hooks["pre_llm_call"](**common, user_message="readme askmarker")
    hooks["post_tool_call"](**common, tool_call_id="tool", tool_name="read_file", args={"path": "/repo/notes.md"}, result=result)
    hooks["post_llm_call"](**common, assistant_response=reply)
    hooks["on_session_end"](**common, completed=True)

def fire_history_reply(session):
    # History the plugin did not see (a restart) holds an ordinary reply that mentions a password. It is scanned as a
    # reply, not as tool output, so the session after it stays readable.
    hooks = ctx.hooks
    common = {"session_id": session, "turn_id": "turn"}
    history = [{"role": "user", "content": "check it"}, {"role": "assistant", "content": "Your password is too short."},
               {"role": "user", "content": "historyaskmarker"}]
    hooks["pre_llm_call"](**common, user_message="historyaskmarker")
    hooks["pre_api_request"](**common, api_request_id="api", request_messages=history)
    hooks["post_api_request"](**common, api_request_id="api", response={"assistant_message": {"content": "historyreplykeepmarker"}})
    hooks["post_llm_call"](**common, assistant_response="historyreplykeepmarker")
    hooks["on_session_end"](**common, completed=True)

def fire_rewritten_history(session):
    # Compaction rewrites the history to the same length; the rewritten message shows a secret value.
    hooks = ctx.hooks
    common = {"session_id": session, "turn_id": "turn"}
    first = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}]
    second = [{"role": "user", "content": "a"}, {"role": "tool", "content": [{"type": "tool_result", "content": {"token": "plainrewritevalue"}}]}, {"role": "user", "content": "c"}]
    hooks["pre_llm_call"](**common, user_message="hello")
    hooks["pre_api_request"](**common, api_request_id="api", request_messages=first)
    hooks["pre_api_request"](**common, api_request_id="api2", request_messages=second)
    hooks["post_llm_call"](**common, assistant_response="plainrewritevalue")
    hooks["on_session_end"](**common, completed=True)

def fire_free_text(session, text):
    # The same value through what the model and the user say, where no tool policy applies.
    hooks = ctx.hooks
    common = {"session_id": session, "turn_id": "turn"}
    hooks["pre_llm_call"](**common, user_message=text)
    hooks["post_llm_call"](**common, assistant_response=text)
    hooks["on_session_end"](**common, completed=True)

def fire_delegation(session):
    hooks = ctx.hooks
    common = {"session_id": session, "turn_id": "turn"}
    hooks["pre_llm_call"](**common, user_message="hello")
    hooks["post_tool_call"](**common, tool_call_id="tool", tool_name="delegate_task", args={"goal": "delegatedgoalmarker"}, result="ok")
    hooks["on_session_end"](**common, completed=True)

def fire_delegated_child(parent, child):
    # Neither the parent's scope nor a ticket reference reaches the child: only the delegation does.
    hooks = ctx.hooks
    common = {"session_id": child, "turn_id": "turn"}
    hooks["subagent_start"](parent_session_id=parent, parent_turn_id="turn", child_session_id=child, child_role="leaf", child_goal="work")
    hooks["pre_llm_call"](**common, user_message="childbodymarker", platform="", parent_session_id=parent)
    hooks["on_session_end"](**common, completed=True)

def fire_instructions(session):
    # A value written in the system prompt reaches the model like any other text, and the model repeats it alone.
    hooks = ctx.hooks
    common = {"session_id": session, "turn_id": "turn"}
    hooks["pre_llm_call"](**common, user_message="what is it")
    hooks["pre_api_request"](**common, api_request_id="api", system_prompt="rules\npassword: sysinstrvalue\n\nmore\nrules",
                             request_messages=[{"role": "system", "content": "the password is devinstrvalue"}, {"role": "user", "content": "what is it"}])
    hooks["post_api_request"](**common, api_request_id="api", response={"assistant_message": {"content": "sysinstrvalue devinstrvalue"}})
    hooks["post_llm_call"](**common, assistant_response="sysinstrvalue devinstrvalue")
    hooks["on_session_end"](**common, completed=True)

def fire_running_child(parent, child):
    # The parent sees a secret while its child is already running, then the child repeats it.
    hooks = ctx.hooks
    parent_common = {"session_id": parent, "turn_id": "turn"}
    common = {"session_id": child, "turn_id": "turn"}
    hooks["pre_llm_call"](**parent_common, user_message="delegate")
    hooks["subagent_start"](parent_session_id=parent, parent_turn_id="turn", child_session_id=child, child_role="leaf", child_goal="work")
    hooks["pre_llm_call"](**common, user_message="childgoal", platform="", parent_session_id=parent)
    hooks["post_tool_call"](**parent_common, tool_call_id="key", tool_name="read_file", args={"path": "/repo/k.pem"},
                            result="-----BEGIN PRIVATE KEY-----\nrunningchildkeyvalue\n-----END PRIVATE KEY-----")
    hooks["pre_api_request"](**common, api_request_id="api", request_messages=[{"role": "user", "content": "runningchildkeyvalue"}])
    hooks["post_api_request"](**common, api_request_id="api", response={"assistant_message": {"content": "runningchildkeyvalue"}})
    hooks["post_llm_call"](**common, assistant_response="runningchildkeyvalue")
    hooks["on_session_end"](**common, completed=True)
    hooks["on_session_end"](**parent_common, completed=True)

def fire_sensitive_call(session):
    # The reply that asks to write .env is kept before the tool hook fires; its call arguments carry the value.
    hooks = ctx.hooks
    common = {"session_id": session, "turn_id": "turn"}
    hooks["pre_llm_call"](**common, user_message="write settings")
    hooks["pre_api_request"](**common, api_request_id="api", request_messages=[{"role": "user", "content": "write settings"}])
    hooks["post_api_request"](**common, api_request_id="api", response={"assistant_message": {"content": "writing", "tool_calls": [
        {"id": "c", "type": "function", "function": {"name": "write_file", "arguments": '{"path": "/app/.env", "content": "X=envcallvaluezz"}'}}]}})
    hooks["post_llm_call"](**common, assistant_response="done")
    hooks["on_session_end"](**common, completed=True)

def fire_grandchild_source(top, mid, low):
    # A grandchild reads a sensitive source; the grandparent repeats the value before the middle session finishes.
    hooks = ctx.hooks
    hooks["pre_llm_call"](session_id=top, turn_id="turn", user_message="delegate")
    hooks["subagent_start"](parent_session_id=top, parent_turn_id="turn", child_session_id=mid, child_role="orchestrator", child_goal="work")
    hooks["pre_llm_call"](session_id=mid, turn_id="turn", user_message="midgoal", platform="", parent_session_id=top)
    hooks["subagent_start"](parent_session_id=mid, parent_turn_id="turn", child_session_id=low, child_role="leaf", child_goal="work")
    hooks["pre_llm_call"](session_id=low, turn_id="turn", user_message="lowgoal", platform="", parent_session_id=mid)
    hooks["post_tool_call"](session_id=low, turn_id="turn", tool_call_id="env", tool_name="read_file", args={"path": "/app/.env"},
                            result="value grandsourcevaluezz")
    hooks["post_llm_call"](session_id=top, turn_id="turn", assistant_response="grandsourcevaluezz")
    hooks["on_session_end"](session_id=top, turn_id="turn", completed=True)
    hooks["on_session_end"](session_id=low, turn_id="turn", completed=True)
    hooks["on_session_end"](session_id=mid, turn_id="turn", completed=True)

n = 0
while True:
    n += 1
    fire(f"kohako-g2-{n}", False)
    fire(f"kohako-g2-clean-{n}", True)
    fire_flagged_secret_read(f"kohako-g2-kube-{n}")
    fire_env_dump(f"kohako-g2-env-{n}", "env LANG=C", "OPAQUE=plainenvvalue")
    fire_env_dump(f"kohako-g2-envout-{n}", "python3 show.py", "PATH=/usr/bin\nHOME=/root\nOPAQUE=plainenvvalue")
    fire_env_dump(f"kohako-g2-nested-{n}", "cat result.json", {"output": '{"password":"plainnestedvalue"}'})
    fire_env_dump(f"kohako-g2-flag-{n}", "cat result.json", {"output": "mytool --password plainflagvalue\n- name: DB_PASSWORD\n  value: plainentryvalue"})
    fire_env_dump(f"kohako-g2-array-{n}", "cat result.json", {"output": '{"password":["first","plainarrayvalue"]}'})
    fire_resumed_env_dump(f"kohako-g2-resume-{n}")
    fire_env_dump(f"kohako-g2-xml-{n}", "cat config.xml", "<password>plainxmlvalue</password>")
    fire_env_dump(f"kohako-g2-file-{n}", "cat notes.txt", "harmlessfileoutputmarker")
    fire_env_dump(f"kohako-g2-filesecret-{n}", "cat notes.txt", "db:\n  password: plainfilesecret\n")
    fire_free_text(f"kohako-g2-xmltext-{n}", "<password>\n\n\nplainxmlvalue\n\n\n</password>")
    fire_free_text(f"kohako-g2-prose-{n}", "the password is plainprosevalue")
    fire_ordinary_read(f"kohako-g2-read-{n}", "harmlessreadmemarker", "harmlessreplymarker")
    fire_ordinary_read(f"kohako-g2-readsecret-{n}", "api_key: plainreadsecret", "plainreadsecret")
    fire_ordinary_read(f"kohako-g2-readdict-{n}", {"data": {"auth": {"token": "plaindictvalue"}}}, "plaindictvalue")
    fire_ordinary_read(f"kohako-g2-readlower-{n}", "the password is lowerprosevalue", "lowerprosevalue")
    fire_ordinary_read(f"kohako-g2-readshort-{n}", "password: qzv", "qzv")
    fire_ordinary_read(f"kohako-g2-readja-{n}", "パスワードは ひみつのもじれつ", "ひみつのもじれつ")
    fire_ordinary_read(f"kohako-g2-readjaunspaced-{n}", "パスワードはひみつのもじれつです", "ひみつのもじれつ")
    fire_ordinary_read(f"kohako-g2-readshortlater-{n}", "password: alpha qzw", "qzw")
    fire_ordinary_read(f"kohako-g2-readtokenfile-{n}", '{"refresh": "opaquefilevaluezz"}', "opaquefilevaluezz", path="/tmp/tokens.json")
    fire_ordinary_read(f"kohako-g2-readkeyfile-{n}", "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCkeybodyvaluezz", "keybodyvaluezz", path="/etc/app/private.key")
    fire_ordinary_read(f"kohako-g2-readpemfile-{n}", "pembodyvaluezzAAAAB3NzaC1", "pembodyvaluezz", path="/etc/ssl/server.pem")
    fire_ordinary_read(f"kohako-g2-readlarge-{n}", "the password is plainfarvalue\n" + "line\n" * 20 + "plainfarvalue\n" + "filler line\n" * 30000, "ok")
    fire_ordinary_read(f"kohako-g2-readstopword-{n}", "password: negotiate", "negotiate")
    fire_ordinary_read(f"kohako-g2-readliteral-{n}", "password: xxxxxxxxxxxx", "xxxxxxxxxxxx")
    fire_ordinary_read(f"kohako-g2-readmixedstop-{n}", "the password is username horsestaplezz", "username")
    fire_ordinary_read(f"kohako-g2-readblockword-{n}", "password:\n  - firstvalue\n  - blockwordleak horse battery\n", "blockwordleak")
    fire_ordinary_read(f"kohako-g2-readbase64-{n}", "password: b64leakvaluezz", base64.b64encode(b"b64leakvaluezz").decode())
    fire_read_then_reply(f"kohako-g2-readsecretword-{n}", "password: my-secretword-leakzz", base64.b64encode(b"my-secretword-leakzz").decode())
    fire_ordinary_read(f"kohako-g2-readbefore-{n}", "beforeprosevaluezz is my password", "beforeprosevaluezz")
    fire_read_then_reply(f"kohako-g2-replyrepeat-{n}", "harmless notes", "password: replyrepeatvaluezz\nfiller one\nfiller two\nreplykeepmarker\nagain replyrepeatvaluezz")
    fire_read_then_reply(f"kohako-g2-replyshort-{n}", "harmless notes", "the password is qzk\nfiller one\nreplyshortkeepmarker\nqzk")
    fire_read_then_reply(f"kohako-g2-replyencshort-{n}", "harmless notes", "the password is qzj\nfiller one\nreplyencshortkeepmarker\n" + base64.b64encode(b"xqzjy").decode())
    fire_history_reply(f"kohako-g2-historyreply-{n}")
    fire_read_then_reply(f"kohako-g2-replyja-{n}", "harmless notes", "パスワードは ないしょのことば\nfiller one\nreplyjakeepmarker\nことば")
    fire_ordinary_read(f"kohako-g2-readjasentence-{n}", "パスワードを設定してください", "jasentencereplymarker")
    fire_instructions(f"kohako-g2-instructions-{n}")
    fire_running_child(f"kohako-g2-runparent-{n}", f"runchild-{n}")
    fire_sensitive_call(f"kohako-g2-envcall-{n}")
    fire_grandchild_source(f"kohako-g2-grandtop-{n}", f"grandmid-{n}", f"grandlow-{n}")
    fire_rewritten_history(f"kohako-g2-rewrite-{n}")
    fire_delegation(f"kohako-g2-delegation-{n}")
    fire_delegated_child(f"tg-{n}", f"subagent-{n}")
    print(f"round {n}", flush=True)
    time.sleep(1)
