"""A stand-in for `dsh --profile acp`, replaying what the real dsh 0.2.1-rc.2 sent.

Every message shape, error text, config option and session-log row below was
copied from captures of the real binary (no key, then a local fake upstream in
place of api.deepseek.com). Nothing here calls a model.

The scenario is read from $DSH_HOME/fake-scenario.json: {"case": ..., "answer": ...}.
Cases: text, tool, escalate, subagent, quota, ratelimit, auth, leak, hang.
Each launch appends one row to $DSH_HOME/fake-trace.jsonl: argv, permission
mode, whether a key arrived and its SHA-256 (never the key), and every method.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

HOME = Path(os.environ["DSH_HOME"])
SCENARIO = json.loads((HOME / "fake-scenario.json").read_text()) if (HOME / "fake-scenario.json").exists() else {}
CASE = SCENARIO.get("case", "text")
KEY = os.environ.get("DEEPSEEK_API_KEY")
MODE = os.environ.get("DSH_PERMISSION_MODE", "workspace-write")
PROVIDER = "deepseek-official"
TRACE = {"argv": sys.argv[1:], "cwd": os.getcwd(), "permission_mode": MODE, "has_key": KEY is not None,
         "key_sha256": hashlib.sha256(KEY.encode()).hexdigest() if KEY else None,
         "base_url_variable": "DEEPSEEK_BASE_URL" in os.environ, "methods": [], "selected": {}}


TRACE_PATH = HOME / "fake-traces" / f"{time.time_ns()}-{os.getpid()}.json"


def finish_trace():
    # Rewritten after every request, so a launch the client kills still leaves its trace.
    TRACE_PATH.parent.mkdir(exist_ok=True)
    TRACE_PATH.write_text(json.dumps(TRACE))


def send(value):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **value}) + "\n")
    sys.stdout.flush()


def model_value(model):
    return json.dumps([PROVIDER, model], separators=(",", ":"))


def config_options(current="deepseek-v4-flash", effort="high"):
    # Without a key dsh offers only its configured ACP model.
    choices = [{"value": model_value("deepseek-v4-flash"), "name": "deepseek-v4-flash"}]
    if KEY:
        choices += [{"value": model_value("deepseek-flash"), "name": "DeepSeek-V4.1-Flash"},
                    {"value": model_value("deepseek-v4-pro"), "name": "DeepSeek-V4-Pro",
                     "description": "Stronger agentic coding, knowledge, and difficult reasoning; suited to complex or quality-critical tasks at higher cost."}]
    return [{"id": "model", "name": "Model", "category": "model", "type": "select", "currentValue": model_value(current),
             "options": [{"group": PROVIDER, "name": "DeepSeek", "options": choices}]},
            {"id": "reasoning_effort", "name": "Reasoning effort", "category": "thought_level", "type": "select",
             "currentValue": effort, "options": [{"value": value, "name": value.title()} for value in ("off", "low", "high", "max")]}]


def plain_logs():
    for index, value in enumerate(sys.argv):
        if value == "--patch" and index + 1 < len(sys.argv):
            text = Path(sys.argv[index + 1]).read_text()
            return "compression: none" in text and "session-persistence-jsonl" in text
    return False


PLAIN = plain_logs()


class Log:
    """The session log as dsh's persistence plugin writes it with compression: none."""

    def __init__(self, sid, cwd, parent=None):
        directory = HOME / "sessions" / ("-" + cwd.replace("/", "-") + "--") / sid
        self.path = directory / ("session.v4.jsonl" if PLAIN else "session.v4.jsonl.zstd")
        self.sid, self.cwd, self.parent = sid, cwd, parent
        self.seq = 0
        if self.path.exists():
            self.seq = sum(1 for _ in self.path.open()) - 1

    def append(self, kind, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        with self.path.open("a") as stream:
            if new:
                header = {"type": "session", "version": 4, "id": self.sid, "createdAt": int(time.time() * 1000), "cwd": self.cwd}
                if self.parent:
                    header.update(parentSession=self.parent, isSeeded=False, origin="subagent", delegationDepth=1)
                else:
                    header.update(isSeeded=False, delegationDepth=0)
                stream.write(json.dumps(header) + "\n")
            stream.write(json.dumps({"type": kind, "seq": self.seq, "time": int(time.time() * 1000), "data": data}) + "\n")
            self.seq += 1


sessions = {}  # sid -> {"cwd", "model", "effort", "turn", "log"}


def find_log(sid):
    for path in (HOME / "sessions").glob("*/" + sid + "/session.v4.jsonl"):
        return json.loads(path.open().readline())
    return None


def update(sid, value):
    send({"method": "session/update", "params": {"sessionId": sid, "update": value}})


def assistant(state, text, usage, *, model=None):
    message_id = str(uuid.uuid4())
    update(state["sid"], {"sessionUpdate": "agent_message_chunk", "messageId": message_id, "content": {"type": "text", "text": text}})
    update(state["sid"], {"sessionUpdate": "usage_update", "used": 5763, "size": 1000000})
    state["log"].append("assistant/message", {"turn": state["turn"], "step": 1, "message": {
        "role": "assistant", "content": [{"type": "text", "text": text}],
        "source": {"kind": "model", "provider": PROVIDER, "model": model or state["model"]}, "id": message_id},
        "usage": {**usage, "totalTokens": sum(usage.values())}})


def usage(inp, out, cache=0):
    return {"inputTokens": inp, "outputTokens": out, "cacheReadTokens": cache, "cacheWriteTokens": 0}


def read_request():
    raw = sys.stdin.readline()
    if not raw:
        finish_trace()
        sys.exit(0)
    return json.loads(raw)


def fail_turn(state, identifier, message, code, status=None, retries=0):
    for _ in range(retries):
        state["log"].append("llm/retry", {"provider": PROVIDER, "failure": {"code": code}})
    error = {"message": message, "code": code, **({"status": status} if status else {})}
    state["log"].append("turn/end", {"turn": state["turn"], "reason": {"kind": "error", "error": error}})
    send({"id": identifier, "error": {"code": -32603, "message": "Internal error: turn failed: " + message}})


def prompt(identifier, params):
    state = sessions[params["sessionId"]]
    sid = state["sid"]
    state["turn"] += 1
    state["log"].append("turn/start", {"turn": state["turn"]})
    if not KEY:
        return fail_turn(state, identifier, 'llm-deepseek: no API key for provider route "deepseek-official"; store DEEPSEEK_API_KEY '
                         "through the credentials service (the web Models page writes it), or export DEEPSEEK_API_KEY in the launching environment",
                         "MISSING_CREDENTIAL")
    if CASE == "quota":
        return fail_turn(state, identifier, "Insufficient Balance", "QUOTA", 402)
    if CASE == "ratelimit" or CASE == "ratelimit_once" and not (HOME / "fake-rate-limited").exists():
        (HOME / "fake-rate-limited").touch()
        return fail_turn(state, identifier, "Rate Limit Reached", "RATE_LIMIT", 429, retries=5)
    if CASE == "auth":
        return fail_turn(state, identifier, "Authentication Fails, Your api key: ****" + KEY[-4:] + " is invalid", "AUTH", 401)
    if CASE == "hang":
        time.sleep(3600)
    answer = SCENARIO.get("answer", "hi")
    if CASE == "leak":
        # As if the model ran `env` and the key were in it (real dsh keeps it out
        # of command environments): the same tool_call / tool_call_update /
        # agent_message_chunk shapes as the capture, each carrying the key.
        call = "toolu_env_1"
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": call, "title": "bash", "kind": "other", "status": "in_progress",
                     "rawInput": {"command": "env; echo " + KEY, "description": "check env"}})
        output = "PATH=/usr/bin:/bin\nDEEPSEEK_API_KEY=" + KEY + "\n" + KEY + "\n[exit code: 0]"
        update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": call, "status": "completed",
                     "content": [{"type": "content", "content": {"type": "text", "text": output}}]})
        state["log"].append("tool/result", {"turn": state["turn"], "step": 1, "message": {"role": "tool", "toolCallId": call,
                                            "content": [{"type": "text", "text": output}], "isError": False}})
        assistant(state, "The environment has DEEPSEEK_API_KEY=" + KEY + ".", usage(1200, 40, 800))
        if "write" in SCENARIO:
            Path(SCENARIO["write"]["path"]).write_text(SCENARIO["write"]["text"])
        answer = answer.replace("KEY", KEY)
    if CASE in {"tool", "escalate"}:
        call = "toolu_fake_1"
        target = SCENARIO.get("write", {"path": "made-by-dsh.txt", "text": "hello\n"})
        command = f"printf %s {json.dumps(target['text'])} > {target['path']} && cat {target['path']}"
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": call, "title": "bash", "kind": "other", "status": "in_progress",
                     "rawInput": {"command": command, "description": "write a small file"}})
        state["log"].append("tool/call", {"turn": state["turn"], "step": 1, "callId": call, "name": "bash",
                                          "arguments": json.dumps({"command": command, "description": "write a small file"})})
        outcome, text = "completed", target["text"]
        if CASE == "escalate" or MODE == "read-only":
            if MODE == "danger-full-access":
                Path(target["path"]).write_text(target["text"])
            else:
                # Real dsh: the sandbox refuses, the model asks to widen it, dsh asks the client.
                send({"id": 0, "method": "session/request_permission", "params": {"sessionId": sid, "toolCall": {"toolCallId": call},
                      "options": [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                                  {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}]}})
                reply = read_request()
                chosen = reply.get("result", {}).get("outcome", {}).get("optionId")
                TRACE["permission_answer"] = chosen
                if chosen == "allow-once":
                    Path(target["path"]).write_text(target["text"])
                else:
                    outcome = "failed"
                    text = ('Error: the user rejected escalating this command to "workspace-write"; it stays denied, '
                            "so stop and explain instead of working around it")
        else:
            Path(target["path"]).write_text(target["text"])
        update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": call, "status": outcome,
                     "content": [{"type": "content", "content": {"type": "text", "text": text}}]})
        state["log"].append("tool/result", {"turn": state["turn"], "step": 1, "message": {"role": "tool", "toolCallId": call,
                                            "content": [{"type": "text", "text": text}], "isError": outcome == "failed"}})
        assistant(state, "Running a command.", usage(1200, 40, 800))
    if CASE == "subagent":
        child_id = str(uuid.uuid4())
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": "toolu_sub_1", "title": "subagent", "kind": "other",
                     "status": "in_progress", "rawInput": {"description": "child check", "prompt": "CHILD-TASK"}})
        assistant(state, "Delegating.", usage(1200, 40, 800))
        child = {"sid": child_id, "turn": 1, "model": state["model"], "log": Log(child_id, state["cwd"], parent=sid)}
        child["log"].append("turn/start", {"turn": 1})
        child["log"].append("assistant/message", {"turn": 1, "step": 1, "message": {"role": "assistant", "content": [{"type": "text", "text": "child done"}],
                            "source": {"kind": "model", "provider": PROVIDER, "model": "deepseek-flash"}, "id": str(uuid.uuid4())},
                            "usage": {**usage(300, 3), "totalTokens": 303}})
        child["log"].append("turn/end", {"turn": 1, "reason": {"kind": "completed"}})
        update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "toolu_sub_1", "status": "completed",
                     "content": [{"type": "content", "content": {"type": "text", "text": json.dumps({"kind": "activation", "subagentId": child_id})}}]})
        state["log"].append("turn/end", {"turn": state["turn"], "reason": {"kind": "completed"}})
        state["turn"] += 1  # the child's completion notice starts another root turn
        state["log"].append("turn/start", {"turn": state["turn"]})
    assistant(state, answer, usage(1500, 7, 1000))
    state["log"].append("turn/end", {"turn": state["turn"], "reason": {"kind": "completed"}})
    send({"id": identifier, "result": {"stopReason": "end_turn"}})


