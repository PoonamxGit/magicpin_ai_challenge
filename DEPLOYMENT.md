# Setup and Render deployment

Run Python 3.11+; there are no third-party packages or runtime secrets.
From the project folder:
```powershell
python -X utf8 -m unittest -v test_bot
python -X utf8 dataset/generate_dataset.py --seed-dir dataset --out expanded
python -X utf8 submission_tools.py generate
python -X utf8 bot.py --host 0.0.0.0 --port 8080
```
In another terminal: `python -X utf8 submission_tools.py verify --reset`.
The bot listens on HTTP locally. Render terminates HTTPS in front of it.

## Render
The checked-in `render.yaml` defines one Python web service, one instance, a paid
always-on compute plan (`0.5c-512mb`), no automatic deploys, and health probing at
`/v1/healthz`. Confirm the price in your account before provisioning. Do not use
a sleeping free service for a judged run: process shutdown loses in-memory state.

1. Put this project in a GitHub repository you control (private is fine).
   Keep `.env`, keys and tokens out of commits. The personal contact phone is
   not needed by the API and is not stored in project metadata.
2. Sign in at https://dashboard.render.com and connect that repository.
3. Choose **New → Blueprint**, select the repository and review `render.yaml`.
   Alternatively create a Python Web Service using:
   build `python -m unittest -q test_bot`, start `python -u bot.py`,
   health check `/v1/healthz`, one instance, and the environment variables in
   `render.yaml`. Render provides `PORT`; the process binds `0.0.0.0`.
4. Deploy only after reviewing the account's compute charge.
5. Copy the actual HTTPS URL from Render; do not infer it from the service name.
6. Before judging, verify:
```powershell
python -X utf8 submission_tools.py verify --url https://ACTUAL-HOST.onrender.com --reset
```
This performs all five routes, all 355 context pushes, a real composition, a reply,
dedup and teardown. Results go to `results/public-verification.json`.
It deliberately clears test state both before and after verification.
7. Form **Submission URL** is exactly that base URL, e.g.
   `https://ACTUAL-HOST.onrender.com`, without `/v1` or `/healthz`.
   No form is submitted by this project.
8. Keep one instance running without restart/deploy through evaluation.
   In-memory state is permitted by the brief but does not survive crashes.
   At test end POST `{}` as JSON to `/v1/teardown`, or stop the service.

Deployment needs access to your Render account and a connected repository.
No public URL is claimed until deployment and public route verification succeed.

## Official simulator
The supplied `judge_simulator.py` is preserved unchanged. It hardcodes configuration
and demands an LLM key even for its unscored scenarios. Use the adapter to inject
environment configuration without putting a key into source:
```powershell
# Set LLM_API_KEY securely in this terminal; do not put it in a tracked file.
$env:BOT_URL = "http://127.0.0.1:8080"
$env:LLM_PROVIDER = "openai"
python -X utf8 submission_tools.py judge --scenario phase2_short
```
For checks that cannot generate a quality score:
```powershell
python -X utf8 submission_tools.py judge-operational --scenario all
python -X utf8 submission_tools.py judge-operational --scenario phase2_short --simulated-now 2026-04-26T10:00:00Z
python -X utf8 submission_tools.py judge-operational --scenario full_evaluation --simulated-now 2026-04-26T10:00:00Z
```
These execute the official scenario methods with a no-scoring observer.
The explicit fixture clock avoids silently treating April fixtures as current.
The original harness reads seed files (10 merchants, 15 customers, 25 triggers),
does not use the expanded canonical pairs, and does not push customers in its
full-evaluation path. Independent HTTP verification loads all expanded contexts.

References checked during implementation:
- https://partners.magicpin.com/vera/ai-challenge
- https://render.com/docs/blueprint-spec
- https://render.com/docs/deploy-flask
