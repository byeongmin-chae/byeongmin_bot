# Telegram News Bot

Telegram bot that sends Korean news by category every morning at 08:00 KST.

## Features

- Daily news delivery to subscribed chats
- Category-based news lookup with `/news`
- Google News RSS fallback
- Optional NewsAPI integration
- Subscriber persistence in a local JSON file

## Commands

- `/start` - subscribe to daily news
- `/daily` - receive the full daily bundle now
- `/news 경제/부동산` - receive a specific category
- `/categories` - list available categories
- `/newsapi` - check NewsAPI configuration
- `/status` - check subscription status
- `/stop` - unsubscribe

## Setup

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Set the Telegram bot token:

```bash
export TELEGRAM_BOT_TOKEN="your-telegram-bot-token"
```

Optionally set a NewsAPI key:

```bash
export NEWSAPI_KEY="your-newsapi-key"
```

Run the bot:

```bash
python bot_news.py
```

## Notes

The bot can also read secrets from these local files on your Desktop:

- `telegram_bot_token.txt`
- `newsapi_key.txt`

Do not commit token files or subscriber data. They are ignored by `.gitignore`.
