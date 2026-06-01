import asyncio
import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher
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

# ── Тексты бота. Правь здесь. {project} подставляется именем проекта. ──────────
ADMIN_WELCOME = (
    "<b>HQ Access Bot</b>\n"
    "Пропускной контроль рабочих групп проектов.\n\n"
    "<b>Как это работает</b>\n"
    "Пользователь подаёт заявку по пригласительной ссылке. Бот сверяет его "
    "со списком доступа проекта и мгновенно одобряет или отклоняет. "
    "Об отклонённых попытках вы получаете уведомление.\n\n"
    "<b>Команды администратора</b>\n"
    "/add &lt;проект&gt; &lt;@username|id&gt; [ещё...] — добавить участников\n"
    "/remove &lt;проект&gt; &lt;@username|id&gt; [ещё...] — убрать участников\n"
    "/reload — показать списки доступа\n"
    "/status — статус и счётчики\n"
    "/clear — очистить эту переписку\n"
    "/id — узнать chat_id (отправьте команду в группе)\n"
    "/me — показать ваш ID\n\n"
    "<b>Проекты добавляются сами</b> при добавлении бота в группу.\n"
    "Текущие: Brierly, PTTC, VaultForge, Scentio."
)

USER_WELCOME = (
    "<b>HQ Access Bot</b>\n"
    "Система доступа к рабочим группам проектов.\n\n"
    "Чтобы войти в проект, откройте пригласительную ссылку и подайте заявку. "
    "Если вы в списке доступа, она одобрится автоматически за пару секунд. "
    "Если нет — доступ выдаёт администратор.\n\n"
    "<b>Команда</b>\n"
    "/me — показать ваш ID (его может попросить администратор)\n\n"
    'По всем вопросам: <a href="https://t.me/stashowner">@stashowner</a>'
)

APPROVE_TEXT = (
    "Доступ подтверждён. Добро пожаловать в проект {project}.\n"
    "Группа уже открыта в вашем списке чатов."
)

DENY_TEXT = (
    "Заявка отклонена.\n\n"
    "Вашего аккаунта нет в списке доступа этого проекта. "
    "Если это ошибка — напишите администратору, он добавит вас в список."
)

BOT_DESCRIPTION = (
    "HQ Access Bot — пропускной контроль приватных рабочих групп. "
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

bot = Bot(token=BOT_TOKEN)
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
    """Сохраняет whitelist.json. Бот читает файл на каждую заявку,
    поэтому правки через /add и /remove действуют сразу."""
    WHITELIST_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


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


def count_people(data: dict) -> int:
    return sum(
        len(r.get("ids", [])) + len(r.get("usernames", [])) for r in data.values()
    )


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
        parse_mode="HTML",
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
            rules.setdefault("ids", []).append(user.id)
            save_whitelist(data)
        await safe_dm(user.id, APPROVE_TEXT.format(project=project))
        return

    await event.decline()
    logger.info("DECLINED id=%s @%s -> %s", user.id, user.username, chat_id)
    audit.info("DECLINED id=%s @%s project=%s", user.id, user.username, project)
    await safe_dm(user.id, DENY_TEXT)
    await notify_admins(
        f"Отклонена заявка в проект {project}.\n"
        f"Кто: {user.full_name} @{user.username or '—'} (id {user.id})"
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
    data = load_whitelist()
    key = str(chat.id)
    if key in data:
        return  # группа уже заведена, ничего не трогаем
    data[key] = {"project": chat.title or key, "ids": [], "usernames": []}
    save_whitelist(data)
    logger.info("AUTO-ADDED group %s «%s» в whitelist", key, chat.title)
    audit.info("GROUP-ADDED %s «%s»", key, chat.title)
    await notify_admins(
        f"Бот добавлен в группу «{chat.title}» ({key}).\n"
        f"Группа заведена в список доступа, пока пустая.\n"
        f"Добавь людей: /add {key} @username"
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
    await message.answer(
        f"chat_id: <code>{message.chat.id}</code>", parse_mode="HTML"
    )


@dp.message(Command("me"))
async def cmd_me(message: Message) -> None:
    # Любой может написать боту /me в ЛС и узнать свой числовой ID.
    u = message.from_user
    await message.answer(
        f"user_id: <code>{u.id}</code>\nusername: @{u.username or '—'}",
        parse_mode="HTML",
    )


@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    data = load_whitelist()
    up = int(time.time() - STARTED_AT)
    h, rem = divmod(up, 3600)
    m, _ = divmod(rem, 60)
    await message.answer(
        "Статус: работает\n"
        f"Аптайм: {h} ч {m} мин\n"
        f"Групп заведено: {len(data)}\n"
        f"Людей в списках: {count_people(data)}"
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
        await message.answer("Формат: /add <проект|chat_id> <@username|id> [ещё...]")
        return
    target, who_list = parts[1], parts[2:]
    data = load_whitelist()
    key = find_chat_key(data, target)
    if key is None:
        await message.answer(f"Не нашёл группу: {target}")
        return
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
    save_whitelist(data)
    proj = rules.get("project", key)
    if added:
        audit.info("ADD project=%s %s", proj, ", ".join(added))
    lines = [f"Проект {proj}:"]
    if added:
        lines.append("добавлено: " + ", ".join(added))
    if skipped:
        lines.append("уже были: " + ", ".join(skipped))
    await message.answer("\n".join(lines))


@dp.message(Command("remove"))
async def cmd_remove(message: Message) -> None:
    # Только админ, только в личке. Можно сразу несколько:
    # /remove <проект|chat_id> <@username|id> [ещё ...]
    if message.chat.type != "private" or message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer("Формат: /remove <проект|chat_id> <@username|id> [ещё...]")
        return
    target, who_list = parts[1], parts[2:]
    data = load_whitelist()
    key = find_chat_key(data, target)
    if key is None:
        await message.answer(f"Не нашёл группу: {target}")
        return
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
    save_whitelist(data)
    proj = rules.get("project", key)
    if removed:
        audit.info("REMOVE project=%s %s", proj, ", ".join(removed))
        await message.answer(f"Проект {proj}: удалено: " + ", ".join(removed))
    else:
        await message.answer(f"Проект {proj}: не нашёл кого удалять.")


@dp.message(Command("reload"))
async def cmd_reload(message: Message) -> None:
    # Только для админов. Показывает, что бот видит в whitelist.json сейчас.
    if message.from_user.id not in ADMIN_IDS:
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
        users_line = ", ".join("@" + u.lstrip("@") for u in users) or "—"
        blocks.append(
            f"{rules.get('project', '?')} ({chat_id})\n"
            f"  ids: {ids_line}\n"
            f"  users: {users_line}"
        )
    await message.answer("Whitelist:\n\n" + "\n\n".join(blocks))


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
    logger.info("Загружено групп: %d, людей в списках: %d", len(data), count_people(data))

    await bot.delete_webhook(drop_pending_updates=True)
    await setup_bot_profile()
    logger.info("Бот запущен. Админы: %s", ADMIN_IDS or "не заданы")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
