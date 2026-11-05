# Job Agent — local, review-before-submit MVP

A working starter for **public ATS job discovery → deduplication/versioning → scoring → fact-verified application packet → manual approval and submission tracking**.

## Run on macOS, Linux, or Windows with WSL

Prerequisites: Docker Desktop or Docker Engine with Compose v2, and `openssl` available on your terminal.

```bash
unzip job-agent-starter.zip
cd job-agent-starter
bash setup.sh
```

Open http://localhost:3000. The API and interactive API docs are at http://localhost:8000/docs.

`setup.sh` generates a local database password, copies `config/profile.example.json` to `config/profile.json`, and creates the user-restricted Gmail credential directory before starting Compose. The generated files are excluded from Git. **Update the profile with verified facts before preparing application packets.** Run `docker compose up -d --force-recreate api worker` after changing environment variables; the profile JSON itself is read on each scoring/preparation request.

## Add your first source

In the UI, enter the company, ATS provider, and the company's board token from its public careers URL:

- Greenhouse: `https://job-boards.greenhouse.io/BOARD_TOKEN` or `https://boards.greenhouse.io/BOARD_TOKEN`
- Lever: `https://jobs.lever.co/BOARD_TOKEN`
- Ashby: `https://jobs.ashbyhq.com/BOARD_TOKEN`

Then click **Run collectors now**. The separate worker will poll configured boards every two hours by default. Public API read requests are authenticated by neither the user nor the employer; some boards may block access or have special formats.

You can also add boards by API:

```bash
curl -X POST http://localhost:8000/api/boards \
  -H 'Content-Type: application/json' \
  -d '{"company":"Example","provider":"ashby","slug":"example"}'

curl -X POST http://localhost:8000/api/collect
curl 'http://localhost:8000/api/jobs?min_score=80'
```

The company above is **illustrative**; replace it with a verified board token.

## Career-facts JSON

Edit `config/profile.json`. Add facts with stable IDs; the PDF generator uses those exact fact strings, not AI-invented achievements.

```json
{
  "name": "Your Name",
  "email": "you@example.com",
  "location": "New York, NY",
  "summary": "Your verified one-line professional summary.",
  "skills": ["Python", "TypeScript", "React"],
  "target_titles": ["senior software engineer", "senior backend engineer"],
  "preferred_locations": ["new york", "nyc", "remote"],
  "verified_experience": [
    {
      "id": "project-001",
      "company": "Your Actual Employer",
      "role": "Your Real Title",
      "dates": "Exact Employment Dates",
      "bullets": ["An accomplishment you can verify with the accurate metrics, if any."]
    }
  ]
}
```

## AI configuration (optional)

Without an OpenAI API key, **scoring is rule-based**, and packet creation selects verified facts deterministically. This remains usable without any paid AI service.

To generate AI-assisted packet drafts and optionally AI job-fit scoring:

1. Put `OPENAI_API_KEY=...` in `.env`.
2. Leave `ENABLE_AI_SCORING=false` initially to avoid calling the model for every newly discovered job. Packet generation will still call the model when a key exists.
3. To turn on AI scoring for subsequently added/changed jobs, set `ENABLE_AI_SCORING=true` and run `docker compose up -d --force-recreate api worker`.
4. `OPENAI_MODEL` defaults to `gpt-4o-mini` and can be changed to an available compatible model.

Cost and access depend on your API account/model. The code uses structured outputs with `store=False`, but job descriptions and career facts are sent to the model provider when enabled. Do not put sensitive personal data in the profile unless you have reviewed data handling requirements.

## Review-before-submit workflow

1. Browse jobs and select a match.
2. Click **Prepare packet** (requires at least one verified career fact).
3. Review the selected experience and the draft answer, and download the PDF.
4. Click **Approve materials**; this does **not** submit an application.
5. Open the employer's application and submit it personally; you may use a separate form-autofill extension if permitted.
6. Click **I submitted (unverified)**, then **Confirm with receipt** when you have evidence.

`/api/jobs/{id}/status` can move confirmed applications into interview/rejected/withdrawn states. Application events are recorded.

## Included / not included

**Included:** Public Greenhouse / Lever / Ashby collectors; bounded Lever pagination; title/location/description normalization; idempotency within each board; job-description versioning; two-successful-snapshot closure rule; explainable heuristic matching; optional AI matching; optional AI fact-selection and answer drafting; verified-fact resume PDF; review dashboard; manual LinkedIn/Built In leads; Gmail read-only OAuth inbox sync verified against mocks; manual submission status and evidence note; periodic polling; tests.

