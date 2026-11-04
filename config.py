import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _int(name: str) -> int:
    v = os.getenv(name, "0").strip()
    return int(v) if v.isdigit() else 0


TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = _int("GUILD_ID")

SELLER_ROLE_ID = _int("SELLER_ROLE_ID") or 1566721051415674990      # Selling Team
CUSTOMER_ROLE_ID = _int("CUSTOMER_ROLE_ID") or 1566721053340733514  # given to the customer when the owner runs complete
# Extra user IDs (besides the server owner) allowed to run the complete command, comma separated
OWNER_IDS = [int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x.isdigit()]
SUPPORT_ROLE_ID = _int("SUPPORT_ROLE_ID")
# Leave the category IDs at 0 and the bot creates/uses a category with this name automatically
TICKET_CATEGORY_NAME = "・𝙏𝙞𝙘𝙠𝙚𝙩"
# Transcript + log channels are auto-created (staff only) if the IDs below are 0
TRANSCRIPT_CHANNEL_NAME = "・𝙏𝙧𝙖𝙣𝙨𝙘𝙧𝙞𝙥𝙩𝙨"
LOGS_CHANNEL_NAME = "・𝙇𝙤𝙜𝙨"

# ---- Welcome system ----
# Leave WELCOME_CHANNEL_ID at 0 and the bot auto-creates this channel (read-only for members)
WELCOME_CHANNEL_NAME = "・𝙒𝙚𝙡𝙘𝙤𝙢𝙚"
WELCOME_ROLE_ID = _int("WELCOME_ROLE_ID") or 1566721055966625804  # role given to every new member on join
WELCOME_CHANNEL_ID = _int("WELCOME_CHANNEL_ID")  # optional: use your own channel
WELCOME_IMAGE = "assets/static.png"              # use "assets/standard.gif" for the animated banner (6 MB per join)
WELCOME_TITLE = "✧･ﾟ welcome ･ﾟ✧"
# Placeholders: {flower} {user} {server} {count} (e.g. 154th) {created}
WELCOME_TEXT = (
    "{flower} Welcome {user} to **{server}**!\n\n"
    "⋆｡°✩ you're our **{count}** member\n"
    "⋆｡°✩ account created {created}\n"
    "⋆｡°✩ open a ticket anytime to shop or get support\n\n"
    "✧ ‧ ₊ ˚ ⋆ ✦ ⋆ ˚ ₊ ‧ ✧"
)
PURCHASE_CATEGORY_ID = _int("PURCHASE_CATEGORY_ID")
SUPPORT_CATEGORY_ID = _int("SUPPORT_CATEGORY_ID")
TRANSCRIPT_CHANNEL_ID = _int("TRANSCRIPT_CHANNEL_ID")
COMPLETED_CHANNEL_ID = _int("COMPLETED_CHANNEL_ID")

# ---- Branding ----
DEFAULT_PREFIX = "."   # change anytime in Discord with the setprefix command

BRAND = "Divine Mart"
BOT_NAME = "Divine Mart Helper"
EMOJI = "<:flower:1567145445724135485>"
CART_EMOJI = "<a:cart:1567146084361310218>"
SUPPORT_EMOJI = "<a:support:1567147463083565076>"
COLOR = 0xFF7EB6
FOOTER = BOT_NAME
BANNER_FILE = "assets/standard.gif"   # animated banner on the panel
STATIC_FILE = "assets/static.png"     # fallback if the gif is missing

# ---- Shop ----
# (label, description) - edit freely
CATEGORIES = [
    ("Nitro", "Discord Nitro subscriptions"),
    ("Nitro-Ids", "Nitro ID accounts & tokens"),
    ("Boosts", "Discord server boosts"),
    ("Members", "Server member packages"),
    ("Decos", "Discord decorations"),
    ("Accounts", "Aged & verified accounts"),
    ("Auto-Product", "Automated product delivery"),
]
