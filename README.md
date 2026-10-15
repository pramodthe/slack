# Band Slack

A Slack-style chat app where several people and a team of AI agents share channels. It runs on [Band](https://docs.band.ai): every channel is a Band chat room, every agent is a real Band agent, and a **Master** agent can create new specialist agents on its own, GrokBot-style.

## How it works

```
Browser (Alice) ─┐                                 ┌─ Master (creates agents) ─┐
Browser (Bob)  ──┼─ FastAPI app ── Band org key ───┼─ Researcher              ─┼─ Band rooms
                 │   (SQLite)    (Human API)       └─ Writer, ...             ─┘
```

- **One org-level key.** `.env` holds your Band user API key (`BAND_USER_API_KEY`, starts with `band_u_`). The app uses Band's [Human API](https://docs.band.ai/api/human-api) with that key for everything. Nobody pastes agent IDs or agent keys.
- **Channels are Band rooms.** Creating a channel creates a Band chat room at the same moment (`POST /me/chats`). The Master joins every room. Adding an agent to a channel adds it as a participant of the room.
- **Humans post through the org account.** When Alice writes in a channel, the message is posted into the Band room as `**Alice:** ...`, with real Band mentions for any agents she @mentions. The whole conversation is also visible in the Band web app.
- **Agents are created in two ways**, both registered on Band with the org key (`POST /me/agents/register`). Each agent runs inside this server with `band-sdk`'s `AnthropicAdapter`.
  - **By hand:** click **+** next to *Agents*, then pick a role and a name.
  - **By the Master:** ask `@Master` for something. It has a `create_agent` tool (and `list_agents`), so it decides which specialists it needs, creates them in the current room, and delegates by @mention.
- **Everything shows up in the channel.** Messages sent by the app's agents appear instantly. The app also polls each room, so messages posted on Band itself (from the Band UI or external agents) show up too. Agent-to-agent messages appear as indented "chatter", which you can hide with the **Agent chatter** toggle.

## Setup

You need:
- Python 3.12+ with [uv](https://docs.astral.sh/uv/).
- Node 20+.
- An [Anthropic API key](https://console.anthropic.com/).
- A [Band](https://app.band.ai) account whose plan includes the Human API. Band documents the Human API as an Enterprise feature. Without it the key is rejected with `403 plan_required`, and the app shows that error in a banner.

### 1. Configure

1. Create a user API key at [app.band.ai/users/settings](https://app.band.ai/users/settings): go to **API Keys**, then **Create API Key**. It starts with `band_u_` and is shown only once.
2. Fill in `.env` in the project root (copy `.env.example` if it doesn't exist):
   - `BAND_USER_API_KEY`: the key from step 1.
   - `ANTHROPIC_API_KEY`: your Anthropic key.
   - `APP_SECRET`: a long random string.

### 2. Run

```bash
# backend (http://localhost:8000)
cd backend
uv sync
uv run uvicorn app.main:app --reload --port 8000

# frontend dev server (http://localhost:5173), in a second terminal
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. To serve everything from one port instead, run `npm run build` in `frontend/`; the backend then serves the built UI at http://localhost:8000.

On first start the app connects to Band and does two things:
- It creates a room for `#general`.
- It registers the **Master** agent and adds it to every channel. The name is set by `MASTER_AGENT_NAME` and defaults to `Master`.

### 3. Use it

1. Sign up. Ask teammates to sign up too, from their own browsers.
2. Try: `@Master write a short launch announcement for our new app. Get a researcher to find three selling points and a writer to draft it.` The Master creates the agents it needs, and you watch them work in the channel.
3. Or create agents yourself with **+** next to *Agents*, and @mention them directly.

## Project layout

```
backend/app/
  main.py          REST routes, the /ws socket, and serving the built UI
  service.py       Channels, agents (incl. the Master's tools), message flow and Band sync
  band_org.py      The org's Band account via the Human API: agents, rooms, participants, messages
  agent_runner.py  Runs agents in-process and captures what they send
  roles.py         Role templates, the Master role and the shared team prompt
  models.py        SQLModel tables (SQLite by default)
  auth.py          Password hashing, session tokens, API-key encryption
  events.py        WebSocket fan-out
frontend/src/      React + Vite UI
```

## Tests

```bash
cd backend
uv run pytest
```

## Notes

- Agent keys issued by Band are stored encrypted with a key derived from `APP_SECRET`. If you change `APP_SECRET`, delete the database and let the app register fresh agents.
- Deleting an agent in the app also deletes it on Band, including its history.
- Each Band agent key can only be used by one running process at a time. Don't run two copies of the server against the same database.
- Band plan limits apply. For example, the Free plan allows 20 external agents. Errors from Band, such as `limit_reached`, are shown in the channel or passed back to the Master.
- The default database is a local SQLite file. When deploying to a host with an ephemeral disk (such as Render), run `uv add "psycopg[binary]"` and point `DATABASE_URL` at Postgres (`postgresql+psycopg://...`).
