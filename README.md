# DrakBot

A Discord bot built around a bullet economy — users earn and spend bullets to interact with each other in voice channels.

---

## Features

### Bullet Economy
Bullets are a per-server currency stored in a local SQLite database.

| Command | Description |
|---|---|
| `/ammo [@user]` | Check your bullet count, or another user's |
| `/trade @user amount` | Send bullets to another user |
| `/daily` | Claim your daily bullet allowance (once per calendar day, resets at midnight EST) |

### Combat
| Command | Description |
|---|---|
| `/shoot @user` | Spend 1 bullet to disconnect a user from voice. Rolls a d20 — a **1** backfires and times you out, a **20** is a critical hit that also times out the target |

### Admin Commands
Require the role set in `BULLET_ADMIN_ROLE`.

| Command | Description |
|---|---|
| `/arm @user amount` | Add bullets to a user |
| `/disarm @user` | Remove all bullets from a user |
| `/flee [@user]` | When a user is set, everyone else in their voice channel is moved to an adjacent channel whenever they join. Omit the user to disable |

### Deathroll
A WoW-inspired gambling game. Players alternate rolling a shrinking number — whoever rolls **1** loses and pays the other player the staked bullets.

| Command | Description |
|---|---|
| `/deathroll amount` | Post an open challenge anyone can accept |
| `/deathroll amount @user` | Challenge a specific user |

- Minimum stake: **5 bullets**
- Bullets are held in escrow when a game starts
- Each player has **30 seconds** to roll — a warning is posted at 20 seconds
- The challenger or challengee can cancel/decline before the game starts
- Only one active or pending challenge per user at a time

### Stream Guard
When enabled, any user who starts streaming video (camera or Go Live) within 5 seconds of joining a voice channel is automatically disconnected. Controlled via the `STREAM_GUARD_ENABLED` env var.

---

## Setup

### Prerequisites
- Python 3.12+
- A Discord bot token (see [Creating a Discord Bot](#creating-a-discord-bot) below)

### Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/movery/DrakBot.git
   cd DrakBot
   ```

2. **Create and activate a virtual environment**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables**
   ```bash
   cp .env.example .env
   ```
   Then edit `.env` and fill in your values:
   ```
   DISCORD_TOKEN=your_bot_token_here
   BULLET_ADMIN_ROLE=YourRoleName
   DAILY_BULLET_AMOUNT=5
   STREAM_GUARD_ENABLED=false
   ```

5. **Run the bot**
   ```bash
   python main.py
   ```
   Slash commands are synced to Discord automatically on startup.

### Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | Yes | — | Your bot's token from the Discord Developer Portal |
| `BULLET_ADMIN_ROLE` | Yes | — | Exact name of the Discord role that can use admin commands |
| `DAILY_BULLET_AMOUNT` | No | `5` | Bullets granted by `/daily` |
| `STREAM_GUARD_ENABLED` | No | `false` | Set to `true` to enable the stream guard feature |

---

## Running the Tests

The project includes a test suite built on Python's standard-library `unittest` module — no extra dependencies required. From the project root, with the virtual environment activated:

```bash
source .venv/bin/activate && python -m unittest discover -s tests
```

The suite lives in `tests/` and covers:

- **`tests/test_db.py`** — every function in the bullet-economy database layer, run against a throwaway SQLite file (the schema migration, atomic deductions, transfers, and the daily-claim cooldown/reset cycle).
- **`tests/test_deathroll.py`** — the deathroll message builders and the in-memory game/pending-challenge state tracking.

Discord interaction handlers are not unit-tested (they require a live gateway connection); verify those by running the bot. Run the suite before and after any change to `db.py` or the deathroll game logic to confirm behavior is preserved.

---

## Creating a Discord Bot

### 1. Create an Application
1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application**, give it a name, and confirm

### 2. Create the Bot
1. In the left sidebar, click **Bot**
2. Click **Add Bot** and confirm
3. Under **Token**, click **Reset Token** and copy it — this is your `DISCORD_TOKEN`
4. Keep this token secret; do not commit it to version control

### 3. Enable Required Intents
Still on the **Bot** page, scroll to **Privileged Gateway Intents** and enable:
- **Server Members Intent** — needed to look up member info
- **Voice State Intent** — required for all voice channel features (shoot, flee, stream guard, deathroll timeouts)

Click **Save Changes**.

### 4. Invite the Bot to Your Server
1. In the left sidebar, click **OAuth2 → URL Generator**
2. Under **Scopes**, select:
   - `bot`
   - `applications.commands`
3. Under **Bot Permissions**, select:
   - `Send Messages`
   - `Move Members` — required to disconnect/move users from voice
   - `Moderate Members` — required for the timeout mechanic on critical hits
4. Copy the generated URL, open it in your browser, and select the server to invite the bot to

### 5. Set Up the Admin Role
Create a role in your Discord server whose name exactly matches the `BULLET_ADMIN_ROLE` value in your `.env`. Assign it to anyone who should be able to use admin commands (`/arm`, `/disarm`, `/flee`).
