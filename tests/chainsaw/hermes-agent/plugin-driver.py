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

def fire_harmless_tool(session, tool, args):
    # Positive control (Owner contract): an ordinary tool, its output and the replies after it are sent, this turn and the next.
    hooks = ctx.hooks
    common = {"session_id": session, "turn_id": "turn"}
    hooks["pre_llm_call"](**common, user_message="harmlessrequestmarker")
    hooks["pre_api_request"](**common, api_request_id="api", request_messages=[{"role": "user", "content": "harmlessrequestmarker"}])
    hooks["post_tool_call"](**common, tool_call_id="tool", tool_name=tool, args=args, result="harmlesstooloutputmarker")
    hooks["post_api_request"](**common, api_request_id="api", response={"assistant_message": {"content": "harmlessreplymarker"}})
    hooks["post_llm_call"](**common, assistant_response="harmlessreplymarker")
    hooks["on_session_end"](**common, completed=True)
    hooks["pre_llm_call"](session_id=session, turn_id="next", user_message="harmlessnextturnmarker")
    hooks["post_llm_call"](session_id=session, turn_id="next", assistant_response="harmlessnextreplymarker")
    hooks["on_session_end"](session_id=session, turn_id="next", completed=True)

def fire_harmless_lineage(parent, child):
    # Positive control: a parent that used terminal delegates; the child's request and the parent's summary are sent.
    hooks = ctx.hooks
    hooks["pre_llm_call"](session_id=parent, turn_id="turn", user_message="hello")
    hooks["post_tool_call"](session_id=parent, turn_id="turn", tool_call_id="tool", tool_name="terminal", args={"command": "ls -la"}, result="a.txt")
    hooks["subagent_start"](parent_session_id=parent, parent_turn_id="turn", child_session_id=child, child_role="leaf", child_goal="work")
    hooks["pre_llm_call"](session_id=child, turn_id="turn", user_message="harmlesschildmarker", platform="", parent_session_id=parent)
    hooks["post_tool_call"](session_id=child, turn_id="turn", tool_call_id="tool", tool_name="read_file", args={"path": "/a/notes.txt"}, result="harmlesschildtoolmarker")
    hooks["post_llm_call"](session_id=child, turn_id="turn", assistant_response="harmlesschildreplymarker")
    hooks["on_session_end"](session_id=child, turn_id="turn", completed=True)
    hooks["subagent_stop"](parent_session_id=parent, parent_turn_id="turn", child_session_id=child, child_summary="harmlesssummarymarker")
    hooks["post_llm_call"](session_id=parent, turn_id="turn", assistant_response="harmlessparentreplymarker")
    hooks["on_session_end"](session_id=parent, turn_id="turn", completed=True)

def fire_known_format_in_output(session):
    # A tool result with a credential-shaped known format: it is masked, and the reply that repeats the value is withheld.
    hooks = ctx.hooks
    common = {"session_id": session, "turn_id": "turn"}
    hooks["pre_llm_call"](**common, user_message="hello")
    hooks["post_tool_call"](**common, tool_call_id="tool", tool_name="terminal", args={"command": "cat app.yaml"},
                            result="host: db\nAuthorization: Bearer plainformatvalue\nneighbour\nkeptdistantmarker")
    hooks["post_api_request"](**common, api_request_id="api", response={"assistant_message": {"content": "plainformatvalue"}})
    hooks["post_llm_call"](**common, assistant_response="plainformatvalue")
    hooks["on_session_end"](**common, completed=True)
    hooks["pre_llm_call"](session_id=session, turn_id="next", user_message="plainformatvalue")
    hooks["on_session_end"](session_id=session, turn_id="next", completed=True)

def fire_name_only_in_output(session):
    # Ordinary code that names a secret, and a secret-name assignment: the value is masked in place, and the replies
    # and the next turn stay readable (BOXP-428 production regression: these withheld whole sessions).
    hooks = ctx.hooks
    common = {"session_id": session, "turn_id": "turn"}
    hooks["pre_llm_call"](**common, user_message="hello")
    hooks["post_tool_call"](**common, tool_call_id="tool", tool_name="read_file", args={"path": "/src/app.ts"},
                            result="fetch(url, { credentials: 'same-origin' })\nTOKEN_FILE=/work/state\n\n\n\npassword: plainnamedvalue")
    hooks["post_llm_call"](**common, assistant_response="nameonlyreplymarker")
    hooks["on_session_end"](**common, completed=True)
    hooks["pre_llm_call"](session_id=session, turn_id="next", user_message="nameonlynextmarker")
    hooks["on_session_end"](session_id=session, turn_id="next", completed=True)

def fire_known_source_table(session, args):
    # K0 table additions (THREAT_MODEL.md): the call and the rest of the session are withheld.
    hooks = ctx.hooks
    common = {"session_id": session, "turn_id": "turn"}
    hooks["pre_llm_call"](**common, user_message="hello")
    hooks["post_tool_call"](**common, tool_call_id="tool", tool_name="terminal", args=args, result="plaintablevalue")
    hooks["post_llm_call"](**common, assistant_response="plaintablevalue")
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
    fire_env_dump(f"kohako-g2-file-{n}", "cat notes.txt", "harmlessfilemarker")  # an ordinary file is sent (positive control)
    fire_free_text(f"kohako-g2-xmltext-{n}", "<password>\n\n\nplainxmlvalue\n\n\n</password>")
    fire_free_text(f"kohako-g2-prose-{n}", "the password is plainprosevalue")
    fire_harmless_tool(f"kohako-g2-terminal-{n}", "terminal", {"command": "ls -la /tmp"})
    fire_harmless_tool(f"kohako-g2-read-{n}", "read_file", {"path": "/tmp/notes.txt"})
    fire_harmless_tool(f"kohako-g2-unknown-{n}", "a_tool_added_later", {"query": "the token refresh flow"})
    fire_harmless_lineage(f"tg-lineage-{n}", f"subagent-lineage-{n}")
    fire_known_format_in_output(f"kohako-g2-format-{n}")
    fire_name_only_in_output(f"kohako-g2-nameonly-{n}")
    fire_known_source_table(f"kohako-g2-kube-config-{n}", {"command": "cat ~/.kube/config"})
    fire_known_source_table(f"kohako-g2-pem-{n}", {"command": "cat /etc/ssl/server.pem"})
    fire_known_source_table(f"kohako-g2-gcloud-{n}", {"command": "gcloud auth print-access-token"})
    fire_known_source_table(f"kohako-g2-tokens-{n}", {"command": "cat /var/lib/app/tokens.json"})
    fire_known_source_table(f"kohako-g2-idkey-{n}", {"command": "cat /backup/id_xmss"})
    fire_known_source_table(f"kohako-g2-baretokens-{n}", {"command": "cat tokens"})
    fire_known_source_table(f"kohako-g2-tokenpath-{n}", {"path": "tokens"})
    fire_delegation(f"kohako-g2-delegation-{n}")
    fire_delegated_child(f"tg-{n}", f"subagent-{n}")
    print(f"round {n}", flush=True)
    time.sleep(1)
