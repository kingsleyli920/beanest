"""Real loopback run/accept/export routes with a non-network fake provider."""
import asyncio
import base64
import http.client
import io
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import quote
import zipfile

from local_agent_coordinator.room_server import create_room_server, publish_connection
from local_agent_coordinator.room_service import RoomService
from local_agent_coordinator.partner_manager import PartnerManager
from tests import test_team_service as fixtures
from wait_support import scaled

HAS_MCP = importlib.util.find_spec("mcp") is not None
if HAS_MCP:
    from local_agent_coordinator.room_mcp import RoomHTTPClient


class TeamRunHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.provider = fixtures.FakeTeamProvider()
        self.contents = {"main":"# A downloadable guide\n","checks":'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10" fill="green"/></svg>'}
        proposals = [{**fixtures.task("main","guide.md"),"capability":"text"},
                     {**fixtures.task("checks","chart.svg"),"capability":"text"}]
        self.provider.handler = fixtures.TeamScenario(self,proposals,{"main":"claude","checks":"codex"},self.contents)
        self.service = RoomService(self.root/"rooms.sqlite3",self.root/"workspaces",invoke=self.provider)
        self.server = create_room_server(self.service,port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,kwargs={"poll_interval":0.01})
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()
        self.temporary.cleanup()

    def request(self, method, path, value=None, *, mcp=False, token=None):
        connection = http.client.HTTPConnection("127.0.0.1",self.port,timeout=scaled(3))
        headers = {"X-LAC-Token":token or (self.server.mcp_token if mcp else self.server.token)}
        if mcp:
            headers["X-LAC-Instance"] = self.server.instance_id
        if value is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method,path,body=json.dumps(value) if value is not None else None,headers=headers)
            response = connection.getresponse()
            return response.status,json.loads(response.read())
        finally:
            connection.close()

    def create(self):
        code,snapshot = self.request("POST","/api/rooms",{"goal":"Prepare a guide and a static vector chart", "target":"all",
                                   "request_id":"http-team","mode":"text_artifacts","collaboration":True})
        self.assertEqual(code,200)
        return self.ready(snapshot["room"]["id"])

    def ready(self, room_id):
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            code,snapshot = self.request("GET","/api/rooms/"+room_id)
            self.assertEqual(code,200)
            if snapshot["current_run"]["can_continue"]:
                return snapshot
            if snapshot["team"]["phase"] in {"stopped","unknown"}:
                self.fail("Fixture stopped: "+str(snapshot["team"]["stop_reason"]))
            time.sleep(0.005)
        self.fail("Fixture did not settle")

    def test_run_accept_and_history_are_real_http_mutations_with_exact_markers(self):
        first = self.create()
        room = first["room"]["id"]
        run = first["current_run"]
        evidence = {"run_id":run["id"],"revision":run["revision"],"digest":run["digest"]}
        code,accepted = self.request("POST",f"/api/rooms/{room}/accept",{"request_id":"accept",**evidence})
        self.assertEqual(code,200)
        self.assertIsNotNone(accepted["current_run"]["accepted_at"])
        code,_ = self.request("POST",f"/api/mcp/rooms/{room}/accept",{"request_id":"agent-accept",**evidence},mcp=True)
        self.assertEqual(code,403)
        body={"text":"Keep both files and explain them again","target":"all","request_id":"next",**evidence,"retry":False}
        code,second = self.request("POST",f"/api/rooms/{room}/runs",body)
        self.assertEqual(code,200)
        self.assertEqual(second["current_run"]["number"],2)
        self.ready(room)
        count=len(self.provider.calls)
        self.assertEqual(self.request("POST",f"/api/rooms/{room}/runs",body)[0],200)
        self.assertEqual(len(self.provider.calls),count)
        self.assertEqual(self.request("POST",f"/api/rooms/{room}/runs",{**body,"request_id":"stale"})[0],409)
        code,history=self.request("GET",f"/api/rooms/{room}/runs?run_id="+run["id"])
        self.assertEqual(code,200)
        self.assertEqual(history["run"]["status"],"accepted")
        self.assertEqual(len(history["team"]["tasks"]),2)

    def test_download_and_svg_preview_return_only_verified_public_file_data(self):
        snapshot=self.create()
        room,revision=snapshot["room"]["id"],snapshot["project"]["revision"]
        count=len(self.provider.calls)
        code,export=self.request("GET",f"/api/rooms/{room}/export?revision={revision}")
        self.assertEqual(code,200)
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(export["content_base64"]))) as archive:
            self.assertEqual(archive.read("guide.md").decode(),self.contents["main"])
            self.assertEqual(archive.read("chart.svg").decode(),self.contents["checks"])
            self.assertFalse(any("session" in name or "messages" in name for name in archive.namelist()))
        code,preview=self.request("GET",f"/api/rooms/{room}/preview?revision={revision}&path=chart.svg")
        self.assertEqual(code,200)
        self.assertEqual(preview["content_type"],"image/svg+xml")
        for query in ("", "?revision=true",f"?revision={revision}&revision={revision}",f"?revision={revision}&extra=1"):
            self.assertEqual(self.request("GET",f"/api/rooms/{room}/export"+query)[0],400)
        self.assertEqual(self.request("GET",f"/api/rooms/{room}/export?revision={revision-1}")[0],409)
        self.assertEqual(self.request("GET",f"/api/rooms/{room}/preview?revision={revision}&path="+quote("../chart.svg"))[0],409)
        self.assertEqual(len(self.provider.calls),count)

    @unittest.skipUnless(HAS_MCP,"Install MCP extra to exercise its HTTP client")
    def test_mcp_client_explicit_continue_uses_the_same_contract_without_user_acceptance(self):
        first=self.create()
        current=first["current_run"]
        client=RoomHTTPClient(publish_connection(self.server,self.root))
        value=asyncio.run(client.continue_run(first["room"]["id"],"Another explicit goal","all","mcp-next",
                        run_id=current["id"],revision=current["revision"],digest=current["digest"]))
        self.assertEqual(value["current_run"]["number"],2)
        value=self.ready(first["room"]["id"])
        self.assertTrue(any(event["kind"]=="run_started" and event["sender"]=="mcp" for event in value["events"]))
        archive=asyncio.run(client.export(first["room"]["id"],value["project"]["revision"]))
        self.assertEqual(archive["files_count"],2)


    def test_mcp_capability_cannot_be_used_as_user_acceptance_or_configuration(self):
        first = self.create()
        run, room = first["current_run"], first["room"]["id"]
        published = json.loads(publish_connection(self.server,self.root).read_text())
        self.assertEqual(published["token"], self.server.mcp_token)
        self.assertNotEqual(published["token"], self.server.token)
        evidence = {"request_id":"credential-confusion", "run_id":run["id"],
                    "revision":run["revision"], "digest":run["digest"]}
        for method,path,body in (("POST",f"/api/rooms/{room}/accept",evidence),
                                 ("POST","/api/partners/discover",{}),
                                 ("GET","/api/status",None)):
            code,value = self.request(method,path,body,token=published["token"])
            self.assertEqual((code,value),(403,{"error":"token_scope_rejected"}))
        code,value = self.request("GET","/api/mcp/status",mcp=True,token=self.server.token)
        self.assertEqual((code,value),(403,{"error":"token_scope_rejected"}))
        code,unchanged = self.request("GET",f"/api/rooms/{room}")
        self.assertEqual(code,200)
        self.assertIsNone(unchanged["current_run"]["accepted_at"])
        self.assertFalse(any(event["kind"] == "run_accepted" for event in unchanged["events"]))
        self.assertEqual(self.request("POST",f"/api/mcp/rooms/{room}/accept",evidence,mcp=True)[0],403)
        code,accepted = self.request("POST",f"/api/rooms/{room}/accept",evidence)
        self.assertEqual(code,200)
        self.assertIsNotNone(accepted["current_run"]["accepted_at"])
        self.assertEqual(next(event for event in accepted["events"] if event["kind"]=="run_accepted")["sender"],"user")

    def test_partner_configuration_and_discovery_are_user_only_and_mcp_redacts_connection(self):
        self.service.partner_manager = PartnerManager(self.root / "partners", self.service.registry)
        prefix = "local_agent_coordinator.partner_manager.adapters."
        connection = {"protocol":"beanest-json-v1", "executable":"/private/user/connector",
                      "args":["--private-user-argument"], "billing":"local"}
        with patch(prefix+"find_claude",return_value="/virtual/claude"), \
             patch(prefix+"find_codex",return_value="/virtual/codex"), \
             patch(prefix+"find_json_cli",return_value="/virtual/connector"), \
             patch(prefix+"login_confirmed",new=AsyncMock(return_value=True)), \
             patch(prefix+"_run",side_effect=AssertionError("No native commands in this test")), \
             patch(prefix+"check_json_cli",new=AsyncMock(return_value={"ready":True,"version":"1.0","billing":"local"})):
            code,discovery = self.request("POST","/api/partners/discover",{})
            self.assertEqual(code,200)
            self.assertEqual(discovery["discovery"]["state"],"completed")
            code,created = self.request("POST","/api/partners",{"id":"helper","name":"Helper","adapter":"local_json"})
            self.assertEqual(code,200)
            self.assertFalse(next(item for item in created["partners"] if item["id"]=="helper")["selectable"])
            code,configured = self.request("POST","/api/partners/helper/connection",{"connection":connection})
            self.assertEqual(code,200)
            self.assertEqual(next(item for item in configured["partners"] if item["id"]=="helper")["connection"],connection)
            self.assertNotIn("helper",self.service.store.agents)
            code,checked = self.request("POST","/api/partners/helper/check",{})
            self.assertEqual(code,200)
            self.assertTrue(next(item for item in checked["partners"] if item["id"]=="helper")["selectable"])
            self.assertIn("helper",self.service.store.agents)
            self.assertEqual(self.request("POST","/api/partners/helper/connection",{"connection":{**connection,"args":[]}})[0],409)
            self.assertEqual(self.request("POST","/api/partners/helper/connection",{"connection":{}})[0],400)
            for path,body in (("/api/mcp/partners/discover",{}),("/api/mcp/partners/helper/connection",{"connection":connection})):
                self.assertEqual(self.request("POST",path,body,mcp=True)[0],404)
            code,public = self.request("GET","/api/mcp/partners",mcp=True)
            self.assertEqual(code,200)
            encoded = json.dumps(public)
            self.assertNotIn(connection["executable"],encoded)
            self.assertNotIn(connection["args"][0],encoded)
            self.assertTrue(all("connection" not in item for item in public["partners"]))
        self.assertEqual(self.provider.calls,[])

if __name__ == "__main__":
    unittest.main()
