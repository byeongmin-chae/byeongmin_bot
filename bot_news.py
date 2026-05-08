import asyncio
import html
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, time, timedelta
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import httpx
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


KST = ZoneInfo("Asia/Seoul")
NEWS_TIME = time(hour=8, minute=0, tzinfo=KST)

DESKTOP = Path.home() / "Desktop"
TELEGRAM_TOKEN_FILE = DESKTOP / "telegram_bot_token.txt"
NEWSAPI_KEY_FILE = DESKTOP / "newsapi_key.txt"
SUBSCRIBERS_FILE = DESKTOP / "news_bot_subscribers.json"

NEWSAPI_URL = "https://newsapi.org/v2/everything"
GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko"

DAILY_HEADLINES_PER_SUBCATEGORY = 2
SINGLE_HEADLINES_PER_CATEGORY = 5

CATEGORY_GROUPS = {
    "전체": {
        "주요": "한국 주요 뉴스 OR 정치 OR 경제 OR 사회 OR 국제",
        "속보": "속보 OR 긴급 OR 최신 뉴스",
        "브리핑": "오늘 뉴스 OR 아침 브리핑 OR 주요 이슈",
    },
    "경제": {
        "전체": "경제 OR 금융 OR 산업 OR 부동산 OR 물가",
        "금융": "은행 OR 보험 OR 카드 OR 대출 OR 금융",
        "부동산": "부동산 OR 주택 OR 전세 OR 청약 OR 건설",
        "산업": "산업 OR 대기업 OR 제조 OR 에너지 OR 자동차",
        "소비": "소비 OR 물가 OR 유통 OR 식품 OR 생활경제",
        "환율금리": "환율 OR 원달러 OR 기준금리 OR 채권 OR 금리",
    },
    "IT": {
        "전체": "IT OR 기술 OR 플랫폼 OR 디지털",
        "AI": "인공지능 OR 생성형 AI OR AI OR OpenAI",
        "반도체": "반도체 OR 삼성전자 OR SK하이닉스 OR 엔비디아 OR TSMC",
        "빅테크": "애플 OR 구글 OR 메타 OR 마이크로소프트 OR 아마존",
        "스타트업": "스타트업 OR 벤처 OR 창업 OR 투자유치",
        "보안": "해킹 OR 개인정보 OR 사이버 보안 OR 보안",
        "모바일": "스마트폰 OR 통신사 OR 모바일 OR 앱",
    },
    "증시": {
        "전체": "증시 OR 주식 OR 코스피 OR 나스닥",
        "국내": "코스피 OR 코스닥 OR 한국 주식 OR 국내 증시",
        "미국": "나스닥 OR 다우 OR S&P500 OR 미국 증시",
        "테마주": "테마주 OR 2차전지 OR AI 주식 OR 바이오 주식",
        "공시실적": "실적 발표 OR 기업 공시 OR 어닝 OR 매출",
        "가상자산": "비트코인 OR 이더리움 OR 가상자산 OR 코인",
    },
    "국내외": {
        "국내": "한국 주요 뉴스 OR 국내 뉴스",
        "정치": "대통령실 OR 국회 OR 정당 OR 선거 OR 정치",
        "사회": "사건 OR 교육 OR 노동 OR 복지 OR 사회",
        "국제": "미국 OR 중국 OR 일본 OR 유럽 OR 국제",
        "외교안보": "북한 OR 국방 OR 외교 OR 안보 OR 지정학",
    },
}


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


def read_secret(env_name: str, file_path: Path) -> str | None:
    value = os.environ.get(env_name)
    if value:
        return value.strip()

    if file_path.exists():
        return file_path.read_text(encoding="utf-8").strip()

    return None


def read_telegram_token() -> str | None:
    return read_secret("TELEGRAM_BOT_TOKEN", TELEGRAM_TOKEN_FILE)


def read_newsapi_key() -> str | None:
    return read_secret("NEWSAPI_KEY", NEWSAPI_KEY_FILE)


