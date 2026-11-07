# About this copy

This folder is a copy of https://github.com/Anil-matcha/Open-Generative-AI (MIT, see LICENSE), taken at commit
c4cdbb6 (2026-11-06). Left out to keep the repo small: the `.git` history, demo videos and marketing images
under `docs/assets` and the root `*.png` files, and the three sub-projects below.

The app needs three sub-projects that its author keeps as git submodules. `setup.sh` fetches them at these
exact commits into `packages/` (they are not stored here):

| Folder | Repository | Commit |
|---|---|---|
| packages/Open-AI-Design-Agent | Anil-matcha/Open-AI-Design-Agent | ebc0ce7650baad0d13797ccd471c883e78be3161 |
| packages/Open-Poe-AI | Anil-matcha/Open-Poe-AI | 3e21ebc92d93bd699ffc6000bbcf980eaa8830cb |
| packages/Vibe-Workflow | SamurAIGPT/Vibe-Workflow | c65ce897e82bf73659c2725528c8492cd609d831 |

## Run it
```
bash setup.sh                                  # fetch the sub-projects, npm install --ignore-scripts
NEXT_TELEMETRY_DISABLED=1 npm run dev          # http://localhost:3000, opens on /studio
```

## What to know
- It is a front end to MuAPI, a paid third-party service. Generating anything needs a MuAPI account and API key,
  and the app's own server forwards requests to `https://api.muapi.ai`.
- The key is entered in the page and kept in the browser's local storage, then sent to the app's server in an
  `x-api-key` header on each request.
- `npm audit` reports 47 known vulnerabilities in its dependencies (2 critical, 28 high). Do not expose the dev
  server to the internet, and do not put a key you cannot afford to rotate into it.
- Nothing in this copy has been security-reviewed beyond a SkillSpector-style read of install scripts and hosts.
