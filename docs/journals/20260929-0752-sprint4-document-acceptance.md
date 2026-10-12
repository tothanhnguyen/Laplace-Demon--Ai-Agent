# Sprint 4 document acceptance session

## Context

Continued Sprint 4 live acceptance through the Telegram bot. User uploaded the prepared TXT, DOCX and PDF fixtures individually.

## Findings

- PDF summary was complete because its bounded observation fit within the generic tool observation limit.
- TXT and DOCX summaries lost early evidence because the generic 1,200-character observation limit truncated document payloads.
- DOCX pagination also allowed an early final answer or context eviction to hide earlier units.

## Changes

- Added a bounded 6,000-character observation limit for `read_document`; other tools remain at 1,200 characters.
- Added trusted runtime document progress and completion ledgers.
- Runtime now continues unread document units before accepting a final answer.
- Final document synthesis uses a lean context with all gathered document evidence and no unnecessary tool catalog.
- Added filtered tool specifications for overflow recovery, including pending authorized reminders.
- Added regression coverage for document truncation, pagination, model/runtime continuation, final evidence retention, and document-plus-reminder overflow.
- Explicit web-search requests now receive runtime-enforced `web_search`; explicit source-reading requests also receive `fetch_page` from a URL returned by search, even if the model prematurely proposes a final answer.

## Verification

- Isolated DOCX flow read all units through `table:1/row:4`, including all owners and deadlines.
- Isolated TXT flow read all source lines and retained the objective, owners, deadlines and risk.
- Full Python suite: `156 passed`.
- Ruff: passed.
- Telegram bot restarted with the corrected build.
- User-provided Telegram retest outputs (2026-09-29): DOCX returned five findings with paragraph/table locators; TXT returned five findings and a synthesis with line locators. Both retained goals, owners, deadlines, and risks in the shown responses.
- User-provided Telegram screenshot shows reminder `#4` created pending at `2026-09-29T01:59:26Z` (08:59:26 Vietnam time) after the 08:54 request, then a reminder notification at 08:59. This verifies live create-and-deliver for one reminder, not restart recovery, update/cancel, or exact delivery SLA.
- Debug evidence from the failed weather turn: the assistant persisted a final answer with one `agent_action` LLM call and no `Task`/`ToolCall` rows. A direct Tavily search and extract both succeeded, confirming that the failure was model tool selection.
- Shared-runtime smoke with the configured LLM and real Tavily completed `web_search → fetch_page` and returned a Vietnamese weather summary with the extracted URL.
- Focused chat/web tests: `63 passed`; full Python suite after the fix: `164 passed`; Ruff: passed.
- An initial bot restart attempt did not reach confirmed readiness; direct Telegram `getMe` timed out after 45 seconds and that process was stopped. A later bot run successfully handled a real Telegram request.
- Live Telegram task `14` stored successful `web_search` (14,501 ms) and `fetch_page` (1,811 ms) calls. The extracted URL matched the citation in the user's screenshot; the response included the weather summary and source preview. One live L2 web case is now verified.
- Manual citation cross-check against the bundled acceptance fixtures passed: TXT references lines 4–9; DOCX references paragraphs 2–4 and `table:1/row:2–4`.
- The owner confirms a later corrected Telegram E2E completed web search, source reading, reminder creation, restart against the same DB, and post-restart delivery. This is owner-attested; run-specific IDs/timestamps were not added to this workspace.
- The available local DB contains an earlier, separate attempt: task `16` called `create_reminder → web_search` without `fetch_page`; reminder `#5` was sent normally in the same process with Telegram message ID `107`. Do not cite that attempt as restart evidence.
- A sanitized public Sprint 4 report with an acceptance video checklist is present at `docs/reports/261002-bao-cao-sprint-04.md`.
- On 2026-10-02 the owner confirmed all required Telegram acceptance cases passed, including web/source reading, documents, reminder CRUD/restart/recovery, `/forget`, failure paths and scan-only PDF disclosure. This is owner-attested; raw run IDs, timestamps and Telegram message IDs were not added here.

## Resume point

DOCX/TXT locators match the bundled fixtures. The owner confirms all Sprint 4 acceptance gates passed; the plan, Phase 5 checklist and sanitized public report record completion. The later runs remain owner-attested rather than recorded with raw run-specific identifiers. The earlier local DB records are not evidence for those later runs. Sprint 4 is complete; no further acceptance work is pending. Do not commit, stage or push changes; user handles Git delivery.