def load_subscribers() -> set[int]:
    if not SUBSCRIBERS_FILE.exists():
        return set()

    try:
        data = json.loads(SUBSCRIBERS_FILE.read_text(encoding="utf-8"))
        return {int(chat_id) for chat_id in data.get("chat_ids", [])}
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("Could not load subscribers: %s", exc)
        return set()


def save_subscribers(chat_ids: set[int]) -> None:
    data = {"chat_ids": sorted(chat_ids)}
    SUBSCRIBERS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def add_subscriber(chat_id: int) -> None:
    chat_ids = load_subscribers()
    chat_ids.add(chat_id)
    save_subscribers(chat_ids)


def remove_subscriber(chat_id: int) -> None:
    chat_ids = load_subscribers()
    chat_ids.discard(chat_id)
    save_subscribers(chat_ids)


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def category_paths() -> list[str]:
    return [
        f"{group}/{subcategory}"
        for group, subcategories in CATEGORY_GROUPS.items()
        for subcategory in subcategories
    ]


def resolve_category(raw_category: str | None) -> tuple[str, str] | None:
    if not raw_category:
        return ("전체", "주요")

    normalized = raw_category.strip().replace(" ", "")
    if "/" in normalized:
        group, subcategory = normalized.split("/", 1)
        for known_group, subcategories in CATEGORY_GROUPS.items():
            if group.lower() == known_group.lower():
                for known_subcategory in subcategories:
                    if subcategory.lower() == known_subcategory.lower():
                        return (known_group, known_subcategory)
        return None

    matches = []
    for group, subcategories in CATEGORY_GROUPS.items():
        if normalized.lower() == group.lower():
            matches.append((group, "전체" if "전체" in subcategories else next(iter(subcategories))))
        for subcategory in subcategories:
            if normalized.lower() == subcategory.lower():
                matches.append((group, subcategory))

    if len(matches) == 1:
        return matches[0]

    return None


def category_query(group: str, subcategory: str) -> str:
    return CATEGORY_GROUPS[group][subcategory]


def fetch_newsapi_news(group: str, subcategory: str, limit: int) -> list[dict[str, str]]:
    api_key = read_newsapi_key()
    if not api_key:
        return []

    response = httpx.get(
        NEWSAPI_URL,
        params={
            "q": category_query(group, subcategory),
            "language": "ko",
            "sortBy": "publishedAt",
            "pageSize": limit,
        },
        headers={
            "User-Agent": "TelegramNewsBot/1.0",
            "X-Api-Key": api_key,
        },
        timeout=15,
        follow_redirects=True,
    )
    response.raise_for_status()

    articles = response.json().get("articles", [])
    news_items: list[dict[str, str]] = []

    for article in articles[:limit]:
        source_data = article.get("source") or {}
        news_items.append(
            {
                "title": strip_html(article.get("title") or "제목 없음"),
                "link": article.get("url") or "",
                "source": source_data.get("name") or "NewsAPI",
            }
        )

    return news_items


def fetch_google_news(group: str, subcategory: str, limit: int) -> list[dict[str, str]]:
    query = quote_plus(category_query(group, subcategory))
    feed_url = f"https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"

    if group == "전체" and subcategory == "주요":
        feed_url = GOOGLE_NEWS_RSS_URL

    response = httpx.get(
        feed_url,
        headers={"User-Agent": "Mozilla/5.0 TelegramNewsBot/1.0"},
        timeout=15,
        follow_redirects=True,
    )
    response.raise_for_status()

    root = ET.fromstring(response.content)
    items = root.findall("./channel/item")
    news_items: list[dict[str, str]] = []

    for item in items[:limit]:
        news_items.append(
            {
                "title": strip_html(item.findtext("title", default="제목 없음")),
                "link": item.findtext("link", default=""),
                "source": item.findtext("source", default="Google News"),
            }
        )

    return news_items


