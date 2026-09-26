"""Vera challenge API and deterministic four-context composer. Python 3.11+."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UTC = timezone.utc
VERSION = "1.0.0"
SCOPES = ("category", "merchant", "customer", "trigger")
URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
AUTO = re.compile(r"thank(?:s| you) for (?:contacting|reaching)|our team will respond|automated (?:assistant|reply|message)|auto.?reply|currently (?:closed|unavailable)|team tak pahunch|स्वचालित", re.I)
STOP = re.compile(r"\b(?:stop|unsubscribe|opt[ -]?out|remove me|do not (?:message|contact)|don't (?:message|contact)|no more messages|band karo|mat bhej|spam)\b|बंद करो|मैसेज मत", re.I)
NO = re.compile(r"\b(?:not interested|no thanks|no thank you|nahi chahiye|nahin chahiye)\b|^no[.! ]*$", re.I)
YES = re.compile(r"\b(?:yes|yeah|yep|ok|okay|go ahead|let'?s do it|send|draft|proceed|confirm|join|haan|han|chalo|judrna|judna|jurna)\b|हाँ|हां", re.I)
TEMPLATES = {
    "vera_context_v1": "{{1}}, {{2}} {{3}}",
    "merchant_context_v1": "{{1}}, {{2}} {{3}}",
}
CONSENT = {
    "recall_due": {"recall_reminders"},
    "appointment_tomorrow": {"appointment_reminders"},
    "chronic_refill_due": {"refill_reminders"},
    "customer_lapsed_soft": {"winback_offers", "promotional_offers"},
    "customer_lapsed_hard": {"winback_offers", "promotional_offers"},
    "wedding_package_followup": {"bridal_package_followup"},
    "bridal_followup": {"bridal_package_followup"},
    "trial_followup": {"kids_program_updates", "program_updates", "trial_followup"},
    "unplanned_slot_open": {"promotional_offers", "winback_offers"},
    "supply_alert": {"recall_alerts"},
}
ALIASES = {"research_digest_release": "research_digest",
           "category_research_digest_release": "research_digest",
           "festival": "festival_upcoming", "scheduled_recurring": "curious_ask_due"}
NOUNS = {"dentists": "treatment", "salons": "service", "restaurants": "dish",
         "gyms": "class", "pharmacies": "product"}
CTAS = {
    "en": "Reply YES for the draft; STOP to opt out.",
    "hi": "Draft ke liye YES bhejiye; messages band karne ke liye STOP.",
    "ta": "Draft venumna YES anuppunga; messages nirutha STOP.",
    "te": "Draft kavalante YES pampandi; messages aapadaniki STOP.",
    "kn": "Draft bekadare YES kaluhisi; messages nillisalu STOP.",
    "mr": "Draft hava asel tar YES pathva; messages thambavnyasathi STOP.",
}
GREET = {"en": "Hi", "hi": "Namaste", "ta": "Vanakkam", "te": "Namaskaram",
         "kn": "Namaskara", "mr": "Namaskar"}


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Expected ISO-8601 timestamp")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timestamp must include timezone")
    return dt.astimezone(UTC)


def utcnow():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]


def clean(value):
    return " ".join(URL.sub("", str(value)).split()).strip()


def human(value):
    return clean(value).replace("_", " ")


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def language(merchant, customer=None):
    identity = (customer or merchant).get("identity", {})
    pref = identity.get("language_pref") or identity.get("preferred_language")
    langs = [pref] if pref else identity.get("languages", ["en"])
    if pref:
        code = str(pref).lower()[:2]
        return code if code in CTAS else ("en" if code == "en" else None)
    # Hindi code-mix is explicitly encouraged when hi is among merchant languages.
    if "hi" in langs:
        return "hi"
    for lang in langs:
        if lang[:2] in CTAS:
            return lang[:2]
    return None


def opted_out(person):
    consent = person.get("consent", {})
    prefs = person.get("preferences", {})
    if person.get("opted_out") or consent.get("opted_out") or consent.get("revoked_at"):
        return True
    if consent.get("status") in ("revoked", "opted_out") or prefs.get("reminder_opt_in") is False:
        return True
    history = person.get("conversation_history", [])
    if isinstance(history, dict):
        history = history.get("turns", [])
    return any(isinstance(h, dict) and h.get("from") in ("merchant", "customer", "user")
               and (STOP.search(h.get("body", "")) or h.get("engagement") == "opted_out")
               for h in history)


def target(trigger, field):
    return trigger.get(field) or trigger.get("payload", {}).get(field)


def consent_ok(customer, kind):
    c = customer.get("consent", {})
    channel = customer.get("preferences", {}).get("channel", "")
    return (not opted_out(customer) and bool(c.get("opted_in_at"))
            and channel in ("whatsapp", "whatsapp_via_son", "whatsapp_via_parent")
            and bool(set(c.get("scope", [])) & CONSENT.get(kind, set())))


def item_for(category, trigger):
    p = trigger.get("payload", {})
    reference = p.get("top_item_id") or p.get("digest_item_id") or p.get("alert_id")
    if reference:
        return next((x for x in category.get("digest", []) if x.get("id") == reference), None)
    if isinstance(p.get("top_item"), dict):
        return p["top_item"]
    return next((x for x in reversed(category.get("digest", []))
                 if x.get("kind") == "research"), None)


def snapshot(merchant):
    p = merchant.get("performance", {})
    bits = [f"{p[k]} {k}" for k in ("views", "calls", "directions") if number(p.get(k))]
    window = f" over {p['window_days']} days" if number(p.get("window_days")) else ""
    return ("Your recorded profile activity" + window + ": " + ", ".join(bits) + ".") if bits else ""


def slots_text(payload):
    # Prefer canonical ISO values: fixture weekday labels are inconsistent.
    slots = payload.get("available_slots", payload.get("next_session_options", []))
    valid = []
    for slot in slots[:2]:
        if isinstance(slot, dict) and slot.get("iso"):
            try:
                timestamp(slot["iso"])
                valid.append(slot["iso"])
            except ValueError:
                pass
    return "Listed options (subject to confirmation): " + " or ".join(valid) + "." if valid else ""


def planning_draft(merchant, trigger):
    name = clean(merchant.get("identity", {}).get("name", "Your business"))
    topic = human(trigger.get("payload", {}).get("intent_topic", "profile update"))
    offer = next((o for o in merchant.get("offers", [])
                  if o.get("status") == "active" and "@" in o.get("title", "")), None)
    lines = [f"Draft for review — {name}: {topic}."]
    if offer:
        lines.append("Current listed offer: " + clean(offer["title"]) + ".")
    lines.append("Proposed plan: confirm the service, capacity and schedule before publishing.")
    lines.append("New pricing and availability are still to be agreed; nothing has been published.")
    return " ".join(lines)


def _parts(category, merchant, trigger, customer=None):
    """Return (greeting, evidence, primary CTA, CTA enum, rationale); no I/O."""
    k = ALIASES.get(trigger.get("kind"), trigger.get("kind"))
    p = trigger.get("payload", {})
    lang = language(merchant, customer)
    if not lang:
        return None, "unsupported_language"
    if category.get("slug") != merchant.get("category_slug"):
        return None, "category_mismatch"
    if p.get("category") and p["category"] != category.get("slug"):
        return None, "trigger_category_mismatch"
    if opted_out(customer or merchant):
        return None, "recipient_opted_out"
    mid = target(trigger, "merchant_id")
    if mid and mid != merchant.get("merchant_id"):
        return None, "merchant_mismatch"
    if trigger.get("scope") == "customer" and not customer:
        return None, "customer_context_missing"
    if customer:
        if k == "chronic_refill_due" and category.get("slug") != "pharmacies":
            return None, "refill_category_mismatch"
        if k in ("wedding_package_followup", "bridal_followup") and category.get("slug") != "salons":
            return None, "bridal_category_mismatch"
        if customer.get("merchant_id") != merchant.get("merchant_id"):
            return None, "customer_merchant_mismatch"
        if target(trigger, "customer_id") and target(trigger, "customer_id") != customer.get("customer_id"):
            return None, "customer_mismatch"
        if not consent_ok(customer, k):
            return None, "missing_or_revoked_scoped_consent"
    name = clean((customer or merchant).get("identity", {}).get(
        "name" if customer else "owner_first_name") or merchant.get("identity", {}).get("name", ""))
    if not customer and category.get("slug") == "dentists" and name and not name.startswith("Dr."):
        name = "Dr. " + name
    greeting = f"{GREET[lang]} {name}".strip()
    evidence, ask, cta = "", CTAS[lang], "binary_yes_no"
    why = f"composer {VERSION}; {k}; facts from supplied contexts; language={lang}"
    if customer:
        biz = clean(merchant.get("identity", {}).get("name", ""))
        channel = customer.get("preferences", {}).get("channel")
        if channel == "whatsapp_via_parent":
            match = re.search(r"parent:\s*([^)]*)", name)
            if match:
                greeting = f"{GREET[lang]} {match.group(1)}"
        elif channel == "whatsapp_via_son":
            greeting = f"{GREET[lang]} — for {name}'s family"
        evidence = f"{biz} via Vera. "
        if k == "recall_due" and p.get("due_date"):
            evidence += f"Your {human(p.get('service_due', 'recall'))} reminder is due {clean(p['due_date'])}."
        elif k == "appointment_tomorrow" and (p.get("appointment_at") or p.get("appointment_time_iso") or p.get("appointment_date")):
            evidence += "Appointment reminder: " + clean(p.get("appointment_at") or p.get("appointment_time_iso") or p.get("appointment_date")) + "."
        elif k == "chronic_refill_due" and p.get("stock_runs_out_iso"):
            evidence += "Your recorded refill date is " + clean(p["stock_runs_out_iso"]) + ". A pharmacist must confirm stock and prescription details."
        elif k in ("wedding_package_followup", "bridal_followup") and p.get("wedding_date"):
            evidence += f"Following up on your trial for your {clean(p['wedding_date'])} wedding. Package details and prices need confirmation."
        elif k == "trial_followup" and p.get("trial_date"):
            evidence += f"Following your {clean(p['trial_date'])} trial, we can discuss the next session."
        elif k in ("customer_lapsed_soft", "customer_lapsed_hard") and (p.get("days_since_last_visit") is not None or customer.get("relationship", {}).get("last_visit")):
            last = customer.get("relationship", {}).get("last_visit")
            evidence += f"Your last recorded visit was {clean(last)}." if last else f"It has been {p['days_since_last_visit']} days since your last visit."
            evidence += " You can restart at your own pace."
        elif k == "unplanned_slot_open" and p.get("available_slots"):
            evidence += "An appointment option has been listed."
        elif k == "supply_alert" and p.get("affected_batches"):
            evidence += "A supplied recall notice lists batches " + ", ".join(map(clean, p["affected_batches"])) + ". Please check with the pharmacist."
        else:
            return None, "missing_customer_event_facts"
        evidence += " " + slots_text(p)
        # No promotions piggybacked onto reminder-only consent.
        if lang == "hi":
            ask = "Aage baat karne ke liye YES bhejiye; reminders band karne ke liye STOP."
        elif lang == "en":
            ask = "Reply YES to discuss the next step; STOP to opt out."
        else:
            ask = CTAS[lang].replace("Draft", "Next step")
        return (greeting, evidence.strip(), ask, cta, why + "; consent checked; no booking asserted"), None

    if k in ("research_digest", "regulation_change", "cde_opportunity"):
        item = item_for(category, trigger)
        if not item or not item.get("title") or not item.get("source"):
            return None, "referenced_source_missing"
        evidence = f"From the supplied update ({clean(item['source'])}): {clean(item['title'])}."
        if item.get("summary"):
            evidence += " " + clean(item["summary"])
        if k == "research_digest" and number(item.get("trial_n")):
            evidence += f" Reported study size: {item['trial_n']}."
        cohort = merchant.get("customer_aggregate", {}).get("high_risk_adult_count")
        if k == "research_digest" and item.get("patient_segment") in ("high_risk_adults", "high-risk adults") and number(cohort):
            evidence += f" Your records list {cohort} high-risk adult patients."
        if k in ("research_digest", "regulation_change", "cde_opportunity"):
            ask = ("Supplied summary ke liye YES bhejiye; messages band karne ke liye STOP." if lang == "hi"
                   else CTAS[lang].replace("draft", "supplied summary").replace("Draft", "Summary"))
        if p.get("deadline_iso"):
            evidence += " Listed deadline: " + clean(p["deadline_iso"]) + "."
        if k == "cde_opportunity" and item.get("date"):
            evidence += " Listed date: " + clean(item["date"]) + "."
        why += "; attributed supplied synthetic source, not independently verified"
    elif k in ("perf_dip", "perf_spike", "seasonal_perf_dip"):
        metric = p.get("metric")
        delta = p.get("delta_pct")
        if metric and number(delta):
            evidence = f"Your {human(metric)} changed {delta * 100:+g}% over {human(p.get('window', 'the reported window'))}."
        else:
            changes = merchant.get("performance", {}).get("delta_7d", {})
            candidates = [(m, v) for m, v in sorted(changes.items()) if number(v) and
                          ((v < 0) if k != "perf_spike" else (v > 0))]
            if candidates:
                metric, delta = candidates[0]
                evidence = f"Your {human(metric.removesuffix('_pct'))} changed {delta * 100:+g}% in the recorded 7-day comparison."
        evidence += " " + snapshot(merchant) if evidence else ""
        if k == "seasonal_perf_dip" and p.get("is_expected_seasonal"):
            evidence += " The supplied alert marks this as seasonal; it does not establish the cause."
        if not evidence:
            return None, "performance_evidence_missing"
    elif k == "renewal_due":
        sub = merchant.get("subscription", {})
        days = sub.get("days_remaining", p.get("days_remaining"))
        if not number(days):
            return None, "renewal_details_missing"
        evidence = f"Your {clean(sub.get('plan', p.get('plan', '')))} plan has {days} days remaining."
        if number(p.get("renewal_amount")):
            evidence += f" Listed renewal amount: ₹{p['renewal_amount']}."
        evidence += " I can lay out the renewal details for review."
    elif k == "curious_ask_due":
        noun = NOUNS.get(category["slug"], "service")
        biz = clean(merchant.get("identity", {}).get("name", "your business"))
        evidence = f"For this week's check-in at {biz}, I can turn your answer into a post draft."
        ask = (f"Is hafte sabse zyada kis {noun} ki enquiry aayi?" if lang == "hi"
               else f"Which {noun} drew the most enquiries this week?")
        cta = "open_ended"
    elif k == "active_planning_intent" and p.get("intent_topic"):
        evidence = planning_draft(merchant, trigger)
        ask = "Is draft mein kya badalna hai?" if lang == "hi" else "What should change in this draft?"
        cta = "open_ended"
    elif k == "festival_upcoming" and p.get("festival"):
        evidence = f"Planning note: {clean(p['festival'])}" + (f" is listed for {clean(p['date'])}." if p.get("date") else ".")
        offer = next((o for o in merchant.get("offers", []) if o.get("status") == "active" and "@" in o.get("title", "")), None)
        if offer:
            evidence += " Your current offer is " + clean(offer["title"]) + "; festival validity still needs confirmation."
    elif k == "ipl_match_today" and p.get("match"):
        evidence = f"Match note: {clean(p['match'])}"
        if p.get("match_time_iso"):
            evidence += " at " + clean(p["match_time_iso"])
        if p.get("venue"):
            evidence += ", " + clean(p["venue"])
        evidence += "."
        offers = [clean(o["title"]) for o in merchant.get("offers", []) if o.get("status") == "active"]
        if offers:
            evidence += " Your listed offer is " + offers[0] + "; retain its stated day restrictions."
        evidence += " I can draft a match-day post without changing offer terms."
    elif k == "review_theme_emerged":
        themes = merchant.get("review_themes", [])
        data = p if p.get("theme") else (themes[0] if themes else {})
        if not data.get("theme"):
            return None, "review_evidence_missing"
        evidence = "Review theme: " + human(data["theme"]) + "."
        if number(data.get("occurrences_30d")):
            evidence += f" Mentioned {data['occurrences_30d']} times in 30 days."
        evidence += " I can draft a reply that acknowledges the concern without promising a fix."
    elif k == "milestone_reached" and number(p.get("value_now")):
        evidence = f"Your recorded {human(p.get('metric', 'count'))} is {p['value_now']}."
        if number(p.get("milestone_value")):
            gap = p["milestone_value"] - p["value_now"]
            evidence += f" Still {gap:g} short of {p['milestone_value']}." if gap > 0 else f" The {p['milestone_value']} milestone is reached."
    elif k == "competitor_opened" and p.get("competitor_name"):
        evidence = f"The supplied alert names {clean(p['competitor_name'])}"
        if number(p.get("distance_km")):
            evidence += f", {p['distance_km']} km away"
        evidence += "."
        if p.get("their_offer"):
            evidence += " Their listed offer: " + clean(p["their_offer"]) + "."
        evidence += " I can draft positioning around your existing services without a price cut."
    elif k == "supply_alert" and p.get("affected_batches"):
        item = item_for(category, trigger)
        source = item.get("source") if item else p.get("source")
        if not source:
            return None, "alert_source_missing"
        evidence = f"Supplied alert ({clean(source)}): {clean(p.get('molecule', 'product'))}, batches {', '.join(map(clean, p['affected_batches']))}."
        evidence += " Verify the original notice against inventory before contacting affected customers; no dispensing list was supplied."
    elif k == "category_seasonal" and p.get("trends"):
        evidence = "Supplied seasonal signals: " + "; ".join(map(human, p["trends"])) + ". These are not your store's measured sales."
    elif k == "gbp_unverified" and merchant.get("identity", {}).get("verified") is False:
        evidence = "Your Google Business Profile is recorded as unverified."
        if p.get("verification_path"):
            evidence += " Listed route: " + human(p["verification_path"]) + "."
        evidence += " I can draft a verification checklist."
    elif k in ("dormant_with_vera", "winback_eligible"):
        days = p.get("days_since_last_merchant_message", p.get("days_since_expiry"))
        if days is not None:
            evidence = f"It has been {days} days since " + ("your last recorded reply." if k == "dormant_with_vera" else "your plan expired.")
        evidence += " " + snapshot(merchant)
        if not evidence.strip():
            return None, "no_useful_facts"
    elif k == "category_trend_movement" and p.get("query") and number(p.get("delta_yoy")):
        evidence = f"Supplied search trend: {clean(p['query'])}, {p['delta_yoy'] * 100:+g}% year on year."
    elif k in ("weather_heatwave", "local_news_event") and (p.get("headline") or p.get("title")):
        evidence = "Supplied local update: " + clean(p.get("headline") or p["title"]) + "."
    else:
        return None, "unsupported_or_under_specified_trigger"
    return (greeting, evidence.strip(), ask, cta, why), None


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None) -> dict:
    """Pure deterministic composition. Empty body means suppress; rationale explains why.
    Temporal dedup/session policy belongs to Engine.tick, which has simulated time.
    """
    parts, reason = _parts(category, merchant, trigger, customer)
    result = {"body": "", "cta": "none",
              "send_as": "merchant_on_behalf" if customer or trigger.get("scope") == "customer" else "vera",
              "suppression_key": trigger.get("suppression_key") or trigger.get("id", ""),
              "rationale": reason or ""}
    if not parts:
        return result
    greeting, evidence, ask, cta, why = parts
    body = f"{greeting}, {evidence} {ask}"
    taboos = category.get("voice", {}).get("taboos", []) + category.get("voice", {}).get("vocab_taboo", [])
    if any(re.search(r"(?<!\w)" + re.escape(t) + r"(?!\w)", body, re.I) for t in taboos if t):
        result["rationale"] = "suppressed: supplied wording violates category taboo"
        return result
    result.update(body=body, cta=cta, rationale=why)
    return result


class APIError(Exception):
    def __init__(self, status, reason, **extra):
        self.status = status
        self.body = {"accepted": False, "reason": reason, **extra}


def required(body, key, expected):
    if key not in body or not isinstance(body[key], expected) or isinstance(body[key], bool):
        raise APIError(400, "invalid_request", details=f"Invalid or missing {key}")
    if expected is str and not body[key].strip():
        raise APIError(400, "invalid_request", details=f"Empty {key}")
    return body[key]


def validate_payload(scope, cid, p):
    idkey = {"category": "slug", "merchant": "merchant_id", "customer": "customer_id", "trigger": "id"}[scope]
    if p.get(idkey) != cid:
        raise APIError(400, "invalid_payload", details=f"{idkey} must equal context_id")
    objects = {"identity", "voice", "performance", "subscription", "consent", "preferences",
               "relationship", "customer_aggregate", "peer_stats", "payload"}
    arrays = {"offers", "signals", "digest", "offer_catalog", "patient_content_library", "trend_signals", "review_themes"}
    for k in objects & p.keys():
        if not isinstance(p[k], dict):
            raise APIError(400, "invalid_payload", details=f"{k} must be object")
    for k in arrays & p.keys():
        if not isinstance(p[k], list):
            raise APIError(400, "invalid_payload", details=f"{k} must be array")
        if k != "signals" and any(not isinstance(x, dict) for x in p[k]):
            raise APIError(400, "invalid_payload", details=f"{k} items must be objects")
    if scope in ("merchant", "customer"):
        required(p, "identity", dict)
        required(p, "category_slug" if scope == "merchant" else "merchant_id", str)
    if scope == "trigger":
        if not isinstance(p.get("urgency", 1), int) or isinstance(p.get("urgency"), bool) or not 1 <= p.get("urgency", 1) <= 5:
            raise APIError(400, "invalid_payload", details="urgency must be integer 1..5")
        for field in ("merchant_id", "customer_id", "suppression_key"):
            if p.get(field) is not None and not isinstance(p[field], str):
                raise APIError(400, "invalid_payload", details=f"{field} must be string or null")
        required(p, "kind", str)
        required(p, "payload", dict)
        if p.get("scope") not in ("merchant", "customer"):
            raise APIError(400, "invalid_payload", details="Invalid trigger scope")
        if not target(p, "merchant_id"):
            raise APIError(400, "invalid_payload", details="Missing merchant_id")
        if p.get("expires_at"):
            timestamp(p["expires_at"])


class Engine:
    """Single-process state; lock covers each decision and update atomically."""
    def __init__(self):
        self.lock = threading.RLock()
        self.started = time.monotonic()
        self.reset()

    def reset(self):
        with self.lock:
            self.contexts = {}
            self.conversations = {}
            self.suppressed = set()
            self.seen_triggers = set()
            self.optouts = set()
            self.sessions = {}
            self.last_sent = {}
            self.sent_counts = {}
            self.backoff = {}
            self.reply_cache = {}
            self.clock = None

    def get(self, scope, cid):
        return self.contexts.get((scope, cid), {}).get("payload")

    def push(self, body):
        scope = required(body, "scope", str)
        if scope not in SCOPES:
            raise APIError(400, "invalid_scope", details="Expected category, merchant, customer or trigger")
        cid, version, payload = required(body, "context_id", str), required(body, "version", int), required(body, "payload", dict)
        timestamp(required(body, "delivered_at", str))
        if version < 1:
            raise APIError(400, "invalid_version")
        validate_payload(scope, cid, payload)
        key = (scope, cid)
        with self.lock:
            old = self.contexts.get(key)
            if old and old["version"] > version:
                raise APIError(409, "stale_version", current_version=old["version"])
            if old and old["version"] == version:
                return copy.deepcopy(old["ack"])
            ack = {"accepted": True, "ack_id": "ack_" + digest([scope, cid, version]), "stored_at": utcnow()}
            self.contexts[key] = {"version": version, "payload": copy.deepcopy(payload), "ack": ack}
            return copy.deepcopy(ack)

    def health(self):
        with self.lock:
            return {"status": "ok", "uptime_seconds": int(time.monotonic() - self.started),
                    "contexts_loaded": {s: sum(k[0] == s for k in self.contexts) for s in SCOPES}}

    def metadata(self):
        return {"team_name": os.getenv("TEAM_NAME", "winwin"),
                "team_members": [s.strip() for s in os.getenv("TEAM_MEMBERS", "Poonam").split(",") if s.strip()],
                "model": "deterministic-rules-no-llm",
                "approach": "Evidence-based composer with scoped consent and atomic conversation state",
                "contact_email": os.getenv("CONTACT_EMAIL", "poonamrrj18@gmail.com"),
                "version": VERSION, "submitted_at": os.getenv("SUBMITTED_AT", "2026-09-26T00:00:00Z")}

    def history_session(self, merchant, customer, now):
        person = customer or merchant
        history = person.get("conversation_history", [])
        if isinstance(history, dict):
            history = history.get("turns", [])
        times = []
        for h in history:
            if not isinstance(h, dict) or h.get("from") not in ("merchant", "customer", "user") or AUTO.search(h.get("body", "")):
                continue
            try:
                dt = timestamp(h.get("ts"))
                if dt <= now:
                    times.append(dt)
            except (ValueError, TypeError):
                continue
        return max(times) if times else None

    def tick(self, body):
        now = timestamp(required(body, "now", str))
        tids = required(body, "available_triggers", list)
        if any(not isinstance(t, str) for t in tids):
            raise APIError(400, "invalid_request", details="Trigger IDs must be strings")
        with self.lock:
            if self.clock and now < self.clock:
                raise APIError(409, "stale_time")
            self.clock = now
            actions = []
            ordered = sorted(set(tids), key=lambda t: (-self.get("trigger", t).get("urgency", 1), t) if self.get("trigger", t) else (0, t))
            for tid in ordered:
                if len(actions) >= 20:
                    break
                trigger = self.get("trigger", tid)
                if not trigger:
                    continue
                mid, cid = target(trigger, "merchant_id"), target(trigger, "customer_id")
                merchant = self.get("merchant", mid)
                customer = self.get("customer", cid) if cid else None
                category = self.get("category", merchant.get("category_slug")) if merchant else None
                recipient = (mid, cid)
                key = (recipient, trigger.get("suppression_key") or tid)
                if not merchant or not category or (cid and not customer):
                    continue
                if trigger.get("scope") == "customer" and not customer:
                    continue
                if recipient in self.optouts or key in self.suppressed or (recipient, tid) in self.seen_triggers:
                    continue
                if trigger.get("expires_at") and timestamp(trigger["expires_at"]) <= now:
                    continue
                if trigger.get("not_before") and timestamp(trigger["not_before"]) > now:
                    continue
                if customer:
                    consent = customer.get("consent", {})
                    try:
                        # Dates in seed consent are local calendar dates, not instants.
                        opted = consent.get("opted_in_at")
                        if not opted:
                            continue
                        opt_time = timestamp(opted if "T" in opted else opted + "T00:00:00Z")
                        expires = consent.get("expires_at")
                        if opt_time > now or (expires and timestamp(expires) <= now):
                            continue
                    except (TypeError, ValueError):
                        continue
                if recipient in self.backoff and self.backoff[recipient] > now:
                    continue
                urgent = trigger.get("urgency", 1) >= 4
                if not urgent and recipient in self.last_sent and now - self.last_sent[recipient] < timedelta(hours=24):
                    continue
                if self.sent_counts.get(recipient, 0) >= 3:
                    continue
                msg = compose(category, merchant, trigger, customer)
                if not msg["body"]:
                    continue
                if any(c.get("recipient") == recipient and msg["body"] in c.get("bodies", []) for c in self.conversations.values()):
                    continue
                conv_id = "conv_" + digest([mid, cid, tid, trigger.get("suppression_key")])
                last_in = self.sessions.get(recipient) or self.history_session(merchant, customer, now)
                in_session = last_in is not None and timedelta(0) <= now - last_in < timedelta(hours=24)
                parts, _ = _parts(category, merchant, trigger, customer)
                template = None if in_session else ("merchant_context_v1" if customer else "vera_context_v1")
                action = {"conversation_id": conv_id, "merchant_id": mid, "customer_id": cid,
                          "trigger_id": tid, "template_name": template,
                          "template_params": list(parts[:3]) if template else [], **msg}
                self.conversations[conv_id] = {"recipient": recipient, "trigger_id": tid, "closed": False,
                                               "bodies": [msg["body"]], "messages": [], "last_turn": 1}
                self.suppressed.add(key)
                self.seen_triggers.add((recipient, tid))
                self.last_sent[recipient] = now
                self.sent_counts[recipient] = self.sent_counts.get(recipient, 0) + 1
                actions.append(action)
            return {"actions": actions}

    def reply(self, body):
        conv_id = required(body, "conversation_id", str)
        message = required(body, "message", str)
        turn = required(body, "turn_number", int)
        now = timestamp(required(body, "received_at", str))
        role = required(body, "from_role", str)
        if role not in ("merchant", "customer") or turn < 1:
            raise APIError(400, "invalid_request", details="Invalid role or turn_number")
        with self.lock:
            c = self.conversations.get(conv_id)
            mid, cid = body.get("merchant_id"), body.get("customer_id")
            if c:
                old_mid, old_cid = c["recipient"]
                if (mid is not None and mid != old_mid) or ("customer_id" in body and cid != old_cid):
                    raise APIError(409, "conversation_identity_mismatch")
                mid, cid = old_mid, old_cid
            if not mid or (role == "customer" and not cid) or (role == "merchant" and cid):
                raise APIError(400, "invalid_request", details="Recipient identity does not match from_role")
            recipient = (mid, cid)
            cachekey = (conv_id, turn)
            fingerprint = digest([recipient, role, message, body["received_at"]])
            if cachekey in self.reply_cache:
                prev = self.reply_cache[cachekey]
                if prev[0] != fingerprint:
                    raise APIError(409, "turn_conflict")
                return copy.deepcopy(prev[1])
            if not c:
                c = {"recipient": recipient, "trigger_id": None, "closed": False, "bodies": [], "messages": [], "last_turn": 0}
                self.conversations[conv_id] = c
            if turn <= c["last_turn"] or (c.get("last_received") and now < c["last_received"]):
                raise APIError(409, "stale_turn")
            c["last_turn"], c["last_received"] = turn, now
            c["messages"].append(message)
            merchant = self.get("merchant", mid) or {}
            customer = self.get("customer", cid) if cid else None
            category = self.get("category", merchant.get("category_slug")) or {}
            trigger = self.get("trigger", c["trigger_id"]) or {}
            lang = language(merchant, customer) or "en"
            if re.search(r"\b(?:haan|nahi|mujhe|chahiye|karo|bhejo)\b|[\u0900-\u097f]", message, re.I):
                lang = "hi"
            end = lambda reason: {"action": "end", "rationale": reason}
            if STOP.search(message):
                self.optouts.add(recipient)
                result = end("Explicit opt-out; suppress this recipient across all conversations until teardown")
            elif c["closed"] or recipient in self.optouts or opted_out(customer or merchant):
                result = end("Conversation closed or recipient opted out")
            elif AUTO.search(message) or c["messages"].count(message) >= 3:
                self.backoff[recipient] = now + timedelta(hours=24)
                result = end("Auto-reply detected; no bot loop, no human session opened")
            elif NO.search(message):
                self.backoff[recipient] = now + timedelta(days=30)
                result = end("Not interested; exit and pause proactive outreach for 30 days")
            else:
                self.sessions[recipient] = max(now, self.sessions.get(recipient, now))
                self.sent_counts[recipient] = 0
                if re.search(r"\b(?:busy|later|not now|tomorrow|baad mein)\b", message, re.I):
                    seconds = 86400 if "tomorrow" in message.lower() else 1800
                    self.backoff[recipient] = now + timedelta(seconds=seconds)
                    result = {"action": "wait", "wait_seconds": seconds, "rationale": "Requested time; proactive outreach paused"}
                elif not merchant or (cid and not customer):
                    result = end("Recipient context missing; no factual reply can be grounded")
                elif turn > 6:
                    result = end("Conversation turn budget reached")
                else:
                    text, cta = self.response_text(category, merchant, trigger, customer, message, lang, c)
                    text = clean(text)
                    taboos = category.get("voice", {}).get("taboos", []) + category.get("voice", {}).get("vocab_taboo", [])
                    unsafe = any(re.search(r"(?<!\w)" + re.escape(t) + r"(?!\w)", text, re.I) for t in taboos if t)
                    if unsafe:
                        result = end("Supplied draft wording violates category voice constraints")
                    elif text in c["bodies"]:
                        result = end("Next response would repeat an earlier message")
                    else:
                        c["bodies"].append(text)
                        result = {"action": "send", "body": text, "cta": cta,
                                  "rationale": "Respond to stated intent using latest contexts; draft only, no external action"}
            if result["action"] == "end":
                c["closed"] = True
            self.reply_cache[cachekey] = (fingerprint, copy.deepcopy(result))
            return result

    def response_text(self, category, merchant, trigger, customer, message, lang, conv):
        hi = lang == "hi"
        low = message.lower()
        if re.search(r"\b(?:gst|tax|politics|cricket score|weather forecast)\b", low):
            return (("GST/tax aur unrelated sawalon mein madad nahi kar sakti. Profile aur message drafts mein madad kar sakti hoon. Yahin rukte hain." if hi else
                     "I can help with profile and message drafts; tax filing and unrelated questions are outside this service. We can leave it here."), "none")
        if re.search(r"\b(?:expensive|cost|price|budget|mehenga|paise)\b", low):
            offers = [clean(o["title"]) for o in merchant.get("offers", []) if o.get("status") == "active"]
            detail = ("Listed offer: " + offers[0] + ". ") if offers else ""
            return (detail + ("Naya discount ya price confirm nahi hai. Kya bina naye offer ka draft chahiye?" if hi else
                             "No new discount or price is confirmed. Should I draft a version without a new offer?"), "binary_yes_no")
        if YES.search(message):
            if customer:
                return (("Aapki interest note kar li hai. Booking, stock aur delivery abhi confirm nahi hain. Apna preferred time bhejiye." if hi else
                         "Interest noted. Booking, stock and delivery are not confirmed. Please share your preferred time."), "open_ended")
            if re.search(r"\b(?:join|judrna|judna|jurna)\b", low):
                name = clean(merchant.get("identity", {}).get("name", ""))
                return (f"Next step — onboarding draft for {name}. " +
                        ("Enrollment abhi nahi hua. Official merchant onboarding par business details verify karke plan review karein." if hi else
                         "Enrollment has not happened. Verify your business details through official merchant onboarding and review the plan before accepting."), "none")
            item = item_for(category, trigger)
            if item and trigger.get("kind") in ("research_digest", "research_digest_release", "regulation_change", "cde_opportunity"):
                note = clean(item.get("summary") or item.get("title", ""))
                source = clean(item.get("source", ""))
                return (f"Here is the supplied summary ({source}): {note} " +
                        ("Yeh summary hai; full paper ya attachment available nahi hai." if hi else
                         "This is the supplied summary; the full paper or attachment is not available."), "none")
            if conv.get("artifact"):
                return (("Draft ki approval note kar li. Publish/send karne ki integration nahi hai; reviewed draft manually use karein." if hi else
                         "Draft approval noted. No publishing or sending integration is connected; use the reviewed draft manually."), "none")
            conv["artifact"] = True
            kind = ALIASES.get(trigger.get("kind"), trigger.get("kind"))
            payload = trigger.get("payload", {})
            if kind in ("perf_dip", "perf_spike", "seasonal_perf_dip"):
                artifact = "Review draft: " + snapshot(merchant) + " Compare the profile's current hours, photos and recent posts before choosing a change; the figures alone do not prove a cause."
            elif kind == "renewal_due":
                sub = merchant.get("subscription", {})
                artifact = f"Renewal review draft: {clean(sub.get('plan', ''))} plan, {sub.get('days_remaining', payload.get('days_remaining', 'unconfirmed'))} days remaining."
                if number(payload.get("renewal_amount")):
                    artifact += f" Listed amount: ₹{payload['renewal_amount']}."
                artifact += " Check the plan terms in the merchant account before paying. No renewal has been executed."
            elif kind == "supply_alert":
                artifact = "Inventory review draft: check batches " + ", ".join(map(clean, payload.get("affected_batches", []))) + " against the original notice, then identify affected dispensing records. No affected-customer list was supplied."
            elif kind == "gbp_unverified":
                artifact = "Verification checklist draft: open your Google Business Profile, review the offered verification method, and complete the steps shown there. Verification has not been performed by this bot."
            elif kind == "review_theme_emerged":
                artifact = "Reply draft for review: Thank you for sharing your experience. We have noted your concern about " + human(payload.get("theme", "your visit")) + ". No resolution timeline is promised."
            else:
                artifact = planning_draft(merchant, trigger)
            return (artifact + (" Edit batayein." if hi else " Share any edits."), "open_ended")
        if trigger.get("kind") == "curious_ask_due":
            name = clean(merchant.get("identity", {}).get("name", ""))
            return (f"Draft for review — {name}: enquiries about {clean(message)} are welcome. " +
                    ("Price aur availability pehle confirm karein." if hi else "Confirm pricing and availability before use."), "none")
        return (("Main profile aur message drafts mein madad kar sakti hoon. Kis detail ko clarify karna hai?" if hi else
                 "I can help clarify the profile update or message draft. Which detail needs clarification?"), "open_ended")


ENGINE = Engine()


class Handler(BaseHTTPRequestHandler):
    server_version = "Vera/1.0"
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass  # Never log payloads or secrets.

    def send_json(self, status, data):
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        if self.path == "/v1/healthz":
            self.send_json(200, ENGINE.health())
        elif self.path == "/v1/metadata":
            self.send_json(200, ENGINE.metadata())
        else:
            self.send_json(404, {"accepted": False, "reason": "not_found"})

    def do_POST(self):
        try:
            if self.path not in ("/v1/context", "/v1/tick", "/v1/reply", "/v1/teardown"):
                raise APIError(404, "not_found")
            if self.headers.get("Transfer-Encoding"):
                raise APIError(400, "unsupported_transfer_encoding")
            if self.headers.get_content_type() != "application/json":
                raise APIError(400, "invalid_content_type")
            length = int(self.headers.get("Content-Length", "0"))
            if length > 500 * 1024:
                raise APIError(413, "payload_too_large")
            if length < 0:
                raise APIError(400, "invalid_content_length")
            raw = self.rfile.read(length)
            data = json.loads(raw or b"{}", parse_constant=lambda x: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
            if not isinstance(data, dict):
                raise APIError(400, "invalid_request", details="Expected JSON object")
            if self.path == "/v1/teardown":
                ENGINE.reset()
                result = {"cleared": True}
            else:
                fn = {"/v1/context": ENGINE.push, "/v1/tick": ENGINE.tick, "/v1/reply": ENGINE.reply}[self.path]
                result = fn(data)
            self.send_json(200, result)
        except APIError as e:
            self.send_json(e.status, e.body)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            self.send_json(400, {"accepted": False, "reason": "invalid_request"})
        except (TimeoutError, ConnectionError):
            self.close_connection = True


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8080")))
    args = parser.parse_args()
    server = Server((args.host, args.port), Handler)
    print(f"Vera listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        ENGINE.reset()


if __name__ == "__main__":
    main()
