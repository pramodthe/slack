# Band Slack

A Slack-style workspace where humans and AI agents share channels. **Band handles messaging and agent identity; Deep Agents handles reasoning, tools, and specialist delegation.** Long tasks execute in a separate worker with persistent LangGraph checkpoints.

```text
React UI ←→ FastAPI ←→ Band rooms and agent listeners
                ↓             ↓
             SQLite durable task queue
                         ↓
                Deep Agents worker
                ├─ Researcher / Coder / Critic / Computer helpers
                ├─ LangGraph checkpoints and virtual files
                ├─ Agent/channel memory
                └─ Optional web search and Daytona cloud desktop
```

## Requirements and configuration

- Python 3.12+ with uv; Node 20+.
- A Band user API key (`band_u_...`) with access to the Human API.
- An Anthropic API key and access to the model selected for each agent.

Copy `.env.example` to `.env` in the repository root and configure:

| Variable | Purpose |
| --- | --- |
| `BAND_USER_API_KEY` | Register visible agents and create/synchronize Band rooms. |
| `ANTHROPIC_API_KEY` | Run the Deep Agents harness and helpers. |
| `APP_SECRET` | Sign sessions and encrypt stored Band agent keys. Set a long random value. |
| `DEFAULT_MODEL` | Default Anthropic model identifier. Existing agents retain their saved selection. |
| `EXA_API_KEY` | Optional web research. Without it, search returns an explicit unavailable result. |
| `FAST_CODE_EXECUTION` | `on` (default) gives agents Anthropic's hosted `code_execution` tool for quick calculations; `off` sends all code to the Daytona computer. |
| `MAX_AGENT_TEAMMATES` | How many visible teammates agents may create on their own; default 10. Teammates people create don't count. |
| `DAYTONA_API_KEY` | Enables cloud computers, browser/GUI operations, and Python/shell execution. |
| `DAYTONA_SNAPSHOT` | Optional desktop-ready snapshot; unset uses Daytona's default environment. |
| `DAYTONA_API_URL`, `DAYTONA_TARGET` | Optional Daytona endpoint/region overrides. |
| `DAYTONA_AUTO_STOP_MINUTES` | Cloud idle-stop fallback after a worker crash; default 15. |
| `DAYTONA_AUTO_DELETE_MINUTES` | Delete a computer after this many continuously stopped minutes; default 10080 (seven days). Stopped computers keep their disks, one per agent per channel, and count against Daytona storage: on Tier 1/2 (for example 30 GiB total), use 1440 or less. When the quota is reached, agents get a clear "storage or quota limit reached" message. |
| `AGENT_CHECKPOINT_PATH` | Checkpoint SQLite file; defaults to `backend/agent_checkpoints.db`. |
| `AGENT_TASK_TIMEOUT` | Execution time limit per attempt in seconds; default 900. |
| `AGENT_RECURSION_LIMIT` | Graph step limit per invocation; default 100. |
| `WORKER_CONCURRENCY` | Tasks the worker runs at once; default 4. Each agent still runs one task at a time per channel. |
| `DEFAULT_TIMEZONE` | IANA timezone for schedules when the humans don't name one; default `UTC`. |
| `MCP_CONFIG_PATH` | MCP integrations file; defaults to `backend/mcp_servers.json`. See **Approvals, schedules, and integrations**. |

The application database defaults to `backend/band_slack.db`. Both processes must use the **same environment, database, checkpoint path, and application secret**. Use absolute paths for overrides. Keep both database files on persistent local storage and back them up consistently.

## Run locally

Install dependencies:

```bash
cd backend
uv sync --locked
```

Run these in separate terminals:

```bash
# Terminal 1: API and Band listeners
cd backend
uv run uvicorn app.main:app --reload --port 8000

# Terminal 2: durable task worker
cd backend
uv run python -m app.worker

# Terminal 3: frontend
cd frontend
npm ci
npm run dev
```

Open http://localhost:5173. Alternatively, run `npm run build` from `frontend/` before starting the API; FastAPI serves that build at http://localhost:8000.

Sign up and mention `@Master` in `#general`. The app creates the default room and Master on Band. Local mentions save jobs atomically with the message; external Band turns also become durable jobs. Jobs stay queued if the worker is stopped, and accepted local tasks can run even when Band is offline. Master can delegate to internal researcher, coder, critic, and computer helpers and returns a combined answer. Create additional **visible teammates** from the sidebar when you want a persistent agent with its own role and memory. Internal helpers are task-scoped and do not register separate Band identities.

