# Repository Guidelines

## Project Structure & Module Organization

Band Slack combines a FastAPI/SQLModel backend with a React/TypeScript frontend and Band-powered agents.

- `backend/app/`: `main.py` is the ASGI entry; `api/http.py` defines REST/WebSocket endpoints and serves the built UI; `workspace/service.py` coordinates channels, agents, and messages; `band/org.py` wraps Band's Human API; `band/listener.py` enqueues agent work. Authentication and configuration stay at the package root. Models live in `persistence/`. The per-task Deep Agent lives in `runtime/` (helpers and tools in their own folders). The queue and worker live in `tasks/`. The Daytona desktop lives in `computer/`.
- `backend/tests/`: pytest tests for API flows, service behavior, and role adapters.
- `frontend/src/`: React components in `components/`, shared types in `types.ts`, API helpers in `api.ts`, and state in `store.ts`. Styling is Tailwind 4 + shadcn/ui (`components/ui/`) + Tool UI elements (`components/assistant-ui/elements/`); the theme tokens and globals live in `src/app.css`, the only stylesheet.
- `frontend/public/`: static assets; `frontend/dist/`: generated production UI.

## Build, Test, and Development Commands

Use Python 3.12+ with uv and Node 20+. Run commands from the indicated directory.

- `cd backend && uv sync`: install backend and development dependencies.
- From `backend/`, `uv run uvicorn app.main:app --reload --port 8000`: start the API.
- `cd frontend && npm install`: install frontend dependencies.
- From `frontend/`, `npm run dev`: start Vite at port 5173.
- From `frontend/`, `npm run build`: run TypeScript checks and build production assets. The backend serves this output when available.
- From `backend/`, `uv run pytest`: run the backend suite.

## Coding Style & Naming Conventions

Follow existing code: Python uses four-space indentation, type hints, and snake_case functions/modules; classes use PascalCase. TypeScript/TSX uses two-space indentation, double quotes, semicolons, camelCase variables, and PascalCase component filenames. Keep shared API types in `types.ts`. Style with Tailwind utilities and the theme tokens in `app.css` (for example `bg-aubergine`, `text-muted-foreground`, `narrow:` for the 900px breakpoint) rather than new CSS; reuse shadcn primitives from `components/ui/` and add missing ones with `npx shadcn@latest add <name>`. Tool UI elements are added with `npx shadcn@latest add @assistant-ui/elements-<name> --yes` and moved into `components/assistant-ui/elements/` (see CLAUDE.md for the cleanup steps). No dedicated formatter or linter is configured; the frontend build checks unused declarations and switch fallthrough.

## Testing Guidelines

Name Python tests `test_*.py` with descriptive `test_*` functions. Use FastAPI's `TestClient` for HTTP/WebSocket behavior. Existing `conftest.py` selects a temporary SQLite database and clears external API keys; preserve offline test isolation. Add regression coverage for changed backend behavior. No coverage threshold or frontend test runner is configured; verify UI changes with a production build and browser checks.

## Commit & Pull Request Guidelines

History currently contains only `Initial commit: Band Slack application.`; no broader convention is established. Use concise, imperative commit subjects. PRs should explain behavior changes, include verification commands/results, link relevant issues, and provide screenshots for UI changes.

## Security & Configuration

Copy `.env.example` to `.env` and configure `BAND_USER_API_KEY`, `ANTHROPIC_API_KEY`, and `APP_SECRET`. Never commit credentials or local databases. Keep `APP_SECRET` stable: it protects stored agent keys. Run only one backend process per database to avoid competing agent sessions.
