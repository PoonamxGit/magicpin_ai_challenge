# winwin — Vera message engine
Poonam · poonamrrj18@gmail.com

A deterministic, dependency-free Python 3.11+ engine. `bot.py` exports
`compose(category, merchant, trigger, customer=None)` and serves the five
required `/v1/*` routes plus teardown. No LLM or network call is made by the bot.

Run `python -X utf8 bot.py` (port 8080; override with `PORT` or `--port`).
Run `python -X utf8 -m unittest -v test_bot`.
Generate artifacts with:
```sh
python -X utf8 dataset/generate_dataset.py --seed-dir dataset --out expanded
python -X utf8 submission_tools.py generate
python -X utf8 submission_tools.py verify --reset
```
The last command needs the server running. It clears state; never run during judging.

The composer selects supported trigger evidence, cites supplied research, matches
category and language, and uses one primary CTA. Missing facts, wrong ownership,
revoked consent and unsupported consent scopes suppress sends. An empty canonical
body records that decision; the HTTP service never emits an empty-body action.
Catalog examples are not treated as live merchant offers. Replies produce drafts
or supplied summaries, never invented bookings, publications, prices or attachments.

The state engine atomically replaces higher context versions, treats identical
versions as successful no-ops, and rejects lower versions with HTTP 409. Dedup,
opt-out, cooldown, session and reply-retry state survive context updates.
First outreach and outreach outside a human-initiated 24h window use explicit
challenge-simulation templates. Auto-replies never open that window.
One process/instance is required; memory lasts until teardown or process exit.

Tradeoffs: rules favor reproducibility and grounded restraint over creative prose.
Unknown event shapes can be suppressed. English evidence is retained in regional
code-mix rather than machine-translating clinical facts. Missing consent produces
no message. Supplied synthetic clinical/regulatory sources are attributed, not
represented as independently verified real-world advice.

The website and main brief permit useful URLs; API examples prohibit them, but
the actual simulator implements no URL penalty. Bodies omit links conservatively
and retain source names. The website/testing prose also override the example's
same-version 409 behavior.

See [DEPLOYMENT.md](DEPLOYMENT.md) for Render setup, public verification and the
exact base-URL convention; [VALIDATION.md](VALIDATION.md) for actual test results
and simulator limitations. The canonical 30 rows are in `submission.jsonl`.
More useful context would be approved template IDs, offer eligibility/validity,
confirmed inventory/schedules, source publication dates and explicit consent expiry.
