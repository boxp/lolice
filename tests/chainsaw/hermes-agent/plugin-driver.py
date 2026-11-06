import importlib.util
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
    fire_ordinary_read(f"kohako-g2-readlarge-{n}", "the password is plainfarvalue\n" + "line\n" * 20 + "plainfarvalue\n" + "filler line\n" * 30000, "ok")
    fire_ordinary_read(f"kohako-g2-readblockword-{n}", "password:\n  - firstvalue\n  - blockwordleak horse battery\n", "blockwordleak")
    fire_running_child(f"kohako-g2-runparent-{n}", f"runchild-{n}")
    fire_rewritten_history(f"kohako-g2-rewrite-{n}")
    fire_delegation(f"kohako-g2-delegation-{n}")
    fire_delegated_child(f"tg-{n}", f"subagent-{n}")
    print(f"round {n}", flush=True)
    time.sleep(1)
