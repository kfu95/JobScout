# Implementation spec / coding-agent handoff

You are developing this repository as a personal job discovery and review-before-submit tool for a New York-area senior software engineer. It already has a Docker Compose application with FastAPI, SQLAlchemy, Postgres, a polling worker, public ATS API collectors, optional AI ranking/packet generation, a Next.js dashboard, and backend tests.

## Non-negotiable safeguards

- Never submit a job application automatically. A human must visit and submit the employer's form.
- Do not scrape LinkedIn or access protected pages using browser automation in violation of their rules. Use user-provided email alerts and authorized access.
- Resume claims must originate from the verified-experience profile. A model may pick and reorder verified bullet points but may not invent qualifications or metrics.
- An `approved` packet and a `submission_reported` application are not proof of submission. Store evidence before moving to `confirmed`.
- API currently has no auth and listens on loopback. Add auth, CSRF protections, HTTPS, rate limits, and secure credentials before any public deployment.
- Preserve audit events and idempotent reprocessing. Avoid duplicate model calls for unchanged job descriptions.

## Milestone 1: Gmail read-only OAuth ingestion (priority)

1. Implement standard Google OAuth 2.0 for a **local installed application** (or supported hosted OAuth), requesting only Gmail read-only permission. Never use passwords or session cookies. Store refresh tokens encrypted or in user-restricted local storage excluded from Git.
2. Pull Gmail messages matching user-configured labels or queries for LinkedIn and Built In job alerts, application confirmations, rejections, recruiter responses, interview invitations. Add pagination and `historyId`/push support with a robust fallback sync path. Treat Gmail content as untrusted input.
3. Store a `gmail_messages` table with provider message ID unique, RFC Message-ID, thread ID, source, subject, received time, minimal snippet, processed_at and classification status. Avoid raw message retention unless user opts in.
4. Parse alert links and canonicalize URLs, separating tracker URLs from actual job URLs when safe. For LinkedIn/Built In, store discovered leads without scraping pages; route the user to the original listing. Deduplicate by canonical URL and stable identifiers where available.
5. Classify message types into `job_alert`, `submission_confirmation`, `recruiter_outreach`, `interview_invite`, `rejection`, `other`. Use explicit heuristics first and optional LLM when ambiguous.
6. Match status updates only on confident evidence: employer domain, company, title, requisition ID, original URL, thread history. Queue ambiguous cases for human review. Never silently mark a submission confirmed from a generic marketing email.
7. Build settings UI for Gmail connection status, search query, resync and review of ambiguous classifications. Add fixture tests for MIME formats, threading, dedup, malformed links, and revoked OAuth credentials.

## Milestone 2: Robust cross-source linking

1. Normalize company domains, ATS requisition IDs, canonical posting URLs and title/location. Add `job_sources` table and canonical `jobs` table so one job can have multiple sources.
2. Prefer exact matches (requisition ID, ATS job ID, canonical URL); use probabilistic matching only as suggested merges for review.
3. Add source health metrics, consecutive-successful-poll missing counts per board, rate-limit handling with jitter and Retry-After, alerting on repeated API failures.
4. Track a job-version history for changed description, title, location, compensation and application URL, with diffs in the UI.

## Milestone 3: Better matching and packets

1. Split hard filters from semantic ranking. Add role family, seniority, locations, work arrangement, compensation preferences, exclusion lists, and explicit uncertainty treatment.
2. Cache score by `(job_hash, profile_hash, prompt_version, model_version)`. Evaluate accuracy against a labeled set of 100 example jobs.
3. Introduce structured candidate career facts with date ranges, technologies, evidence links, and verified/unverified flags. Block unverified facts from generated resumes.
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
- A job disappears only after two complete successful snapshots without it.
- AI failure falls back to heuristic scoring and does not invent data.
- Application cannot be marked reported without approval.
- Application cannot be marked confirmed without evidence.
- External leads do not require scraping LinkedIn or Built In.
- OAuth token files and career facts are excluded from version control.

When implementing changes, show which files changed and run the tests. Do not claim external integrations work until actually exercised with credentials and verified against representative samples.
