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

`setup.sh` generates a local database password, copies `config/profile.example.json` to `config/profile.json`, and creates user-restricted Gmail credential and onboarding data directories before starting Compose. The generated files are excluded from Git. Upload a resume and verify its extracted facts in the dashboard before preparing application packets. Run `docker compose up -d --force-recreate api worker` after changing environment variables; profile skills are read during scoring, and profile identity is read during packet preparation.

## Discover jobs

The worker automatically refreshes a public company-board directory and polls its boards. Click **Run collectors now** to start a cycle immediately; the default cycle interval is two hours.

You can still add a board manually when the directory is missing it or has a stale token. In the UI, enter the company, ATS provider, and the company's board token from its public careers URL:

- Greenhouse: `https://job-boards.greenhouse.io/BOARD_TOKEN` or `https://boards.greenhouse.io/BOARD_TOKEN`
- Lever: `https://jobs.lever.co/BOARD_TOKEN`
- Ashby: `https://jobs.ashbyhq.com/BOARD_TOKEN`

Public API read requests are authenticated by neither the user nor the employer; some boards may block access or have special formats.

You can add a board or trigger collection by API:

```bash
curl -X POST http://localhost:8000/api/boards \
  -H 'Content-Type: application/json' \
  -d '{"company":"Example","provider":"ashby","slug":"example"}'

curl -X POST http://localhost:8000/api/collect
curl 'http://localhost:8000/api/jobs?min_score=80'
```

The company above is **illustrative**; replace it with a verified board token.

## Profile JSON and first-use fact seed

Edit `config/profile.json` for identity, skills, and initial title, location, and annual USD salary defaults. Its `verified_experience` list is an optional one-time seed imported only when `config/onboarding/facts.json` is first created. After that, upload, edit, and verify career facts in the dashboard; later profile fact edits do not change the onboarding fact store. The PDF generator uses the verified onboarding facts exactly, not AI-invented achievements.

```json
{
  "name": "Your Name",
  "email": "you@example.com",
  "location": "New York, NY",
  "summary": "Your verified one-line professional summary.",
  "skills": ["Python", "TypeScript", "React"],
  "target_titles": ["senior software engineer", "senior backend engineer"],
  "preferred_locations": ["new york", "nyc", "remote"],
  "salary_floor": 150000,
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

Cost and access depend on your API account/model. The code uses structured outputs with `store=False`, but job descriptions and career facts are sent to the model provider when enabled. Do not put sensitive personal data in the profile or onboarding facts unless you have reviewed data handling requirements.

## Review-before-submit workflow

1. Browse jobs and select a match.
2. Click **Prepare packet** (requires at least one verified career fact).
3. Review the selected experience and the draft answer, and download the PDF.
4. Click **Approve materials**; this does **not** submit an application.
5. Open the job listing, verify that it is still available, and submit through the employer's form personally; you may use a separate form-autofill extension if permitted.
6. Click **I submitted (unverified)**, then **Confirm with receipt** when you have evidence.

`/api/jobs/{id}/status` can move confirmed applications into interview/rejected/withdrawn states. Application events are recorded.

## Included / not included

**Included:** PDF/DOCX resume import with verified-fact review; job preferences and immediate rescoring; automatic public Greenhouse / Lever / Ashby board discovery and collection; Remotive and optional USAJOBS aggregation; bounded Lever pagination; title/location/description normalization; exact normalized-URL deduplication; job-description versioning; two-successful-snapshot closure for ATS-board jobs; explainable heuristic matching; optional AI matching; optional AI fact-selection and answer drafting; verified-fact resume PDF; review dashboard; manual leads; optional Gmail alert discovery and read-only OAuth inbox sync verified against mocks; manual submission status and evidence note; periodic polling; tests.

**Not yet integrated:** Semantic matching across different listing URLs; ATS-form autofill; recruiter reply classification; migrations and multiuser authentication. These require further work. LinkedIn is **not scraped** and applications are **never auto-submitted**.

## Security and operations

- Both HTTP ports are bound to `127.0.0.1` in `docker-compose.yml`. The API has **no authentication** and must not be publicly deployed as written.
- Keep `.env`, `config/profile.json`, and `config/onboarding/` private. Add authentication, CSRF strategy, HTTPS and a proper secrets manager before exposing it externally.
- Postgres persists in the `pgdata` Docker volume. `docker compose down` keeps the volume; `docker compose down -v` destroys it.
- Read-only ATS polling uses exact allowlisted HTTPS hosts and validated board slugs. No arbitrary URL fetch or application POST is performed.
- Jobs with matching normalized listing URLs are deduplicated across sources. Different URLs for the same job may still appear separately.
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

See `IMPLEMENTATION_SPEC.md` for a coding-agent-ready backlog covering Gmail message handling, recruiter tracking, robust cross-source linking, resume templates, and production hardening.

## Reference documentation

- Greenhouse: https://docs.greenhouse.io/job-board
- Lever: https://github.com/lever/postings-api
- Ashby: https://developers.ashbyhq.com/docs/public-job-posting-api
- OpenAI structured output: https://developers.openai.com/api/docs/guides/structured-outputs

### Gmail read-only ingestion (optional)

Create a Google Cloud project, enable the Gmail API, configure the OAuth consent screen and add your Google account as a test user if the app is in testing. Create an OAuth client of type **Desktop app**, download its JSON, and save it as `config/gmail/client_secret.json`. Keep this file private. `setup.sh` creates the credential directory with user-only permissions; before starting Compose manually, run `mkdir -p config/gmail && chmod 700 config/gmail`. The Compose API disables access logs to avoid logging OAuth callback codes. The API and worker share this directory; the refresh token is stored there in a user-restricted file excluded from Git.

Set `GMAIL_QUERY` and/or `GMAIL_LABELS` in `.env` before starting Compose. If the services are already running, apply these values with `docker compose up -d --force-recreate api worker`. Labels are comma-separated Gmail label IDs; a query uses Gmail search syntax. Both filters apply when both are present. For example, `GMAIL_QUERY=subject:(application OR interview OR rejection)` limits ingestion to likely application-lifecycle messages. Filters targeting Spam, Trash, or `in:anywhere` are rejected, and a label-only preflight excludes Spam and Trash before fetching message metadata or snippets. Time-relative operators such as `after:`, `before:`, `older:`, `newer:`, `older_than:`, and `newer_than:` are unsupported because incremental sync only processes new mailbox events. Configure filters carefully because matching message metadata and a short snippet are stored in the database. For classified job alerts, the message body is read to extract listing links; raw bodies and attachments are not retained.

Start the application, then run `curl -X POST http://localhost:8000/api/gmail/connect` and open the returned `authorization_url` on the same machine. Approve the Gmail read-only permission; Google redirects to `http://127.0.0.1:8000/`. The credential directory is bound to the first authorized mailbox; reconnecting the same mailbox is supported, while authorizing a different mailbox is rejected to protect existing messages and sync state. Check `GET /api/gmail/status` and trigger a sync manually with `POST /api/gmail/sync`. The worker runs Gmail sync daily at approximately 06:00 America/New_York and follows daylight saving time; `POLL_SECONDS` continues to control only ATS collection. Gmail performs a full filtered scan on first sync or when the history cursor expires. Otherwise it processes only new messages reported by Gmail history, including after filter changes, so changing filters does not backfill older mail. Query candidates are checked with narrowly time-bounded Gmail searches, while labels are checked from message metadata. Reconnect the same mailbox if credentials are revoked or expired. This integration is verified with mocked Gmail responses, not a live Google account. This local API has no authentication and must not be exposed publicly without the protections described above.

