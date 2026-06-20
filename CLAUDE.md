# DrakBot — Claude Guide

## Running the Bot

Always use the virtual environment. The launcher is the intended entry point — it enforces a single instance (advisory file lock on `drakbot.lock`), sets up per-run logging under `logs/`, and verifies `DISCORD_TOKEN` before connecting:
```bash
source .venv/bin/activate && python launcher.py
```

`python main.py` still works for quick dev (it falls back to discord.py's own console logging), but it does **not** take the single-instance lock — only `launcher.py` does.

**Important:** When restarting, kill existing processes first in a separate command, then start fresh. Combining kill and start in a single shell command causes the shell to kill itself before the new bot launches:
```bash
# Step 1 — kill
kill $(ps aux | grep "[p]ython /home/movery/DrakBot/\(launcher\|main\).py" | awk '{print $2}') 2>/dev/null; true
# Step 2 — start (separate command)
source .venv/bin/activate && python launcher.py
```

Multiple instances connecting with the same token will race to handle interactions — old instances must be cleared before starting a new one. The launcher's lock prevents a second launcher from starting, but a stale `main.py` process must still be killed manually.

## Architecture

### Cogs
Each feature lives in its own file under `cogs/`. Every cog must be registered in `main.py`'s `setup_hook()`:
```python
await bot.load_extension("cogs.my_feature")
```
Each cog file must expose an `async def setup(bot)` function that calls `await bot.add_cog(...)`.

### Database (`db.py`)
Raw `sqlite3` — no ORM. All DB access goes through `db.py`. The `bullets` table is the single persistent store:

```
bullets(guild_id, user_id, amount, nickname, last_daily)
```

**SQLite migration note:** `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` is not supported on the installed SQLite version. Use a `PRAGMA table_info` check instead:
```python
columns = [row[1] for row in conn.execute("PRAGMA table_info(bullets)").fetchall()]
if "my_column" not in columns:
    conn.execute("ALTER TABLE bullets ADD COLUMN my_column TEXT")
```

### Slash Commands
All commands use `discord.app_commands` on `commands.Cog` subclasses. Commands are synced to Discord via `bot.tree.sync()` in `on_ready`. After adding a new command, Discord can take up to a minute to propagate it to clients.

### In-memory vs. Persistent State
- **Persistent** (survives restart): store in SQLite via `db.py`
- **Ephemeral** (lost on restart): plain Python dicts/sets on the cog instance — used for active deathroll games, pending challenges, flee mode target, stream guard join times

## Key Patterns

### Admin permission check
```python
BULLET_ADMIN_ROLE = os.getenv("BULLET_ADMIN_ROLE", "")
discord.utils.get(interaction.user.roles, name=BULLET_ADMIN_ROLE) is not None
```

### Discord UI Views
- `timeout=None` — buttons never expire but state is lost on restart (no persistence)
- `timeout=N` — discord.py resets the timer after each button interaction
- Always call `self.stop()` when a view is done to cancel pending timeouts
- Store `self.message` on the view to allow `on_timeout` to edit the original message

### Ephemeral error replies in button handlers
```python
await interaction.response.send_message("Error message", ephemeral=True)
return
```
Do not call `self.stop()` on recoverable errors — the user should still be able to interact with the view.

## Environment Variables

| Variable | Purpose |
|---|---|
| `DISCORD_TOKEN` | Bot token |
| `BULLET_ADMIN_ROLE` | Role name that gates admin commands |
| `DAILY_BULLET_AMOUNT` | Bullets granted per `/daily` claim (default: 5) |
| `STREAM_GUARD_ENABLED` | `true` to auto-disconnect early streamers (default: false) |

## Testing

Tests live in `tests/` and use the stdlib `unittest` module (no extra dependencies). Run them with:
```bash
source .venv/bin/activate && python -m unittest discover -s tests
```

- `tests/test_db.py` — characterization tests for every `db.py` function, run against a throwaway SQLite file (`db.DB_PATH` is repointed in `setUp`).
- `tests/test_deathroll.py` — pure message builders and the in-memory game/pending state tracking on `DeathrollCog`.

When changing `db.py` or deathroll state logic, run the suite first to capture a green baseline, then again after the change. Discord interaction handlers are not unit-tested (they require heavy mocking) — verify those by running the bot.

## Cogs Overview

| File | Key responsibility |
|---|---|
| `cogs/bullets.py` | `/arm`, `/disarm`, `/shoot`, `/trade`, `/ammo` |
| `cogs/daily.py` | `/daily` — once-per-calendar-day bullet grant (resets midnight EST) |
| `cogs/flee.py` | `/flee` — moves other users out of a designated user's voice channel |
| `cogs/deathroll.py` | `/deathroll` — bullet gambling game with Discord UI buttons |
| `cogs/stream_guard.py` | Disconnects users who stream within 5s of joining voice |