def fetch_news(group: str, subcategory: str, limit: int) -> tuple[list[dict[str, str]], str]:
    if read_newsapi_key():
        try:
            newsapi_items = fetch_newsapi_news(group, subcategory, limit)
            if newsapi_items:
                return newsapi_items, "NewsAPI"
        except Exception:
            logger.exception(
                "Failed to fetch %s/%s from NewsAPI. Falling back to Google News.",
                group,
                subcategory,
            )

    return fetch_google_news(group, subcategory, limit), "Google News"


def format_news_lines(news_items: list[dict[str, str]]) -> list[str]:
    lines = []
    for index, item in enumerate(news_items, start=1):
        title = html.escape(item["title"])
        source = html.escape(item["source"])
        link = html.escape(item["link"])
        lines.append(f"{index}. <a href=\"{link}\">{title}</a>")
        lines.append(f"   출처: {source}")
    return lines


async def get_news_message(group: str, subcategory: str) -> str:
    news_items, provider = await asyncio.to_thread(
        fetch_news,
        group,
        subcategory,
        SINGLE_HEADLINES_PER_CATEGORY,
    )
    today = datetime.now(KST).strftime("%Y-%m-%d")

    if not news_items:
        return f"{today} {group}/{subcategory} 뉴스\n\n뉴스를 가져오지 못했습니다."

    lines = [f"{today} {group}/{subcategory} 뉴스", f"제공: {provider}", ""]
    lines.extend(format_news_lines(news_items))
    return "\n".join(lines)


async def get_daily_group_message(group: str, subcategories: dict[str, str]) -> str:
    today = datetime.now(KST).strftime("%Y-%m-%d")
    lines = [f"{today} 아침 뉴스: {group}", ""]
    provider_names = set()

    for subcategory in subcategories:
        try:
            news_items, provider = await asyncio.to_thread(
                fetch_news,
                group,
                subcategory,
                DAILY_HEADLINES_PER_SUBCATEGORY,
            )
            provider_names.add(provider)
        except Exception:
            logger.exception("Failed to fetch daily news for %s/%s.", group, subcategory)
            news_items = []

        lines.append(f"[{group}/{subcategory}]")
        if news_items:
            lines.extend(format_news_lines(news_items))
        else:
            lines.append("뉴스를 가져오지 못했습니다.")
        lines.append("")

    provider_text = ", ".join(sorted(provider_names)) if provider_names else "NewsAPI/Google News"
    lines.insert(1, f"제공: {provider_text}")
    return "\n".join(lines).strip()


async def get_all_daily_messages() -> list[str]:
    messages = []
    for group, subcategories in CATEGORY_GROUPS.items():
        messages.append(await get_daily_group_message(group, subcategories))
    return messages


def seconds_until_next_news_time() -> float:
    now = datetime.now(KST)
    next_run = datetime.combine(now.date(), NEWS_TIME)
    if now >= next_run:
        next_run += timedelta(days=1)

    return (next_run - now).total_seconds()


