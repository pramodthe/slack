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
| `DAYTONA_API_KEY` | Enables cloud computers, browser/GUI operations, and Python/shell execution. |
| `DAYTONA_SNAPSHOT` | Optional desktop-ready snapshot; unset uses Daytona's default environment. |
| `DAYTONA_API_URL`, `DAYTONA_TARGET` | Optional Daytona endpoint/region overrides. |
| `DAYTONA_AUTO_STOP_MINUTES` | Cloud idle-stop fallback after a worker crash; default 15. |
| `DAYTONA_AUTO_DELETE_MINUTES` | Delete a computer after this many continuously stopped minutes; default 1440 (one day). |
| `AGENT_CHECKPOINT_PATH` | Checkpoint SQLite file; defaults to `backend/agent_checkpoints.db`. |
| `AGENT_TASK_TIMEOUT` | Execution time limit per attempt in seconds; default 900. |
| `AGENT_RECURSION_LIMIT` | Graph step limit per invocation; default 100. |

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

The **Agent tasks** panel shows progress, cancellation, resume controls, results, downloadable virtual files, and saved memories. Use **Forget** to remove an incorrect memory; ask the agent to save a corrected fact. Task state refreshes through authenticated polling. The API also forwards worker replies over WebSockets so other channels receive unread updates.

## Tools, memory, and recovery

