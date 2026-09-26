"""Reproducible submission generation, HTTP verification, and official harness adapter."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib import request, error

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import bot

def load(path):
    return json.loads(path.read_text(encoding="utf-8"))

def http(base, path, body=None):
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = request.Request(base.rstrip("/") + path, data=raw, headers={"Content-Type": "application/json"})
    start = time.perf_counter()
    try:
        response = request.urlopen(req, timeout=10)
    except error.HTTPError as exc:
        response = exc
    with response:
        return response.status, json.load(response), (time.perf_counter() - start) * 1000

def generate():
    expanded = ROOT / "expanded"
    pairs = load(expanded / "test_pairs.json")["pairs"]
    assert len(pairs) == 30
    rows = []
    for p in pairs:
        m = load(expanded / "merchants" / (p["merchant_id"] + ".json"))
        t = load(expanded / "triggers" / (p["trigger_id"] + ".json"))
        cat = load(expanded / "categories" / (m["category_slug"] + ".json"))
        c = load(expanded / "customers" / (p["customer_id"] + ".json")) if p.get("customer_id") else None
        rows.append({"test_id": p["test_id"], **bot.compose(cat, m, t, c)})
    (ROOT / "submission.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    counts = {s: len(list((expanded / s).glob("*.json"))) for s in ("categories", "merchants", "customers", "triggers")}
    report = {"dataset_counts": counts, "canonical_rows": len(rows), "sendable": sum(bool(r["body"]) for r in rows),
              "suppressed": [{"test_id": r["test_id"], "reason": r["rationale"]} for r in rows if not r["body"]],
              "note": "Canonical compose artifacts are timeless; Engine.tick additionally enforces expiry, cadence and dedup."}
    save("submission-report.json", report)
    print(json.dumps(report, indent=2))

def save(name, data):
    path = ROOT / "results"
    path.mkdir(exist_ok=True)
    (path / name).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

def verify(base, reset):
    if not reset:
        raise SystemExit("Verification resets test state; use --reset only before/after judging.")
    records = []
    def check(path, body=None, expected=200):
        code, data, latency = http(base, path, body)
        assert code == expected, (path, code, data)
        records.append({"path": path, "status": code, "milliseconds": round(latency, 2)})
        return data
    check("/v1/teardown", {})
    try:
        health = check("/v1/healthz")
        assert health["contexts_loaded"] == dict.fromkeys(bot.SCOPES, 0)
        metadata = check("/v1/metadata")
        assert {"team_name", "team_members", "model", "approach", "contact_email", "version", "submitted_at"} <= metadata.keys()
        now = "2026-04-26T10:00:00Z"
        for scope, dirname in (("category", "categories"), ("merchant", "merchants"), ("customer", "customers"), ("trigger", "triggers")):
            for path in sorted((ROOT / "expanded" / dirname).glob("*.json")):
                payload = load(path)
                check("/v1/context", dict(scope=scope, context_id=path.stem, version=1, payload=payload, delivered_at=now))
        assert check("/v1/healthz")["contexts_loaded"] == dict(category=5, merchant=50, customer=200, trigger=100)
        tid = "trg_001_research_digest_dentists"
        tick = check("/v1/tick", {"now": now, "available_triggers": [tid]})
        assert len(tick["actions"]) == 1
        a = tick["actions"][0]
        assert a["template_name"] and a["body"]
        assert check("/v1/tick", {"now": now, "available_triggers": [tid]})["actions"] == []
        reply = dict(conversation_id=a["conversation_id"], merchant_id=a["merchant_id"], customer_id=None,
                     from_role="merchant", message="Yes, send the abstract", received_at=now, turn_number=2)
        assert check("/v1/reply", reply)["action"] == "send"
        reply.update(message="STOP", turn_number=3)
        assert check("/v1/reply", reply)["action"] == "end"
        report = {"base_url": base, "status": "PASS", "requests": len(records),
                  "maximum_ms": max(r["milliseconds"] for r in records), "records": records,
                  "public_https": base.startswith("https://")}
    finally:
        check("/v1/teardown", {})
    save("public-verification.json" if base.startswith("https://") else "local-verification.json", report)
    print(json.dumps({k: v for k, v in report.items() if k != "records"}, indent=2))

def run_judge(base, operational, scenario, simulated_now):
    import judge_simulator as judge
    judge.BOT_URL = base
    judge.LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai")
    judge.LLM_API_KEY = os.getenv("LLM_API_KEY", "")
    judge.LLM_MODEL = os.getenv("LLM_MODEL", "")
    judge.OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
    judge.TEST_SCENARIO = scenario
    if not operational:
        # Use official main and scoring unchanged; keys exist only in environment.
        judge.main()
        return
    class NoScoringProvider(judge.LLMProvider):
        def name(self):
            return "NONE — operational checks only; no LLM score"
        def complete(self, *args, **kwargs):
            raise RuntimeError("Scoring unavailable in operational mode")
    class OperationalJudge(judge.JudgeSimulator):
        def __init__(self):
            super().__init__(NoScoringProvider())
            self.observed = 0
        def _score_and_display(self, action, verbose=True):
            assert action.get("body") and action.get("trigger_id")
            self.observed += 1
            judge.print_info("Action schema observed; quality score deliberately not calculated")
    harness = OperationalJudge()
    if simulated_now:
        class FixtureClient(judge.BotClient):
            def tick(self, triggers):
                return self._request("POST", "/v1/tick", 15,
                                     {"now": simulated_now, "available_triggers": triggers})
        harness.client = FixtureClient(base)
    http(base, "/v1/teardown", {})
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            success = harness.run(scenario)
    finally:
        http(base, "/v1/teardown", {})
    log = re.sub(r"\x1b\[[0-9;]*m", "", output.getvalue())
    name = "judge-operational-" + scenario
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / (name + ".log")).write_text(log, encoding="utf-8")
    # Official scenario methods sometimes return True after printing FAIL; inspect both.
    passed = success and "[FAIL]" not in log
    report = {"scenario": scenario, "passed": passed, "actions_observed": harness.observed,
              "llm_score": None, "simulated_now": simulated_now,
              "adapter": "Official JudgeSimulator scenario methods, no-scoring observer; optional tick clock override. No scorer modified."}
    save(name + ".json", report)
    print(json.dumps(report, indent=2))
    if not passed:
        raise SystemExit(1)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["generate", "verify", "judge", "judge-operational"])
    parser.add_argument("--url", default=os.getenv("BOT_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--scenario", default="all")
    parser.add_argument("--simulated-now")
    args = parser.parse_args()
    if args.command == "generate":
        generate()
    elif args.command == "verify":
        verify(args.url, args.reset)
    else:
        run_judge(args.url, args.command == "judge-operational", args.scenario, args.simulated_now)

if __name__ == "__main__":
    main()
