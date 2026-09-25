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
