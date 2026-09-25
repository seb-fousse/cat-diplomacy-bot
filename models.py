from tortoise import fields
from tortoise.models import Model


class Player(Model):
    id           = fields.IntField(pk=True)
    guild_id     = fields.BigIntField()
    user_id      = fields.BigIntField()
    player_name  = fields.CharField(max_length=100, null=True)
    faction_name = fields.CharField(max_length=100)
    is_eliminated = fields.BooleanField(default=False)
    created_at   = fields.DatetimeField(auto_now_add=True)

    orders: fields.ReverseRelation["Order"]
    confessional_logs: fields.ReverseRelation["ConfessionalLog"]

    class Meta:
        table = "players"
        unique_together = (("guild_id", "user_id"),)


class Order(Model):
    id          = fields.IntField(pk=True)
    player      = fields.ForeignKeyField("models.Player", related_name="orders")
    season      = fields.CharField(max_length=20)       # "Spring" | "Fall" | "Winter"
    year        = fields.IntField()
    unit        = fields.CharField(max_length=100)
    order_type  = fields.CharField(max_length=20)       # HOLD | MOVE | SUPPORT | CONVOY
    target      = fields.CharField(max_length=100, null=True)
    status      = fields.CharField(max_length=20, default="pending")  # pending | submitted | resolved
    submitted_at = fields.DatetimeField(null=True)
    created_at  = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "orders"


class GameState(Model):
    id         = fields.IntField(pk=True)
    guild_id   = fields.BigIntField(unique=True)
    season     = fields.CharField(max_length=20)   # Spring | Fall | Winter
    year       = fields.IntField()
    close_weekdays = fields.CharField(max_length=100, null=True)  # "MON,WED,FRI" — comma-separated day abbreviations
    close_hour_utc = fields.IntField(null=True)    # 0–23
    close_minute_utc = fields.IntField(default=0)  # 0–59
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "game_state"


class ConfessionalLog(Model):
    id             = fields.IntField(pk=True)
    player         = fields.ForeignKeyField("models.Player", related_name="confessional_logs", null=True)
    message        = fields.TextField(null=True)
    attachment_url = fields.CharField(max_length=500, null=True)
    posted_at      = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "confessional_log"