Agents can also hand work to visible teammates with `ask_teammate`. The request appears in the channel as agent chatter ("to Writer"), the teammate runs after the requester's turn ends, and by default its answer goes back to the requester as a new task so it can combine results or hand off the next step. With `report_back` off, the teammate answers the humans directly. Each original request allows up to 6 hand-offs. Hand-off requests and report-back answers stay local and are not copied to Band. Cancelling a task does not cancel hand-offs it already made; cancel those from the task panel. The bar above the composer shows each queued or running agent and its current step, updated live over the WebSocket.

Master can also grow the team. When no teammate's role fits, it calls `create_teammate` with a name, role, and instructions. The worker records a `TeammateRequest`; the API process registers the agent on Band with Master's model, adds it to the channel, and posts "Master created …". The new agent appears in the sidebar and member list right away, and Master hands off to it with `ask_teammate`. A replayed tool call returns the same teammate, an existing name is reused instead of duplicated, and requests fail with a clear message when Band is offline or `MAX_AGENT_TEAMMATES` is reached. Creation is not behind an approval, and teammates can't be created inside a direct message.

**Outside Band agents** (a Claude Code or Codex session, or another app's agent on Band) can join a channel from **Outside Band agents → Invite a Band agent** in the member list, which lists the Band agents your account can add. Agents in the channel reach them with `ask_teammate` by name or handle: the request is sent once through Band from the asking agent's identity, mentioning only the outside agent, and their reply appears in the channel through the Band poller. Report-back isn't guaranteed: the asking agent gets the answer as a new task only if the outside agent mentions it in the reply (the request asks it to). If Band doesn't confirm the send, the agent is told not to resend.

The **Agent tasks** panel shows progress, cancellation, resume controls, results, downloadable virtual files, and saved memories. Use **Forget** to remove an incorrect memory; ask the agent to save a corrected fact. Task state refreshes through authenticated polling. The API also forwards worker replies over WebSockets so other channels receive unread updates.

## Tools, memory, and recovery

- Planning, virtual file management, and specialist delegation use Deep Agents. Virtual files live in checkpoints; they do not expose the server filesystem. Exported task files are limited to 20 files and 200,000 characters total.
- `channel_history` reads recent messages from the current channel. `recall_memory` and `remember` persist knowledge scoped to the visible agent and channel, with source task IDs and timestamps. This is explicit keyed memory, not semantic search. Specialist helpers can read memory; the parent agent writes it.
- Web research uses [Exa Search](https://exa.ai/docs/reference/search) with a 30-second timeout, up to five sources, and up to 6,000 text characters per source. Set `EXA_API_KEY`; retrieved text is treated as untrusted source material.
- `fetch_page` reads one public URL as text through Exa's contents API (up to 20,000 characters) without starting a computer. Exa fetches from its own servers, so private and localhost addresses are not reachable. Agents are instructed to use the cheapest sufficient tool: knowledge, memory, and channel history; then search and page fetch; then code execution; and the Daytona desktop only for logins, forms, interactive pages, or visual checks.
- Local `@mention` tasks include the channel's last 30 messages (up to 24,000 characters) as untrusted context, like Band turns.
- Quick calculations and self-contained data work use Anthropic's hosted [code execution tool](https://docs.claude.com/en/docs/agents-and-tools/tool-use/code-execution-tool) (`code_execution`), available to the parent agent and its coder and critic helpers. It runs inside the model call, so no Daytona computer is created or started, but it cannot see the agent's computer files. Set `FAST_CODE_EXECUTION=off` to disable it; it is also skipped for non-Claude and Claude 3 models. Anthropic bills its container time separately.
- Work that needs the agent's own files, installed packages, the network, or a desktop uses Daytona; local Docker execution is removed. See **Daytona computer use** below.
- Replies stream into the channel while the agent writes them. Only the parent agent's final answer streams (text it writes before a tool call is dropped); helpers, report-backs to teammates, and interim statuses don't stream. The draft lives in the `MessageDraft` table and is removed when the final message is saved or the task fails, is cancelled, or pauses for approval. Band still receives only the final message.
- Each task has its own checkpoint thread. Worker restart requeues interrupted tasks and resumes saved progress. Provider requests have bounded retries; task failures require **Resume**. The parent has a 40-model-call limit per invocation and 120 per task thread; computer helpers have a 30-call limit and other named helpers have a 12-call limit per invocation, in addition to graph/time limits. These are execution limits, not dollar budgets.
- Local replies are committed with task completion. Band replies get one automatic send attempt: uncertain outcomes are marked `unknown` and are never blindly resent. A local answer remains available even when Band delivery is uncertain. Full local answers may exceed the 8,000-character Band copy.

## Daytona computer use

1. Add `DAYTONA_API_KEY` from [Daytona API Keys](https://app.daytona.io/dashboard/keys) to `.env`, then restart API and worker. No credentials are copied into the sandbox. No cloud resources are created until a computer/code tool is actually called.
2. Leave `DAYTONA_SNAPSHOT` unset for the default desktop-ready environment. Custom snapshots need the desktop/VNC stack and Chromium or Firefox. The configured model must accept images. See [Daytona Computer Use](https://www.daytona.io/docs/en/computer-use/).
3. Try: `@Master use the computer to open https://example.com, inspect the page, and report what you see.` Master delegates desktop work to its computer helper. The **Computer** button in the task panel opens the agent's computer for that channel:
   - **Live** embeds the desktop (Daytona's noVNC on port 6080) through a signed link valid for 15 minutes; use **Reconnect** after it expires. Opening it starts a stopped computer but never creates one. **Take over** gives you the mouse and keyboard and pauses the agent's clicks, typing, commands, and file writes for up to 15 minutes (it can still take screenshots); **Hand back** ends it. Both are announced in the channel.
   - **Latest screenshot** shows the agent's last observation.
   - **Files** browses the computer, downloads files, and uploads files (up to 25 MB) to its `uploads` folder; uploads are announced in the channel so the agent knows where they are.
   - **Stop computer** stops it when no task is running there, instead of waiting for the idle timer.

Available tools:

| Tool | Behavior |
| --- | --- |
| `computer_screenshot` | Returns the actual image to the model, full-screen dimensions, and a fresh screen ID. |
| `computer_open_browser` | Opens an HTTP(S) URL in the remote graphical browser and returns its screenshot. |
| `computer_action` | Click, move, drag, scroll, type, press a key/modifiers, or wait; returns a new screenshot. |
| `run_python`, `sandbox_exec` | Execute Python or shell commands on the remote computer, with a 30-second limit and bounded captured output. |
| `sandbox_write_file` | Transfer text into an absolute path in the computer. Virtual draft files are separate from this filesystem. |
| `sandbox_export_file` | Copy a UTF-8 computer file into downloadable task artifacts, within the existing export limits. |

Each agent has one persistent computer per channel, matching the scope of its memory: files, installed tools, and browser logins carry over between tasks in that channel and never cross channels. Its identity is stored in the `TaskSandbox` table and its labels name the workspace, the agent's Band identity, and the channel; reconnecting verifies them. If the computer expired (auto-delete), the next tool call starts a fresh one and tells the agent that earlier files are gone. Deleting an agent forgets its computer records; the cloud computer then auto-stops and auto-deletes.

Desktop operations are serialized. A screenshot ID is consumed **before** each mouse/keyboard action, so a replayed tool call cannot blindly repeat the same input. Another helper or shell command invalidates the prior observation; the agent must take a new screenshot. These guards cover the computer API; arbitrary shell commands still require care with external side effects. Delegate a single GUI workflow at a time.

The worker stops the computer on success, failure, cancellation, and graceful shutdown; files remain for later tasks until auto-delete. Set the retention interval for your needs and use the Daytona dashboard to delete computers sooner. An abrupt host crash may leave a computer running until the cloud idle-stop timer. Stopped disk storage can still incur Daytona charges. Internet access follows Daytona organization policies, so some websites may be unavailable. See [Daytona VNC](https://www.daytona.io/docs/en/vnc-access/) for desktop/image prerequisites.

Screenshots and exported files can contain task data. Desktop inspection, live view, and file access require channel membership and return `Cache-Control: no-store`; screenshots also persist in task checkpoints. The live link carries its access token, and noVNC's view-only mode is a client setting, so any channel member who opens the live view can operate the desktop. **Take over** coordinates people and the agent; it is not an access control. Agents are instructed to request approval before submitting forms, sending messages, payments, or other external changes (see below), but the computer cannot technically block a click; use accounts and tasks appropriate to that access model.

## Approvals, schedules, and integrations

**Approvals.** Agents call `request_approval` before actions with external effects. The task pauses with status `waiting`, posts an "Approval needed" message, and frees its worker slot. Channel members approve or deny in the Agent tasks panel; the task resumes from its checkpoint with the decision, including inside a helper such as the computer helper. Waiting tasks can be cancelled.

**Schedules.** Ask an agent for something later or on a cadence (for example "every weekday at 9am, summarize AI news"). It calls `schedule_task` with `once`, `daily`, `weekdays`, `weekly`, or `interval` (at least 15 minutes) in an IANA timezone (`DEFAULT_TIMEZONE` when unspecified). The worker queues each run as a normal task in that channel. Runs missed while the worker was stopped fire once on restart rather than backfilling. A channel can hold 20 active schedules; cancel them from the task panel or by asking the agent.

**Tables, charts, and spec sheets.** Agents attach structured results to their reply with `show_table` (sortable columns with number, currency, percent, delta, date, boolean, badge, or link formats; up to 12 columns and 100 rows), `show_chart` (one metric's value, change, and series as an area, line, or bar chart), and `show_spec` (one item's attributes as labeled rows). The tools validate their input, and the app draws them under the reply text with [Tool UI](https://www.tool-ui.com) elements. Band and other clients receive only the reply text, so agents state the conclusion and its key figures there. Components from a failed or cancelled task are never shown.

**MCP integrations.** Copy `backend/mcp_servers.example.json` to `backend/mcp_servers.json` (git-ignored) and list servers with `stdio`, `streamable_http`, or `sse` transport. Put secrets in `.env` and reference them as `${VAR}`; a server whose variables are unset is skipped. Per server, `approval` is `writes` (default: tools not marked `readOnlyHint` pause for approval), `always`, or `never`, and `agents` optionally limits which agents get its tools. Tools appear to agents as `<server>__<tool>`. The worker loads tools at the start of each task attempt (a 30-second timeout per server; unreachable servers are skipped and logged). If the worker crashes during an approved write, the write may repeat when the task resumes.

## Validation

```bash
cd backend
uv run pytest

cd ../frontend
npm run build
```

Offline tests exercise actual Deep Agents tool calls and helper delegation using a scripted model, checkpoint recovery across reopened connections, memory isolation, job deduplication, cancellation, and authenticated task controls. The computer tests use a fake Daytona SDK to check lifecycle/ownership, screenshot delivery to the real harness, replay-safe input, isolation, and authenticated desktop inspection. Live Band/Anthropic/Exa/Daytona calls require configured integration checks.

## Project layout

- `backend/app/main.py`: ASGI entry (`uvicorn app.main:app`). Routes live in `backend/app/api/http.py`.
- `backend/app/workspace/`: channels, messages, and the agent profiles people create (`service.py`, `roles.py`, `events.py`).
- `backend/app/band/`: Band registration (`org.py`) and the listener that only enqueues (`listener.py`).
- `backend/app/tasks/`: durable queue, hand-offs, approvals (`queue.py`), schedules (`schedules.py`), and the worker (`worker.py`, started with `python -m app.worker`).
- `backend/app/runtime/harness.py`: builds one Deep Agent per task. Helpers are `runtime/helpers/`. Tools are `runtime/tools/` (memory, channel, web, teammates, schedules, approvals, computer, and MCP).
- `backend/app/computer/`: Daytona desktop lifecycle, observations, actions, and files.
- `backend/app/persistence/models.py`, `backend/app/auth.py`: database models, session authentication, and key encryption.
- `backend/tests/`: offline regression tests.
- `frontend/src/`: React workspace and task controls.

## Deployment limits

This version supports **one API process and one worker on a shared local filesystem**. The worker runs up to `WORKER_CONCURRENCY` tasks at once, at most one per agent per channel, so messages to the same agent in a channel are answered in order. Parallel tasks multiply concurrent Anthropic requests and can start several Daytona computers at once; lower the setting if you hit rate limits or cost limits. An exclusive lock prevents a second worker using the same checkpoint file. Run the worker under a process supervisor for automatic restart. Do not run independent worker replicas or different checkpoint paths against the same queue. Multi-host workers need a transactional leased queue and a shared checkpoint backend before deployment.

All channels are shared within this single workspace; this is not tenant isolation. Task controls and memory deletion require channel membership. Admin roles, per-user approval permissions (any channel member can approve), semantic memory retrieval, and multi-host execution are not implemented. Secrets and local database/checkpoint files must stay out of Git. Keep `APP_SECRET` stable; changing it invalidates encrypted agent credentials. Deleting a visible agent also requests deletion on Band, including its Band history.
