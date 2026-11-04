"""Exercise the real stdio protocol against a fake local CLI, without a model."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

HAS_MCP = importlib.util.find_spec("mcp") is not None
if HAS_MCP:
    from mcp import Client, StdioServerParameters


FAKE_CLI = r'''
import json,pathlib,sys
p=pathlib.Path(__file__).with_suffix('.state')
d=json.loads(p.read_text()) if p.exists() else {'creates':0,'live':False}
operation=sys.argv[3]
sid='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
if operation=='create':
 d['creates']+=1;d['live']=True
 out={'kind':'chat','sessionId':sid}
elif operation=='send':
 out={'sessionId':sid,'queued':False}
elif operation=='close':
 d['live']=False;out={'sessionId':sid,'closed':True}
elif operation=='list':
 out=[{'sessionId':sid,'status':'idle','live':d['live'],'harness':'codex-acp'},
      {'sessionId':'foreign-session-do-not-export','status':'running','live':True}]
elif operation=='read':
 def event(seq,value):return {'cursor':{'epoch':'E','seq':seq},'event':value}
 out={'ok':True,'nextBefore':None,'envelopes':[
  event(1,{'type':'session','session':{'status':'idle','modeId':'read-only'}}),
  event(2,{'type':'turn','turn':{'id':'t1','status':'completed'}}),
  event(3,{'type':'item','item':{'id':'r1','kind':'reasoning','text':'DO_NOT_EXPORT_THOUGHT'}}),
  event(4,{'type':'item','item':{'id':'tool1','kind':'tool_call','toolName':'read','status':'completed','title':'RAW_ARG_SECRET','arguments':{'key':'RAW_ARG_SECRET'}}}),
  event(5,{'type':'item','item':{'id':'m1','kind':'agent_message','text':'visible answer'}})]}
else:raise SystemExit(1)
p.write_text(json.dumps(d));print(json.dumps(out))
'''


@unittest.skipUnless(HAS_MCP, "Install the bridge extra to test the real MCP protocol")
class MCPProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_ownership_roundtrip_and_private_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            (repo / "scripts").mkdir(parents=True)
            (repo / "scripts/dev-cli.ts").write_text("// fake entrypoint")
            executable = root / "fake-bun"
            executable.write_text(f"#!{sys.executable}\n" + FAKE_CLI)
            executable.chmod(0o700)
            config = root / "bridge.json"
            config.write_text(json.dumps({"paradev_repo": str(repo), "bun": str(executable),
                                          "workspace_id": str(uuid4()),
                                          "state_path": str(root / "private/tasks.sqlite")}))
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "local_agent_coordinator.mcp_server", "--config", str(config)],
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            )
            async with Client(parameters, read_timeout_seconds=15) as client:
                tools = await client.list_tools()
                self.assertEqual({t.name for t in tools.tools},
                                 {"coordinator_status", "task_list", "task_start", "task_read", "task_reply", "task_cancel"})
                started = await client.call_tool("task_start", {"agent": "codex", "prompt": "A text task", "request_id": "same-request"})
                self.assertFalse(started.is_error)
                task_id = started.structured_content["task"]["id"]
                repeated = await client.call_tool("task_start", {"agent": "codex", "prompt": "A text task", "request_id": "same-request"})
                self.assertEqual(repeated.structured_content["task"]["id"], task_id)
                self.assertEqual(json.loads(executable.with_suffix('.state').read_text())["creates"], 1)
                read = await client.call_tool("task_read", {"task_id": task_id})
                self.assertFalse(read.is_error)
                self.assertEqual(read.structured_content["task"]["status"], "idle")
                wire = json.dumps(read.structured_content)
                self.assertIn("visible answer", wire)
                self.assertNotIn("DO_NOT_EXPORT_THOUGHT", wire)
                self.assertNotIn("RAW_ARG_SECRET", wire)
                rejected = await client.call_tool("task_read", {"task_id": str(uuid4())})
                self.assertTrue(rejected.is_error)
                status = await client.call_tool("coordinator_status", {})
                self.assertEqual(status.structured_content["owned_live_sessions"], 1)
                self.assertNotIn("foreign-session", json.dumps(status.structured_content))
                cancelled = await client.call_tool("task_cancel", {"task_id": task_id})
                self.assertTrue(cancelled.structured_content["cancellation_confirmed"])
            # A new Python server reads the same private task/message records without dispatch.
            async with Client(parameters, read_timeout_seconds=15) as client:
                cached = await client.call_tool("task_read", {"task_id": task_id, "refresh": False})
                self.assertFalse(cached.is_error)
                self.assertEqual(cached.structured_content["task"]["status"], "cancelled")
                self.assertIn("visible answer", json.dumps(cached.structured_content))
                self.assertEqual(json.loads(executable.with_suffix('.state').read_text())["creates"], 1)
