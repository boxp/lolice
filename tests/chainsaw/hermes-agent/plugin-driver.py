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
    hooks["post_tool_call"](**common, tool_call_id="tool", tool_name="terminal", args={"command": "ls"} if clean else {"command": "echo plainwordvalue > /a/.env"}, result=body)
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
    fire_delegated_child(f"tg-{n}", f"subagent-{n}")
    print(f"round {n}", flush=True)
    time.sleep(1)
