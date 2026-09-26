"""Focused policy and HTTP-contract tests; no network dependencies."""
import copy
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib import request, error
import bot

ROOT = Path(__file__).parent
def read(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))
CATEGORIES = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in (ROOT / "dataset/categories").glob("*.json")}
MERCHANTS = read("dataset/merchants_seed.json")["merchants"]
CUSTOMERS = read("dataset/customers_seed.json")["customers"]
TRIGGERS = read("dataset/triggers_seed.json")["triggers"]
NOW = "2026-04-26T10:00:00Z"


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.e = bot.Engine()
        self.m = copy.deepcopy(MERCHANTS[0])
        self.cat = copy.deepcopy(CATEGORIES["dentists"])
        self.t = copy.deepcopy(TRIGGERS[0])
        self.load("category", self.cat["slug"], self.cat)
        self.load("merchant", self.m["merchant_id"], self.m)
        self.load("trigger", self.t["id"], self.t)

    def load(self, scope, cid, payload, version=1):
        return self.e.push(dict(scope=scope, context_id=cid, version=version, payload=payload, delivered_at=NOW))

    def tick(self, tids=None, now=NOW):
        return self.e.tick(dict(now=now, available_triggers=tids or [self.t["id"]]))["actions"]

    def reply(self, text, conv="standalone", turn=2, **kw):
        body = dict(conversation_id=conv, merchant_id=self.m["merchant_id"], from_role="merchant",
                    message=text, received_at=NOW, turn_number=turn)
        body.update(kw)
        return self.e.reply(body)

    def test_compose_determinism_and_grounding(self):
        a = bot.compose(self.cat, self.m, self.t)
        self.assertEqual(a, bot.compose(self.cat, self.m, self.t))
        self.assertIn("2100", a["body"])
        self.assertIn("JIDA", a["body"])
        self.assertNotIn("complimentary", a["body"])
        self.assertIn("YES", a["body"])
        self.assertIn("Namaste Dr. Meera", a["body"])

    def test_same_version_noop_and_lower_conflict(self):
        a = self.load("merchant", self.m["merchant_id"], self.m, 3)
        modified = copy.deepcopy(self.m)
        modified["performance"]["views"] = 999999
        self.assertEqual(a, self.load("merchant", self.m["merchant_id"], modified, 3))
        self.assertNotEqual(999999, self.e.get("merchant", self.m["merchant_id"])["performance"]["views"])
        with self.assertRaises(bot.APIError) as cm:
            self.load("merchant", self.m["merchant_id"], self.m, 2)
        self.assertEqual(409, cm.exception.status)

    def test_category_update_used(self):
        self.cat["digest"][0]["summary"] = "Updated supplied study result: 17%."
        self.load("category", "dentists", self.cat, 2)
        self.assertIn("17%", self.tick()[0]["body"])

    def test_merchant_update_used(self):
        t = copy.deepcopy(TRIGGERS[3])
        t["merchant_id"] = self.m["merchant_id"]
        self.load("trigger", t["id"], t)
        self.m["performance"]["views"] = 7654
        self.load("merchant", self.m["merchant_id"], self.m, 2)
        self.assertIn("7654", self.tick([t["id"]])[0]["body"])

    def test_expired_trigger(self):
        self.assertEqual([], self.tick(now="2027-01-01T00:00:00Z"))

    def test_duplicate_suppression_survives_version(self):
        self.assertEqual(1, len(self.tick()))
        self.load("trigger", self.t["id"], self.t, 2)
        self.assertEqual([], self.tick())

    def test_concurrent_duplicate_tick(self):
        with ThreadPoolExecutor(max_workers=10) as pool:
            actions = list(pool.map(lambda _: self.tick(), range(10)))
        self.assertEqual(1, sum(map(len, actions)))

    def test_initial_template_renders_exact_body(self):
        action = self.tick()[0]
        template = bot.TEMPLATES[action["template_name"]]
        for i, part in enumerate(action["template_params"], 1):
            template = template.replace("{{" + str(i) + "}}", part)
        self.assertEqual(action["body"], template)

    def test_human_session_open_and_exact_boundary(self):
        self.reply("Hello")
        self.assertIsNone(self.tick()[0]["template_name"])
        e = bot.Engine()
        self.e = e
        self.load("category", "dentists", self.cat)
        self.load("merchant", self.m["merchant_id"], self.m)
        self.load("trigger", self.t["id"], self.t)
        self.reply("Hello")
        self.assertIsNotNone(self.tick(now="2026-04-27T10:00:00Z")[0]["template_name"])

    def test_auto_reply_closes_without_session(self):
        result = self.reply("Thank you for contacting us! Our team will respond shortly.")
        self.assertEqual("end", result["action"])
        self.assertEqual({}, self.e.sessions)
        self.assertEqual([], self.tick())

    def test_optout_blocks_other_conversations_and_context_refresh(self):
        self.assertEqual("end", self.reply("STOP")["action"])
        self.load("merchant", self.m["merchant_id"], self.m, 2)
        self.assertEqual([], self.tick())
        self.assertEqual("end", self.reply("yes", conv="different")["action"])

    def test_customer_optout_does_not_optout_merchant(self):
        c = copy.deepcopy(CUSTOMERS[0])
        self.load("customer", c["customer_id"], c)
        r = self.reply("STOP", customer_id=c["customer_id"], from_role="customer")
        self.assertEqual("end", r["action"])
        self.assertEqual(1, len(self.tick()))

    def test_consent_scope_channel_and_revocation(self):
        c, t = copy.deepcopy(CUSTOMERS[0]), copy.deepcopy(TRIGGERS[2])
        self.assertTrue(bot.compose(self.cat, self.m, t, c)["body"])
        for change in ("scope", "revoked", "channel", "preference"):
            bad = copy.deepcopy(c)
            if change == "scope": bad["consent"]["scope"] = ["promotional_offers"]
            if change == "revoked": bad["consent"]["revoked_at"] = NOW
            if change == "channel": bad["preferences"]["channel"] = "email"
            if change == "preference": bad["preferences"]["reminder_opt_in"] = False
            self.assertEqual("", bot.compose(self.cat, self.m, t, bad)["body"], change)

    def test_customer_missing_and_wrong_merchant(self):
        self.assertFalse(bot.compose(self.cat, self.m, TRIGGERS[2])["body"])
        c = copy.deepcopy(CUSTOMERS[0])
        c["merchant_id"] = "wrong"
        self.assertFalse(bot.compose(self.cat, self.m, TRIGGERS[2], c)["body"])

    def test_no_fake_slots_or_promotional_reminder(self):
        t = copy.deepcopy(TRIGGERS[2])
        t["payload"].pop("available_slots")
        a = bot.compose(self.cat, self.m, t, CUSTOMERS[0])
        self.assertNotIn("₹", a["body"])
        self.assertNotIn("slot", a["body"])
        self.assertNotIn("complimentary", a["body"])
        self.assertIn("2026-11-12", a["body"])

    def test_slots_use_iso_not_wrong_fixture_weekdays(self):
        a = bot.compose(self.cat, self.m, TRIGGERS[2], CUSTOMERS[0])
        self.assertIn("2026-11-05T18:00", a["body"])
        self.assertNotIn("Wed", a["body"])

    def test_missing_digest_reference_never_uses_unrelated_item(self):
        self.t["payload"]["top_item_id"] = "unknown"
        self.assertFalse(bot.compose(self.cat, self.m, self.t)["body"])

    def test_url_removed_and_taboos(self):
        self.cat["digest"][0]["title"] += " https://example.com/research"
        self.assertNotIn("https://", bot.compose(self.cat, self.m, self.t)["body"])
        self.cat["digest"][0]["title"] = "guaranteed results"
        self.assertFalse(bot.compose(self.cat, self.m, self.t)["body"])

    def test_acceptance_produces_draft_without_execution_claim(self):
        r = self.reply("Ok lets do it. Whats next?")
        self.assertEqual("send", r["action"])
        self.assertIn("Draft", r["body"])
        self.assertIn("nothing has been published", r["body"])
        self.assertNotIn("would you", r["body"].lower())

    def test_acceptance_after_context_update(self):
        conv = self.tick()[0]["conversation_id"]
        self.cat["digest"][0]["summary"] = "New supplied abstract: 17%."
        self.load("category", "dentists", self.cat, 2)
        r = self.reply("yes", conv=conv)
        self.assertIn("17%", r["body"])

    def test_reply_retry_idempotent_and_conflict(self):
        r = self.reply("Hello")
        self.assertEqual(r, self.reply("Hello"))
        with self.assertRaises(bot.APIError):
            self.reply("Changed")

    def test_reply_identity_mismatch(self):
        conv = self.tick()[0]["conversation_id"]
        with self.assertRaises(bot.APIError):
            self.reply("yes", conv=conv, merchant_id="wrong")

    def test_reply_optional_identity_resolved(self):
        conv = self.tick()[0]["conversation_id"]
        body = dict(conversation_id=conv, from_role="merchant", message="yes", received_at=NOW, turn_number=2)
        self.assertEqual("send", self.e.reply(body)["action"])

    def test_wait_suppresses_ticks(self):
        self.assertEqual("wait", self.reply("busy, later")["action"])
        self.assertEqual([], self.tick())

    def test_objection_and_off_topic(self):
        self.assertIn("price", self.reply("too expensive")["body"].lower())
        self.assertEqual("none", self.reply("help with GST", conv="gst")["cta"])

    def test_no_repeat_after_acceptance(self):
        self.reply("yes")
        self.assertEqual("send", self.reply("confirm", turn=3)["action"])
        self.assertEqual("end", self.reply("confirm", turn=4)["action"])

    def test_imminent_milestone_not_claimed_reached(self):
        m = MERCHANTS[5]
        a = bot.compose(CATEGORIES["restaurants"], m, TRIGGERS[11])
        self.assertIn("Still 5 short", a["body"])

    def test_ipl_does_not_extend_offer_or_claim_saturday(self):
        a = bot.compose(CATEGORIES["restaurants"], MERCHANTS[4], TRIGGERS[9])
        self.assertIn("Tue-Thu", a["body"])
        self.assertNotIn("Saturday", a["body"])
        self.assertNotIn("delivery-only", a["body"])

    def test_placeholder_does_not_invent_competitor(self):
        self.t.update(kind="competitor_opened", payload={"placeholder": True})
        self.assertFalse(bot.compose(self.cat, self.m, self.t)["body"])

    def test_action_limit(self):
        tids = []
        for i in range(25):
            m, t = copy.deepcopy(self.m), copy.deepcopy(self.t)
            m["merchant_id"] = f"merchant_{i}"
            t.update(id=f"trigger_{i}", merchant_id=m["merchant_id"])
            self.load("merchant", m["merchant_id"], m)
            self.load("trigger", t["id"], t)
            tids.append(t["id"])
        self.assertEqual(20, len(self.tick(tids)))
        self.assertEqual(5, len(self.tick(tids)))

    def test_teardown_clears_all_state(self):
        self.tick()
        self.reply("STOP")
        self.e.reset()
        self.assertEqual(dict.fromkeys(bot.SCOPES, 0), self.e.health()["contexts_loaded"])
        self.assertFalse(self.e.optouts or self.e.conversations or self.e.suppressed or self.e.reply_cache)

    def test_consent_optout_in_history(self):
        self.m["conversation_history"].append({"from": "merchant", "body": "STOP"})
        self.assertFalse(bot.compose(self.cat, self.m, self.t)["body"])

    def test_known_regional_customer_language(self):
        c = CUSTOMERS[11]
        a = bot.compose(CATEGORIES["gyms"], MERCHANTS[7], TRIGGERS[16], c)
        self.assertIn("Vanakkam Sumitra", a["body"])
        self.assertIn("anuppunga", a["body"])



    def test_future_or_expired_consent_suppresses_tick(self):
        c, t = copy.deepcopy(CUSTOMERS[0]), copy.deepcopy(TRIGGERS[2])
        c["consent"]["opted_in_at"] = "2027-01-01"
        self.load("customer", c["customer_id"], c)
        self.load("trigger", t["id"], t)
        self.assertEqual([], self.tick([t["id"]]))
        c["consent"]["opted_in_at"] = "2025-01-01"
        c["consent"]["expires_at"] = "2026-01-01T00:00:00Z"
        self.load("customer", c["customer_id"], c, 2)
        self.assertEqual([], self.tick([t["id"]]))

    def test_reply_taboos_apply_after_context_update(self):
        conv = self.tick()[0]["conversation_id"]
        self.cat["digest"][0]["summary"] = "guaranteed results"
        self.load("category", "dentists", self.cat, 2)
        self.assertEqual("end", self.reply("yes", conv=conv)["action"])

    def test_invalid_urgency_rejected_on_push(self):
        self.t["urgency"] = "high"
        with self.assertRaises(bot.APIError) as cm:
            self.load("trigger", self.t["id"], self.t, 2)
        self.assertEqual(400, cm.exception.status)

    def test_renewal_acceptance_uses_renewal_artifact(self):
        m = copy.deepcopy(MERCHANTS[1])
        t = copy.deepcopy(TRIGGERS[4])
        self.load("merchant", m["merchant_id"], m)
        self.load("trigger", t["id"], t)
        conv = self.tick([t["id"]])[0]["conversation_id"]
        r = self.reply("yes", conv=conv, merchant_id=m["merchant_id"])
        self.assertIn("4999", r["body"])
        self.assertIn("No renewal", r["body"])


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bot.ENGINE = bot.Engine()
        cls.server = bot.Server(("127.0.0.1", 0), bot.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def call(self, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = request.Request(self.url + path, data=data, headers={"Content-Type": "application/json"})
        try:
            response = request.urlopen(req, timeout=2)
        except error.HTTPError as e:
            response = e
        with response:
            return response.status, json.load(response)

    def test_required_routes_and_teardown(self):
        self.call("/v1/teardown", {})
        self.assertEqual(200, self.call("/v1/healthz")[0])
        self.assertEqual(200, self.call("/v1/metadata")[0])
        self.assertEqual((200, {"actions": []}), self.call("/v1/tick", dict(now=NOW, available_triggers=[])))
        p = dict(scope="category", context_id="dentists", version=1, payload=CATEGORIES["dentists"], delivered_at=NOW)
        self.assertEqual(200, self.call("/v1/context", p)[0])
        p["version"] = 0
        self.assertEqual(400, self.call("/v1/context", p)[0])
        self.assertEqual((200, {"cleared": True}), self.call("/v1/teardown", {}))

    def test_malformed_json_and_scope(self):
        self.assertEqual(400, self.call("/v1/context", raw=b"{oops")[0])
        self.assertEqual(400, self.call("/v1/context", {"scope": "wrong"})[0])
        self.assertEqual(400, self.call("/v1/tick", {"now": NOW, "available_triggers": [1]})[0])

    def test_payload_cap(self):
        status, data = self.call("/v1/context", raw=b"x" * (500 * 1024 + 1))
        self.assertEqual(413, status)

    def test_unknown_route(self):
        self.assertEqual(404, self.call("/unknown")[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
