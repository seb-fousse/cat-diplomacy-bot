from tortoise import Tortoise

DB_URL = "sqlite://cat_game.db"


async def init_db():
    await Tortoise.init(
        db_url=DB_URL,
        modules={"models": ["models"]},
    )
    await Tortoise.generate_schemas()
    await _migrate()


async def _migrate():
    # generate_schemas only creates missing tables, never missing columns
    conn = Tortoise.get_connection("default")
    _, rows = await conn.execute_query("PRAGMA table_info(players)")
    if not any(row["name"] == "gold_balance" for row in rows):
        print("[db] Adding players.gold_balance column")
        await conn.execute_script(
            'ALTER TABLE "players" ADD COLUMN "gold_balance" INT NOT NULL DEFAULT 0'
        )

    _, rows = await conn.execute_query("PRAGMA table_info(confessional_log)")
    existing = {row["name"] for row in rows}
    confessional_columns = {
        "guild_id": 'ALTER TABLE "confessional_log" ADD COLUMN "guild_id" BIGINT',
        "attachment_path": 'ALTER TABLE "confessional_log" ADD COLUMN "attachment_path" VARCHAR(500)',
        "attachment_filename": 'ALTER TABLE "confessional_log" ADD COLUMN "attachment_filename" VARCHAR(255)',
        "status": 'ALTER TABLE "confessional_log" ADD COLUMN "status" VARCHAR(20) NOT NULL DEFAULT \'posted\'',
        "scheduled_for": 'ALTER TABLE "confessional_log" ADD COLUMN "scheduled_for" TIMESTAMP',
    }
    for column, statement in confessional_columns.items():
        if column not in existing:
            print(f"[db] Adding confessional_log.{column} column")
            await conn.execute_script(statement)

    _, rows = await conn.execute_query("PRAGMA table_info(gold_transactions)")
    if not any(row["name"] == "market_event_id" for row in rows):
        print("[db] Adding gold_transactions.market_event_id column")
        await conn.execute_script(
            'ALTER TABLE "gold_transactions" ADD COLUMN "market_event_id" INT '
            'REFERENCES "market_events" ("id") ON DELETE SET NULL'
        )

    _, rows = await conn.execute_query("PRAGMA table_info(balance_snapshots)")
    if not any(row["name"] == "locked" for row in rows):
        print("[db] Adding balance_snapshots.locked column")
        await conn.execute_script(
            'ALTER TABLE "balance_snapshots" ADD COLUMN "locked" INT NOT NULL DEFAULT 0'
        )

    _, rows = await conn.execute_query("PRAGMA table_info(game_state)")
    existing = {row["name"] for row in rows}

    # close_hour_utc/close_minute_utc were renamed once the GM could pick a non-UTC timezone
    renames = {
        "close_hour_utc": "close_hour",
        "close_minute_utc": "close_minute",
    }
    for old, new in renames.items():
        if old in existing and new not in existing:
            print(f"[db] Renaming game_state.{old} -> {new}")
            await conn.execute_script(f'ALTER TABLE "game_state" RENAME COLUMN "{old}" TO "{new}"')
            existing.discard(old)
            existing.add(new)

    game_state_columns = {
        "markets_enabled": 'ALTER TABLE "game_state" ADD COLUMN "markets_enabled" INT NOT NULL DEFAULT 0',
        "turn_status": 'ALTER TABLE "game_state" ADD COLUMN "turn_status" VARCHAR(10) NOT NULL DEFAULT \'open\'',
        "closed_at": 'ALTER TABLE "game_state" ADD COLUMN "closed_at" TIMESTAMP',
        "next_turn_reminder_sent_at": 'ALTER TABLE "game_state" ADD COLUMN "next_turn_reminder_sent_at" TIMESTAMP',
        "close_timezone": 'ALTER TABLE "game_state" ADD COLUMN "close_timezone" VARCHAR(64) NOT NULL DEFAULT \'UTC\'',
    }
    for column, statement in game_state_columns.items():
        if column not in existing:
            print(f"[db] Adding game_state.{column} column")
            await conn.execute_script(statement)
