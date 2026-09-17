# Telegram channel connection

GPoint uses the official Bot API `KeyboardButton.request_chat` flow. The API
creates a short-lived, single-use request ID bound to the authenticated
Telegram user, and sends a private bot message containing a channel selector.
Telegram returns the selected channel in a `chat_shared` service message. The
bot forwards only that update to GPoint; the API then verifies the challenge,
channel type, the user membership, and the bot membership with `getChat` and
`getChatMember` before activating the connection.

The minimum bot permission required for ingestion is channel administrator
`can_post_messages`. This is required by the selector and is verified again by
the API. GPoint does not request or require edit/delete permissions.

Import modes are `AUTO`, `HASHTAG`, and `MANUAL`. `#nogpoint` always wins.
Telegram channel IDs and raw captions are operational provenance and are not
included in public creator or post DTOs. Telegram deletion synchronization is
not claimed because the current Bot API does not provide a reliable channel
post deletion update for this workflow; creators can hide imported posts in
GPoint.
