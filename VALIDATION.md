# Validation — final local build

Executed on 2026-09-26 using Python 3.14.6 on Windows.

| Check | Actual result |
| --- | --- |
| `python -B -X utf8 -m unittest -q test_bot` | **41 tests passed**, 0.556 s |
| Supplied dataset generator | 5 categories, 50 merchants, 200 customers, 100 triggers |
| `submission_tools.py generate` | Exactly 30 canonical rows: 22 messages, 8 justified suppressions |
| `submission_tools.py verify --reset` | **PASS**, 363 recorded requests, max 27.16 ms; final teardown also succeeded |
| Direct `python -B -X utf8 judge_simulator.py` | **Exit 1**: `LLM_API_KEY is not set!` |
| Official scenario `all`, operational adapter | **PASS**: warmup, auto-reply exit, intent transition, hostile exit |
| Official `phase2_short`, fixture clock | **PASS**, 1 action observed; no quality score |
| Official `full_evaluation`, fixture clock | **PASS**, 11 actions observed; no quality score |
| Render/public HTTPS verification | **Not run**: account/repository access or deployed URL required |

No LLM quality score is claimed. The operational adapter executes the actual
`JudgeSimulator` methods, substitutes a no-scoring action observer, and additionally
treats any printed FAIL as a failure because some official methods return True
even after printing FAIL. It never fabricates a provider response or score.
Tick runs explicitly used `2026-04-26T10:00:00Z`; the original harness uses wall
time, against mostly expired seed triggers. Original simulator source is unchanged.

The simulator's loader uses only seed files, not the expanded canonical pairs.
Its full evaluation does not push customer contexts. Those gaps are covered
separately by the HTTP verifier and focused consent/customer tests.
Replay output is in `results/judge-operational-all.log`; structured results and
route latency records are in `results/*.json`.

A concrete failure was found and fixed during review: on Windows, immediately
closing an oversized POST with unread upload bytes could abort the connection
before HTTP 413 reached the client. The handler now drains a bounded amount of
the excess upload before responding. The complete regression suite then passed.
Other regression checks cover future/expired consent, updated-context reply
taboos, invalid urgency and renewal-specific acceptance drafts.

## Contract decisions

- The website and testing-brief prose take precedence over conflicting examples:
  equal context version returns the original successful acknowledgement without
  mutation; lower version returns HTTP 409; a higher version replaces in full.
- Body URLs: the website/main brief permit useful links, the examples prohibit
  them, and the actual simulator has no URL penalty implementation. This bot
  conservatively strips URLs and preserves supplied source names; it does not
  claim that Meta prohibits all links.
- Suppression: canonical rows with empty bodies document a decision not to send.
  HTTP ticks omit these rows entirely. Placeholder competitor/milestone events
  do not justify inventing names/counts; unrelated refill categories and missing
  scoped consent are suppressed. See `results/submission-report.json`.
- Time: direct compose has no implicit wall clock. HTTP ticks enforce supplied
  simulated time, expiry, consent dates, one ordinary nudge per recipient per day,
  urgency overrides, and at most three unanswered proactive messages.
- Templates: `vera_context_v1` and `merchant_context_v1` are explicitly simulated
  challenge templates (`{{1}}, {{2}} {{3}}`), not real Meta approvals.
- Research is attributed to the supplied synthetic source. No external merchant
  data, browsing, LLM API, booking API, or publishing API is used by the runtime.
- Public deployment uses one process/instance. Restarting loses test state, so
  automatic deploys are disabled and teardown clears all in-memory context,
  conversations, dedup, sessions, opt-outs and reply caches.

Official website inspected: https://partners.magicpin.com/vera/ai-challenge
