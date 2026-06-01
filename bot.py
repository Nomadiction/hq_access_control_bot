import asyncio
import json
import logging
import os
import time
from html import escape
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    ChatJoinRequest,
    ChatMemberUpdated,
    ErrorEvent,
    LinkPreviewOptions,
    MenuButtonCommands,
    Message,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("hq_access_control")

BASE_DIR = Path(__file__).resolve().parent
WHITELIST_PATH = BASE_DIR / "whitelist.json"
ENV_PATH = BASE_DIR / ".env"
STARTED_AT = time.time()

# Аудит-лог доступов: отдельный файл access.log с ротацией, не зависит от консоли.
audit = logging.getLogger("access")
audit.setLevel(logging.INFO)
_audit_handler = RotatingFileHandler(
    BASE_DIR / "access.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
)
_audit_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
audit.addHandler(_audit_handler)
audit.propagate = False


def esc(value: object) -> str:
    """Экранирует динамический текст (имена, ники, названия групп) под HTML,
    чтобы символы < > & не ломали разметку сообщения."""
    return escape(str(value), quote=False)


def load_dotenv(path: Path) -> None:
    """Читает .env рядом со скриптом и кладёт значения в окружение.
    Уже заданные переменные (например, из systemd) не перетираются.
    Без сторонних зависимостей."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv(ENV_PATH)

# Токен берётся из .env / окружения. В коде токена нет.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# ID администраторов через запятую, например: ADMIN_IDS=111111111,222222222
ADMIN_IDS = {
    int(x)
    for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",")
    if x
}

# ── Тексты бота. HTML-разметка. {project} подставляется именем проекта. ────────
ADMIN_WELCOME = (
    "<b>HQ Access Bot</b>\n"
    "<i>Пропускной контроль рабочих групп проектов.</i>\n\n"
    "<b>Как это работает</b>\n"
    "<blockquote>Пользователь подаёт заявку по пригласительной ссылке. Бот "
    "сверяет его со списком доступа проекта и <u>мгновенно</u> одобряет или "
    "отклоняет. Об отклонённых попытках приходит уведомление.</blockquote>\n\n"
    "<b>Команды</b>\n"
    "<code>/add</code> - добавить участников\n"
    "<code>/remove</code> - убрать участников\n"
    "<code>/reload</code> - списки доступа\n"
    "<code>/status</code> - статус и счётчики\n"
    "<code>/clear</code> - очистить переписку\n"
    "<code>/id</code> - узнать chat_id (в группе)\n"
    "<code>/me</code> - показать ваш ID\n\n"
    "<i>Новые проекты заводятся автоматически при добавлении бота в группу.</i>"
)

USER_WELCOME = (
    "<b>HQ Access Bot</b>\n"
    "<i>Система доступа к рабочим группам проектов.</i>\n\n"
    "<blockquote>Чтобы войти в проект, откройте пригласительную ссылку и "
    "подайте заявку. Если вы в списке доступа - она одобрится <u>автоматически</u> "
    "за пару секунд. Если нет - доступ выдаёт администратор.</blockquote>\n\n"
    "<b>Команда</b>\n"
    "<code>/me</code> - показать ваш ID\n\n"
    'По всем вопросам: <a href="https://t.me/stashowner">@stashowner</a>'
)

APPROVE_TEXT = (
    "<b>Доступ подтверждён.</b>\n"
    "Добро пожаловать в проект <b>{project}</b>. "
    "Группа уже открыта в вашем списке чатов."
)

DENY_TEXT = (
    "<b>Заявка отклонена.</b>\n"
    "Вашего аккаунта нет в списке доступа этого проекта. "
    "Если это ошибка - напишите администратору, он добавит вас в список."
)

BOT_DESCRIPTION = (
    "HQ Access Bot - пропускной контроль приватных рабочих групп. "
    "Подайте заявку по пригласительной ссылке нужного проекта: если вы в "
    "списке доступа, бот одобрит её автоматически за пару секунд. "
    "Нажмите «Start», чтобы начать."
)

BOT_SHORT_DESCRIPTION = (
    "Контроль доступа к приватным рабочим группам. "
    "Заявки одобряются автоматически по белому списку."
)

# Меню команд: базовое для всех, полное для админов.
USER_COMMANDS = [
    BotCommand(command="start", description="Начать и показать справку"),
    BotCommand(command="help", description="Справка и возможности"),
    BotCommand(command="me", description="Показать мой ID"),
]
ADMIN_COMMANDS = [
    BotCommand(command="start", description="Справка администратора"),
    BotCommand(command="status", description="Статус и счётчики"),
    BotCommand(command="reload", description="Показать списки доступа"),
    BotCommand(command="add", description="Добавить участников в проект"),
    BotCommand(command="remove", description="Убрать участников"),
    BotCommand(command="clear", description="Очистить переписку с ботом"),
    BotCommand(command="id", description="Узнать chat_id (в группе)"),
    BotCommand(command="me", description="Показать мой ID"),
    BotCommand(command="help", description="Справка"),
]

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN пуст. Задай его в .env / переменных окружения.")

# Дефолтный режим разметки HTML: все сообщения форматируются автоматически.
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()


def load_whitelist() -> dict:
    """Читает whitelist.json. Вызывается на каждую заявку, поэтому правки
    файла подхватываются без перезапуска бота."""
    try:
        return json.loads(WHITELIST_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.error("whitelist.json не найден: %s", WHITELIST_PATH)
        return {}
    except json.JSONDecodeError as e:
        logger.error("whitelist.json повреждён: %s", e)
        return {}


def save_whitelist(data: dict) -> None:
    """Сохраняет whitelist.json атомарно: пишем во временный файл и заменяем им
    основной. Если процесс умрёт посреди записи, целевой файл не побьётся."""
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = WHITELIST_PATH.with_name(WHITELIST_PATH.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(WHITELIST_PATH)  # os.replace атомарен в пределах ФС


WHITELIST_LOCK = asyncio.Lock()


async def mutate_whitelist(mutator):
    """Сериализует read-modify-write whitelist.json через лок: перечитывает
    свежие данные, применяет mutator(data) и атомарно сохраняет. Исключает
    потерю одновременных правок (две заявки или команды в одну миллисекунду)."""
    async with WHITELIST_LOCK:
        data = load_whitelist()
        result = mutator(data)
        save_whitelist(data)
        return result


def find_chat_key(data: dict, token: str) -> str | None:
    """Находит ключ группы по chat_id или по имени проекта (регистр любой)."""
    if token in data:
        return token
    low = token.lower()
    for key, rules in data.items():
        if rules.get("project", "").lower() == low:
            return key
    return None


def match_rules(rules: dict, user_id: int, username: str | None) -> bool:
    """Проверяет юзера против правил одной группы (id или username)."""
    if user_id in rules.get("ids", []):
        return True
    if username:
        allowed = {u.lower().lstrip("@") for u in rules.get("usernames", [])}
        if username.lower() in allowed:
            return True
    return False


def count_unique(data: dict) -> int:
    """Уникальных участников: объединение всех id и всех username по группам."""
    ids: set[int] = set()
    usernames: set[str] = set()
    for r in data.values():
        ids.update(r.get("ids", []))
        usernames.update(u.lstrip("@").lower() for u in r.get("usernames", []))
    return len(ids) + len(usernames)


async def safe_dm(user_id: int, text: str) -> None:
    """Пишет юзеру в ЛС. Если он не нажимал Start у бота -> 403, это норма."""
    try:
        await bot.send_message(user_id, text)
    except Exception as e:
        logger.warning("ЛС юзеру %s не доставлено: %s", user_id, e)


async def notify_admins(text: str) -> None:
    """Шлёт сообщение всем админам из ADMIN_IDS."""
    for admin_id in ADMIN_IDS:
        await safe_dm(admin_id, text)


async def send_welcome(message: Message) -> None:
    # Админу - полный список с командами, остальным - короткое с контактом.
    text = ADMIN_WELCOME if message.from_user.id in ADMIN_IDS else USER_WELCOME
    await message.answer(
        text,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@dp.chat_join_request()
async def on_join_request(event: ChatJoinRequest) -> None:
    chat_id = event.chat.id
    user = event.from_user
    data = load_whitelist()
    rules = data.get(str(chat_id))
    project = (rules or {}).get("project", "проект")

    # Группа не описана в конфиге или юзера нет в её списке -> отказ.
    if rules and match_rules(rules, user.id, user.username):
        await event.approve()
        logger.info("APPROVED id=%s @%s -> %s (%s)", user.id, user.username, chat_id, project)
        audit.info("APPROVED id=%s @%s project=%s", user.id, user.username, project)
        # Авто-апгрейд: фиксируем числовой id, чтобы доступ не зависел от @username.
        if user.id not in rules.get("ids", []):
            def _upgrade(d: dict) -> None:
                r = d.get(str(chat_id))
                if r is not None and user.id not in r.get("ids", []):
                    r.setdefault("ids", []).append(user.id)

            await mutate_whitelist(_upgrade)
        await safe_dm(user.id, APPROVE_TEXT.format(project=esc(project)))
        return

    await event.decline()
    logger.info("DECLINED id=%s @%s -> %s", user.id, user.username, chat_id)
    audit.info("DECLINED id=%s @%s project=%s", user.id, user.username, project)
    await safe_dm(user.id, DENY_TEXT)
    uname = "@" + esc(user.username) if user.username else "-"
    await notify_admins(
        f"<b>Отклонена заявка</b> в проект <b>{esc(project)}</b>\n"
        f"Кто: {esc(user.full_name)} {uname} (<code>id {user.id}</code>)"
    )


@dp.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated) -> None:
    # Бота добавили в группу или повысили -> сам заводит её в whitelist.json.
    # Больше не нужно вручную дёргать /id и вписывать chat_id.
    chat = event.chat
    if chat.type not in ("group", "supergroup"):
        return
    if event.new_chat_member.status not in ("member", "administrator", "creator"):
        return
    key = str(chat.id)
    if key in load_whitelist():
        return  # группа уже заведена, ничего не трогаем

    def _register(d: dict) -> bool:
        if key in d:  # повторная проверка уже под локом
            return False
        d[key] = {"project": chat.title or key, "ids": [], "usernames": []}
        return True

    if not await mutate_whitelist(_register):
        return
    logger.info("AUTO-ADDED group %s «%s» в whitelist", key, chat.title)
    audit.info("GROUP-ADDED %s «%s»", key, chat.title)
    await notify_admins(
        f"<b>Бот добавлен в группу</b>\n"
        f"«{esc(chat.title)}» (<code>{key}</code>)\n"
        f"Группа заведена в список доступа, пока пустая.\n"
        f"Добавить людей: <code>/add {key} @username</code>"
    )


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await send_welcome(message)


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await send_welcome(message)


@dp.message(Command("id"))
async def cmd_id(message: Message) -> None:
    # Вызвать в нужной группе: /id@hq_access_control_bot
    await message.answer(f"chat_id: <code>{message.chat.id}</code>")


@dp.message(Command("me"))
async def cmd_me(message: Message) -> None:
    # Любой может написать боту /me в ЛС и узнать свой числовой ID.
    u = message.from_user
    uname = "@" + esc(u.username) if u.username else "<i>не задан</i>"
    await message.answer(
        f"Ваш ID: <code>{u.id}</code>\nUsername: {uname}"
    )


@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if message.chat.type != "private" or message.from_user.id not in ADMIN_IDS:
        return
    data = load_whitelist()
    up = int(time.time() - STARTED_AT)
    h, rem = divmod(up, 3600)
    m, _ = divmod(rem, 60)
    await message.answer(
        "<b>Статус бота</b>\n"
        "Состояние: <b>работает</b>\n"
        f"Аптайм: <b>{h} ч {m} мин</b>\n"
        f"Групп заведено: <b>{len(data)}</b>\n"
        f"Участников: <b>{count_unique(data)}</b>"
    )


@dp.message(Command("clear"))
async def cmd_clear(message: Message) -> None:
    # Чистит всю переписку в ЛС с ботом: удаляет сообщения по message_id
    # пакетами по 100, от последнего до первого. Только в личке и только
    # сообщения не старше 48 ч (ограничение Telegram, старее удалить нельзя).
    if message.chat.type != "private":
        return
    chat_id = message.chat.id
    mid = message.message_id
    while mid > 0:
        batch = list(range(mid, max(mid - 100, 0), -1))
        try:
            await bot.delete_messages(chat_id, batch)
        except Exception:
            # Пакет не прошёл (есть старые/неудаляемые) -> по одному, молча.
            for one in batch:
                try:
                    await bot.delete_message(chat_id, one)
                except Exception:
                    pass
        mid -= 100
        await asyncio.sleep(0.3)  # бережём лимиты Telegram от флуда
    # После чистки оставляем только приветствие.
    await send_welcome(message)


@dp.message(Command("add"))
async def cmd_add(message: Message) -> None:
    # Только админ, только в личке. Можно сразу несколько:
    # /add <проект|chat_id> <@username|id> [ещё @username|id ...]
    if message.chat.type != "private" or message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer(
            "Формат: <code>/add &lt;проект|chat_id&gt; &lt;@username|id&gt; [ещё...]</code>\n\n"
            "<b>Например:</b>\n"
            "<blockquote>/add Alpha @user1\n"
            "/add Beta @user2 @user3 123456789</blockquote>"
        )
        return
    target, who_list = parts[1], parts[2:]

    def _apply(data: dict):
        key = find_chat_key(data, target)
        if key is None:
            return None
        rules = data[key]
        rules.setdefault("ids", [])
        rules.setdefault("usernames", [])
        added, skipped = [], []
        for who in who_list:
            if who.lstrip("-").isdigit():
                uid = int(who)
                if uid in rules["ids"]:
                    skipped.append(f"id {uid}")
                else:
                    rules["ids"].append(uid)
                    added.append(f"id {uid}")
            else:
                uname = who.lstrip("@").lower()
                if uname in {u.lstrip("@").lower() for u in rules["usernames"]}:
                    skipped.append("@" + uname)
                else:
                    rules["usernames"].append("@" + uname)
                    added.append("@" + uname)
        return rules.get("project", key), added, skipped

    result = await mutate_whitelist(_apply)
    if result is None:
        await message.answer(f"Не нашёл группу: <b>{esc(target)}</b>")
        return
    proj, added, skipped = result
    if added:
        audit.info("ADD project=%s %s", proj, ", ".join(added))
    lines = [f"<b>{esc(proj)}</b>"]
    if added:
        lines.append("Добавлено: <code>" + esc(", ".join(added)) + "</code>")
    if skipped:
        lines.append("Уже были: <code>" + esc(", ".join(skipped)) + "</code>")
    await message.answer("\n".join(lines))


@dp.message(Command("remove"))
async def cmd_remove(message: Message) -> None:
    # Только админ, только в личке. Можно сразу несколько:
    # /remove <проект|chat_id> <@username|id> [ещё ...]
    if message.chat.type != "private" or message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer(
            "Формат: <code>/remove &lt;проект|chat_id&gt; &lt;@username|id&gt; [ещё...]</code>\n\n"
            "<b>Например:</b>\n"
            "<blockquote>/remove Alpha @user1\n"
            "/remove Beta 123456789</blockquote>"
        )
        return
    target, who_list = parts[1], parts[2:]

    def _apply(data: dict):
        key = find_chat_key(data, target)
        if key is None:
            return None
        rules = data[key]
        removed = []
        for who in who_list:
            if who.lstrip("-").isdigit():
                uid = int(who)
                if uid in rules.get("ids", []):
                    rules["ids"] = [i for i in rules["ids"] if i != uid]
                    removed.append(f"id {uid}")
            else:
                uname = who.lstrip("@").lower()
                before = rules.get("usernames", [])
                after = [u for u in before if u.lstrip("@").lower() != uname]
                if len(after) != len(before):
                    rules["usernames"] = after
                    removed.append("@" + uname)
        return rules.get("project", key), removed

    result = await mutate_whitelist(_apply)
    if result is None:
        await message.answer(f"Не нашёл группу: <b>{esc(target)}</b>")
        return
    proj, removed = result
    if removed:
        audit.info("REMOVE project=%s %s", proj, ", ".join(removed))
        await message.answer(
            f"<b>{esc(proj)}</b>\nУдалено: <code>" + esc(", ".join(removed)) + "</code>"
        )
    else:
        await message.answer(f"<b>{esc(proj)}</b>\nНе нашёл кого удалять.")


@dp.message(Command("reload"))
async def cmd_reload(message: Message) -> None:
    # Только админ и только в личке: список доступа не должен утекать в группу.
    if message.chat.type != "private" or message.from_user.id not in ADMIN_IDS:
        return
    data = load_whitelist()
    if not data:
        await message.answer("whitelist.json пуст или не читается.")
        return
    blocks = []
    for chat_id, rules in data.items():
        ids = rules.get("ids", [])
        users = rules.get("usernames", [])
        ids_line = ", ".join(str(i) for i in ids) or "—"
        users_line = esc(", ".join("@" + u.lstrip("@") for u in users)) or "—"
        blocks.append(
            f"<b>{esc(rules.get('project', '?'))}</b> <code>{chat_id}</code>\n"
            f"ids: <code>{ids_line}</code>\n"
            f"users: {users_line}"
        )
    await message.answer("<b>Списки доступа</b>\n\n" + "\n\n".join(blocks))


@dp.errors()
async def on_error(event: ErrorEvent) -> bool:
    # Глобальный перехват: один сбойный апдейт не роняет поллинг.
    logger.exception("Ошибка обработки апдейта: %s", event.exception)
    return True


async def setup_bot_profile() -> None:
    """Само-настройка профиля бота: описание, кнопка меню, команды по скоупам."""
    try:
        await bot.set_my_description(BOT_DESCRIPTION)
        await bot.set_my_short_description(BOT_SHORT_DESCRIPTION)
    except Exception as e:
        logger.warning("Не удалось задать описание бота: %s", e)
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception as e:
        logger.warning("Не удалось задать кнопку меню: %s", e)
    try:
        await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
        for admin_id in ADMIN_IDS:
            await bot.set_my_commands(
                ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id)
            )
    except Exception as e:
        logger.warning("Не удалось задать меню команд: %s", e)


async def main() -> None:
    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS пуст — админ-команды и уведомления недоступны.")
    data = load_whitelist()
    logger.info("Загружено групп: %d, участников: %d", len(data), count_unique(data))

    await bot.delete_webhook(drop_pending_updates=True)
    await setup_bot_profile()
    logger.info("Бот запущен. Админы: %s", ADMIN_IDS or "не заданы")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
