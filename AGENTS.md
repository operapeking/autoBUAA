# autoBUAA agent notes

## Verification

```bash
uv run --extra dev pytest backend/tests -q
npm --prefix frontend test
npm --prefix frontend run build   # includes vue-tsc type checking
uvx ruff check --select F,I,E9,B --ignore B008 backend/app
uvx ruff format backend/app
```

## Layout

- `backend/app/buaa/`: upstream adapters (SSO/WebVPN session, classroom, boya, ygdk, td). No DB writes except session state.
- `backend/app/framework/`: generic scheduler, triggers, task registry. Business-agnostic.
- `backend/app/modules/`: business logic. `services.py` syncs platforms and plans course windows/reminders via `CourseRefresh`; `automation.py` reconciles module switches with schedules; `tasks.py` registers task handlers.
- `backend/app/api/v1/`: one router per business plus `modules` (module settings) and `network` (network mode).
- Schema and data changes go in a new Alembic revision under `backend/alembic/versions/`; `seed_defaults` only inserts missing defaults.
- Frontend views map to the nav tabs: `CampusView` (校园事务), `ActivityView` (执行记录), `SettingsView` (设置). Display labels and formatters live in `frontend/src/lib/format.ts`.
