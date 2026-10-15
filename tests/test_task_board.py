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
        registry.write_text(json.dumps({"obligations":[{"obligation_id":"ob1","status":"OPEN"}]}))
        self.config["obligation_registry"]=str(registry)
        self.write({"items":[{"id":"c1","registry_link":{"obligation_id":"ob1"}}]})
        self.assertEqual(len(json.loads(self.request()[1])["tasks"]),1)
    def test_roadmap_not_inferred(self):
        roadmap=self.root/"roadmap.json"
        roadmap.write_text(json.dumps({"projects":[{"id":"p","stages":[{"id":"s","observed_state":"original_host_pending","task_ids":["a"]}]}]}))
        self.config["roadmap"]=str(roadmap)
        self.write({"items":[{"id":"a","state":"DONE"}]})
        self.assertEqual(json.loads(self.request()[1])["projects"][0]["stages"][0]["observed_state"],"original_host_pending")
    def test_real_obligation_schema_overdue(self):
        p=self.root/"registry.json"
        p.write_text(json.dumps({"obligations":[{"obligation_id":"ob7","title":"SYNTHETIC obligation","status":"OPEN","deadline":"2000-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"}]}))
        self.config["obligation_registry"]=str(p)
        data=json.loads(self.request()[1])
        task=next(t for t in data["tasks"] if t["id"]=="ob7")
        self.assertEqual(task["title"],"SYNTHETIC obligation")
        self.assertTrue(task["overdue"])
    def test_registry_invalid_schema_visible(self):
        p=self.root/"registry.json";p.write_text('{"unexpected":[]}')
        self.config["obligation_registry"]=str(p)
        data=json.loads(self.request()[1])
        self.assertTrue(any(d["source"]=="obligation_registry" and d["status"]=="ERROR" for d in data["diagnostics"]))
    def test_registry_legacy_items_supported(self):
        p=self.root/"registry.json";p.write_text('{"items":[{"id":"legacy","state":"OPEN"}]}')
        self.config["obligation_registry"]=str(p)
        data=json.loads(self.request()[1])
        self.assertTrue(any(t["id"]=="legacy" for t in data["tasks"]))
        self.assertFalse(any(d["source"]=="obligation_registry" and d["status"]=="ERROR" for d in data["diagnostics"]))
    def test_dedupe_lifecycle_conflict(self):
        p=self.root/"registry.json";p.write_text(json.dumps({"obligations":[{"obligation_id":"ob1","status":"DONE"}]}))
        self.config["obligation_registry"]=str(p)
        self.write({"items":[{"id":"c1","state":"PLANNED","registry_link":{"obligation_id":"ob1"}}]})
        data=json.loads(self.request()[1])
        self.assertEqual(len(data["tasks"]),1)
        self.assertTrue(any(d["status"]=="CONFLICT" for d in data["diagnostics"]))
    def test_cron_name_and_scheduled(self):
        p=self.root/"cron.json";p.write_text(json.dumps({"jobs":[{"name":"SYNTHETIC nightly","enabled":True,"state":"IDLE","next_run_at":"2030-01-01T00:00:00Z","last_status":"DONE"}]}))
        self.config["cron_jobs"]=str(p)
        task=next(t for t in json.loads(self.request()[1])["tasks"] if t["source"]=="cron_jobs")
        self.assertEqual(task["title"],"SYNTHETIC nightly")
        self.assertEqual(task["executor_kind"],"cron")
        self.assertTrue(task["scheduled"])
        self.assertFalse(task["running"])
    def test_relay_real_backlog_status(self):
        state=self.root/"relaystate";state.mkdir()
        (state/"backlog.json").write_text(json.dumps({"items":[{"id":"child","status":"DONE","title":"SYNTHETIC child"}]}))
        (state/"executor_latest.json").write_text(json.dumps({"id":"executor","status":"PENDING","result":"waiting"}))
        job=self.root/"job.json";job.write_text(json.dumps({"state_dir":str(state)}))
        plan=self.root/"plan.json";plan.write_text(json.dumps({"jobs":[str(job)]}))
        self.config["relay_plan"]=str(plan)
        tasks=json.loads(self.request()[1])["tasks"]
        self.assertEqual(next(t for t in tasks if t["id"]=="child")["lifecycle"],"DONE")
        self.assertEqual(next(t for t in tasks if t["id"]=="executor")["lifecycle"],"PENDING")
        self.assertFalse(next(t for t in tasks if t["id"]=="child")["running"])
    def test_source_mtime_and_stale(self):
        import os,time
        os.utime(self.ledger,(time.time()-172800,time.time()-172800))
        diagnostic=next(d for d in json.loads(self.request()[1])["diagnostics"] if d["source"]=="commitment_ledger")
        self.assertTrue(diagnostic["stale"])
        self.assertIsNotNone(diagnostic["source_mtime"])
    def test_pid_start_token_mismatch(self):
        import os
        from unittest.mock import patch
        from cio_market_lab.observability.task_board import health
        if os.name!="posix": return
        with patch("pathlib.Path.exists",return_value=True),patch("pathlib.Path.read_text",return_value="123 (synthetic) S "+" ".join(["0"]*19)+" 456 "+" ".join(["0"]*5)):
            self.assertEqual(health("pid",123,"not-456"),"PID_REUSED")
    def test_pid_exists_not_genuine_running(self):
        import os
        self.write({"items":[{"id":"SYNTHETIC_PID","state":"DISPATCHED","handle":{"kind":"pid","ref":str(os.getpid())}}]})
        self.assertFalse(json.loads(self.request()[1])["tasks"][0]["running"])
    def test_string_dependency_is_atomic(self):
        self.write({"items":[{"id":"SYNTHETIC_DEP","state":"BLOCKED","blocked_by":"SYNTHETIC_EVENT"}]})
        self.assertEqual(json.loads(self.request()[1])["tasks"][0]["dependencies"],["SYNTHETIC_EVENT"])
    def test_registry_failed_history_survives_dedupe(self):
        p=self.root/"registry.json"
        p.write_text(json.dumps({"items":[{"id":"SYNTHETIC_FAILED","state":"FAILED_DEADLINE","deadline":"2000-01-01T00:00:00Z"}]}))
        self.config["obligation_registry"]=str(p)
        self.write({"items":[{"id":"SYNTHETIC_LINK","state":"VERIFIED","registry_link":{"obligation_id":"SYNTHETIC_FAILED"}}]})
        data=json.loads(self.request()[1])
        self.assertEqual(len(data["tasks"]),1)
        self.assertEqual(data["tasks"][0]["registry_lifecycle"],"FAILED_DEADLINE")
        self.assertTrue(any(d["status"]=="CONFLICT" for d in data["diagnostics"]))
    def test_secret_in_allowed_values_redacted(self):
        self.write({"items":[{"id":"x","text":"token=synthetic-secret","reason":"token=synthetic-secret","owner":"token=synthetic-secret","handle":{"kind":"manual","ref":"/home/synthetic/private"}}]})
        body=self.request()[1].decode()
        self.assertNotIn("synthetic-secret",body)
        self.assertNotIn("/home/synthetic/private",body)
    def test_relay_job_outside_root_rejected(self):
        allowed=self.root/"allowed";allowed.mkdir()
        other=self.root/"other";other.mkdir()
        (other/"job.json").write_text(json.dumps({"state_dir":str(other)}))
        (other/"backlog.json").write_text(json.dumps({"items":[{"id":"leak","title":"SYNTHETIC_PRIVATE"}]}))
        plan=allowed/"plan.json";plan.write_text(json.dumps({"jobs":[str(other/"job.json")]}))
        self.config["relay_plan"]=str(plan)
        data=json.loads(self.request()[1])
        self.assertFalse(any(t["id"]=="leak" for t in data["tasks"]))
        self.assertTrue(any(d["source"]=="relay_plan" and d["status"]=="ERROR" for d in data["diagnostics"]))
    def test_malformed_roadmap_isolated(self):
        p=self.root/"roadmap.json";p.write_text('{"projects":[{"id":"p","stages":null}]}')
        self.config["roadmap"]=str(p)
        data=json.loads(self.request()[1])
        self.assertEqual(data["tasks"][0]["id"],"a")
        self.assertTrue(any(d["source"]=="roadmap" and d["status"]=="ERROR" for d in data["diagnostics"]))
    def test_malformed_dependency_row_visible(self):
        self.write({"items":[{"id":"bad","blocked_by":True},{"id":"good","state":"PLANNED"}]})
        data=json.loads(self.request()[1])
        self.assertEqual([t["id"] for t in data["tasks"]],["good"])
        self.assertTrue(any(d["status"]=="ERROR" for d in data["diagnostics"]))
    def test_non_loopback_rejected(self):
        with self.assertRaises(ValueError): serve({},host="0.0.0.0",port=0)
    def test_iso_unix(self): self.assertEqual(timestamp(0),"1970-01-01T00:00:00+00:00")
if __name__=="__main__":unittest.main()