async def send_daily_news(bot: Bot) -> None:
    while True:
        sleep_seconds = seconds_until_next_news_time()
        next_run = datetime.now(KST) + timedelta(seconds=sleep_seconds)
        logger.info("Next daily news delivery: %s", next_run.isoformat())
        await asyncio.sleep(sleep_seconds)

        chat_ids = load_subscribers()
        if not chat_ids:
            logger.info("No subscribers. Skipping daily news delivery.")
            continue

        for chat_id in chat_ids:
            try:
                messages = await get_all_daily_messages()
            except Exception:
                logger.exception("Failed to build daily news messages.")
                messages = ["오늘 뉴스를 가져오지 못했습니다. 잠시 후 /news로 다시 시도해주세요."]

            for message in messages:
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=message,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                    await asyncio.sleep(0.5)
                except Exception:
                    logger.exception("Failed to send news to chat_id=%s", chat_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    add_subscriber(update.effective_chat.id)

    await update.message.reply_text(
        "뉴스 봇 구독이 시작되었습니다.\n"
        "매일 오전 8시(한국시간)에 모든 소분류 뉴스를 대분류별로 보내드릴게요.\n\n"
        "/news 경제/부동산 - 특정 소분류 지금 보기\n"
        "/daily - 전체 자동 전송 내용을 지금 테스트\n"
        "/categories - 카테고리 목록 보기\n"
        "/newsapi - NewsAPI 설정 확인\n"
        "/status - 구독 상태 확인\n"
        "/stop - 구독 중지"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "사용 가능한 명령어:\n"
        "/start - 매일 아침 전체 소분류 뉴스 구독 시작\n"
        "/daily - 매일 받을 전체 묶음을 지금 받기\n"
        "/news 경제/금융 - 특정 소분류 뉴스 받기\n"
        "/news IT/AI - IT AI 뉴스 받기\n"
        "/news 증시/미국 - 미국 증시 뉴스 받기\n"
        "/categories - 카테고리 목록 보기\n"
        "/newsapi - NewsAPI 설정 확인\n"
        "/status - 구독 상태 확인\n"
        "/stop - 구독 중지"
    )


async def news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw_category = " ".join(context.args) if context.args else "전체/주요"
    category = resolve_category(raw_category)

    if not category:
        await update.message.reply_text(
            "카테고리를 찾지 못했습니다.\n"
            "예: /news 경제/부동산, /news IT/AI, /news 증시/미국\n"
            "전체 목록은 /categories 로 볼 수 있어요."
        )
        return

    group, subcategory = category
    await update.message.reply_text(f"{group}/{subcategory} 뉴스를 가져오는 중입니다...")
    message = await get_news_message(group, subcategory)
    await update.message.reply_text(
        message,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("전체 소분류 뉴스를 대분류별로 가져오는 중입니다...")
    for message in await get_all_daily_messages():
        await update.message.reply_text(
            message,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await asyncio.sleep(0.5)


async def categories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = ["카테고리 목록", ""]
    for group, subcategories in CATEGORY_GROUPS.items():
        lines.append(f"{group}")
        for subcategory in subcategories:
            lines.append(f"- {group}/{subcategory}")
        lines.append("")

    lines.append("예시: /news 경제/부동산")
    await update.message.reply_text("\n".join(lines).strip())


async def newsapi_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if read_newsapi_key():
        await update.message.reply_text(
            "NewsAPI 키가 설정되어 있습니다.\n"
            "뉴스는 NewsAPI를 먼저 사용하고, 실패하면 Google News로 대체됩니다."
        )
    else:
        await update.message.reply_text(
            "아직 NewsAPI 키가 설정되어 있지 않습니다.\n"
            "https://newsapi.org/register 에서 키를 받은 뒤 "
            "바탕화면의 newsapi_key.txt 파일에 넣어주세요."
        )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    subscribers = load_subscribers()
    if chat_id in subscribers:
        await update.message.reply_text(
            "현재 매일 오전 8시 전체 소분류 뉴스 구독 중입니다."
        )
    else:
        await update.message.reply_text("현재 구독 중이 아닙니다. /start로 구독할 수 있어요.")


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remove_subscriber(update.effective_chat.id)
    await update.message.reply_text("뉴스 구독을 중지했습니다. 다시 시작하려면 /start를 보내주세요.")


async def reply_to_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "뉴스를 받으려면 /start, 특정 뉴스는 /news 경제/부동산 처럼 보내주세요."
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Exception while handling an update:", exc_info=context.error)


async def post_init(app: Application) -> None:
    app.create_task(send_daily_news(app.bot))


def main() -> None:
    token = read_telegram_token()
    if not token:
        raise RuntimeError(
            "텔레그램 봇 토큰이 없습니다. TELEGRAM_BOT_TOKEN 환경변수를 설정하거나 "
            "바탕화면에 telegram_bot_token.txt 파일을 만들고 토큰을 넣어주세요."
        )

    app = Application.builder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("news", news))
    app.add_handler(CommandHandler("daily", daily))
    app.add_handler(CommandHandler("categories", categories))
    app.add_handler(CommandHandler("newsapi", newsapi_status))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_to_message))
    app.add_error_handler(error_handler)

    logger.info("Telegram news bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
