# Divine Mart Helper

Ticket bot: panel → shop dropdown → purchase/support tickets → claim / close → HTML transcript.

## Setup
1. Create a bot at https://discord.com/developers/applications, copy the token.
   Enable **Message Content Intent** under Bot → Privileged Gateway Intents.
2. Invite it with scopes `bot` + `applications.commands` and permissions: Manage Channels,
   Manage Roles, Manage Messages, Send Messages, Embed Links, Attach Files, Read Message History.
3. `pip install -r requirements.txt`
4. Copy `.env.example` to `.env` and fill in the token, role IDs, category IDs and log channel IDs.
5. `python bot.py`
6. In your ticket channel run `/panel`.

## Commands
Default prefix is `.` (change it with `.setprefix <new>`). Every command also works as a slash command.
- `help` - list all commands
- `panel` (admin) - posts the Divine Mart panel with the animated banner
- `setprefix <prefix>` (admin) - change the prefix (1-5 characters, no spaces)
- `say <message>` (admin) - the bot sends your message (@everyone/@here are blocked)
- `purge <1-100> [@member]` (Manage Messages) - bulk-delete recent messages (pinned ones are skipped); logged with a text file of what was deleted
- `close [reason]` - close the ticket you're in (ticket owner or staff); sends the transcript like the Close button
- `add <@member>` - staff adds a member to the ticket they're in
- `rename <new name>` - staff renames the ticket they're in (Discord limits channel renames to 2 per 10 minutes)
- `setwelcome <#channel>` (admin) - choose where welcome messages go
- `testwelcome` (admin) - preview the welcome message here
- `complete` (server owner only; sellers cannot use it) - records and finishes the order in a purchase ticket

The Customer role is NOT given when a ticket opens - the bot gives it to the ticket opener only when the owner runs `complete`
(the bot's role must sit above it and have Manage Roles). Ticket channels go in an auto-created category named "・𝙏𝙞𝙘𝙠𝙚𝙩" (change `TICKET_CATEGORY_NAME` in `config.py`; a "2", "3"... copy is made when one fills up at 50 channels). Set the category IDs in `.env` to use your own instead.
Transcripts and logs go to auto-created staff-only channels `・𝙏𝙧𝙖𝙣𝙨𝙘𝙧𝙞𝙥𝙩𝙨` and `・𝙇𝙤𝙜𝙨` (Selling/Support roles can read them; set the channel IDs in `.env` to use your own).
Edit shop categories, colours and branding in `config.py`.

Message logs: edited and deleted messages are posted to the logs channel as Message Edited (Before / After) and Message Deleted. Ticket channels are skipped, and only messages the bot has seen since it started can be shown. Give the bot Manage Messages for `purge`.

Precise server logs (all in the logs channel, with who did it from the audit log): roles given/removed (who gave it, who got it),
nickname changes, timeouts, joins, leaves, kicks, bans, unbans, channel created/deleted/edited (name, topic, category, slowmode,
permission overwrites), role created/deleted/edited (incl. permission changes), voice join/leave/move, message edited (Before/After)
and message deleted (says who deleted it when a moderator did).
Needs: **Server Members Intent** on in the Developer Portal and the **View Audit Log** permission for the bot.

Welcome system: new members get an aesthetic embed (avatar, member number, account age, banner) in an auto-created read-only channel `・𝙒𝙚𝙡𝙘𝙤𝙢𝙚`. Edit the text in `config.py` (WELCOME_TEXT, WELCOME_TITLE). Optional auto-role via WELCOME_ROLE_ID.
