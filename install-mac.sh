#!/usr/bin/env bash
# uni-sync installer for macOS (needs Homebrew: https://brew.sh). Run from the uni-sync folder:
#   ./install-mac.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
MODEL="${UNISYNC_MODEL:-qwen2.5:3b}"
step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh"; exit 1; }

step "Python, Node.js and Ollama"
brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12
command -v node >/dev/null || brew install node
command -v ollama >/dev/null || brew install ollama
brew services start ollama >/dev/null 2>&1 || true

step "Python environment"
"$(brew --prefix python@3.12)/bin/python3.12" -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

step "Hidden browser for the student portal"
"$DIR/.venv/bin/playwright" install chromium

step "WhatsApp bridge"
(cd "$DIR/wa-bridge" && npm ci --omit=dev --no-audit --no-fund)

step "Local AI model ($MODEL, about 2 GB; runs only on this Mac)"
sleep 2; ollama pull "$MODEL" || echo "Run later: ollama pull $MODEL"

step "The uni-sync command"
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/uni-sync" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="$DIR"
exec "$DIR/.venv/bin/python" -P -X utf8 -m unisync "\$@"
EOF
chmod +x "$HOME/.local/bin/uni-sync"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.zshrc"; export PATH="$HOME/.local/bin:$PATH";; esac

printf '\n\033[1;32mInstalled. Starting setup...\033[0m\n'
"$HOME/.local/bin/uni-sync" setup
