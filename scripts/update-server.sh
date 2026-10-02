#!/usr/bin/env bash
# Update the CourtPilot server at /opt/courtpilot to a branch of the GitHub repo
# and restart it, keeping the database and the existing .env.
#
#   Usage (on the server):  bash update-server.sh [branch]      default branch: web-app
#
# What it does, in order (it stops at the first problem):
#   1. Backs up the database and .env to ~/courtpilot-backups/
#   2. Saves any files that were edited directly on the server as a patch
#   3. Switches the code to the new branch (app files are now at the repo root)
#   4. Moves .env to the root and makes it production-safe
#   5. Rebuilds and restarts everything, then checks the site responds
set -euo pipefail

BRANCH="${1:-web-app}"
APP_DIR=/opt/courtpilot
BACKUP_DIR="$HOME/courtpilot-backups/$(date +%Y%m%d-%H%M%S)"
say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mXX %s\033[0m\n' "$*"; exit 1; }

cd "$APP_DIR" || die "$APP_DIR not found"
[ -d .git ] || die "$APP_DIR is not a git checkout. Send this message to Claude."
mkdir -p "$BACKUP_DIR"

say "1/5 Backing up database and settings to $BACKUP_DIR"
ENV_FILE=""
[ -f .env ] && ENV_FILE=.env
if [ -z "$ENV_FILE" ]; then
  ENV_FILE=$(find . -mindepth 2 -maxdepth 4 -name .env -not -path './.git/*' | head -1 || true)
fi
[ -n "$ENV_FILE" ] || die "No .env file found under $APP_DIR. Send this message to Claude."
cp "$ENV_FILE" "$BACKUP_DIR/env"
echo "settings file: $ENV_FILE"
PG=$(docker ps --format '{{.Names}}' | grep -E 'postgres' | head -1 || true)
if [ -n "$PG" ]; then
  docker exec "$PG" pg_dump -U courtpilot courtpilot > "$BACKUP_DIR/courtpilot.sql"
  echo "database: $(du -h "$BACKUP_DIR/courtpilot.sql" | cut -f1) from container $PG"
else
  warn "No running postgres container found; skipping database backup (data volume is kept anyway)"
fi

say "2/5 Checking for files edited directly on the server"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  git diff > "$BACKUP_DIR/server-edits.patch"
  git stash push -m "server edits before $BRANCH update" >/dev/null
  warn "Some files had been edited on the server. Saved to $BACKUP_DIR/server-edits.patch"
  warn "(and in 'git stash'). Send that file to Claude so nothing is lost."
else
  echo "none"
fi

say "3/5 Getting branch '$BRANCH' from GitHub"
git fetch origin "$BRANCH"
git checkout -B "$BRANCH" "origin/$BRANCH"
git log --oneline -1

say "4/5 Preparing .env"
[ -f .env ] || cp "$BACKUP_DIR/env" .env
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
  setv APP_BASE_URL "http://$(curl -s -4 --max-time 5 ifconfig.me || echo 187.127.175.129):8000"
fi
echo "APP_BASE_URL=$(getv APP_BASE_URL)"

# Which login methods will work after this update
METHODS=()
[ -n "$(getv TELEGRAM_BOT_TOKEN)" ] && METHODS+=("phone code via Telegram")
[ -n "$(getv SMS_PROVIDER)" ] && [ -n "$(getv SMS_API_KEY)" ] && METHODS+=("phone code via SMS")
[ -n "$(getv SMTP_USER)" ] && [ -n "$(getv SMTP_PASSWORD)" ] && METHODS+=("email code")
[ -n "$(getv GOOGLE_CLIENT_ID)" ] && [ -n "$(getv GOOGLE_CLIENT_SECRET)" ] && METHODS+=("Google")
if [ ${#METHODS[@]} -eq 0 ]; then
  warn "No login method is configured: nobody will be able to log in."
  warn "Add TELEGRAM_BOT_TOKEN or SMTP_USER/SMTP_PASSWORD to $APP_DIR/.env, then run:"
  warn "  cd $APP_DIR && docker compose --profile polling up -d"
else
  echo "Login methods: ${METHODS[*]}"
fi

say "5/5 Rebuilding and restarting (a few minutes the first time)"
PROFILE=()
# Without a public https webhook, the bot must poll Telegram itself
[ -z "$(getv TELEGRAM_WEBHOOK_URL)" ] && [ -n "$(getv TELEGRAM_BOT_TOKEN)" ] && PROFILE=(--profile polling)
docker compose "${PROFILE[@]}" up -d --build --remove-orphans

printf 'Waiting for the site'
for _ in $(seq 1 40); do
  if curl -fs localhost:8000/health >/dev/null 2>&1; then
    printf '\n'
    say "Done. Open $(getv APP_BASE_URL) on your phone."
    docker compose "${PROFILE[@]}" ps --format 'table {{.Service}}\t{{.Status}}'
    exit 0
  fi
  printf '.'; sleep 3
done
printf '\n'
warn "The site didn't come up. Last log lines from the app:"
docker compose logs --tail 40 api
die "Send the output above to Claude. Your backup is in $BACKUP_DIR"
