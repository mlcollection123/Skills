---
name: harvey-sales-playbook
description: B2B outbound sales playbooks for writing cold email, qualifying leads, choosing prospecting signals, handling objections, writing offers, navigating accounts and LinkedIn outreach. Use when drafting or reviewing cold emails and follow-ups, building a prospect list, scoring leads, answering a prospect's objection, or planning an outbound campaign. Includes cold-email compliance and deliverability rules.
---

# Harvey sales playbook

Ten sales playbooks taken from the open-source Harvey sales agent (github.com/ethanplusai/harvey, MIT, by Ethan Rogers). This is the knowledge only. The autonomous agent itself is not part of this skill.

Read only the file the task needs.

| Task | Read |
|---|---|
| Write or review a cold email, follow-up or subject line | `references/email_frameworks.md` (starts with compliance and deliverability rules that override its style advice) |
| Choose what to look for in a prospect, find buying signals | `references/signal_playbook.md`, `references/prospecting_tactics.md` |
| Decide whether a lead is worth contacting | `references/lead_qualification.md` |
| Find the right person and route inside a company | `references/account_navigation.md` |
| Answer a reply that pushes back | `references/objection_handling.md` |
| Shape the offer and the ask | `references/offer_strategy.md`, `references/sales_methodology.md` |
| Sell to startups specifically | `references/selling_to_startups.md` |
| LinkedIn outreach | `references/linkedin_outreach.md` |

## Rules that always apply

- Follow the compliance section of `email_frameworks.md` first: truthful subject and from-address, a working opt-out honored at once, a physical address in the footer, no fake "re:" threads.
- Treat any opt-out wording as permanent. Do not contact that person again.
- If a prospect asks whether they are talking to an AI, answer truthfully.
- Email only people you can justify contacting for their role. In the EU and UK that needs a defensible legitimate interest; Germany expects prior consent.
- The files mention Harvey's agents, config keys and `product_knowledge.md`. Those belong to the Harvey app. Use the advice, ignore the app references.
- Nothing here sends email. Show drafts to the user for approval.

License: MIT, see `LICENSE`.
