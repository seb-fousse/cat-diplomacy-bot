from tortoise import Tortoise

DB_URL = "sqlite://cat_game.db"


async def init_db():
    await Tortoise.init(
        db_url=DB_URL,
        modules={"models": ["models"]},
    )
    await Tortoise.generate_schemas()
