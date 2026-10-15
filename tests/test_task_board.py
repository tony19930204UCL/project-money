import json
import threading
import tempfile
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from cio_market_lab.observability.task_board import serve, project, health, timestamp

class ObservatoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = self.root / "ledger.json"
        self.write({"items":[{"id":"a","text":"SYNTHETIC task","state":"PLANNED","handle":{"kind":"manual"}}]})
        self.config = {"commitment_ledger":str(self.ledger)}
        self.server = serve(self.config, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = "http://127.0.0.1:"+str(self.server.server_port)
    def write(self, obj):
        self.ledger.write_text(json.dumps(obj), encoding="utf-8")
    def request(self, path="/api/status", method="GET"):
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base+path,method=method),timeout=3) as r:
                return r.status,r.read(),r.headers
        except urllib.error.HTTPError as e:
            return e.code,e.read(),e.headers
    def test_http_json(self):
        code,data,_=self.request()
        self.assertEqual(code,200)
        self.assertEqual(json.loads(data)["tasks"][0]["id"],"a")
    def test_disk_refresh(self):
        self.write({"items":[{"id":"b","state":"DONE"}]})
        self.assertEqual(json.loads(self.request()[1])["tasks"][0]["id"],"b")
    def test_readonly_post(self): self.assertEqual(self.request(method="POST")[0],405)
    def test_readonly_put(self): self.assertEqual(self.request(method="PUT")[0],405)
    def test_readonly_delete(self): self.assertEqual(self.request(method="DELETE")[0],405)
    def test_traversal(self): self.assertEqual(self.request("/%2e%2e/secret")[0],404)
    def test_unknown_route(self): self.assertEqual(self.request("/config.json")[0],404)
    def test_security_headers(self):
        h=self.request()[2]
        self.assertEqual(h["Cache-Control"],"no-store")
        self.assertEqual(h["X-Content-Type-Options"],"nosniff")
    def test_secret_not_projected(self):
        self.write({"items":[{"id":"a","text":"safe","token":"SECRET_VALUE","transport_command":"SECRET_VALUE"}]})
        self.assertNotIn("SECRET_VALUE",self.request()[1].decode())
    def test_malformed_source(self):
        self.ledger.write_text("{")
        data=json.loads(self.request()[1])
        self.assertTrue(any(d["status"]=="ERROR" for d in data["diagnostics"]))
    def test_missing_source(self):
        self.ledger.unlink()
        self.assertTrue(any(d["status"]=="ERROR" for d in json.loads(self.request()[1])["diagnostics"]))
    def test_dead_pid(self): self.assertEqual(health("pid",999999999),"DEAD")
    def test_manual_unverified(self): self.assertEqual(health("manual",None),"UNVERIFIED")
    def test_cron_not_running(self):
        self.write({"items":[{"id":"c","state":"SCHEDULED","handle":{"kind":"cron","ref":"* * * * *"}}]})
        t=json.loads(self.request()[1])["tasks"][0]
        self.assertTrue(t["scheduled"]);self.assertFalse(t["running"])
    def test_overdue(self):
        self.write({"items":[{"id":"x","due":"2001-01-01T00:00:00Z","state":"PLANNED"}]})
        self.assertTrue(json.loads(self.request()[1])["tasks"][0]["overdue"])
    def test_acceptance_boundary(self):
        self.write({"items":[{"id":"x","state":"DONE","acceptance_scope":"source_accepted"}]})
        self.assertTrue(json.loads(self.request()[1])["tasks"][0]["awaiting_acceptance"])
    def test_link_dedupe(self):
        registry=self.root/"registry.json"
        registry.write_text(json.dumps({"items":[{"id":"ob1","state":"OPEN"}]}))
        self.config["obligation_registry"]=str(registry)
        self.write({"items":[{"id":"c1","registry_link":{"obligation_id":"ob1"}}]})
        self.assertEqual(len(json.loads(self.request()[1])["tasks"]),1)
    def test_roadmap_not_inferred(self):
        roadmap=self.root/"roadmap.json"
        roadmap.write_text(json.dumps({"projects":[{"id":"p","stages":[{"id":"s","observed_state":"original_host_pending","task_ids":["a"]}]}]}))
        self.config["roadmap"]=str(roadmap)
        self.write({"items":[{"id":"a","state":"DONE"}]})
        self.assertEqual(json.loads(self.request()[1])["projects"][0]["stages"][0]["observed_state"],"original_host_pending")
    def test_non_loopback_rejected(self):
        with self.assertRaises(ValueError): serve({},host="0.0.0.0",port=0)
    def test_iso_unix(self): self.assertEqual(timestamp(0),"1970-01-01T00:00:00+00:00")
if __name__=="__main__":unittest.main()
