import asyncio
import json
import logging
import os
import time
from html import escape
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher, F
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

try:
    import fcntl  # только Linux: страховка от второго экземпляра
except ImportError:
    fcntl = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("hq_access_control")

BASE_DIR = Path(__file__).resolve().parent
WHITELIST_PATH = BASE_DIR / "whitelist.json"
ENV_PATH = BASE_DIR / ".env"
LOCK_PATH = BASE_DIR / "hqbot.lock"
MIGRATED_FLAG = BASE_DIR / ".migrated_v2"
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
    """Экранирует динамический текст под HTML, чтобы < > & не ломали разметку."""
    return escape(str(value), quote=False)


def spoil(value: object) -> str:
    """Прячет id под спойлер (видно по тапу). Без <code> внутри: Telegram при
    паре спойлер+код роняет спойлер и показывает текст открыто."""
    return f"<tg-spoiler>{esc(value)}</tg-spoiler>"


def load_dotenv(path: Path) -> None:
    """Читает .env рядом со скриптом. Уже заданные переменные не перетираются."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv(ENV_PATH)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x)
    for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",")
    if x
}

# ── Тексты бота (HTML). {project} подставляется именем проекта. ────────────────
ADMIN_WELCOME = (
    "🔑 <b>HQ Access Bot</b>\n"
    "<i>Пропускной контроль рабочих групп проектов.</i>\n\n"
    "<b>Как это работает</b>\n"
    "<blockquote>Пользователь подаёт заявку по пригласительной ссылке. Бот "
    "сверяет его со списком доступа проекта и <u>мгновенно</u> одобряет или "
    "отклоняет. Об отклонённых попытках приходит уведомление.</blockquote>\n\n"
    "<b>Команды</b>\n"
    "<code>/add</code> - добавить участников\n"
    "<code>/remove</code> - убрать участников\n"
    "<code>/reload</code> - списки доступа\n"
    "<code>/delgroup</code> - убрать группу из списка\n"
    "<code>/status</code> - статус и счётчики\n"
    "<code>/clear</code> - очистить переписку\n"
    "<code>/id</code> - узнать chat_id (в группе)\n"
    "<code>/me</code> - показать ваш ID\n\n"
    "<i>Новые проекты заводятся автоматически при добавлении бота в группу. "
    "Вышедших и кикнутых бот убирает из списка сам.</i>"
)

USER_WELCOME = (
    "🔑 <b>HQ Access Bot</b>\n"
    "<i>Система доступа к рабочим группам проектов.</i>\n\n"
    "<blockquote>Чтобы войти в проект, откройте пригласительную ссылку и "
    "подайте заявку. Если вы в списке доступа - она одобрится <u>автоматически</u> "
    "за пару секунд. Если нет - доступ выдаёт администратор.</blockquote>\n\n"
    "<b>Команда</b>\n"
    "<code>/me</code> - показать ваш ID\n\n"
    'По всем вопросам: <a href="https://t.me/stashowner">@stashowner</a>'
)

APPROVE_TEXT = (
    "✅ <b>Доступ подтверждён</b>\n"
    "Добро пожаловать в проект <b>{project}</b>.\n"
    "<i>Группа уже открыта в вашем списке чатов.</i>"
)

DENY_TEXT = (
    "⛔ <b>Заявка отклонена</b>\n"
    "Вашего аккаунта нет в списке доступа этого проекта.\n"
    "<i>Если это ошибка - напишите администратору, он добавит вас в список.</i>"
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
    BotCommand(command="delgroup", description="Убрать группу из списка"),
    BotCommand(command="clear", description="Очистить переписку с ботом"),
    BotCommand(command="id", description="Узнать chat_id (в группе)"),
    BotCommand(command="me", description="Показать мой ID"),
    BotCommand(command="help", description="Справка"),
]

ADD_USAGE = (
    "Формат: <code>/add &lt;проект&gt; &lt;@username|id&gt; [ещё...]</code>\n\n"
    "<b>Например:</b>\n"
    "<blockquote>/add Alpha @user1\n"
    "/add Beta @user2 @user3 123456789</blockquote>"
)
REMOVE_USAGE = (
    "Формат: <code>/remove &lt;проект&gt; &lt;@username|id&gt; [ещё...]</code>\n\n"
    "<b>Например:</b>\n"
    "<blockquote>/remove Alpha @user1\n"
    "/remove Beta 123456789</blockquote>"
)
DELGROUP_USAGE = (
    "Формат: <code>/delgroup &lt;проект|chat_id&gt;</code>\n\n"
    "<b>Например:</b>\n"
    "<blockquote>/delgroup Test\n"
    "/delgroup -1001234567890</blockquote>"
)

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN пуст. Задай его в .env / переменных окружения.")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# ── Модель данных ─────────────────────────────────────────────────────────────
# whitelist.json: { "<chat_id>": {"project": "Имя", "members": [{"id":int, "username":str}]} }
# Только супергруппы (id начинается с -100). Старый формат с ids/usernames мигрируется.
# Участник = пермит. Бот синхронизирует пермиты с реальным членством в группе:
# вышел/кикнут -> убирается; сменил/убрал username -> обновляется.


def is_supergroup_key(key: str) -> bool:
    return key.startswith("-100")


def normalize_group(rules: dict) -> dict:
    """Приводит группу к {project, members:[{id, username}]}, чистит дубли."""
    project = rules.get("project", "")
    members: list[dict] = []
    seen: set = set()

    def add(member: dict) -> None:
        key = ("id", member["id"]) if "id" in member else ("un", member["username"])
        if key in seen:
            return
        seen.add(key)
        members.append(member)

    if isinstance(rules.get("members"), list):
        for m in rules["members"]:
            member: dict = {}
            if m.get("id") is not None:
                member["id"] = int(m["id"])
            un = (m.get("username") or "").lstrip("@").lower()
            if un:
                member["username"] = un
            if m.get("in"):  # подтверждённое членство в группе
                member["in"] = True
            name = (m.get("name") or "").strip()
            if name:  # отображаемое имя - чтобы опознать юзеров без @username
                member["name"] = name
            if member:
                add(member)
    else:  # старый формат
        for i in rules.get("ids", []):
            add({"id": int(i)})
        for u in rules.get("usernames", []):
            un = u.lstrip("@").lower()
            if un:
                add({"username": un})
    return {"project": project, "members": members}


def normalize(data: dict) -> dict:
    return {key: normalize_group(rules) for key, rules in data.items()}


def load_whitelist() -> dict:
    try:
        return normalize(json.loads(WHITELIST_PATH.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        logger.error("whitelist.json повреждён: %s", e)
        return {}


def save_whitelist(data: dict) -> None:
    """Атомарная запись: временный файл + замена. Не побьётся при сбое."""
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = WHITELIST_PATH.with_name(WHITELIST_PATH.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(WHITELIST_PATH)


WHITELIST_LOCK = asyncio.Lock()


async def mutate_whitelist(mutator):
    """Сериализует read-modify-write whitelist.json через лок."""
    async with WHITELIST_LOCK:
        data = load_whitelist()
        result = mutator(data)
        save_whitelist(data)
        return result


def find_chat_key(data: dict, token: str) -> str | None:
    """Находит группу по chat_id или имени проекта (регистр любой, имя - из
    нескольких слов)."""
    if token in data:
        return token
    low = token.lower()
    for key, rules in data.items():
        if rules.get("project", "").lower() == low:
            return key
    return None


def match_member(members: list[dict], user_id: int, username: str | None) -> dict | None:
    un = username.lower() if username else None
    for m in members:
        if m.get("id") == user_id:
            return m
        if un and m.get("username") == un:
            return m
    return None


def dedup_keep(members: list[dict], keep: dict) -> list[dict]:
    """Оставляет keep, выкидывает других участников-дублей (тот же id или тот же
    username, что у keep)."""
    return [
        x for x in members
        if x is keep
        or (x.get("id") != keep.get("id")
            and (not keep.get("username") or x.get("username") != keep.get("username")))
    ]


def remove_member_refs(members: list[dict], uid: int | None, username: str | None):
    """Убирает участников, совпавших по id или username. -> (оставшиеся, метки)."""
    un = username.lower() if username else None
    kept, removed = [], []
    for m in members:
        if (uid is not None and m.get("id") == uid) or (un and m.get("username") == un):
            removed.append("@" + m["username"] if m.get("username") else f"id {m.get('id')}")
        else:
            kept.append(m)
    return kept, removed


def count_unique(data: dict) -> int:
    keys: set = set()
    for g in data.values():
        for m in g.get("members", []):
            if m.get("id") is not None:
                keys.add(("id", m["id"]))
            elif m.get("username"):
                keys.add(("un", m["username"]))
    return len(keys)


def split_targets(tokens: list[str]) -> tuple[str, list[str]]:
    """Делит аргументы на (проект, пользователи). Пользователи - хвостовые токены
    @username или положительное число. Всё до них - имя проекта/chat_id."""
    i = len(tokens)
    while i > 0 and (tokens[i - 1].startswith("@") or tokens[i - 1].isdigit()):
        i -= 1
    return " ".join(tokens[:i]), tokens[i:]


def member_line(m: dict) -> str:
    """Строка участника для /reload. Метка: @username, иначе имя (для юзеров без
    username). id - спойлером. «ожидает входа» - пока не подтверждён в группе."""
    if m.get("username"):
        who = "@" + esc(m["username"])
    elif m.get("name"):
        who = esc(m["name"]) + " <i>(без @username)</i>"
    else:
        who = "<i>без имени</i>"
    line = "• " + who
    if m.get("id") is not None:
        line += " " + spoil(m["id"])
    if not m.get("in"):
        line += " <i>· ожидает входа</i>"
    return line


async def safe_dm(user_id: int, text: str) -> None:
    try:
        await bot.send_message(user_id, text)
    except Exception as e:
        logger.warning("ЛС юзеру %s не доставлено: %s", user_id, e)


async def notify_admins(text: str) -> None:
    for admin_id in ADMIN_IDS:
        await safe_dm(admin_id, text)


async def send_welcome(message: Message) -> None:
    text = ADMIN_WELCOME if message.from_user.id in ADMIN_IDS else USER_WELCOME
    await message.answer(text, link_preview_options=LinkPreviewOptions(is_disabled=True))


@dp.chat_join_request()
async def on_join_request(event: ChatJoinRequest) -> None:
    chat_id = event.chat.id
    user = event.from_user
    data = load_whitelist()
    rules = data.get(str(chat_id))
    project = (rules or {}).get("project", "проект")
    member = match_member(rules["members"], user.id, user.username) if rules else None

    if member is not None:
        await event.approve()
        logger.info("APPROVED id=%s @%s -> %s (%s)", user.id, user.username, chat_id, project)
        audit.info("APPROVED id=%s @%s project=%s", user.id, user.username, project)
        cur_un = user.username.lower() if user.username else None
        if (member.get("id") != user.id or not member.get("in")
                or (cur_un and member.get("username") != cur_un)):
            def _upgrade(d: dict) -> None:
                g = d.get(str(chat_id))
                if not g:
                    return
                mm = match_member(g["members"], user.id, user.username)
                if mm is None:
                    return
                mm["id"] = user.id
                mm["in"] = True  # человек реально вошёл в группу
                mm["name"] = user.full_name  # имя - для опознания юзеров без username
                if cur_un:
                    mm["username"] = cur_un
                g["members"] = dedup_keep(g["members"], mm)

            await mutate_whitelist(_upgrade)
        await safe_dm(user.id, APPROVE_TEXT.format(project=esc(project)))
        uname = "@" + esc(user.username) if user.username else "<i>без username</i>"
        await notify_admins(
            f"✅ <b>Заявка одобрена</b> · {esc(project)}\n"
            f"👤 {esc(user.full_name)} · {uname}\n"
            f"id {spoil(user.id)}"
        )
        return

    await event.decline()
    logger.info("DECLINED id=%s @%s -> %s", user.id, user.username, chat_id)
    audit.info("DECLINED id=%s @%s project=%s", user.id, user.username, project)
    await safe_dm(user.id, DENY_TEXT)
    uname = "@" + esc(user.username) if user.username else "<i>без username</i>"
    await notify_admins(
        f"⛔ <b>Заявка отклонена</b> · {esc(project)}\n"
        f"👤 {esc(user.full_name)} · {uname}\n"
        f"id {spoil(user.id)}"
    )


@dp.chat_member()
async def on_chat_member(event: ChatMemberUpdated) -> None:
    # Слежение за участниками группы. Бот должен быть админом, чтобы получать это.
    # Вышел/кикнут -> убираем пермит. Зашёл/активен -> обновляем id и username.
    key = str(event.chat.id)
    if key not in load_whitelist():
        return  # группа не в системе
    u = event.new_chat_member.user
    new = event.new_chat_member.status
    uid = u.id
    un = u.username.lower() if u.username else None

    if new in ("left", "kicked"):
        def _rm(d: dict):
            g = d.get(key)
            if not g:
                return [], ""
            g["members"], removed = remove_member_refs(g["members"], uid, un)
            return removed, g.get("project", key)

        removed, proj = await mutate_whitelist(_rm)
        if removed:
            logger.info("MEMBER-LEFT id=%s project=%s removed=%s", uid, proj, removed)
            audit.info("MEMBER-LEFT id=%s project=%s %s", uid, proj, ", ".join(removed))
            who = ("@" + esc(u.username)) if u.username else esc(u.full_name)
            await notify_admins(
                f"👋 <b>Участник вышел</b> · {esc(proj)}\n"
                f"👤 {who} · id {spoil(uid)}\n"
                f"<i>Убран из списка доступа.</i>"
            )
        return

    if new in ("member", "administrator", "creator"):
        data = load_whitelist()
        g = data.get(key)
        if g and match_member(g["members"], uid, u.username):
            def _up(d: dict) -> None:
                gg = d.get(key)
                if not gg:
                    return
                mm = match_member(gg["members"], uid, u.username)
                if mm is None:
                    return
                mm["id"] = uid
                mm["in"] = True  # подтверждено: участник в группе
                mm["name"] = u.full_name
                if un:
                    mm["username"] = un
                gg["members"] = dedup_keep(gg["members"], mm)

            await mutate_whitelist(_up)


@dp.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated) -> None:
    chat = event.chat
    old = event.old_chat_member.status
    new = event.new_chat_member.status
    key = str(chat.id)
    title = chat.title or key

    # Бота убрали из группы -> чистим запись.
    if new in ("left", "kicked"):
        removed = await mutate_whitelist(lambda d: d.pop(key, None) is not None)
        if removed:
            logger.info("GROUP-REMOVED %s «%s» (бот удалён)", key, title)
            audit.info("GROUP-REMOVED %s «%s»", key, title)
            await notify_admins(
                f"👥 <b>Бот удалён из группы</b> · {esc(title)}\n"
                f"<i>Группа убрана из списка доступа.</i>"
            )
        return

    if new not in ("member", "administrator", "creator"):
        return

    # Обычная группа: заявки на вступление тут не работают - не заводим, подсказываем.
    if chat.type != "supergroup":
        if old in ("left", "kicked"):
            await notify_admins(
                f"⚠️ <b>Добавлен в обычную группу</b> · {esc(title)}\n"
                f"<i>Заявки на вступление работают только в супергруппах. Выдайте "
                f"боту права администратора - группа станет супергруппой и попадёт "
                f"в список доступа.</i>"
            )
        return

    # Супергруппа: завести, если ещё нет; иначе отметить повышение до админа.
    def _ensure(d: dict) -> bool:
        if key in d:
            return False
        d[key] = {"project": title, "members": []}
        return True

    created = await mutate_whitelist(_ensure)
    if created:
        logger.info("AUTO-ADDED group %s «%s»", key, title)
        audit.info("GROUP-ADDED %s «%s»", key, title)
        await notify_admins(
            f"👥 <b>Группа добавлена в систему</b>\n"
            f"<b>{esc(title)}</b>\n"
            f"<i>Пока пустая.</i> Добавить людей: <code>/add {esc(title)} @username</code>"
        )
    elif new == "administrator" and old in ("member", "restricted"):
        await notify_admins(f"✅ <b>Бот назначен администратором</b> · {esc(title)}")
    elif new == "member" and old in ("administrator", "creator"):
        await notify_admins(
            f"⚠️ <b>У бота забрали админку</b> · {esc(title)}\n"
            f"<i>Заявки не обрабатываются, участники не отслеживаются. "
            f"Верните боту права администратора.</i>"
        )


@dp.message(F.migrate_from_chat_id)
async def on_migration(message: Message) -> None:
    # Группа стала супергруппой -> её id сменился. Переносим запись на новый id,
    # чтобы не плодить дубль. (aiogram issue #1000: ловим migrate_from_chat_id.)
    old_key = str(message.migrate_from_chat_id)
    new_key = str(message.chat.id)
    title = message.chat.title or new_key

    def _rename(d: dict) -> bool:
        old = d.pop(old_key, None)
        if old is None:
            return False
        if new_key in d:  # новый ключ уже есть - сливаем участников
            existing = d[new_key].setdefault("members", [])
            seen = {
                ("id", m["id"]) if "id" in m else ("un", m.get("username"))
                for m in existing
            }
            for m in old.get("members", []):
                k = ("id", m["id"]) if "id" in m else ("un", m.get("username"))
                if k not in seen:
                    existing.append(m)
                    seen.add(k)
        else:
            d[new_key] = old
        return True

    if await mutate_whitelist(_rename):
        logger.info("MIGRATED %s -> %s «%s»", old_key, new_key, title)
        audit.info("MIGRATED %s -> %s", old_key, new_key)
        await notify_admins(
            f"🔄 <b>Группа стала супергруппой</b> · {esc(title)}\n"
            f"<i>id обновлён, список доступа сохранён.</i>"
        )


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await send_welcome(message)


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await send_welcome(message)


@dp.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"🆔 chat_id: <code>{message.chat.id}</code>")


@dp.message(Command("me"))
async def cmd_me(message: Message) -> None:
    # Свой id показываем моноширинным (копируется), это не чужой - прятать незачем.
    u = message.from_user
    if u.username:
        await message.answer(
            f"🆔 Ваш ID: <code>{u.id}</code>\n👤 Username: @{esc(u.username)}"
        )
    else:
        await message.answer(
            f"🆔 Ваш ID: <code>{u.id}</code>\n"
            f"⚠️ <b>У вас не задан @username.</b> Передайте этот ID администратору - "
            f"он добавит вас по нему. Нажмите на номер, чтобы скопировать."
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
        "📊 <b>Статус бота</b>\n"
        "Состояние: <b>работает</b> ✅\n"
        f"Аптайм: <b>{h} ч {m} мин</b>\n"
        f"Групп: <b>{len(data)}</b> · Участников: <b>{count_unique(data)}</b>"
    )


@dp.message(Command("clear"))
async def cmd_clear(message: Message) -> None:
    if message.chat.type != "private":
        return
    chat_id = message.chat.id
    mid = message.message_id
    while mid > 0:
        batch = list(range(mid, max(mid - 100, 0), -1))
        try:
            await bot.delete_messages(chat_id, batch)
        except Exception:
            for one in batch:
                try:
                    await bot.delete_message(chat_id, one)
                except Exception:
                    pass
        mid -= 100
        await asyncio.sleep(0.3)
    await send_welcome(message)


@dp.message(Command("add"))
async def cmd_add(message: Message) -> None:
    if message.chat.type != "private" or message.from_user.id not in ADMIN_IDS:
        return
    target, who_list = split_targets((message.text or "").split()[1:])
    if not target or not who_list:
        await message.answer(ADD_USAGE)
        return

    def _apply(data: dict):
        key = find_chat_key(data, target)
        if key is None:
            return None
        members = data[key].setdefault("members", [])
        added, skipped = [], []
        for who in who_list:
            if who.isdigit():
                uid = int(who)
                if any(m.get("id") == uid for m in members):
                    skipped.append(f"id {uid}")
                else:
                    members.append({"id": uid})
                    added.append(f"id {uid}")
            else:
                un = who.lstrip("@").lower()
                if not un:
                    continue
                if any(m.get("username") == un for m in members):
                    skipped.append("@" + un)
                else:
                    members.append({"username": un})
                    added.append("@" + un)
        return data[key].get("project", key), added, skipped

    result = await mutate_whitelist(_apply)
    if result is None:
        await message.answer(f"Не нашёл группу: <b>{esc(target)}</b>")
        return
    proj, added, skipped = result
    if added:
        audit.info("ADD project=%s %s", proj, ", ".join(added))
    lines = [f"<b>{esc(proj)}</b>"]
    if added:
        lines.append("✅ Добавлено: " + esc(", ".join(added)))
    if skipped:
        lines.append("⏭ Уже были: " + esc(", ".join(skipped)))
    await message.answer("\n".join(lines))


@dp.message(Command("remove"))
async def cmd_remove(message: Message) -> None:
    if message.chat.type != "private" or message.from_user.id not in ADMIN_IDS:
        return
    target, who_list = split_targets((message.text or "").split()[1:])
    if not target or not who_list:
        await message.answer(REMOVE_USAGE)
        return

    def _apply(data: dict):
        key = find_chat_key(data, target)
        if key is None:
            return None
        members = data[key].get("members", [])
        removed = []
        for who in who_list:
            if who.isdigit():
                members, r = remove_member_refs(members, int(who), None)
                removed += r
            else:
                members, r = remove_member_refs(members, None, who.lstrip("@").lower())
                removed += r
        data[key]["members"] = members
        return data[key].get("project", key), removed

    result = await mutate_whitelist(_apply)
    if result is None:
        await message.answer(f"Не нашёл группу: <b>{esc(target)}</b>")
        return
    proj, removed = result
    if removed:
        audit.info("REMOVE project=%s %s", proj, ", ".join(removed))
        await message.answer(f"<b>{esc(proj)}</b>\n🗑 Удалено: " + esc(", ".join(removed)))
    else:
        await message.answer(f"<b>{esc(proj)}</b>\nНикого не нашёл для удаления.")


@dp.message(Command("delgroup"))
async def cmd_delgroup(message: Message) -> None:
    # Убирает группу целиком из списка доступа (для чистки мусорных/тестовых).
    if message.chat.type != "private" or message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(DELGROUP_USAGE)
        return
    target = parts[1].strip()

    def _del(data: dict):
        key = find_chat_key(data, target)
        if key is None:
            return None
        proj = data[key].get("project", key)
        del data[key]
        return proj, key

    result = await mutate_whitelist(_del)
    if result is None:
        await message.answer(f"Не нашёл группу: <b>{esc(target)}</b>")
        return
    proj, key = result
    audit.info("GROUP-DELETED %s «%s» (вручную)", key, proj)
    await message.answer(f"🗑 Группа <b>{esc(proj)}</b> убрана из списка доступа.")


async def reconcile_membership(data: dict) -> dict:
    """getChatMember по каждому id -> {(key, id): (в_группе, username|None, имя)}."""
    info: dict = {}
    for chat_key, g in data.items():
        for m in g.get("members", []):
            uid = m.get("id")
            if uid is None:
                continue
            try:
                cm = await bot.get_chat_member(int(chat_key), uid)
            except Exception:
                continue  # не достучались - не трогаем
            cur_un = (cm.user.username or "").lower() or None
            info[(chat_key, uid)] = (
                cm.status not in ("left", "kicked"),
                cur_un,
                cm.user.full_name,
            )
    return info


@dp.message(Command("reload"))
async def cmd_reload(message: Message) -> None:
    if message.chat.type != "private" or message.from_user.id not in ADMIN_IDS:
        return
    snapshot = load_whitelist()
    if not snapshot:
        await message.answer("Список доступа пуст.")
        return

    info = await reconcile_membership(snapshot)

    def _sync(data: dict) -> None:
        for chat_key, g in data.items():
            kept = []
            gone_unames: set = set()  # ники вышедших - для чистки парных username-записей
            for m in g.get("members", []):
                uid = m.get("id")
                state = info.get((chat_key, uid)) if uid is not None else None
                if state is None:
                    kept.append(m)  # не проверяли (нет id или не достучались)
                    continue
                in_group, un, name = state
                if in_group:
                    m["in"] = True
                    if un:
                        m["username"] = un
                    else:
                        m.pop("username", None)
                    if name:
                        m["name"] = name
                    kept.append(m)
                elif m.get("in"):
                    # был в группе, теперь вышел/кикнут -> убираем id и его username-дубль
                    if un:
                        gone_unames.add(un)
                    if m.get("username"):
                        gone_unames.add(m["username"])
                    continue
                else:
                    kept.append(m)  # пермит-ожидание (ещё не заходил) - оставляем
            kept = [
                m for m in kept
                if not (m.get("id") is None and m.get("username") in gone_unames)
            ]
            # склейка username-only дублей с активными id-участниками
            id_unames = {
                m["username"]
                for m in kept
                if m.get("id") is not None and m.get("username")
            }
            g["members"] = [
                m for m in kept
                if not (m.get("id") is None and m.get("username") in id_unames)
            ]

    await mutate_whitelist(_sync)

    data = load_whitelist()
    blocks = []
    for chat_id, g in data.items():
        lines = [f"<b>{esc(g.get('project', '?'))}</b>"]
        members = g.get("members", [])
        if members:
            lines.extend(member_line(m) for m in members)
        else:
            lines.append("<i>пусто</i>")
        blocks.append("\n".join(lines))
    await message.answer("📋 <b>Списки доступа</b>\n\n" + "\n\n".join(blocks))


@dp.errors()
async def on_error(event: ErrorEvent) -> bool:
    logger.exception("Ошибка обработки апдейта: %s", event.exception)
    return True


_lock_handle = None


def acquire_singleton_lock() -> None:
    """flock-страховка: второй экземпляр не стартует (защита от 409 Conflict при
    случайном двойном запуске). На Windows пропускается."""
    global _lock_handle
    if fcntl is None:
        return
    _lock_handle = open(LOCK_PATH, "w")
    try:
        fcntl.flock(_lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("Другой экземпляр бота уже запущен. Выходим.")


def prune_basic_groups(data: dict) -> int:
    """Убирает из списка обычные (не супер) группы - в них заявки не работают."""
    junk = [k for k in data if not is_supergroup_key(k)]
    for k in junk:
        del data[k]
    return len(junk)


def migrate_mark_present(data: dict) -> bool:
    """Разовая миграция: помечает существующих id-участников как реально
    входивших (in=True), чтобы /reload смог почистить тех, кто уже вышел.
    Выполняется один раз (по флаг-файлу), новые пермиты не затрагивает."""
    if MIGRATED_FLAG.exists():
        return False
    for g in data.values():
        for m in g.get("members", []):
            if m.get("id") is not None:
                m["in"] = True
    MIGRATED_FLAG.touch()
    return True


async def setup_bot_profile() -> None:
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
    acquire_singleton_lock()
    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS пуст — админ-команды и уведомления недоступны.")

    data = load_whitelist()
    pruned = prune_basic_groups(data)
    marked = migrate_mark_present(data)
    if data or pruned:
        save_whitelist(data)  # миграция формата + чистка мусорных групп + пометка in
    if pruned:
        logger.info("Почищено мусорных (не супер) групп: %d", pruned)
    if marked:
        logger.info("Разовая миграция: id-участники помечены как входившие.")
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
