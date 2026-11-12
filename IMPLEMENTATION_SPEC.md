# Implementation spec / coding-agent handoff

You are developing this repository as a personal job discovery and review-before-submit tool for a New York-area senior software engineer. It already has a Docker Compose application with FastAPI, SQLAlchemy, Postgres, a polling worker, automatic public ATS board discovery, public job aggregators, optional AI ranking/packet generation, a Next.js dashboard, and backend tests.

## Non-negotiable safeguards

- Never submit a job application automatically. A human must visit and submit the employer's form.
- Do not scrape LinkedIn or access protected pages using browser automation in violation of their rules. Use public APIs, user-provided links, and authorized access.
- Resume claims must originate from verified onboarding facts. A model may pick and reorder verified bullet points but may not invent qualifications or metrics.
- An `approved` packet and a `submission_reported` application are not proof of submission. Store evidence before moving to `confirmed`.
- API currently has no auth and listens on loopback. Add auth, CSRF protections, HTTPS, rate limits, and secure credentials before any public deployment.
- Preserve audit events and idempotent reprocessing. Avoid duplicate model calls for unchanged job descriptions.

## Milestone 1: Gmail read-only ingestion

1. Implement standard Google OAuth 2.0 for a **local installed application** (or supported hosted OAuth), requesting only Gmail read-only permission. Never use passwords or session cookies. Store refresh tokens encrypted or in user-restricted local storage excluded from Git.
2. Pull Gmail messages matching user-configured labels or queries for job alerts, application confirmations, rejections, recruiter responses, and interview invitations. Add pagination and `historyId`/push support with a robust fallback sync path. Treat Gmail content as untrusted input.
3. Store a `gmail_messages` table with provider message ID unique, RFC Message-ID, thread ID, source, subject, received time, minimal snippet, processed_at and classification status. Avoid raw message retention unless user opts in.
4. Parse job-alert links from LinkedIn, Built In, Glassdoor, and Indeed emails and canonicalize URLs, separating tracker URLs from actual job URLs when safe. Add discovered jobs to the existing preference-matched pipeline and deduplicate by canonical URL and stable identifiers. Never scrape those sites.
5. Classify message types into `job_alert`, `submission_confirmation`, `recruiter_outreach`, `interview_invite`, `rejection`, `other`. Use explicit heuristics first and optional LLM when ambiguous.
6. Match status updates only on confident evidence: employer domain, company, title, requisition ID, original URL, thread history. Queue ambiguous cases for human review. Never silently mark a submission confirmed from a generic marketing email.
7. Build settings UI for Gmail connection status, search query, resync and review of ambiguous classifications. Add fixture tests for MIME formats, threading, dedup, malformed links, and revoked OAuth credentials.

## Milestone 2: Robust cross-source linking

1. Add company-domain and cross-source ATS requisition-ID normalization. Introduce a `job_sources` table and canonical `jobs` table so one job can have multiple sources.
2. Keep exact normalized-URL matching authoritative and add exact requisition-ID matching; use probabilistic matching only as suggested merges for review.
3. Add source health metrics and alerting on repeated API failures while retaining per-board consecutive-successful-poll missing counts and provider rate-limit handling.
4. Extend job-version history to compensation and application URL, and show version diffs in the UI.

## Milestone 3: Better matching and packets

1. Refine the current hard-filter and semantic-ranking split with richer role-family handling and explicit uncertainty treatment.
2. Cache score by `(job_hash, profile_hash, prompt_version, model_version)`. Evaluate accuracy against a labeled set of 100 example jobs.
3. Add evidence links to the existing structured candidate career facts.
4. Create a PDF resume layout with validated section ordering, page breaks, and a preview/diff against the base resume. Support manual edits with an approval timestamp.
5. Use LLM structured outputs for selecting fact IDs and drafting optional application questions, never to fabricate work history.

## Milestone 4: Production readiness

1. Move auto-created DB schema to Alembic migrations. Introduce job retries and durable queue semantics (Postgres jobs table with SKIP LOCKED or Redis queue).
2. Implement user authentication and authorization; avoid public write endpoints. Add secrets management and backups.
3. Add structured logs, request tracing, source-level telemetry, LLM cost budgets, and nightly integration tests with mocked ATS responses.
4. Split manual local actions from worker actions. Keep the system compliant with each ATS/provider terms and rate limits.

## Acceptance tests

- Repeated polling the same posting is idempotent.
- A changed description is versioned exactly once.
- Transient failed fetches do not mark jobs closed.
- An ATS-board job disappears only after two complete successful snapshots without it.
- AI failure falls back to heuristic scoring and does not invent data.
- Application cannot be marked reported without approval.
- Application cannot be marked confirmed without evidence.
- External leads do not require scraping their source sites.
- OAuth token files and career facts are excluded from version control.

When implementing changes, show which files changed and run the tests. Do not claim external integrations work until actually exercised with credentials and verified against representative samples.
