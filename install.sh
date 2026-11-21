#!/usr/bin/env bash
# uni-sync installer for Ubuntu 24.04. Run as your normal user (it uses sudo where needed):
#   ./install.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="$(id -un)"
HOME_DIR="$HOME"
DATA="$HOME/.local/share/uni-sync"
CONF="$HOME/.config/uni-sync"
MODEL="${UNISYNC_MODEL:-qwen2.5:3b}"

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

[[ $EUID -eq 0 ]] && { echo "Run this as your normal user, not root."; exit 1; }

step "System packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip curl ca-certificates

step "Node.js 22 (for the WhatsApp bridge)"
if ! command -v node >/dev/null || [[ "$(node -p 'process.versions.node.split(".")[0]')" -lt 20 ]]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
  sudo apt-get install -y -qq nodejs
fi
node --version

step "Python environment"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

step "Headless Chromium (for the portal)"
sudo "$DIR/.venv/bin/playwright" install-deps chromium
"$DIR/.venv/bin/playwright" install chromium

step "WhatsApp bridge"
(cd "$DIR/wa-bridge" && npm ci --omit=dev --no-audit --no-fund)

step "Ollama + local model ($MODEL) — runs only on this machine"
if ! command -v ollama >/dev/null; then
  curl -fsSL https://ollama.com/install.sh | sh
fi
# Make sure Ollama only listens on localhost
sudo mkdir -p /etc/systemd/system/ollama.service.d
printf '[Service]\nEnvironment="OLLAMA_HOST=127.0.0.1:11434"\nEnvironment="OLLAMA_NUM_PARALLEL=1"\nEnvironment="OLLAMA_MAX_LOADED_MODELS=1"\n' \
  | sudo tee /etc/systemd/system/ollama.service.d/local.conf >/dev/null
sudo systemctl daemon-reload && sudo systemctl restart ollama
sleep 3
ollama pull "$MODEL"

step "Config"
mkdir -p "$CONF" "$DATA" && chmod 700 "$CONF" "$DATA"
if [[ ! -f "$CONF/config.toml" ]]; then
  cp "$DIR/config.example.toml" "$CONF/config.toml"
  sed -i "s/^model = \"[^\"]*\"/model = \"$MODEL\"/" "$CONF/config.toml"
fi
chmod 600 "$CONF/config.toml"

step "Command + background services"
chmod +x "$DIR/bin/uni-sync"
sudo ln -sf "$DIR/bin/uni-sync" /usr/local/bin/uni-sync
for f in "$DIR"/systemd/*; do
  sed -e "s|@USER@|$USER_NAME|g" -e "s|@DIR@|$DIR|g" -e "s|@HOME@|$HOME_DIR|g" -e "s|@DATA@|$DATA|g" \
    "$f" | sudo tee "/etc/systemd/system/$(basename "$f")" >/dev/null
done
sudo systemctl daemon-reload
for t in portal canvas webwork wa-process mail digest evening; do
  sudo systemctl enable --now "uni-sync-$t.timer"
done
# older single-account bridge → replaced by uni-sync-wa@<name>
if [[ -f /etc/systemd/system/uni-sync-wa.service ]]; then
  sudo systemctl disable --now uni-sync-wa.service 2>/dev/null || true
  sudo rm -f /etc/systemd/system/uni-sync-wa.service
  sudo systemctl daemon-reload
fi
# restart any already-linked WhatsApp accounts on the new code
for d in "$DATA"/wa-auth-*; do
  grep -qs '"account"' "$d/creds.json" && sudo systemctl enable --now "uni-sync-wa@${d##*/wa-auth-}"
done

cat <<EOF

✅ Installed. Next:
  1. nano $CONF/config.toml           (portal login, calendar id, Gmail)
  2. put your Google key at $CONF/service-account.json
  3. uni-sync doctor --send-test
  4. uni-sync portal-sync --dry-run
  5. uni-sync wa-link personal 971XXXXXXXXX   (and: uni-sync wa-link business 92XXXXXXXXXX)
See README.md for the details of each step.
EOF
