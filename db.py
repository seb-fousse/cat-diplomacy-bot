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
