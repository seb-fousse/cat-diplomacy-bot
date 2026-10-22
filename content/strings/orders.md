<!--
Messages from /orders. Each "## key" heading starts one message, and the bot looks it
up as "orders.<key>" — rename a key only together with the code.
{name} placeholders are filled in by the bot; keep the ones a message already has.
In a message that has placeholders, write a literal brace as {{ or }}.
-->

## deadline_not_set
⚠️ *The GM hasn't set the current turn yet.*

## deadline_closed
🔒 **Submissions are closed for this turn.** Wait for the GM to advance to the next turn.

## deadline_planned
⏰ The GM plans to close orders <t:{epoch}:R> (<t:{epoch}:F>).

## no_map
🗺️ *No map posted yet.*

## map_caption
🗺️ *Map — {map}*

## map_unavailable
🗺️ *Map — {map} (image unavailable)*

## winter
❄️ *Winter adjustments — add or remove units.*

## saved
💾 *Saved automatically.*

## not_saved
⚠️ **Not saved** — your latest change could not be stored. See the message above.

## empty
*No orders yet.*
Use **Add Order** to begin building your turn.

## added
✅ **Added and saved:** {order}

## removed
↩️ **Removed and saved:** {order}

## cleared
🗑️ **All orders cleared.** You have no orders saved for this turn.

## not_saved_no_turn
⚠️ The GM hasn't set the current turn yet, so your change wasn't saved. Ask them to run `/gm turn`.

## not_saved_closed
⚠️ Submissions are closed for this turn, so your change wasn't saved. Wait for the GM to advance to the next turn.

## not_saved_unregistered
⚠️ You're not registered as a player — your change wasn't saved. Ask the GM.

## eliminated
⚠️ You have been eliminated and can no longer submit orders.

## not_player
⚠️ Only active players can submit orders.

## wrong_channel
⚠️ This command can only be used in your private orders channel.
