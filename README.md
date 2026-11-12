# JobScout

JobScout collects public job listings, scores them against your preferences, and helps you review application materials. You decide whether to apply. It runs locally and is intended for one user.

## Quickstart

Install Git and Docker Desktop on macOS or Windows, or Docker Engine with the Compose plugin on Linux. Start Docker. Windows users should run the commands in WSL; Linux users need Docker access and `openssl`. On macOS and Linux, `openssl` must be available in the terminal.

```bash
git clone https://github.com/kfu95/JobScout.git
cd JobScout
bash setup.sh
```

The script creates a random local database password in `.env`, builds the containers, and opens the dashboard where supported. Otherwise open http://localhost:3000. The first build may take several minutes. Upload a PDF or DOCX resume, review the extracted facts, and save your job preferences. You can configure optional integrations in the same screen. The API is at http://localhost:8000/docs.

The worker continues polling while Docker is running, including after a reboot if Docker starts automatically. A morning digest can only run while Docker is running. Gmail imports filtered application messages and discovers jobs from alerts. Department and years of experience are saved but do not yet affect matching.

## Integrations and advanced configuration

Adzuna requires a free app ID and key, which can be saved in the dashboard or set as `ADZUNA_APP_ID` and `ADZUNA_APP_KEY` in `.env`. USAJOBS requires a free API key and the email used to request it. Save them in the dashboard, or set `USAJOBS_API_KEY` and `USAJOBS_EMAIL` in `.env`. Dashboard values take precedence. Gmail requires a Google Cloud Desktop OAuth client with the Gmail API enabled. Upload its JSON in the dashboard, save the settings, then select Connect Gmail and authorize the read-only permission. Set a Gmail query and/or labels in the dashboard before syncing; the integration can classify filtered lifecycle messages and discover jobs from alerts. Restart the API and worker after editing `.env`:

```bash
docker compose up -d --force-recreate api worker
```

`.env` also supports optional OpenAI settings and the polling interval and `TZ` (an IANA timezone used for the daily Gmail sync, default UTC); see `.env.example`. Copy `config/profile.example.json` to `config/profile.json` if you want an advanced source of profile and initial preference defaults. Preferences saved in the dashboard take precedence. Keep these files private. Both web and API ports are bound to loopback. The API has no authentication and must not be exposed to a network.

Postgres, Gmail credentials, and onboarding data are stored in named Docker volumes. Back them up before removing volumes. Existing installations that used `config/onboarding` or `config/gmail` bind mounts need to copy those directories into the new volumes before starting the updated containers. Stop the old containers first, then run these commands from the repository root:

```bash
docker compose down
docker compose run --rm --no-deps --entrypoint sh api -c 'cp -an /config/onboarding/. /onboarding/ && cp -an /config/gmail/. /gmail/'
```

Create either missing source directory first. Existing files in the volumes are not overwritten.

## Update, stop, and uninstall

```bash
git pull
docker compose up -d --build
docker compose logs -f api worker web
docker compose down
```

Stopping retains your data. To uninstall and permanently delete the named volumes and their data, run `docker compose down -v`, then remove the checkout and its private `.env` and `config/profile.json`. This cannot be undone without a backup.

## Development

Run backend tests with `cd backend && python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt && pytest -q`. Applications are never submitted automatically. USAJOBS and Adzuna use the first target title for their searches. Remote region eligibility is approximate. Public board discovery can miss or duplicate listings; check the original listing before applying.

The public board directory uses [LastRound AI’s ATS Company Directory](https://github.com/fyrosofttech/lastroundai-hiring-data/tree/main/ats-directory), licensed CC BY 4.0. [Remotive](https://github.com/remotive-com/remote-jobs-api) supplies delayed remote listings and prohibits submitting them to other job sites.

[Remote OK](https://remoteok.com/api) and [Hacker News Search](https://hn.algolia.com/api) supply additional listings. Hacker News comments are free-form and may describe multiple jobs. [Adzuna](https://developer.adzuna.com/docs/search) supplies US listings, limited to the 50 most recent results per request; descriptions are excerpts and predicted salaries are not used for filtering. Its [terms](https://developer.adzuna.com/docs/terms_of_service) permit personal job search; commercial or organizational use may require a license.

### Morning digest

Enter the SMTP host, port, username, password, sender and recipient in the dashboard and save. For Gmail, use `smtp.gmail.com`, port `587`, your Gmail address and a Google app password. SMTP uses STARTTLS and does not change Gmail OAuth permissions. The dashboard's **Send test digest** button checks delivery without marking jobs as sent.

The worker sends new matched open jobs daily at `DIGEST_TIME` (24-hour `HH:MM`, default `08:00`) in `DIGEST_TIMEZONE` (IANA timezone, default `UTC`). Set these in `.env` and recreate the worker to choose your local morning. Set `DASHBOARD_URL` to the address you use to reach the dashboard from your email device. No email is sent when there are no new matches.

Alternatively configure `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM` and `DIGEST_TO` in `.env`. Dashboard values take precedence. Keep Docker running for scheduled delivery.

Gmail classification uses local heuristics unless both `GMAIL_AI_CLASSIFY=true` and `OPENAI_API_KEY` are set, which sends ambiguous message subject, snippet, and sender to OpenAI. Gmail filtering excludes Spam and Trash, and does not support time-relative search operators. Matching message metadata and snippets are retained; classified job-alert bodies are read to extract listing links, but raw bodies and attachments are not retained. LinkedIn, Built In, Glassdoor, and Indeed listings are not scraped. Changing filters does not backfill older mail. The integration is verified with mocks, not a live Google account.
