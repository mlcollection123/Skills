#!/usr/bin/env bash
# Fetch the three sub-projects this app needs at the exact commits it was copied with, then install.
# They are not stored in this repo. Run from this folder:  bash setup.sh
set -euo pipefail
cd "$(dirname "$0")"

fetch() {  # fetch <folder> <repo-url> <commit>
  local dir="packages/$1" url="$2" sha="$3"
  if [ -d "$dir/.git" ] && [ "$(git -C "$dir" rev-parse HEAD)" = "$sha" ]; then echo "ok  $1 @ ${sha:0:7}"; return; fi
  rm -rf "$dir"; mkdir -p "$dir"
  git -C "$dir" init -q
  git -C "$dir" fetch -q --depth 1 "$url" "$sha"
  git -C "$dir" checkout -q FETCH_HEAD
  echo "got $1 @ ${sha:0:7}"
}

fetch Open-AI-Design-Agent https://github.com/Anil-matcha/Open-AI-Design-Agent ebc0ce7650baad0d13797ccd471c883e78be3161
fetch Open-Poe-AI          https://github.com/Anil-matcha/Open-Poe-AI          3e21ebc92d93bd699ffc6000bbcf980eaa8830cb
fetch Vibe-Workflow        https://github.com/SamurAIGPT/Vibe-Workflow          c65ce897e82bf73659c2725528c8492cd609d831

# --ignore-scripts: no dependency may run code during install. Nothing here needs it to start the dev server.
npm install --ignore-scripts
echo "Done. Start with:  npm run dev   (http://localhost:3000)"