def main():
    while True:
        request = read_request()
        method, identifier, params = request.get("method"), request.get("id"), request.get("params", {})
        TRACE["methods"].append(method)
        finish_trace()
        if method == "initialize":
            send({"id": identifier, "result": {"protocolVersion": 1, "agentInfo": {"name": "deepseek-harness-acp", "version": "0.0.1"},
                  "agentCapabilities": {"mcpCapabilities": {"http": True}, "promptCapabilities": {"image": False, "audio": False, "embeddedContext": False},
                                        "sessionCapabilities": {"close": {}, "list": {}, "resume": {}}}, "authMethods": []}})
        elif method == "authenticate":
            send({"id": identifier, "result": {}})
        elif method == "session/new":
            sid = str(uuid.uuid4())
            sessions[sid] = {"sid": sid, "cwd": params["cwd"], "model": "deepseek-v4-flash", "effort": "high", "turn": 0,
                             "log": Log(sid, params["cwd"])}
            preset = MODE
            sessions[sid]["log"].append("permission/preset", {"preset": preset})
            sessions[sid]["log"].append("sandbox/mode", {"mode": preset})
            sessions[sid]["log"].append("approval/policy", {"policy": "never" if preset == "danger-full-access" else "ask"})
            send({"id": identifier, "result": {"sessionId": sid, "configOptions": config_options()}})
        elif method == "session/resume":
            header = find_log(params["sessionId"])
            if header is None or header.get("cwd") != params["cwd"]:
                send({"id": identifier, "error": {"code": -32602, "message": "Invalid params: unknown session"}})
                continue
            sid = params["sessionId"]
            log = Log(sid, params["cwd"])
            turns = sum(1 for line in log.path.open() if '"turn/start"' in line)
            sessions[sid] = {"sid": sid, "cwd": params["cwd"], "model": "deepseek-v4-flash", "effort": "high", "turn": turns, "log": log}
            send({"id": identifier, "result": {"configOptions": config_options()}})
        elif method == "session/set_config_option":
            state = sessions.get(params.get("sessionId"))
            values = [choice["value"] for option in config_options() if option["id"] == params.get("configId")
                      for group in option["options"] for choice in (group.get("options") or [group])]
            if state is None or params.get("value") not in values:
                kind = "model" if params.get("configId") == "model" else "option"
                send({"id": identifier, "error": {"code": -32602, "message": f"Invalid params: unknown {kind} option: {params.get('value')}"}})
                continue
            if params["configId"] == "model":
                state["model"] = json.loads(params["value"])[1]
            else:
                state["effort"] = params["value"]
            TRACE["selected"][params["configId"]] = params["value"]
            send({"id": identifier, "result": {"configOptions": config_options(state["model"], state["effort"])}})
        elif method == "session/prompt":
            prompt(identifier, params)
        elif method == "session/close":
            send({"id": identifier, "result": {}})
        elif method == "session/cancel":
            pass
        elif identifier is not None:
            send({"id": identifier, "error": {"code": -32601, "message": f'"Method not found": {method}', "data": {"method": method}}})


main()
