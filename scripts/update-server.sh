#!/usr/bin/env bash
# Update the CourtPilot server to a branch of the GitHub repo and restart it,
# keeping the database and the existing .env.
#
#   Usage (on the server):  bash update-server.sh [branch]      default branch: web-app
#
# Steps (stops at the first problem):
#   1. Backs up the database and .env to ~/courtpilot-backups/<time>/
#   2. Gets the new code. First run: /opt/courtpilot was copied in by hand, so it
#      is kept as /opt/courtpilot-old-<time>, any server-side edits are saved as a
#      patch, and a git checkout replaces it. Later runs just `git pull`.
#   3. Puts .env at the root and makes it production-safe
#   4. Rebuilds and restarts under the same compose project (= same database)
#   5. Checks the site answers
set -euo pipefail

BRANCH="${1:-web-app}"
REPO="https://github.com/sunnyboygiri-maker/court-pilot.git"
APP_DIR="${COURTPILOT_DIR:-/opt/courtpilot}"
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="$HOME/courtpilot-backups/$STAMP"
say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mXX %s\033[0m\n' "$*"; exit 1; }

[ -d "$APP_DIR" ] || die "$APP_DIR not found. Send this message to Claude."
command -v git >/dev/null || die "git is not installed. Run: apt-get install -y git   then run this again."
mkdir -p "$BACKUP_DIR"

say "1/5 Backing up database and settings to $BACKUP_DIR"
if [ -f "$APP_DIR/.env" ]; then
  ENV_FILE="$APP_DIR/.env"
else
  ENV_FILE=$(find "$APP_DIR" -mindepth 2 -maxdepth 5 -name .env -not -path '*/.git/*' | head -1 || true)
fi
[ -n "$ENV_FILE" ] || die "No .env settings file found under $APP_DIR. Send this message to Claude."
cp "$ENV_FILE" "$BACKUP_DIR/env"
echo "settings file: $ENV_FILE"

PG=$(docker ps --format '{{.Names}}' | grep -E 'postgres' | head -1 || true)
# The running app's compose project name decides which data volume (= which
# database) the restarted app uses, so reuse it
PROJECT=courtpilot
if [ -n "$PG" ]; then
  P=$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$PG" 2>/dev/null || true)
  [ -n "$P" ] && PROJECT="$P"
  docker exec "$PG" pg_dump -U courtpilot courtpilot > "$BACKUP_DIR/courtpilot.sql"
  echo "database: $(du -h "$BACKUP_DIR/courtpilot.sql" | cut -f1) backed up from container $PG"
else
  warn "No running database container found; skipping the database backup (its data volume is kept anyway)"
fi
echo "compose project: $PROJECT"

say "2/5 Getting branch '$BRANCH' from GitHub"
cd /
if [ -d "$APP_DIR/.git" ]; then
  cd "$APP_DIR"
  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    git diff > "$BACKUP_DIR/server-edits.patch"
    git stash push -m "server edits before $BRANCH update $STAMP" >/dev/null
    warn "Files edited on the server were saved to $BACKUP_DIR/server-edits.patch. Send it to Claude."
  fi
  git fetch origin "$BRANCH"
  git checkout -B "$BRANCH" "origin/$BRANCH"
else
  echo "First update: $APP_DIR is a hand-copied folder, switching it to a GitHub checkout"
  TMP=$(mktemp -d)
  git clone -q --depth 1 -b main "$REPO" "$TMP/original"
  ORIG=$(dirname "$(find "$TMP/original" -name docker-compose.yml -not -path '*/.git/*' | head -1)")
  CUR=$(dirname "$(find "$APP_DIR" -maxdepth 5 -name docker-compose.yml -not -path '*/.git/*' | head -1)")
  if [ -n "$CUR" ] && [ -d "$CUR" ]; then
    diff -ruN --strip-trailing-cr -x .env -x '__pycache__' -x '*.pyc' -x '.pytest_cache' -x 'celerybeat-schedule*' \
      "$ORIG" "$CUR" > "$BACKUP_DIR/server-edits.patch" || true
    if [ -s "$BACKUP_DIR/server-edits.patch" ]; then
      warn "The server's copy differs from your original GitHub upload."
      warn "Saved the differences to $BACKUP_DIR/server-edits.patch. Send it to Claude so nothing is lost."
    else
      rm -f "$BACKUP_DIR/server-edits.patch"
      echo "server files match the original upload (no edits made on the server)"
    fi
  fi
  rm -rf "$TMP"
  mv "$APP_DIR" "$APP_DIR-old-$STAMP"
  echo "old folder kept as $APP_DIR-old-$STAMP"
  git clone -q -b "$BRANCH" "$REPO" "$APP_DIR"
  cd "$APP_DIR"
fi
git log --oneline -1

say "3/5 Preparing settings (.env)"
[ -f .env ] || cp "$BACKUP_DIR/env" .env
chmod 600 .env
setv() {  # setv KEY VALUE: replace or append
  if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi
}
getv() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }
setv DEBUG false
KEY=$(getv SECRET_KEY)
if [ ${#KEY} -lt 32 ] || [ "$KEY" = "generate-a-secure-random-key" ] || [ "$KEY" = "change-me-in-production" ]; then
  setv SECRET_KEY "$(openssl rand -hex 32)"
  echo "SECRET_KEY was weak: replaced (everyone will need to log in again)"
fi
BASE=$(getv APP_BASE_URL)
if [ -z "$BASE" ] || [ "$BASE" = "https://courtpilot.in" ]; then
  IP=$(curl -s -4 --max-time 5 ifconfig.me || true)
  setv APP_BASE_URL "http://${IP:-187.127.175.129}:8000"
fi
echo "APP_BASE_URL=$(getv APP_BASE_URL)"

METHODS=()
[ -n "$(getv TELEGRAM_BOT_TOKEN)" ] && METHODS+=("phone code via Telegram")
[ -n "$(getv SMS_PROVIDER)" ] && [ -n "$(getv SMS_API_KEY)" ] && METHODS+=("phone code via SMS")
[ -n "$(getv SMTP_USER)" ] && [ -n "$(getv SMTP_PASSWORD)" ] && METHODS+=("email code")
[ -n "$(getv GOOGLE_CLIENT_ID)" ] && [ -n "$(getv GOOGLE_CLIENT_SECRET)" ] && METHODS+=("Google")
if [ ${#METHODS[@]} -eq 0 ]; then
  warn "No login method is set up yet: nobody can log in until one is added to $APP_DIR/.env"
  warn "(TELEGRAM_BOT_TOKEN, or SMTP_USER + SMTP_PASSWORD). Ask Claude how."
else
  echo "Login methods: ${METHODS[*]}"
fi

say "4/5 Rebuilding and restarting (a few minutes the first time)"
PROFILE=()
# Without a public https webhook, the bot must poll Telegram itself
[ -z "$(getv TELEGRAM_WEBHOOK_URL)" ] && [ -n "$(getv TELEGRAM_BOT_TOKEN)" ] && PROFILE=(--profile polling)
docker compose -p "$PROJECT" "${PROFILE[@]}" up -d --build --remove-orphans

say "5/5 Checking the site"
for _ in $(seq 1 40); do
  if curl -fs localhost:8000/health >/dev/null 2>&1; then
    docker compose -p "$PROJECT" "${PROFILE[@]}" ps --format 'table {{.Service}}\t{{.Status}}'
    say "Done. Open $(getv APP_BASE_URL) on your phone."
    echo "Backup: $BACKUP_DIR"
    exit 0
  fi
  printf '.'; sleep 3
done
printf '\n'
warn "The site didn't come up. Last log lines from the app:"
docker compose -p "$PROJECT" logs --tail 40 api || true
die "Send everything above to Claude. Your backup is in $BACKUP_DIR"
