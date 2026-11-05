#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo 'Docker is required. Install Docker Desktop (macOS/Windows) or Docker Engine (Linux), then rerun.' >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo 'Docker Compose v2 is required (docker compose).' >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo 'Docker is installed but the daemon is not running. Start Docker Desktop / Docker Engine and rerun.' >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  if ! command -v openssl >/dev/null 2>&1; then
    echo 'openssl is required to generate a local database password.' >&2
    exit 1
  fi
  password="$(openssl rand -hex 24)"
  sed "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=${password}/" .env.example > .env
  chmod 600 .env
  echo 'Created .env with a random local database password.'
else
  echo 'Using existing .env (not overwriting credentials).'
fi
if [[ ! -f config/profile.json ]]; then
  cp config/profile.example.json config/profile.json
  chmod 600 config/profile.json
  echo 'Created config/profile.json; update identity and skills there if needed.'
fi
mkdir -p config/gmail config/onboarding
chmod 700 config/gmail config/onboarding

docker compose up --build -d
printf '\nDashboard: http://localhost:3000\nAPI docs:  http://localhost:8000/docs\n\n'
echo 'Next: upload a resume, verify its facts, set preferences, and run job collection in the dashboard.'
echo 'To stop: docker compose down'
echo 'To view logs: docker compose logs -f api worker web'
