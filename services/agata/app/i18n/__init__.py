"""Transport-neutral Locale Pack foundation.

One character keeps the same Identity Core across languages; a Locale Pack only
changes the language and form of finite system messages. It never changes
personality, memory, relationships, safety policy or user facts.

This package must not import transport, channel or provider (Telegram, MAX,
web, mobile) types. The authoritative locale source is the Stage 8
``user_character_locale_preferences`` table; provider profile hints (for example
the Telegram ``language_code``) are deliberately not accepted here.
"""
