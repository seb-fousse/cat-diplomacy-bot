from tortoise import fields
from tortoise.models import Model


class Player(Model):
    id           = fields.IntField(pk=True)
    guild_id     = fields.BigIntField()
    user_id      = fields.BigIntField()
    player_name  = fields.CharField(max_length=100, null=True)
    faction_name = fields.CharField(max_length=100)
    is_eliminated = fields.BooleanField(default=False)
    gold_balance = fields.IntField(default=0)  # cache of the GoldTransaction ledger; only mutate via cogs.economy
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
    id                  = fields.IntField(pk=True)
    player              = fields.ForeignKeyField("models.Player", related_name="confessional_logs", null=True)
    guild_id            = fields.BigIntField(null=True)
    message             = fields.TextField(null=True)
    attachment_url      = fields.CharField(max_length=500, null=True)
    attachment_path     = fields.CharField(max_length=500, null=True)  # local file, kept while a delayed post is pending
    attachment_filename = fields.CharField(max_length=255, null=True)
    status              = fields.CharField(max_length=20, default="posted")  # pending | posted | cancelled
    scheduled_for       = fields.DatetimeField(null=True)
    posted_at           = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "confessional_log"


class GoldTransaction(Model):
    id               = fields.IntField(pk=True)
    guild_id         = fields.BigIntField()
    # Null from_player = gold created (GM grant); null to_player = gold destroyed (GM deduction)
    from_player      = fields.ForeignKeyField("models.Player", related_name="gold_sent", null=True)
    to_player        = fields.ForeignKeyField("models.Player", related_name="gold_received", null=True)
    amount           = fields.IntField()                    # always positive
    transaction_type = fields.CharField(max_length=20)      # TRANSFER | GM_GRANT | GM_DEDUCT
    reason           = fields.CharField(max_length=500, null=True)
    season           = fields.CharField(max_length=20, null=True)
    year             = fields.IntField(null=True)
    initiated_by     = fields.BigIntField()                 # Discord user id
    from_balance_after = fields.IntField(null=True)
    to_balance_after   = fields.IntField(null=True)
    created_at       = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "gold_transactions"


class BalanceSnapshot(Model):
    id         = fields.IntField(pk=True)
    player     = fields.ForeignKeyField("models.Player", related_name="balance_snapshots")
    season     = fields.CharField(max_length=20)
    year       = fields.IntField()
    balance    = fields.IntField()
    created_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "balance_snapshots"
        unique_together = (("player", "season", "year"),)