- Planning, virtual file management, and specialist delegation use Deep Agents. Virtual files live in checkpoints; they do not expose the server filesystem. Exported task files are limited to 20 files and 200,000 characters total.
- `channel_history` reads recent messages from the current channel. `recall_memory` and `remember` persist knowledge scoped to the visible agent and channel, with source task IDs and timestamps. This is explicit keyed memory, not semantic search. Specialist helpers can read memory; the parent agent writes it.
- Web research uses [Exa Search](https://exa.ai/docs/reference/search) with a 30-second timeout, up to five sources, and up to 6,000 text characters per source. Set `EXA_API_KEY`; retrieved text is treated as untrusted source material.
- Computer and code execution use Daytona; local Docker execution is removed. See **Daytona computer use** below.
- Each task has its own checkpoint thread. Worker restart requeues interrupted tasks and resumes saved progress. Provider requests have bounded retries; task failures require **Resume**. The parent has a 40-model-call limit per invocation and 120 per task thread; computer helpers have a 30-call limit and other named helpers have a 12-call limit per invocation, in addition to graph/time limits. These are execution limits, not dollar budgets.
- Local replies are committed with task completion. Band replies get one automatic send attempt: uncertain outcomes are marked `unknown` and are never blindly resent. A local answer remains available even when Band delivery is uncertain. Full local answers may exceed the 8,000-character Band copy.

## Daytona computer use

1. Add `DAYTONA_API_KEY` from [Daytona API Keys](https://app.daytona.io/dashboard/keys) to `.env`, then restart API and worker. No credentials are copied into the sandbox. No cloud resources are created until a computer/code tool is actually called.
2. Leave `DAYTONA_SNAPSHOT` unset for the default desktop-ready environment. Custom snapshots need the desktop/VNC stack and Chromium or Firefox. The configured model must accept images. See [Daytona Computer Use](https://www.daytona.io/docs/en/computer-use/).
3. Try: `@Master use the computer to open https://example.com, inspect the page, and report what you see.` Master delegates desktop work to its computer helper. The **Computer** button in the task panel shows the latest observed screen. The Daytona dashboard link lets channel members locate that sandbox and choose **VNC** to watch its live desktop. The app panel is a screenshot viewer, not a live VNC stream.

Available tools:

| Tool | Behavior |
| --- | --- |
| `computer_screenshot` | Returns the actual image to the model, full-screen dimensions, and a fresh screen ID. |
| `computer_open_browser` | Opens an HTTP(S) URL in the remote graphical browser and returns its screenshot. |
| `computer_action` | Click, move, drag, scroll, type, press a key/modifiers, or wait; returns a new screenshot. |
| `run_python`, `sandbox_exec` | Execute Python or shell commands on the remote computer, with a 30-second limit and bounded captured output. |
| `sandbox_write_file` | Transfer text into an absolute path in the computer. Virtual draft files are separate from this filesystem. |
| `sandbox_export_file` | Copy a UTF-8 computer file into downloadable task artifacts, within the existing export limits. |

Each task gets a private, named sandbox. Its identity is stored in a separate `TaskSandbox` table, so existing application tables need no column changes. Reconnecting verifies the task/workspace ownership labels. Worker retry uses the same filesystem; a missing or deleted sandbox is reported rather than silently replaced beneath saved task state. Start a new task when its computer has expired.

Desktop operations are serialized. A screenshot ID is consumed **before** each mouse/keyboard action, so a replayed tool call cannot blindly repeat the same input. Another helper or shell command invalidates the prior observation; the agent must take a new screenshot. These guards cover the computer API; arbitrary shell commands still require care with external side effects. Delegate a single GUI workflow at a time.

The worker stops the computer on success, failure, cancellation, and graceful shutdown; files remain available for retry until auto-delete. Set the retention interval for your needs and use the Daytona dashboard to delete computers sooner. An abrupt host crash may leave a computer running until the cloud idle-stop timer. Stopped disk storage can still incur Daytona charges. Internet access follows Daytona organization policies, so some websites may be unavailable. See [Daytona VNC](https://www.daytona.io/docs/en/vnc-access/) for desktop/image prerequisites.

Screenshots and exported files can contain task data. Desktop inspection requires channel membership and returns `Cache-Control: no-store`; screenshots also persist in task checkpoints. Per-action approval for website submissions, payments, or other external changes is not implemented. Use accounts and tasks appropriate to that access model.

## Validation

```bash
cd backend
uv run pytest

cd ../frontend
npm run build
```

Offline tests exercise actual Deep Agents tool calls and helper delegation using a scripted model, checkpoint recovery across reopened connections, memory isolation, job deduplication, cancellation, and authenticated task controls. The computer tests use a fake Daytona SDK to check lifecycle/ownership, screenshot delivery to the real harness, replay-safe input, isolation, and authenticated desktop inspection. Live Band/Anthropic/Exa/Daytona calls require configured integration checks.

## Project layout

- `backend/app/main.py`: HTTP/WebSocket routes and built UI hosting.
- `backend/app/service.py`, `band_org.py`: Band synchronization and visible agent management.
- `backend/app/agent_runner.py`: lightweight Band listeners that enqueue work.
- `backend/app/tasks.py`, `worker.py`: durable queue, recovery, execution, and reply delivery.
- `backend/app/harness.py`, `agent_tools.py`: Deep Agents configuration, helpers, and scoped tools.
- `backend/app/computer.py`: task-owned Daytona lifecycle, desktop observations/actions, and remote file/command tools.
- `backend/app/models.py`, `auth.py`: persistence, session authentication, and key encryption.
- `backend/tests/`: offline regression tests.
- `frontend/src/`: React workspace and task controls.

## Deployment limits

This version supports **one API process and one worker on a shared local filesystem**. An exclusive lock prevents a second worker using the same checkpoint file. Run the worker under a process supervisor for automatic restart. Do not run independent worker replicas or different checkpoint paths against the same queue. Multi-host workers need a transactional leased queue and a shared checkpoint backend before deployment.

All channels are shared within this single workspace; this is not tenant isolation. Task controls and memory deletion require channel membership. Admin roles, per-user tool approvals, semantic memory retrieval, scheduled tasks, and multi-host execution are not implemented. Secrets and local database/checkpoint files must stay out of Git. Keep `APP_SECRET` stable; changing it invalidates encrypted agent credentials. Deleting a visible agent also requests deletion on Band, including its Band history.