**Not yet integrated:** LinkedIn or Built In email-alert parsing and job extraction; automatic company-wide source discovery; cross-board semantic deduplication; ATS-form autofill; recruiter reply classification; migrations and multiuser authentication. These require further work. LinkedIn is **not scraped** and applications are **never auto-submitted**.

## Security and operations

- Both HTTP ports are bound to `127.0.0.1` in `docker-compose.yml`. The API has **no authentication** and must not be publicly deployed as written.
- Keep `.env` and `config/profile.json` private. Add authentication, CSRF strategy, HTTPS and a proper secrets manager before exposing it externally.
- Postgres persists in the `pgdata` Docker volume. `docker compose down` keeps the volume; `docker compose down -v` destroys it.
- Read-only ATS polling uses exact allowlisted HTTPS hosts and validated board slugs. No arbitrary URL fetch or application POST is performed.
- Jobs from different ATS boards are **not yet merged** automatically. Within-board duplicate ingestion is prevented with a uniqueness constraint.
- The current schema is created automatically on startup (`create_all`) and is a starter only: add Alembic migrations before changing schema in production.
- OpenAI key changes require container recreation: `docker compose up -d --force-recreate api worker`.

### Useful commands

```bash
docker compose ps
docker compose logs -f api worker web
docker compose up -d --force-recreate api worker
docker compose down
```

### Run backend tests locally

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest -q
```

## Next implementation phase

See `IMPLEMENTATION_SPEC.md` for a coding-agent-ready backlog covering Gmail alert parsing, recruiter tracking, cross-source deduplication, resume templates, and production hardening.

## Reference documentation

- Greenhouse: https://docs.greenhouse.io/job-board
- Lever: https://github.com/lever/postings-api
- Ashby: https://developers.ashbyhq.com/docs/public-job-posting-api
- OpenAI structured output: https://developers.openai.com/api/docs/guides/structured-outputs

### Gmail read-only ingestion

Create a Google Cloud project, enable the Gmail API, configure the OAuth consent screen and add your Google account as a test user if the app is in testing. Create an OAuth client of type **Desktop app**, download its JSON, and save it as `config/gmail/client_secret.json`. Keep this file private. `setup.sh` creates the credential directory with user-only permissions; before starting Compose manually, run `mkdir -p config/gmail && chmod 700 config/gmail`. The Compose API disables access logs to avoid logging OAuth callback codes. The API and worker share this directory; the refresh token is stored there in a user-restricted file excluded from Git.

Set `GMAIL_QUERY` and/or `GMAIL_LABELS` in `.env` before starting Compose. If the services are already running, apply these values with `docker compose up -d --force-recreate api worker`. Labels are comma-separated Gmail label IDs; a query uses Gmail search syntax. Both filters apply when both are present. For example, `GMAIL_QUERY=from:(jobalerts-noreply@linkedin.com)` limits ingestion to that sender. Filters targeting Spam, Trash, or `in:anywhere` are rejected, and Spam and Trash are never read. Time-relative operators such as `after:`, `before:`, `older:`, `newer:`, `older_than:`, and `newer_than:` are unsupported because incremental sync only processes new mailbox events. Configure filters carefully because matching message metadata and a short snippet are stored in the database. Raw message bodies and attachments are not retained.

Start the application, then run `curl -X POST http://localhost:8000/api/gmail/connect` and open the returned `authorization_url` on the same machine. Approve the Gmail read-only permission; Google redirects to `http://127.0.0.1:8000/`. The credential directory is bound to the first authorized mailbox; reconnecting the same mailbox is supported, while authorizing a different mailbox is rejected to protect existing messages and sync state. Check `GET /api/gmail/status` and trigger a sync manually with `POST /api/gmail/sync`. The worker runs Gmail sync daily at approximately 06:00 America/New_York and follows daylight saving time; `POLL_SECONDS` continues to control only ATS collection. Gmail performs a full filtered scan on first sync or when the history cursor expires. Otherwise it processes only new messages reported by Gmail history, including after filter changes, so changing filters does not backfill older mail. Query candidates are checked with narrowly time-bounded Gmail searches, while labels are checked from message metadata. Reconnect the same mailbox if credentials are revoked or expired. This integration is verified with mocked Gmail responses, not a live Google account. This local API has no authentication and must not be exposed publicly without the protections described above.