### Automatic discovery

Collection refreshes a directory of public Greenhouse, Lever, and Ashby boards weekly and polls up to 300 boards per cycle in rotation. Manual boards still work. The directory comes from [LastRound AI's ATS Company Directory](https://github.com/fyrosofttech/lastroundai-hiring-data/tree/main/ats-directory), licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); see its [pay transparency study](https://lastroundai.com/blog/salary-transparency-study-2026). Tokens may become stale and this dataset is not a complete list. Requests use a roughly one-second interval with jitter and honor Retry-After. Failed boards do not close their postings.

[Remotive's public API](https://github.com/remotive-com/remote-jobs-api) supplies remote software jobs, extending New York searches to eligible remote roles without a key. Listings link to Remotive and show its name as the provider. Its public feed is delayed 24 hours and is fetched at most once per six hours, including after failures. Its terms prohibit submitting these jobs to other job sites. [USAJOBS](https://developer.usajobs.gov/api-reference/get-api-search) supplies federal software roles. Request a free key at the [API request page](https://developer.usajobs.gov/apirequest/index), then set `USAJOBS_API_KEY` and `USAJOBS_EMAIL` in `.env`; the email must be the one used for the key. If either is missing, USAJOBS is skipped with a logged notice. No personal email is built in.

Saved dashboard preferences are the single source for discovery filtering and scoring. Until preferences are saved, `config/profile.json` supplies the initial `target_titles`, `preferred_locations`, and optional annual `salary_floor` (USD). Each collection run uses one preference snapshot. When locations are selected, a listing is eligible only if its location matches or it is remote and Remote is a selected work mode. Known mismatches already stored are hidden, not deleted; new mismatches are not stored. Listings with unknown salary remain eligible, and salary filtering applies only where an annual maximum is recognizable. Results are deduplicated by source ID and normalized listing URL, although aggregator links may differ from an employer's direct URL. Aggregator listings are not automatically marked closed yet; verify availability on the linked listing. Directory rotation can take multiple days. Gmail alert links also enter discovery when their title and location match preferences.

Deferred: concurrent collection coordination across API and worker, more complete salary and remote eligibility parsing, improved cross-provider identity matching, and closing aggregator listings absent from complete feeds.

### Resume and preferences

Upload a text-based PDF or DOCX in the dashboard. Extracted experience and skills are drafts: review and edit each fact, then check and save its verification before preparing a packet. Existing `config/profile.json` verified experience is imported only when the onboarding fact store is first created. The source document is discarded after extraction. Facts and preferences are stored in `config/onboarding/`, excluded from Git and restricted to the local user. Keep a private backup of this directory. Scanned PDFs require text recognition before upload.

Preferences affect stored job scores and visibility immediately. Target-title phrases and selected locations act as hard filters, with selected Remote work admitting remote listings outside those locations. Selected seniority and work-mode terms raise rankings when matched. A stated salary below the annual USD floor removes a match. Listings without recognizable salary remain visible. Salary detection recognizes explicit USD salary, base-pay, pay-range, and compensation-range text plus provider-supplied annual USD salary maxima; it does not treat foreign currencies, stipends, bonuses, equity, nonannual rates, or unlabeled amounts as annual salary. Review compensation in the original listing.
