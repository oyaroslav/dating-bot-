"""
Telegram Dating Bot — бот знакомств.
Анкета: Имя, Возраст, Пол, Кого ищет, Город, Церковь,
        Семейное положение, Дети, Хобби, Фото.
Логика: лайки/дизлайки → при взаимном лайке оба получают контакт другого.
"""
import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove,
    TelegramObject, InputMediaPhoto, FSInputFile,
)
from typing import Any, Awaitable, Callable, Dict
from dotenv import load_dotenv

import os as _os
import database as db
import photo_utils

# ----------- Настройка -----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден. Создай файл .env и положи туда токен.")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# Список ID админов через запятую: ADMIN_IDS=123456,789012
ADMIN_IDS = set()
admin_ids_raw = os.getenv("ADMIN_IDS", "")
for part in admin_ids_raw.split(","):
    part = part.strip()
    if part.isdigit():
        ADMIN_IDS.add(int(part))
if ADMIN_IDS:
    logging.info(f"Админы загружены: {ADMIN_IDS}")
else:
    logging.warning("ADMIN_IDS не задан. Команды бана недоступны. "
                    "Добавь ADMIN_IDS=твой_telegram_id в .env")


def is_root(user_id: int) -> bool:
    """Root — админ из .env, разжаловать нельзя, макс. права."""
    return user_id in ADMIN_IDS


async def is_admin(user_id: int) -> bool:
    """Admin — root + любой admin в БД. Модер — НЕ включается сюда."""
    if user_id in ADMIN_IDS:
        return True
    role = await db.get_role(user_id)
    return role == "admin"


async def is_moderator(user_id: int) -> bool:
    """Modrator+ — root, admin или moderator. Т.е. любой уровень админства."""
    if user_id in ADMIN_IDS:
        return True
    role = await db.get_role(user_id)
    return role in ("admin", "moderator")


async def get_admin_level(user_id: int) -> str | None:
    """Возвращает уровень: 'root' | 'admin' | 'moderator' | None."""
    if user_id in ADMIN_IDS:
        return "root"
    role = await db.get_role(user_id)
    return role  # 'admin' / 'moderator' / None


# ----------- Парсер target_id для админ-команд -----------
# Принимает любую разумную форму:
#   123456            → TG-юзер 123456
#   -123456           → VK-юзер -123456
#   vk.com/id123456   → VK-юзер -123456
#   https://vk.com/id123456 → то же самое
#   @username         → TG-юзер по username (если есть в нашей БД)
#   t.me/username     → то же самое
import re as _re
_VK_ID_RE = _re.compile(r"vk\.com/(?:id)?(\d+)")
_TG_USERNAME_RE = _re.compile(r"(?:t\.me/|@)([A-Za-z][\w]{3,31})")


async def parse_target_id(arg: str) -> int | None:
    """Превращает строку в db_user_id. Возвращает None если не распознано
    или пользователь не найден (для @username)."""
    if not arg:
        return None
    arg = arg.strip()

    # Прямое число (TG-id положительный, VK-id отрицательный)
    try:
        return int(arg)
    except ValueError:
        pass

    # vk.com/id12345 → -12345
    m = _VK_ID_RE.search(arg)
    if m:
        return -int(m.group(1))

    # @username или t.me/username → ищем в нашей БД
    m = _TG_USERNAME_RE.search(arg)
    if m:
        username = m.group(1)
        user = await db.get_user_by_username(username)
        if user:
            return user["user_id"]

    return None

# ----------- Обязательная подписка на каналы -----------
# Формат в .env: REQUIRED_CHANNELS=@channel1,@channel2,-1001234567890
# Можно использовать как @username, так и числовой id (для приватных каналов).
# ВАЖНО: бот должен быть админом в каждом из этих каналов!
REQUIRED_CHANNELS: list[str] = []
for ch in os.getenv("REQUIRED_CHANNELS", "").split(","):
    ch = ch.strip()
    if ch:
        REQUIRED_CHANNELS.append(ch)
if REQUIRED_CHANNELS:
    logging.info(f"Обязательные каналы: {REQUIRED_CHANNELS}")
else:
    logging.info("REQUIRED_CHANNELS не задан — проверка подписки выключена.")


# ----------- Согласие на обработку ПДн (152-ФЗ) -----------
# Тексты политики и соглашения хранятся в файлах рядом с ботом.
# Они отдаются прямо в чат — без переходов на внешние сайты.
def _load_doc(filename: str, fallback: str) -> str:
    """Грузим текст документа из файла. Если файла нет — fallback."""
    path = os.path.join(os.path.dirname(__file__), filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        logging.warning(f"Файл {filename} не найден, использую заглушку.")
        return fallback


PRIVACY_POLICY_TEXT = _load_doc(
    "PRIVACY_POLICY.md",
    "Текст политики не настроен. Свяжись с администратором."
)
USER_AGREEMENT_TEXT = _load_doc(
    "USER_AGREEMENT.md",
    "Текст соглашения не настроен. Свяжись с администратором."
)
# При смене версии все согласия обнулятся — пользователи примут заново.
CONSENT_VERSION = "v1"

# Telegram ограничивает длину сообщения 4096 символами.
# Если документ длиннее — режем на части и шлём подряд.
TG_MSG_LIMIT = 4000  # с запасом на форматирование


def split_for_telegram(text: str, limit: int = TG_MSG_LIMIT) -> list[str]:
    """Делит длинный текст на куски по строкам, не разрывая абзацы."""
    if len(text) <= limit:
        return [text]
    parts, buf = [], []
    cur_len = 0
    for line in text.split("\n"):
        # +1 за \n
        if cur_len + len(line) + 1 > limit and buf:
            parts.append("\n".join(buf))
            buf, cur_len = [], 0
        buf.append(line)
        cur_len += len(line) + 1
    if buf:
        parts.append("\n".join(buf))
    return parts


async def send_long_document(chat_id: int, text: str):
    """Отправляет длинный документ в чат, разбив на части если нужно."""
    for part in split_for_telegram(text):
        try:
            await bot.send_message(chat_id, part, disable_web_page_preview=True)
        except Exception as e:
            logging.warning(f"Не смог отправить часть документа: {e}")


def consent_kb() -> InlineKeyboardMarkup:
    """Клавиатура: показать политику, показать соглашение, принять, отказаться."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Читать политику обработки ПДн",
                              callback_data="consent:show_privacy")],
        [InlineKeyboardButton(text="📜 Читать пользовательское соглашение",
                              callback_data="consent:show_agreement")],
        [InlineKeyboardButton(text="✅ Принимаю",
                              callback_data="consent:accept")],
        [InlineKeyboardButton(text="❌ Отказываюсь",
                              callback_data="consent:decline")],
    ])


async def require_consent(message_or_call) -> bool:
    """Проверяет, есть ли у пользователя согласие. Если нет — показывает
    запрос с кнопками и возвращает False. Если есть — True."""
    user_id = message_or_call.from_user.id
    if await db.has_consent(user_id):
        return True

    text = (
        "📋 <b>Согласие на обработку персональных данных</b>\n\n"
        "Для пользования ботом необходимо согласие на обработку "
        "персональных данных в соответствии с Федеральным законом "
        "№ 152-ФЗ «О персональных данных».\n\n"
        "Бот собирает: имя, возраст, пол, город, церковь, семейное положение, "
        "наличие детей, описание интересов, фотографии. Эти данные "
        "показываются другим пользователям бота, чтобы вы могли знакомиться.\n\n"
        "<b>Перед тем как принять</b> — ознакомься с документами по кнопкам ниже."
    )
    if isinstance(message_or_call, CallbackQuery):
        try:
            await message_or_call.message.answer(text, reply_markup=consent_kb())
        except Exception:
            pass
    else:
        await message_or_call.answer(text, reply_markup=consent_kb())
    return False


async def get_unsubscribed_channels(user_id: int) -> list[dict]:
    """Возвращает список каналов, на которые пользователь НЕ подписан.
    Каждый элемент: {chat_id, title, invite_link}.
    Если список пустой — пользователь подписан на все нужные каналы."""
    if not REQUIRED_CHANNELS:
        return []
    if is_root(user_id):
        return []  # root-админам не мешаем

    missing = []
    for ch in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(ch, user_id)
            # Допустимые статусы: creator, administrator, member, restricted (не banned/left)
            if member.status in ("left", "kicked"):
                chat = await bot.get_chat(ch)
                # ссылка: для публичного канала t.me/username, для приватного — invite_link
                if chat.username:
                    link = f"https://t.me/{chat.username}"
                else:
                    link = chat.invite_link or ""
                missing.append({
                    "chat_id": ch,
                    "title": chat.title or str(ch),
                    "link": link,
                })
        except Exception as e:
            logging.warning(
                f"Не смог проверить подписку user={user_id} на {ch}: {e}. "
                f"Убедись, что бот — админ в этом канале."
            )
            # Если канал недоступен боту — лучше пропустить проверку, чем блокировать всех
    return missing


def subscription_kb(missing: list[dict]) -> InlineKeyboardMarkup:
    """Клавиатура с кнопками-ссылками на каналы + кнопкой «Я подписался»."""
    rows = []
    for m in missing:
        if m["link"]:
            rows.append([InlineKeyboardButton(
                text=f"📢 {m['title']}", url=m["link"]
            )])
    rows.append([InlineKeyboardButton(
        text="✅ Я подписался — проверить", callback_data="check_sub"
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def require_subscription(message_or_call) -> bool:
    """Проверяет подписку. Если не подписан — отправляет сообщение и возвращает False.
    Если подписан — возвращает True.
    Принимает Message или CallbackQuery."""
    user_id = message_or_call.from_user.id
    missing = await get_unsubscribed_channels(user_id)
    if not missing:
        return True

    text = (
        "📢 <b>Для пользования ботом нужна подписка</b>\n\n"
        "Подпишись на наши каналы и нажми кнопку «✅ Я подписался»:"
    )
    kb = subscription_kb(missing)

    if isinstance(message_or_call, CallbackQuery):
        try:
            await message_or_call.message.answer(text, reply_markup=kb)
        except Exception:
            pass
    else:
        await message_or_call.answer(text, reply_markup=kb)
    return False


# ----------- Middleware: блокировка забаненных -----------
class BanMiddleware(BaseMiddleware):
    """Перед любым действием проверяем: пользователь не забанен?
    Админов не проверяем, они всегда могут.
    """
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        # достаём id пользователя из события
        user = data.get("event_from_user")
        if user and not is_root(user.id):
            ban = await db.get_ban(user.id)
            if ban:
                # Тихо отвечаем — без диалога с забаненным
                if isinstance(event, Message):
                    try:
                        await event.answer(
                            f"⛔ Ты заблокирован в этом боте.\n"
                            f"Причина: {ban['reason'] or 'не указана'}"
                        )
                    except Exception:
                        pass
                elif isinstance(event, CallbackQuery):
                    try:
                        await event.answer("⛔ Ты заблокирован.", show_alert=True)
                    except Exception:
                        pass
                return  # обрываем обработку
        return await handler(event, data)


dp.message.middleware(BanMiddleware())
dp.callback_query.middleware(BanMiddleware())


# ----------- Состояния анкеты (FSM) -----------
class Form(StatesGroup):
    name = State()
    age = State()
    gender = State()
    partner_age_min = State()
    partner_age_max = State()
    city = State()
    denomination = State()           # выбор конфессии кнопкой
    denomination_other = State()     # если "Другое" — ввод текстом
    church = State()
    church_role = State()            # служение в церкви
    job = State()                    # работа/учёба (можно пропустить)
    marital = State()
    children = State()
    hobbies = State()
    photo = State()


class LegacyFillin(StatesGroup):
    """FSM для дозаполнения старых анкет — те, что были зарегистрированы
    ДО введения полей denomination/church_role/job."""
    denomination = State()
    denomination_other = State()
    church_role = State()
    job = State()


class ReportForm(StatesGroup):
    """Отдельный FSM для жалобы — пользователь нажал 🚩 и теперь пишет причину."""
    reason = State()


class BroadcastForm(StatesGroup):
    """FSM рассылки от админа (/rassylka).
      text     — пишет текст рассылки
      menu     — главное меню фильтров (или «Всем»)
      f_age    — ввод диапазона возраста (например, 25-40)
      f_city   — ввод города (точное название)
      confirm  — подтверждение перед отправкой"""
    text = State()
    menu = State()
    f_age = State()
    f_city = State()
    confirm = State()


class AssignRoleForm(StatesGroup):
    """FSM назначения новой роли — админ вводит ID/username/ссылку."""
    waiting_target = State()


class EditProfileForm(StatesGroup):
    """FSM редактирования анкеты — одно поле за раз."""
    # Каждое поле — своё состояние (чтобы после ввода вернуться в меню)
    edit_name = State()
    edit_age = State()
    edit_age_min = State()      # мин возраст партнёра
    edit_age_max = State()      # макс возраст партнёра
    edit_city = State()
    edit_denomination = State()
    edit_denomination_other = State()  # если выбрано «Другое»
    edit_church = State()
    edit_church_role = State()
    edit_job = State()
    edit_marital = State()
    edit_children = State()
    edit_hobbies = State()
    edit_photos = State()        # загрузка фото


# ----------- Список конфессий -----------
DENOMINATIONS = [
    "Баптисты", "Пятидесятники", "АСД",
    "Евангельские Христиане", "Лютеране", "Православные",
    "Католики", "Методисты", "Пресвитериане",
]
# Помечаем последнюю кнопку «Другое» отдельно — она триггерит ввод текстом


# ----------- Клавиатуры -----------
def main_menu_kb() -> ReplyKeyboardMarkup:
    """Обычное меню для всех юзеров."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Смотреть анкеты")],
            [KeyboardButton(text="👤 Моя анкета"), KeyboardButton(text="💌 Мои матчи")],
            [KeyboardButton(text="✏️ Заполнить заново"), KeyboardButton(text="⚙️ Настройки")],
        ],
        resize_keyboard=True,
    )


def main_menu_admin_kb() -> ReplyKeyboardMarkup:
    """Меню для тех, у кого есть админская роль — как обычное + «👑 Админ»."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Смотреть анкеты")],
            [KeyboardButton(text="👤 Моя анкета"), KeyboardButton(text="💌 Мои матчи")],
            [KeyboardButton(text="✏️ Заполнить заново"), KeyboardButton(text="⚙️ Настройки")],
            [KeyboardButton(text="👑 Админ")],
        ],
        resize_keyboard=True,
    )


async def menu_for(user_id: int) -> ReplyKeyboardMarkup:
    """Возвращает подходящее меню — админское или обычное."""
    if await is_moderator(user_id):
        return main_menu_admin_kb()
    return main_menu_kb()


def hidden_menu_kb() -> ReplyKeyboardMarkup:
    """Меню для скрытых юзеров — только одна кнопка «Вернуть анкету»."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🙋 Вернуть анкету")]],
        resize_keyboard=True,
    )


def swipe_kb(photo_idx: int = 0, photos_total: int = 1) -> InlineKeyboardMarkup:
    """Клавиатура под фото анкеты.
    Стрелки листают фото внутри одной анкеты, а на границах — к соседней."""
    counter = f"{photo_idx + 1}/{photos_total}" if photos_total > 1 else "📷"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="◀", callback_data="swipe:prev"),
            InlineKeyboardButton(text=counter, callback_data="swipe:noop"),
            InlineKeyboardButton(text="▶", callback_data="swipe:next"),
        ],
        [
            InlineKeyboardButton(text="❌", callback_data="swipe:dislike"),
            InlineKeyboardButton(text="❤️", callback_data="swipe:like"),
        ],
        [
            InlineKeyboardButton(text="🚩 Жалоба", callback_data="swipe:report"),
            InlineKeyboardButton(text="⏹ Стоп", callback_data="swipe:stop"),
        ],
    ])


def gender_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Мужчина"), KeyboardButton(text="Женщина")]],
        resize_keyboard=True, one_time_keyboard=True,
    )


def marital_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Не женат / не замужем")],
            [KeyboardButton(text="В разводе")],
            [KeyboardButton(text="Вдовец / вдова")],
        ],
        resize_keyboard=True, one_time_keyboard=True,
    )


def children_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Нет детей")],
            [KeyboardButton(text="Есть, живут со мной")],
            [KeyboardButton(text="Есть, живут отдельно")],
        ],
        resize_keyboard=True, one_time_keyboard=True,
    )


def denomination_kb() -> ReplyKeyboardMarkup:
    """Клавиатура с 9 конфессиями + «Другое».
    Раскладка 3x3 + 1 кнопка «Другое» внизу."""
    rows = []
    for i in range(0, len(DENOMINATIONS), 3):
        rows.append([KeyboardButton(text=d) for d in DENOMINATIONS[i:i+3]])
    rows.append([KeyboardButton(text="Другое")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True, one_time_keyboard=True,
    )


def skip_kb() -> ReplyKeyboardMarkup:
    """Клавиатура с одной кнопкой «Пропустить» (используется для job)."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Пропустить")]],
        resize_keyboard=True, one_time_keyboard=True,
    )


# ----------- /start -----------
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    # Сначала — согласие на обработку ПДн (152-ФЗ).
    # Без него нельзя ничего показывать и тем более собирать данные.
    if not await require_consent(message):
        return
    # Затем — обязательная подписка на каналы
    if not await require_subscription(message):
        return
    user = await db.get_user(message.from_user.id)
    if user:
        await message.answer(
            f"С возвращением, {user['name']}! Что будем делать?",
            reply_markup=await menu_for(message.from_user.id),
        )
    else:
        await message.answer(
            "👋 Здравствуй! Это бот для знакомства верующих людей.\n\n"
            "Давай заполним твою анкету.\n\n"
            "<b>Как тебя зовут?</b>",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.set_state(Form.name)


# ----------- Обработчики кнопок согласия -----------
@router.callback_query(F.data == "consent:accept")
async def on_consent_accept(call: CallbackQuery, state: FSMContext):
    await db.grant_consent(call.from_user.id, CONSENT_VERSION)
    await call.answer("✅ Согласие принято", show_alert=False)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    # Дальше как в /start — проверяем подписку, потом ведём в регистрацию
    if not await require_subscription(call):
        return

    user = await db.get_user(call.from_user.id)
    if user:
        await bot.send_message(
            call.from_user.id,
            f"С возвращением, {user['name']}! Что будем делать?",
            reply_markup=await menu_for(call.from_user.id),
        )
    else:
        await bot.send_message(
            call.from_user.id,
            "Спасибо! Теперь давай заполним анкету.\n\n"
            "<b>Как тебя зовут?</b>",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.set_state(Form.name)


@router.callback_query(F.data == "consent:decline")
async def on_consent_decline(call: CallbackQuery):
    await call.answer()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await bot.send_message(
        call.from_user.id,
        "Без согласия на обработку персональных данных пользоваться "
        "ботом нельзя. Если передумаешь — напиши /start ещё раз."
    )


# ----------- Показ документов прямо в чате -----------
@router.callback_query(F.data == "consent:show_privacy")
async def on_show_privacy(call: CallbackQuery):
    await call.answer()
    await send_long_document(call.from_user.id, PRIVACY_POLICY_TEXT)
    # Дублируем окно с кнопками — чтобы можно было принять/отказаться
    # после прочтения
    await bot.send_message(
        call.from_user.id,
        "👆 Это была Политика обработки ПДн.\nТеперь выбери:",
        reply_markup=consent_kb(),
    )


@router.callback_query(F.data == "consent:show_agreement")
async def on_show_agreement(call: CallbackQuery):
    await call.answer()
    await send_long_document(call.from_user.id, USER_AGREEMENT_TEXT)
    await bot.send_message(
        call.from_user.id,
        "👆 Это было Пользовательское соглашение.\nТеперь выбери:",
        reply_markup=consent_kb(),
    )


# Команды доступны всегда — можно прочитать документы и просто так
@router.message(Command("privacy"))
async def cmd_privacy(message: Message):
    await send_long_document(message.from_user.id, PRIVACY_POLICY_TEXT)


@router.message(Command("agreement"))
async def cmd_agreement(message: Message):
    await send_long_document(message.from_user.id, USER_AGREEMENT_TEXT)


# Кнопка «Я подписался — проверить»
@router.callback_query(F.data == "check_sub")
async def on_check_sub(call: CallbackQuery, state: FSMContext):
    missing = await get_unsubscribed_channels(call.from_user.id)
    if not missing:
        await call.answer("✅ Отлично! Подписка подтверждена.", show_alert=True)
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        # Запускаем регистрацию или показываем главное меню
        user = await db.get_user(call.from_user.id)
        if user:
            await bot.send_message(
                call.from_user.id,
                f"С возвращением, {user['name']}! Что будем делать?",
                reply_markup=await menu_for(call.from_user.id),
            )
        else:
            await bot.send_message(
                call.from_user.id,
                "Спасибо! Теперь давай заполним твою анкету.\n\n"
                "<b>Как тебя зовут?</b>",
                reply_markup=ReplyKeyboardRemove(),
            )
            await state.set_state(Form.name)
    else:
        # Всё ещё не подписан — обновляем сообщение
        await call.answer(
            f"⚠️ Не подписан на: {', '.join(m['title'] for m in missing)}",
            show_alert=True,
        )


# ----------- Заполнение анкеты -----------
# ============= НАСТРОЙКИ И «СКРЫТЬ АНКЕТУ» =============

def _settings_kb() -> InlineKeyboardMarkup:
    """Меню настроек: скрыть анкету + возможно другие в будущем."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👁 Скрыть мою анкету",
                              callback_data="settings:hide")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="settings:back")],
    ])


def _hide_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, скрыть",
                              callback_data="settings:hide_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="settings:back")],
    ])


def _return_prompt_kb() -> InlineKeyboardMarkup:
    """Клавиатура для сообщения через 30 дней: вернуть/продлить/удалить."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🙋 Да, вернуть", callback_data="return:yes")],
        [InlineKeyboardButton(text="🕊 Оставить скрытой ещё на 30 дней",
                              callback_data="return:extend")],
        [InlineKeyboardButton(text="🗑 Удалить анкету навсегда",
                              callback_data="return:delete")],
    ])


@router.message(F.text == "⚙️ Настройки")
async def settings_menu(message: Message):
    u = await db.get_user(message.from_user.id)
    if not u:
        await message.answer("Сначала заполни анкету: /start")
        return
    # Проверка что не скрыт
    if u.get("is_hidden"):
        await message.answer(
            "Твоя анкета скрыта. Нажми «🙋 Вернуть анкету», чтобы снова "
            "листать ленту.",
            reply_markup=hidden_menu_kb(),
        )
        return
    await message.answer(
        "⚙️ <b>Настройки</b>\n\n"
        "Здесь можно временно скрыть свою анкету — она перестанет "
        "показываться другим, но все свайпы, лайки и матчи сохранятся.",
        reply_markup=_settings_kb(),
    )


@router.callback_query(F.data.startswith("settings:"))
async def settings_callback(call: CallbackQuery):
    action = call.data.split(":", 1)[1]
    user_id = call.from_user.id

    if action == "back":
        await call.answer()
        try:
            await call.message.delete()
        except Exception:
            pass
        return

    if action == "hide":
        await call.answer()
        await call.message.edit_text(
            "👁 <b>Скрыть анкету?</b>\n\n"
            "• Твою анкету перестанут показывать другим\n"
            "• Ты не сможешь листать анкеты, пока не вернёшь свою\n"
            "• Все свайпы, лайки и матчи сохранятся\n"
            "• Через 30 дней бот сам напомнит: вернуть или удалить\n"
            "• Ты можешь вернуть анкету в любой момент кнопкой в меню",
            reply_markup=_hide_confirm_kb(),
        )
        return

    if action == "hide_confirm":
        await call.answer("Анкета скрыта")
        await db.hide_user(user_id)
        try:
            await call.message.delete()
        except Exception:
            pass
        await bot.send_message(
            user_id,
            "✅ Твоя анкета скрыта. Через 30 дней я напомню — "
            "вернуть или окончательно удалить.\n\n"
            "Если передумаешь раньше — жми «🙋 Вернуть анкету».",
            reply_markup=hidden_menu_kb(),
        )
        return


@router.message(F.text == "🙋 Вернуть анкету")
async def return_from_hidden(message: Message):
    u = await db.get_user(message.from_user.id)
    if not u or not u.get("is_hidden"):
        await message.answer("Твоя анкета уже активна.",
                             reply_markup=main_menu_kb())
        return
    await db.unhide_user(message.from_user.id)
    await message.answer(
        "🙋 Анкета снова активна! Люди видят её, ты можешь листать ленту.",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data.startswith("return:"))
async def return_prompt_callback(call: CallbackQuery):
    """Обработка выбора в напоминании через 30 дней."""
    action = call.data.split(":", 1)[1]
    user_id = call.from_user.id

    if action == "yes":
        await call.answer("Анкета возвращена")
        await db.unhide_user(user_id)
        try:
            await call.message.edit_text(
                "🙋 Отлично! Твоя анкета снова в ленте."
            )
        except Exception:
            pass
        await bot.send_message(user_id, "С возвращением!",
                                reply_markup=main_menu_kb())
        return

    if action == "extend":
        await call.answer("Продлил ещё на 30 дней")
        await db.extend_hide(user_id)
        try:
            await call.message.edit_text(
                "🕊 Хорошо, продлил скрытие ещё на 30 дней. "
                "Через месяц снова напомню."
            )
        except Exception:
            pass
        return

    if action == "delete":
        await call.answer()
        # Подтверждение
        confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, удалить навсегда",
                                  callback_data="return:delete_confirm")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="return:cancel_delete")],
        ])
        try:
            await call.message.edit_text(
                "🗑 <b>Удалить анкету навсегда?</b>\n\n"
                "Будут удалены: анкета, все фото, свайпы, лайки, матчи. "
                "Это действие необратимо.",
                reply_markup=confirm_kb,
            )
        except Exception:
            pass
        return

    if action == "delete_confirm":
        await call.answer("Удаляю…")
        await db.delete_user_completely(user_id)
        try:
            await call.message.edit_text(
                "🗑 Анкета удалена. Если захочешь вернуться — /start."
            )
        except Exception:
            pass
        return

    if action == "cancel_delete":
        await call.answer("Отменено")
        try:
            await call.message.edit_text(
                "Отмена. Анкета осталась скрытой. Через 30 дней снова напомню."
            )
        except Exception:
            pass
        return


@router.message(F.text == "✏️ Заполнить заново")
async def restart_form(message: Message, state: FSMContext):
    await state.clear()
    # Помечаем что это перезаполнение — после успешного сохранения
    # обнулим свайпы, чтобы ленту можно было листать заново.
    # Если пользователь бросит регистрацию на полпути — флаг исчезнет
    # вместе со state, и старая анкета продолжит жить как ни в чём не бывало.
    await state.update_data(is_restart=True)
    await message.answer("Хорошо, начнём заново.\n\n<b>Как тебя зовут?</b>",
                         reply_markup=ReplyKeyboardRemove())
    await state.set_state(Form.name)


@router.message(Form.name)
async def form_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not (1 <= len(name) <= 30):
        await message.answer("Имя должно быть от 1 до 30 символов. Попробуй ещё раз.")
        return
    await state.update_data(name=name)
    await message.answer("<b>Сколько тебе лет?</b> (числом)")
    await state.set_state(Form.age)


@router.message(Form.age)
async def form_age(message: Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        await message.answer("Напиши возраст числом, например: 28")
        return
    age = int(message.text.strip())
    if not (18 <= age <= 99):
        await message.answer("Возраст должен быть от 18 до 99.")
        return
    await state.update_data(age=age)
    await message.answer("<b>Твой пол?</b>", reply_markup=gender_kb())
    await state.set_state(Form.gender)


@router.message(Form.gender)
async def form_gender(message: Message, state: FSMContext):
    if message.text not in ("Мужчина", "Женщина"):
        await message.answer("Выбери из кнопок.", reply_markup=gender_kb())
        return
    gender = "M" if message.text == "Мужчина" else "F"
    # В христианском боте: мужчина ищет женщину, женщина — мужчину.
    # Это устанавливается автоматически без отдельного вопроса.
    opposite = "F" if gender == "M" else "M"
    await state.update_data(gender=gender, looking_for=opposite)

    partner = "девушки" if gender == "M" else "молодого человека"
    await message.answer(
        f"<b>Минимальный возраст {partner}?</b>\n"
        f"Например: 22",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(Form.partner_age_min)


@router.message(Form.partner_age_min)
async def form_partner_age_min(message: Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        await message.answer("Напиши возраст числом, например: 22")
        return
    age_min = int(message.text.strip())
    if not (18 <= age_min <= 99):
        await message.answer("Возраст должен быть от 18 до 99.")
        return
    await state.update_data(partner_age_min=age_min)

    data = await state.get_data()
    gender = data.get("gender")
    partner = "девушки" if gender == "M" else "молодого человека"
    await message.answer(
        f"<b>Максимальный возраст {partner}?</b>\n"
        f"Например: 40"
    )
    await state.set_state(Form.partner_age_max)


@router.message(Form.partner_age_max)
async def form_partner_age_max(message: Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        await message.answer("Напиши возраст числом, например: 40")
        return
    age_max = int(message.text.strip())
    if not (18 <= age_max <= 99):
        await message.answer("Возраст должен быть от 18 до 99.")
        return
    data = await state.get_data()
    age_min = data.get("partner_age_min", 18)
    if age_max < age_min:
        await message.answer(
            f"Максимальный возраст не может быть меньше минимального ({age_min}). "
            f"Введи число от {age_min}."
        )
        return
    await state.update_data(partner_age_max=age_max)
    await message.answer("<b>Из какого ты города?</b>")
    await state.set_state(Form.city)


@router.message(Form.city)
async def form_city(message: Message, state: FSMContext):
    city = (message.text or "").strip()
    if not (2 <= len(city) <= 50):
        await message.answer("Название города от 2 до 50 символов.")
        return
    await state.update_data(city=city)
    await message.answer(
        "<b>Какой конфессии ты принадлежишь?</b>\nВыбери из кнопок.",
        reply_markup=denomination_kb(),
    )
    await state.set_state(Form.denomination)


@router.message(Form.denomination)
async def form_denomination(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == "Другое":
        await message.answer(
            "Напиши свою конфессию (2–50 символов).",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.set_state(Form.denomination_other)
        return
    if text not in DENOMINATIONS:
        await message.answer("Выбери из кнопок.", reply_markup=denomination_kb())
        return
    await state.update_data(denomination=text)
    await message.answer(
        "<b>Как называется твоя церковь?</b>\n"
        "Например: «Вифания», «Дом благодати», «Свет Спасения».",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(Form.church)


@router.message(Form.denomination_other)
async def form_denomination_other(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not (2 <= len(text) <= 50):
        await message.answer("Название конфессии от 2 до 50 символов.")
        return
    await state.update_data(denomination=text)
    await message.answer(
        "<b>Как называется твоя церковь?</b>\n"
        "Например: «Вифания», «Дом благодати», «Свет Спасения».",
    )
    await state.set_state(Form.church)


@router.message(Form.church)
async def form_church(message: Message, state: FSMContext):
    church = (message.text or "").strip()
    if not (2 <= len(church) <= 100):
        await message.answer("Название церкви от 2 до 100 символов.")
        return
    await state.update_data(church=church)
    await message.answer(
        "<b>Какое у тебя служение в церкви?</b>\n"
        "Например: «прихожанин», «диакон», «лидер молодёжи», «руководитель прославления».",
    )
    await state.set_state(Form.church_role)


@router.message(Form.church_role)
async def form_church_role(message: Message, state: FSMContext):
    role = (message.text or "").strip()
    if not (2 <= len(role) <= 100):
        await message.answer("Опиши служение в 2–100 символов.")
        return
    await state.update_data(church_role=role)
    await message.answer(
        "<b>Кем работаешь или на кого учишься?</b>\n"
        "Одной строкой. Можно пропустить.",
        reply_markup=skip_kb(),
    )
    await state.set_state(Form.job)


@router.message(Form.job)
async def form_job(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == "Пропустить":
        job = None
    else:
        if not (2 <= len(text) <= 100):
            await message.answer(
                "От 2 до 100 символов, либо «Пропустить».",
                reply_markup=skip_kb(),
            )
            return
        job = text
    await state.update_data(job=job)
    await message.answer("<b>Семейное положение?</b>", reply_markup=marital_kb())
    await state.set_state(Form.marital)


@router.message(Form.marital)
async def form_marital(message: Message, state: FSMContext):
    valid = {"Не женат / не замужем", "В разводе", "Вдовец / вдова"}
    if message.text not in valid:
        await message.answer("Выбери из кнопок.", reply_markup=marital_kb())
        return
    await state.update_data(marital=message.text)
    await message.answer("<b>Есть ли дети?</b>", reply_markup=children_kb())
    await state.set_state(Form.children)


@router.message(Form.children)
async def form_children(message: Message, state: FSMContext):
    valid = {"Нет детей", "Есть, живут со мной", "Есть, живут отдельно"}
    if message.text not in valid:
        await message.answer("Выбери из кнопок.", reply_markup=children_kb())
        return
    await state.update_data(children=message.text)
    await message.answer(
        "<b>Расскажи о себе</b> — минимум <b>10 слов</b>.\n\n"
        "Опиши работу, увлечения, что важно в отношениях, что ищешь. "
        "Чем подробнее — тем интереснее твоя анкета.\n\n"
        "Пример: «Люблю читать классику, играю на гитаре в служении молитвы, "
        "увлекаюсь походами в горы. Ищу верующую жену, готовую строить "
        "христианскую семью.»",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(Form.hobbies)


@router.message(Form.hobbies)
async def form_hobbies(message: Message, state: FSMContext):
    hobbies = (message.text or "").strip()
    if not (1 <= len(hobbies) <= 500):
        await message.answer("От 1 до 500 символов. Попробуй ещё раз.")
        return
    # Проверка минимального числа слов
    word_count = db.count_meaningful_words(hobbies, db.HOBBIES_MIN_WORD_LEN)
    if word_count < db.HOBBIES_MIN_WORDS:
        await message.answer(
            f"Слишком коротко — минимум <b>{db.HOBBIES_MIN_WORDS} слов</b> "
            f"(у тебя {word_count}).\n\n"
            f"Расскажи подробнее о себе, работе, увлечениях, что важно "
            f"в отношениях."
        )
        return
    await state.update_data(hobbies=hobbies)
    await message.answer(
        "<b>Пришли своё фото</b> 📸\n\n"
        "Можно добавить от 2 до 5 фотографий — присылай по одной.\n"
        "После каждой бот предложит добавить ещё или завершить."
    )
    await state.update_data(photos=[])
    await state.set_state(Form.photo)


def photo_done_kb(count: int) -> InlineKeyboardMarkup:
    """Кнопка «Готово» появляется когда фото уже минимум 2."""
    buttons = []
    if count >= 2:
        buttons.append([InlineKeyboardButton(
            text=f"✅ Готово ({count}/5)", callback_data="photos:done"
        )])
    if count > 0:
        buttons.append([InlineKeyboardButton(
            text="🗑 Удалить последнее", callback_data="photos:undo"
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None


@router.message(Form.photo, F.photo)
async def form_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    if len(photos) >= 5:
        await message.answer("Максимум 5 фото. Жми ✅ Готово.",
                             reply_markup=photo_done_kb(len(photos)))
        return

    file_id = message.photo[-1].file_id
    user_id = message.from_user.id

    # Сразу скачиваем фото на наш сервер.
    # Это нужно для общего хранилища: VK-боту понадобится файл,
    # чтобы показать TG-анкету.
    folder = db.user_photos_dir(user_id)
    pos = len(photos)
    target_path = _os.path.join(folder, f"{pos}.jpg")
    ok = await photo_utils.download_tg_photo(bot, file_id, target_path)
    if not ok:
        # Скачивание не удалось — не сломаем регистрацию, продолжим через photo_id.
        # На показ это не повлияет (есть fallback в photo_source).
        logging.warning(f"Не смог скачать фото {file_id} для user {user_id}")
        target_path = None

    photos.append({"photo_id": file_id, "file_path": target_path})
    await state.update_data(photos=photos)

    if len(photos) < 2:
        text = (f"📷 Принято фото {len(photos)}/5.\n\n"
                f"Нужно ещё минимум {2 - len(photos)}. Пришли следующее.")
    elif len(photos) < 5:
        text = (f"📷 Принято фото {len(photos)}/5.\n\n"
                f"Можно добавить ещё или нажать ✅ Готово.")
    else:
        text = f"📷 Принято фото {len(photos)}/5. Это максимум — жми ✅ Готово."

    await message.answer(text, reply_markup=photo_done_kb(len(photos)))


@router.callback_query(Form.photo, F.data == "photos:undo")
async def form_photo_undo(call: CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    photos = data.get("photos", [])
    if photos:
        removed = photos.pop()
        # Удаляем файл с диска, если был скачан
        if isinstance(removed, dict) and removed.get("file_path"):
            try:
                if _os.path.exists(removed["file_path"]):
                    _os.remove(removed["file_path"])
            except Exception as e:
                logging.warning(f"Не смог удалить {removed['file_path']}: {e}")
        await state.update_data(photos=photos)
    if not photos:
        await call.message.edit_text("Удалил все фото. Пришли первое фото заново.")
        return
    await call.message.edit_text(
        f"Удалил последнее. Сейчас фото: {len(photos)}/5.\n"
        f"Пришли ещё или жми ✅ Готово." if len(photos) >= 2
        else f"Удалил последнее. Сейчас фото: {len(photos)}/5.\n"
             f"Пришли ещё минимум одно.",
        reply_markup=photo_done_kb(len(photos)),
    )


@router.callback_query(Form.photo, F.data == "photos:done")
async def form_photo_done(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    if len(photos) < 2:
        await call.answer("Нужно минимум 2 фото!", show_alert=True)
        return
    await call.answer()
    # Запомним, было ли это перезаполнение — нужно ПЕРЕД state.clear()
    is_restart = data.get("is_restart", False)
    await db.save_user(
        user_id=call.from_user.id,
        username=call.from_user.username,
        name=data["name"],
        age=data["age"],
        gender=data["gender"],
        looking_for=data["looking_for"],
        partner_age_min=data["partner_age_min"],
        partner_age_max=data["partner_age_max"],
        city=data["city"],
        church=data["church"],
        denomination=data.get("denomination"),
        church_role=data.get("church_role"),
        job=data.get("job"),
        marital=data["marital"],
        children=data["children"],
        hobbies=data["hobbies"],
        photos=photos,
        platform="tg",
    )

    # Если это было перезаполнение — сбрасываем личную историю,
    # чтобы пользователь снова видел все анкеты с начала.
    # ВАЖНО: чужие лайки в его адрес НЕ трогаем (это чужие данные).
    # Также не трогаем баны и жалобы — иначе можно было бы уходить от модерации.
    extra_text = ""
    if is_restart:
        await db.reset_user_swipes(call.from_user.id)
        viewer_state.pop(call.from_user.id, None)  # чистим в памяти тоже
        extra_text = "\n\nИстория свайпов сброшена — будешь видеть всех заново."

    await state.clear()
    await call.message.edit_reply_markup(reply_markup=None)
    await bot.send_message(
        call.from_user.id,
        f"✅ Анкета сохранена! Загружено фото: {len(photos)}." + extra_text + "\n\n"
        "Жми «🔍 Смотреть анкеты», чтобы начать знакомиться. "
        "Если кто-то ответит взаимностью — я пришлю тебе его контакт.",
        reply_markup=await menu_for(call.from_user.id),
    )


@router.message(Form.photo)
async def form_photo_invalid(message: Message):
    await message.answer("Это не фото. Пришли изображением, не файлом.")


# ----------- Формат анкеты для показа -----------
def photo_source(file_path: str | None, photo_id: str):
    """Возвращает то, что можно отправить в send_photo / InputMediaPhoto.

    Приоритет: если есть локальный файл — отдаём его через FSInputFile.
    Если файла нет или не существует — fallback на старый photo_id Telegram.

    Это даёт устойчивость: даже если миграция была неполной или файл
    случайно пропал, бот всё равно покажет фото через Telegram-кеш."""
    if file_path and _os.path.exists(file_path) and _os.path.getsize(file_path) > 0:
        return FSInputFile(file_path)
    return photo_id


def photo_source_from_dict(p: dict):
    """Хелпер для словарей вида {photo_id, file_path}."""
    return photo_source(p.get("file_path"), p["photo_id"])


def format_profile(u: dict) -> str:
    """Форматирует анкету. Новые поля (denomination, church_role, job)
    показываются ТОЛЬКО если заполнены — у старых анкет их может не быть."""
    lines = [f"<b>{u['name']}, {u['age']}</b>",
             f"📍 {u['city']}"]
    if u.get("denomination"):
        lines.append(f"✝️ {u['denomination']}")
    lines.append(f"⛪ {u['church']}")
    if u.get("church_role"):
        lines.append(f"🙏 {u['church_role']}")
    if u.get("job"):
        lines.append(f"💼 {u['job']}")
    lines.append(f"💍 {u['marital']}")
    lines.append(f"👶 {u['children']}")
    return "\n".join(lines) + f"\n\n<b>О себе:</b>\n{u['hobbies']}"


# ----------- Состояние просмотра в памяти -----------
# Для каждого пользователя помним, какую анкету и какое фото он сейчас видит.
# Это нужно, чтобы стрелки правильно листали фото внутри анкеты,
# а на границах переходили к соседней анкете.
viewer_state: dict[int, dict] = {}  # user_id -> {target_id, photo_idx, photos}


async def show_profile_to(user_id: int, chat_id: int,
                          target_user_id: int, photo_idx: int = 0,
                          edit_message: Message | None = None):
    """Показать конкретную анкету. Если edit_message — редактируем существующее
    сообщение (быстрая смена фото). Иначе шлём новое."""
    profile = await db.get_user(target_user_id)
    if not profile:
        return False
    # photos — список словарей {photo_id, file_path}
    photos = await db.get_user_photos_with_paths(target_user_id)
    if not photos:
        photos = [{"photo_id": profile["photo_id"],
                   "file_path": profile.get("photo_path")}]
    photo_idx = max(0, min(photo_idx, len(photos) - 1))

    viewer_state[user_id] = {
        "target_id": target_user_id,
        "photo_idx": photo_idx,
        "photos": photos,
    }
    # Дублируем в БД — чтобы лайк/дизлайк знал, на кого
    await db.set_last_shown(user_id, target_user_id)

    caption = format_profile(profile)
    kb = swipe_kb(photo_idx, len(photos))
    src = photo_source_from_dict(photos[photo_idx])

    if edit_message is not None:
        try:
            # При edit_media если src — FSInputFile, Telegram перезагружает файл.
            # Если photo_id (строка) — использует существующий кеш Telegram.
            await edit_message.edit_media(
                media=InputMediaPhoto(media=src, caption=caption,
                                      parse_mode="HTML"),
                reply_markup=kb,
            )
            return True
        except Exception as e:
            # если не получилось отредактировать (старое сообщение и т.п.) — шлём новое
            logging.warning(f"edit_media failed: {e}")

    await bot.send_photo(chat_id, photo=src,
                         caption=caption, reply_markup=kb)
    return True


async def show_next_profile(user_id: int, chat_id: int,
                            edit_message: Message | None = None):
    profile = await db.get_next_profile(user_id)
    if not profile:
        viewer_state.pop(user_id, None)
        if edit_message:
            try:
                await edit_message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        await bot.send_message(
            chat_id,
            "🤷 Анкеты закончились. Загляни позже — появятся новые!",
            reply_markup=main_menu_kb(),
        )
        return
    await show_profile_to(user_id, chat_id, profile["user_id"], 0, edit_message)


# ----------- Жалоба: получение причины и отмена -----------
@router.message(ReportForm.reason, F.text)
async def report_form_reason(message: Message, state: FSMContext):
    reason = (message.text or "").strip()
    if not (30 <= len(reason) <= 200):
        await message.answer(
            f"Причина должна быть от 30 до 200 символов "
            f"(сейчас {len(reason)}). Попробуй ещё раз или нажми «Отмена».",
        )
        return

    data = await state.get_data()
    target_id = data.get("report_target_id")
    user_id = message.from_user.id

    if not target_id:
        # На всякий случай — если потеряли target
        await state.clear()
        await message.answer("Что-то пошло не так. Попробуй ещё раз.")
        return

    # Проверяем повторную жалобу прямо здесь — на случай если пользователь
    # параллельно успел отправить жалобу с другого устройства
    already = await db.has_reported(user_id, target_id)
    if already:
        await state.clear()
        await message.answer("Ты уже жаловался на эту анкету.")
        return

    await db.add_report(user_id, target_id, reason=reason)
    total = await db.count_reports(target_id)
    await state.clear()
    await message.answer(
        "✅ Жалоба отправлена администрации. Спасибо!",
        reply_markup=main_menu_kb(),
    )
    await notify_admins_about_report(user_id, target_id, total, reason=reason)


@router.message(ReportForm.reason)
async def report_form_reason_invalid(message: Message):
    """На случай не-текстовых сообщений в этом состоянии."""
    await message.answer("Напиши причину текстом (30–200 символов) или нажми «Отмена».")


@router.callback_query(F.data == "report_cancel")
async def report_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer("Отменено")
    try:
        await call.message.edit_text("❌ Жалоба отменена.")
    except Exception:
        pass


# ----------- Дозаполнение старых анкет (legacy fill-in) -----------
async def require_fillin(message: Message, state: FSMContext) -> bool:
    """Проверяет, нужно ли пользователю дозаполнить новые поля анкеты
    (denomination, church_role). Если да — запускает FSM LegacyFillin
    и возвращает False (обработчик должен остановить выполнение).
    Если нет — возвращает True (продолжаем как обычно).

    job не требуем — он опциональный."""
    user_id = message.from_user.id
    if not await db.needs_legacy_fillin(user_id):
        return True

    await state.clear()
    await state.set_state(LegacyFillin.denomination)
    await message.answer(
        "👋 <b>У нас обновление!</b>\n\n"
        "Ответь на пару вопросов — это поможет другим узнать о тебе больше, "
        "и сможешь снова листать анкеты.\n\n"
        "<b>Какой конфессии ты принадлежишь?</b>",
        reply_markup=denomination_kb(),
    )
    return False


@router.message(LegacyFillin.denomination)
async def legacy_form_denomination(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == "Другое":
        await message.answer(
            "Напиши свою конфессию (2–50 символов).",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.set_state(LegacyFillin.denomination_other)
        return
    if text not in DENOMINATIONS:
        await message.answer("Выбери из кнопок.", reply_markup=denomination_kb())
        return
    await state.update_data(denomination=text)
    await message.answer(
        "<b>Какое у тебя служение в церкви?</b>\n"
        "Например: «прихожанин», «диакон», «лидер молодёжи».",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(LegacyFillin.church_role)


@router.message(LegacyFillin.denomination_other)
async def legacy_form_denomination_other(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not (2 <= len(text) <= 50):
        await message.answer("Название конфессии от 2 до 50 символов.")
        return
    await state.update_data(denomination=text)
    await message.answer(
        "<b>Какое у тебя служение в церкви?</b>\n"
        "Например: «прихожанин», «диакон», «лидер молодёжи».",
    )
    await state.set_state(LegacyFillin.church_role)


@router.message(LegacyFillin.church_role)
async def legacy_form_church_role(message: Message, state: FSMContext):
    role = (message.text or "").strip()
    if not (2 <= len(role) <= 100):
        await message.answer("Опиши служение в 2–100 символов.")
        return
    await state.update_data(church_role=role)
    await message.answer(
        "<b>Кем работаешь или на кого учишься?</b>\n"
        "Одной строкой. Можно пропустить.",
        reply_markup=skip_kb(),
    )
    await state.set_state(LegacyFillin.job)


@router.message(LegacyFillin.job)
async def legacy_form_job(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text == "Пропустить":
        job = None
    else:
        if not (2 <= len(text) <= 100):
            await message.answer(
                "От 2 до 100 символов, либо «Пропустить».",
                reply_markup=skip_kb(),
            )
            return
        job = text

    data = await state.get_data()
    await db.update_profile_fields(
        message.from_user.id,
        denomination=data["denomination"],
        church_role=data["church_role"],
        job=job,
    )
    await state.clear()
    await message.answer(
        "✅ Спасибо! Анкета обновлена. Можешь дальше пользоваться ботом.",
        reply_markup=main_menu_kb(),
    )


@router.message(F.text == "🔍 Смотреть анкеты")
async def browse_profiles(message: Message, state: FSMContext):
    if not await require_subscription(message):
        return
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала заполни анкету: /start")
        return
    if user.get("is_hidden"):
        await message.answer(
            "Твоя анкета скрыта — сначала верни её, чтобы листать других.",
            reply_markup=hidden_menu_kb(),
        )
        return
    if not await require_fillin(message, state):
        return
    await show_next_profile(message.from_user.id, message.chat.id)


# ----------- Свайпы и листание -----------
@router.callback_query(F.data.startswith("swipe:"))
async def on_swipe(call: CallbackQuery, state: FSMContext):
    action = call.data.split(":")[1]
    user_id = call.from_user.id

    if action == "noop":
        await call.answer()
        return

    if action == "stop":
        await call.answer()
        viewer_state.pop(user_id, None)
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await bot.send_message(user_id, "Окей, остановились.",
                               reply_markup=main_menu_kb())
        return

    st = viewer_state.get(user_id)
    target_id = st["target_id"] if st else await db.get_last_shown(user_id)

    # Жалоба — запрашиваем причину, потом записываем и уведомляем админов
    if action == "report":
        if not target_id:
            await call.answer("Сначала открой анкету.", show_alert=True)
            return
        already = await db.has_reported(user_id, target_id)
        if already:
            await call.answer("Ты уже жаловался на эту анкету.", show_alert=True)
            return

        await call.answer()
        # Кнопка отмены — на случай если человек случайно нажал 🚩
        cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена",
                                 callback_data="report_cancel"),
        ]])
        # Сохраняем target_id в FSM, переводим в ожидание причины
        await state.set_state(ReportForm.reason)
        await state.update_data(report_target_id=target_id)
        await bot.send_message(
            user_id,
            "🚩 <b>Жалоба на эту анкету</b>\n\n"
            "Напиши причину жалобы (30–200 символов). "
            "Это поможет администраторам разобраться.\n\n"
            "Если нажал случайно — нажми <b>«❌ Отмена»</b>.",
            reply_markup=cancel_kb,
        )
        return

    # СТРЕЛКА ВПРАВО ▶ — листаем фото внутри анкеты, на последнем → следующая анкета
    if action == "next":
        await call.answer()
        if st and st["photo_idx"] + 1 < len(st["photos"]):
            # листаем фото внутри анкеты
            await show_profile_to(user_id, call.message.chat.id,
                                  st["target_id"], st["photo_idx"] + 1,
                                  edit_message=call.message)
        else:
            # это последнее фото — идём к следующей анкете
            await show_next_profile(user_id, call.message.chat.id,
                                    edit_message=call.message)
        return

    # СТРЕЛКА ВЛЕВО ◀ — листаем назад, на первом → предыдущая анкета
    if action == "prev":
        await call.answer()
        if st and st["photo_idx"] > 0:
            await show_profile_to(user_id, call.message.chat.id,
                                  st["target_id"], st["photo_idx"] - 1,
                                  edit_message=call.message)
        else:
            # на первом фото — пробуем уйти к предыдущей анкете
            prev = await db.get_prev_profile(user_id)
            if not prev:
                await call.answer("Это первая анкета — назад уже некуда.",
                                  show_alert=True)
                return
            await show_profile_to(user_id, call.message.chat.id,
                                  prev["user_id"], 0,
                                  edit_message=call.message)
        return

    # ❤️ / ❌ — отмечаем выбор и идём дальше
    if not target_id:
        await call.answer()
        await show_next_profile(user_id, call.message.chat.id,
                                edit_message=call.message)
        return

    if action == "like":
        is_match = await db.add_like(user_id, target_id)
        await call.answer("❤️ Лайк отправлен" if not is_match else "🎉 Матч!")
        if is_match:
            await notify_match(user_id, target_id)
    elif action == "dislike":
        await call.answer("❌ Пропущено")
        await db.add_dislike(user_id, target_id)

    await show_next_profile(user_id, call.message.chat.id,
                            edit_message=call.message)


async def notify_match(user_a_id: int, user_b_id: int):
    """Уведомляем обоих о взаимном лайке.
    Каждый получает уведомление через ту платформу, на которой он зарегистрирован.
    A — тот, кто только что лайкнул (источник вызова).
    B — кого лайкнул.
    Эта функция Telegram-бота уведомляет ТОЛЬКО Telegram-пользователей.
    Если в матче участвует VK-пользователь, его уведомит VK-бот через общий
    механизм (см. notify_match_for_user в database.py).
    """
    a = await db.get_user(user_a_id)
    b = await db.get_user(user_b_id)
    if not a or not b:
        return

    # Тексты с использованием универсальной функции построения ссылки
    text_for_a = (
        f"🎉 <b>Вы понравились друг другу!</b>\n\n"
        f"<b>{b['name']}, {b['age']}</b>\n"
        f'Контакт: <a href="{db.contact_link(b)}">'
        f'{"@" + b["username"] if b["username"] else "написать"}</a>'
    )
    text_for_b = (
        f"🎉 <b>Вы понравились друг другу!</b>\n\n"
        f"<b>{a['name']}, {a['age']}</b>\n"
        f'Контакт: <a href="{db.contact_link(a)}">'
        f'{"@" + a["username"] if a["username"] else "написать"}</a>'
    )

    # Telegram-бот: VK-пользователей не можем напрямую уведомить через VK API,
    # поэтому кладём в очередь — VK-бот сам разошлёт.
    a_photo = photo_source(a.get("photo_path"), a["photo_id"])
    b_photo = photo_source(b.get("photo_path"), b["photo_id"])

    if db.is_tg_user(user_a_id):
        try:
            await bot.send_photo(user_a_id, b_photo, caption=text_for_a)
        except Exception as e:
            logging.warning(f"Не смог уведомить TG-юзера {user_a_id}: {e}")
    else:
        # VK-юзер — в очередь, чтобы VK-бот доставил
        await db.queue_match_notification(user_a_id, user_b_id)

    if db.is_tg_user(user_b_id):
        try:
            await bot.send_photo(user_b_id, a_photo, caption=text_for_b)
        except Exception as e:
            logging.warning(f"Не смог уведомить TG-юзера {user_b_id}: {e}")
    else:
        await db.queue_match_notification(user_b_id, user_a_id)


# ----------- Моя анкета -----------
@router.message(F.text == "👤 Моя анкета")
async def my_profile(message: Message, state: FSMContext):
    u = await db.get_user(message.from_user.id)
    if not u:
        await message.answer("Анкеты ещё нет. /start чтобы создать.")
        return
    if not await require_fillin(message, state):
        return
    photos = await db.get_user_photos_with_paths(message.from_user.id)
    if not photos:
        photos = [{"photo_id": u["photo_id"], "file_path": u.get("photo_path")}]

    # Фильтруем только валидные фото:
    # - файл существует на диске
    # - файл не пустой (>0 байт)
    # Если файла нет/битый — оставляем photo_id (Telegram-кеш) как fallback
    valid_photos = []
    for p in photos:
        fp = p.get("file_path")
        if fp and _os.path.exists(fp) and _os.path.getsize(fp) > 0:
            valid_photos.append(p)
        elif p.get("photo_id"):
            # Оставляем словарь с file_path=None — photo_source возьмёт photo_id
            valid_photos.append({"photo_id": p["photo_id"], "file_path": None})
    if not valid_photos:
        # Совсем никаких фото не осталось (странно, но такое бывает)
        await message.answer(
            "⚠️ Не могу показать твои фото — файлы утеряны.\n"
            "Нажми «✏️ Редактировать» → «Фото» и загрузи заново.",
            reply_markup=await menu_for(message.from_user.id),
        )
        return

    # К стандартной подписи добавляем личный фильтр по возрасту партнёра
    # (его видит только сам пользователь — другим это не показывается)
    caption = format_profile(u)
    age_min = u.get("partner_age_min")
    age_max = u.get("partner_age_max")
    if age_min and age_max:
        partner = "девушки" if u["gender"] == "M" else "молодого человека"
        caption += f"\n\n🔎 <b>Ищу возраст {partner}:</b> {age_min}–{age_max} лет"

    # Если анкета скрыта — показываем предупреждение и особую клавиатуру
    if u.get("is_hidden"):
        caption += "\n\n👁 <b>Анкета скрыта</b> — сейчас её никто не видит."
        kb = hidden_menu_kb()
    else:
        kb = await menu_for(message.from_user.id)

    # Отправка фото. Стратегия — устойчивая к ошибкам Telegram:
    # если media_group упадёт (например IMAGE_PROCESS_FAILED),
    # шлём каждое фото отдельно.
    async def _send_single(idx: int, photo: dict, cap: str | None) -> bool:
        """Возвращает True если удалось отправить."""
        # 1. Пробуем локальный файл
        src = photo_source_from_dict(photo)
        try:
            await message.answer_photo(src, caption=cap)
            return True
        except Exception as e1:
            logging.warning(f"my_profile photo {idx} FS failed: {e1}")
            # 2. Fallback: photo_id (кеш Telegram)
            if photo.get("photo_id"):
                try:
                    await message.answer_photo(photo["photo_id"], caption=cap)
                    return True
                except Exception as e2:
                    logging.warning(f"my_profile photo {idx} ID failed: {e2}")
        return False

    edit_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✏️ Редактировать анкету",
                              callback_data="edit:open"),
    ]])

    if len(valid_photos) == 1:
        ok = await _send_single(0, valid_photos[0], caption)
        if not ok:
            await message.answer(caption)
        await message.answer("Что дальше?", reply_markup=edit_kb)
        # Основное меню
        await message.answer("Меню:", reply_markup=kb)
    else:
        # Пытаемся media_group, если не выйдет — по одному
        try:
            media = [InputMediaPhoto(media=photo_source_from_dict(valid_photos[0]),
                                     caption=caption, parse_mode="HTML")]
            for p in valid_photos[1:]:
                media.append(InputMediaPhoto(media=photo_source_from_dict(p)))
            await message.answer_media_group(media)
        except Exception as e:
            logging.warning(f"my_profile media_group failed, falling back: {e}")
            # Fallback: шлём по одному, подпись на первом
            for i, p in enumerate(valid_photos):
                cap = caption if i == 0 else None
                sent = await _send_single(i, p, cap)
                if not sent and i == 0:
                    # Хотя бы текст покажем
                    await message.answer(caption)
        await message.answer(
            f"Загружено фото: {len(valid_photos)}.",
            reply_markup=edit_kb,
        )
        await message.answer("Меню:", reply_markup=kb)


# ----------- Список матчей -----------
@router.message(F.text == "💌 Мои матчи")
async def my_matches(message: Message, state: FSMContext):
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала заполни анкету: /start")
        return
    if not await require_fillin(message, state):
        return
    matches = await db.get_matches(message.from_user.id)
    my_kb = await menu_for(message.from_user.id)
    if not matches:
        await message.answer("Пока ни одного матча. Лайкай анкеты — найдёшь!",
                             reply_markup=my_kb)
        return
    lines = ["<b>💌 Твои матчи:</b>\n"]
    for m in matches:
        # Иконка платформы: 🌐 для VK, 📱 для TG
        is_vk = db.is_vk_user(m["user_id"])
        icon = "🌐" if is_vk else "📱"
        # Ссылка на профиль
        if is_vk:
            # VK — screen_name или id
            username = m.get("username") or f"id{db.db_id_to_vk_id(m['user_id'])}"
            link = f"https://vk.com/{username}"
        else:
            # TG — по username
            if m.get("username"):
                link = f"https://t.me/{m['username']}"
            else:
                link = None
        # Имя-fallback: если пусто, показываем «Без имени»
        name = m.get("name") or "Без имени"
        display = f"{name}, {m['age']}"
        if link:
            lines.append(f'{icon} <a href="{link}">{display}</a>')
        else:
            # У TG-юзеров без username ссылки нет — показываем просто текст
            lines.append(f"{icon} <b>{display}</b> (без username, id {m['user_id']})")
    await message.answer("\n".join(lines), reply_markup=my_kb,
                         disable_web_page_preview=True)


# ----------- Уведомление админов о жалобах -----------
async def notify_admins_about_report(reporter_id: int, target_id: int,
                                      total: int, reason: str = None):
    """Шлём всем админам уведомление о новой жалобе с причиной."""
    if not ADMIN_IDS:
        return
    target = await db.get_user(target_id)
    reporter = await db.get_user(reporter_id)
    target_name = target["name"] if target else f"id {target_id}"
    reporter_name = reporter["name"] if reporter else f"id {reporter_id}"

    reason_block = ""
    if reason:
        # Экранируем HTML, чтобы случайные < > не сломали разметку
        safe = (reason.replace("&", "&amp;")
                       .replace("<", "&lt;").replace(">", "&gt;"))
        reason_block = f"\n<b>Причина:</b>\n<i>{safe}</i>\n"

    text = (
        f"🚩 <b>Новая жалоба</b>\n\n"
        f"От: <b>{reporter_name}</b> (id <code>{reporter_id}</code>)\n"
        f"На: <b>{target_name}</b> (id <code>{target_id}</code>)\n"
        f"Всего жалоб на этого пользователя: <b>{total}</b>\n"
        f"{reason_block}\n"
        f"<b>Действия:</b>\n"
        f"• Анкета нарушителя: <code>/userinfo {target_id}</code>\n"
        f"• Кто жалуется: <code>/userinfo {reporter_id}</code>\n"
        f"• Забанить: <code>/ban {target_id} причина</code>"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception as e:
            logging.warning(f"Не смог уведомить админа {admin_id}: {e}")


# ----------- Команды для пользователя (право на удаление по 152-ФЗ) -----------
@router.message(Command("forget"))
async def cmd_forget(message: Message, state: FSMContext):
    """Право пользователя на удаление своих данных по 152-ФЗ, ст. 14.
    Удаляем всё, что связано с пользователем."""
    await state.clear()
    user_id = message.from_user.id

    # Двойное подтверждение через кнопки, чтобы не удалить случайно
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="forget:cancel"),
        InlineKeyboardButton(text="🗑 Да, удалить", callback_data="forget:confirm"),
    ]])
    await message.answer(
        "🗑 <b>Удаление всех данных</b>\n\n"
        "Будут удалены: анкета, фотографии, все лайки и матчи, согласие. "
        "Это <b>необратимо</b>.\n\n"
        "Если кто-то лайкнул тебя — они тоже узнают, что ты ушёл (твоя анкета "
        "просто исчезнет из их матчей).\n\n"
        "Точно удалить?",
        reply_markup=kb,
    )


@router.callback_query(F.data == "forget:cancel")
async def on_forget_cancel(call: CallbackQuery):
    await call.answer("Отменено")
    try:
        await call.message.edit_text("Отмена. Данные сохранены.")
    except Exception:
        pass


@router.callback_query(F.data == "forget:confirm")
async def on_forget_confirm(call: CallbackQuery):
    user_id = call.from_user.id
    await db.delete_user_completely(user_id)
    viewer_state.pop(user_id, None)
    await call.answer("Данные удалены", show_alert=True)
    try:
        await call.message.edit_text(
            "🗑 Все твои данные удалены.\n\n"
            "Если когда-нибудь захочешь вернуться — напиши /start."
        )
    except Exception:
        pass


# ----------- Админ-команды -----------
@router.message(Command("admin"))
async def cmd_admin(message: Message):
    """Справка по командам админа."""
    if not await is_moderator(message.from_user.id):
        return
    await message.answer(
        "<b>Админ-команды:</b>\n\n"
        "<code>/stats</code> — статистика бота\n"
        "<code>/userinfo ID</code> — полное досье на пользователя\n"
        "<code>/ban ID причина</code> — забанить пользователя\n"
        "<code>/unban ID</code> — разбанить\n"
        "<code>/banlist</code> — список забаненных\n"
        "<code>/baninfo ID</code> — посмотреть анкету и жалобы\n"
        "<code>/reports</code> — последние жалобы\n"
        "<code>/migrate_photos</code> — миграция фото на локальное хранилище (одноразово)\n\n"
        f"Твой ID: <code>{message.from_user.id}</code>"
    )


# ----------- /stats -----------
@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not await is_moderator(message.from_user.id):
        return
    s = await db.get_stats()

    def fmt_top(items: list, label: str) -> str:
        if not items:
            return f"<i>нет {label}</i>"
        return "\n".join(f"  {i+1}. {name} — {cnt}"
                         for i, (name, cnt) in enumerate(items))

    # Форматируем конфессии — с разбивкой по полу
    def fmt_denoms(items: list) -> str:
        if not items:
            return "  <i>нет данных</i>"
        lines = []
        for i, (name, total, male, female) in enumerate(items, 1):
            if total == 0:
                # Показываем даже нули — чтобы был виден весь список
                lines.append(f"  {i}. {name} — <b>0</b>")
            else:
                lines.append(
                    f"  {i}. {name} — <b>{total}</b> (М: {male} / Ж: {female})"
                )
        return "\n".join(lines)

    def fmt_other(items: list) -> str:
        if not items:
            return "  <i>нет</i>"
        return "\n".join(f"  {i+1}. {name} — {cnt}"
                          for i, (name, cnt) in enumerate(items))

    text = (
        "📊 <b>Статистика бота</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"

        "👥 <b>Пользователи</b>\n"
        f"  Всего анкет: <b>{s['users_total']}</b>\n"
        f"  ├ Мужчин: {s['users_male']}\n"
        f"  └ Женщин: {s['users_female']}\n"
        f"\n"
        f"  Новых за 24 часа: <b>+{s['users_24h']}</b>\n"
        f"  Новых за 7 дней: +{s['users_7d']}\n"
        f"  Новых за 30 дней: +{s['users_30d']}\n"
        f"  Активных за 7 дней: {s['users_active_7d']}\n\n"

        "💫 <b>Активность</b>\n"
        f"  Всего свайпов: <b>{s['swipes_total']}</b>\n"
        f"  ├ ❤️ Лайков: {s['likes_total']}\n"
        f"  └ ❌ Дизлайков: {s['dislikes_total']}\n"
        f"  Конверсия лайков: {s['like_rate']}%\n"
        f"  Свайпов за 24 часа: <b>{s['swipes_24h']}</b>\n\n"

        "💕 <b>Матчи</b>\n"
        f"  Всего пар: <b>{s['matches_total']}</b>\n"
        f"  За 24 часа: +{s['matches_24h']}\n"
        f"  За 7 дней: +{s['matches_7d']}\n\n"

        "🏙 <b>Топ городов</b>\n"
        f"{fmt_top(s['top_cities'], 'данных')}\n\n"

        "✝️ <b>Конфессии</b>\n"
        f"{fmt_denoms(s['denominations'])}\n"
        f"\n"
        f"📝 <b>Другое ({s['denom_other_total']})</b>:\n"
        f"{fmt_other(s['denom_other_top'])}\n"
        + (f"\n<i>Не указано: {s['denom_not_set']}</i>\n"
           if s['denom_not_set'] else "")
        + "\n"

        "🛡 <b>Модерация</b>\n"
        f"  Жалоб всего: {s['reports_total']}\n"
        f"  Жалоб за 24 часа: <b>{s['reports_24h']}</b>\n"
        f"  Забанено: {s['banned_total']}"
    )
    await message.answer(text)


@router.message(Command("ban"))
async def cmd_ban(message: Message):
    if not await is_moderator(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Использование: <code>/ban ID причина</code>")
        return
    target_id = await parse_target_id(parts[1])
    if target_id is None:
        await message.answer("Не распознал ID. Можно: число, vk.com/idXXX, @username.")
        return
    reason = parts[2] if len(parts) > 2 else "не указана"

    if await is_moderator(target_id):
        await message.answer("⚠️ Нельзя забанить админа или модератора.")
        return

    target = await db.get_user(target_id)
    target_name = target["name"] if target else f"id {target_id}"

    await db.ban_user(target_id, reason, message.from_user.id)
    await message.answer(
        f"✅ Пользователь <b>{target_name}</b> (id {target_id}) забанен.\n"
        f"Причина: {reason}"
    )

    # Сообщаем забаненному. Для TG-юзеров — напрямую. Для VK — в очередь,
    # которую разгребёт VK-бот.
    ban_text = (
        f"⛔ Ты заблокирован администратором.\n"
        f"Причина: {reason}\n\n"
        f"Если считаешь это ошибкой — напиши администратору."
    )
    if db.is_tg_user(target_id):
        try:
            await bot.send_message(target_id, ban_text)
        except Exception:
            pass  # пользователь мог заблокировать бота
    else:
        # VK — кладём в очередь как «системное сообщение»
        await db.queue_system_message(target_id, ban_text)


@router.message(Command("unban"))
async def cmd_unban(message: Message):
    if not await is_moderator(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: <code>/unban ID</code>")
        return
    target_id = await parse_target_id(parts[1])
    if target_id is None:
        await message.answer("Не распознал ID. Можно: число, vk.com/idXXX, @username.")
        return
    ok = await db.unban_user(target_id)
    if ok:
        await message.answer(f"✅ Пользователь id {target_id} разбанен.")
        unban_text = "✅ Тебя разбанили. Можешь снова пользоваться ботом."
        if db.is_tg_user(target_id):
            try:
                await bot.send_message(target_id, unban_text)
            except Exception:
                pass
        else:
            await db.queue_system_message(target_id, unban_text)
    else:
        await message.answer(f"Пользователь id {target_id} не был забанен.")


@router.message(Command("banlist"))
async def cmd_banlist(message: Message):
    if not await is_moderator(message.from_user.id):
        return
    bans = await db.get_all_bans()
    if not bans:
        await message.answer("Бан-лист пуст.")
        return
    lines = ["<b>Забаненные пользователи:</b>\n"]
    for b in bans[:50]:  # максимум 50 за раз
        name = b.get("name") or "(нет анкеты)"
        lines.append(
            f"• <b>{name}</b> (id {b['user_id']}) — {b['reason'] or 'без причины'}"
        )
    if len(bans) > 50:
        lines.append(f"\n…и ещё {len(bans) - 50}")
    await message.answer("\n".join(lines))


@router.message(Command("baninfo"))
async def cmd_baninfo(message: Message):
    if not await is_moderator(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Использование: <code>/baninfo ID</code>")
        return
    target_id = await parse_target_id(parts[1])
    if target_id is None:
        await message.answer("Не распознал ID. Можно: число, vk.com/idXXX, @username.")
        return
    u = await db.get_user(target_id)
    reports_count = await db.count_reports(target_id)
    ban = await db.get_ban(target_id)

    if not u:
        await message.answer(
            f"Анкеты с id {target_id} нет.\n"
            f"Жалоб: {reports_count}\n"
            f"Бан: {'да — ' + (ban['reason'] or '') if ban else 'нет'}"
        )
        return

    status = f"⛔ Забанен ({ban['reason'] or 'без причины'})" if ban else "✅ Активен"
    caption = (
        f"{status}\n"
        f"Жалоб: <b>{reports_count}</b>\n\n"
        f"<b>{u['name']}, {u['age']}</b>\n"
        f"id: <code>{u['user_id']}</code>\n"
        f"@{u['username'] or '(нет username)'}\n\n"
        f"📍 {u['city']}\n"
        + (f"✝️ {u['denomination']}\n" if u.get("denomination") else "")
        + f"⛪ {u['church']}\n"
        + (f"🙏 {u['church_role']}\n" if u.get("church_role") else "")
        + (f"💼 {u['job']}\n" if u.get("job") else "")
        + f"💍 {u['marital']}\n"
        f"👶 {u['children']}\n\n"
        f"<b>О себе:</b>\n{u['hobbies']}"
    )
    try:
        await message.answer_photo(
            photo_source(u.get("photo_path"), u["photo_id"]),
            caption=caption,
        )
    except Exception:
        await message.answer(caption)

    # Дополнительно — последние жалобы на этого пользователя с причинами
    if reports_count > 0:
        reports = await db.get_reports_on(target_id, limit=10)
        if reports:
            lines = ["<b>🚩 Последние жалобы:</b>\n"]
            for r in reports:
                reporter = r["reporter_name"] or f"id {r['reporter_id']}"
                rsn = r.get("reason")
                if rsn:
                    safe = (rsn.replace("&", "&amp;")
                                .replace("<", "&lt;").replace(">", "&gt;"))
                    if len(safe) > 200:
                        safe = safe[:200] + "…"
                    lines.append(f"• От {reporter}: <i>{safe}</i>")
                else:
                    lines.append(f"• От {reporter}: <i>(без причины — старая жалоба)</i>")
            await message.answer("\n".join(lines))


# ----------- /userinfo — полное досье на пользователя -----------
@router.message(Command("userinfo"))
async def cmd_userinfo(message: Message):
    if not await is_moderator(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer(
            "Использование: <code>/userinfo ID</code>\n\n"
            "ID можно указать как:\n"
            "• Число (TG: положительное, VK: отрицательное)\n"
            "• Ссылку vk.com/idXXXX\n"
            "• @username (для TG)"
        )
        return
    target_id = await parse_target_id(parts[1])
    if target_id is None:
        await message.answer(
            "Не распознал ID. Можно: число, vk.com/idXXX, @username."
        )
        return
    info = await db.get_user_info(target_id)
    u = info["profile"]
    ban = info["ban"]

    # Статус
    if ban:
        status = f"⛔ <b>Забанен</b> — {ban['reason'] or 'без причины'}"
    else:
        status = "✅ <b>Активен</b>"

    # Соотношение лайков/дизлайков (как разборчив)
    my_total = info["my_likes"] + info["my_dislikes"]
    my_like_pct = (
        round(100 * info["my_likes"] / my_total, 1) if my_total > 0 else 0
    )

    received_total = info["likes_received"] + info["dislikes_received"]
    received_like_pct = (
        round(100 * info["likes_received"] / received_total, 1)
        if received_total > 0 else 0
    )

    # Сигнал тревоги, если жалоб подаёт неадекватно много относительно активности
    suspicious_reporter = ""
    if info["my_reports"] >= 5 and my_total > 0:
        report_rate = round(100 * info["my_reports"] / my_total, 1)
        if report_rate > 30:
            suspicious_reporter = (
                f"\n⚠️ <b>Подозрительно много жалоб</b>: "
                f"{info['my_reports']} жалоб на {my_total} свайпов ({report_rate}%)"
            )

    # Платформа и публичная ссылка
    if db.is_vk_user(target_id):
        platform_label = "🌐 VK"
        public_link = f"https://vk.com/id{db.db_id_to_vk_id(target_id)}"
    else:
        platform_label = "📱 Telegram"
        if u and u.get("username"):
            public_link = f"https://t.me/{u['username']}"
        else:
            public_link = None

    body = (
        f"{status}\n"
        f"id: <code>{target_id}</code>\n"
        f"Платформа: {platform_label}\n"
    )
    if public_link:
        body += f'Ссылка: <a href="{public_link}">{public_link}</a>\n'
    body += "\n"

    if u:
        if db.is_vk_user(target_id):
            username_line = f"VK: {u['username'] or '(нет screen_name)'}\n"
        else:
            username_line = f"@{u['username'] or '(нет username)'}\n"
        body += (
            username_line + "\n"
            f"<b>{u['name']}, {u['age']}</b>\n"
            f"📍 {u['city']}\n"
            + (f"✝️ {u['denomination']}\n" if u.get("denomination") else "")
            + f"⛪ {u['church']}\n"
            + (f"🙏 {u['church_role']}\n" if u.get("church_role") else "")
            + (f"💼 {u['job']}\n" if u.get("job") else "")
            + f"💍 {u['marital']}\n"
            f"👶 {u['children']}\n"
            f"📅 Зарегистрирован: {u['created_at']}\n\n"
            f"<b>О себе:</b>\n{u['hobbies']}\n"
        )
    else:
        body += "<i>Анкеты нет</i>\n"

    body += (
        f"\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Его активность</b>\n"
        f"  ❤️ Лайков поставил: {info['my_likes']}\n"
        f"  ❌ Дизлайков поставил: {info['my_dislikes']}\n"
        f"  Доля лайков: {my_like_pct}%\n"
        f"  🚩 Жалоб подал: <b>{info['my_reports']}</b>\n"
        f"  💕 Матчей: {info['matches']}\n"
        f"  Последний свайп: {info['last_active'] or 'нет данных'}\n\n"
        f"📥 <b>Что получил от других</b>\n"
        f"  ❤️ Лайков получил: {info['likes_received']}\n"
        f"  ❌ Дизлайков получил: {info['dislikes_received']}\n"
        f"  Привлекательность: {received_like_pct}%\n"
        f"  🚩 Жалоб на него: <b>{info['reports_against']}</b>"
        f"{suspicious_reporter}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Действия: <code>/ban {target_id} причина</code> · "
        f"<code>/unban {target_id}</code>"
    )

    # Если есть фото — шлём с фото, иначе текстом
    if u and u.get("photo_id"):
        try:
            await message.answer_photo(
                photo_source(u.get("photo_path"), u["photo_id"]),
                caption=body,
            )
            return
        except Exception:
            pass  # caption может быть длиннее лимита — упадём в текстовый
    await message.answer(body)


@router.message(Command("reports"))
async def cmd_reports(message: Message):
    if not await is_moderator(message.from_user.id):
        return
    reports = await db.get_recent_reports(limit=20)
    if not reports:
        await message.answer("Жалоб пока нет.")
        return
    lines = ["<b>Последние жалобы:</b>\n"]
    for r in reports:
        target_name = r["target_name"] or f"id {r['target_id']}"
        reporter_name = r["reporter_name"] or f"id {r['reporter_id']}"
        reason = r.get("reason")
        line = (
            f"• <b>{target_name}</b> (id {r['target_id']}) ← от {reporter_name}"
        )
        if reason:
            # Экранируем HTML; обрезаем длинное
            safe = (reason.replace("&", "&amp;")
                          .replace("<", "&lt;").replace(">", "&gt;"))
            if len(safe) > 100:
                safe = safe[:100] + "…"
            line += f"\n   <i>{safe}</i>"
        lines.append(line)
    lines.append("\nПодробнее: <code>/baninfo ID</code>")
    await message.answer("\n".join(lines))


# ----------- /migrate_photos: миграция существующих фото на локальное хранилище -----------
@router.message(Command("migrate_photos"))
async def cmd_migrate_photos(message: Message):
    """Скачивает все TG-фото с серверов Telegram на наш VPS.
    Выполняется один раз при переходе на локальное хранилище.
    Идемпотентна: повторный запуск пропускает уже скачанные файлы."""
    if not await is_moderator(message.from_user.id):
        return

    import photo_utils
    progress_msg = await message.answer("⏳ Готовлю миграцию фотографий…")

    last_update = 0

    async def progress(done, total):
        nonlocal last_update
        # обновляем сообщение не чаще раза в 5 фоток, чтобы не упереться в rate limit
        if done == total or done - last_update >= 5:
            last_update = done
            try:
                await progress_msg.edit_text(
                    f"📥 Миграция фотографий: <b>{done}/{total}</b>"
                )
            except Exception:
                pass

    try:
        stats = await photo_utils.migrate_all_tg_photos(bot, progress_callback=progress)
        await message.answer(
            f"✅ <b>Миграция фото завершена</b>\n\n"
            f"Всего: {stats['total']}\n"
            f"Скачано: {stats['success']}\n"
            f"Пропущено (уже было): {stats['skipped']}\n"
            f"Ошибок: {stats['failed']}\n\n"
            f"Фотографии теперь лежат локально в <code>/root/dating_bot/photos/</code>"
        )
    except Exception as e:
        logging.exception("migrate_photos failed")
        await message.answer(f"❌ Ошибка миграции: <code>{e}</code>")


# ============= РАССЫЛКА (/rassylka) =============

import re as _re_bc

# Удаляет HTML-теги для VK (VK не понимает HTML)
def _strip_html_for_vk(text: str) -> str:
    """Грубое удаление HTML-тегов для VK."""
    if not text:
        return text
    # Заменяем <br> и <br/> на перевод строки
    text = _re_bc.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=_re_bc.IGNORECASE)
    # Удаляем все остальные теги (включая <a href="..">текст</a> — оставляем «текст»)
    text = _re_bc.sub(r"<[^>]+>", "", text)
    # Раскодируем HTML-сущности
    text = (text.replace("&amp;", "&")
                 .replace("&lt;", "<")
                 .replace("&gt;", ">")
                 .replace("&quot;", '"')
                 .replace("&#39;", "'"))
    return text


def _filter_summary(filters: dict) -> str:
    """Краткое описание текущих фильтров для меню."""
    lines = []
    # Платформа
    p = filters.get("platform", "all")
    p_label = {"all": "Все", "tg": "Только TG", "vk": "Только VK"}.get(p, "Все")
    lines.append(f"📱 Платформа: <b>{p_label}</b>")
    # Пол
    g = filters.get("gender", "all")
    g_label = {"all": "Все", "M": "Только М", "F": "Только Ж"}.get(g, "Все")
    lines.append(f"👤 Пол: <b>{g_label}</b>")
    # Возраст
    age_min = filters.get("age_min")
    age_max = filters.get("age_max")
    if age_min and age_max:
        lines.append(f"📅 Возраст: <b>{age_min}–{age_max}</b>")
    else:
        lines.append("📅 Возраст: <b>Все</b>")
    # Город
    city = filters.get("city")
    lines.append(f"📍 Город: <b>{city or 'Все'}</b>")
    # Конфессия
    denom = filters.get("denomination")
    lines.append(f"✝️ Конфессия: <b>{denom or 'Все'}</b>")
    return "\n".join(lines)


def _broadcast_main_menu_kb() -> InlineKeyboardMarkup:
    """Главное меню — выбор «Всем» или «Настроить»."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Всем", callback_data="bc:all")],
        [InlineKeyboardButton(text="⚙️ Настроить фильтры", callback_data="bc:setup")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="bc:cancel")],
    ])


def _broadcast_filter_menu_kb() -> InlineKeyboardMarkup:
    """Меню настройки фильтров."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Платформа", callback_data="bc:f:platform"),
         InlineKeyboardButton(text="👤 Пол", callback_data="bc:f:gender")],
        [InlineKeyboardButton(text="📅 Возраст", callback_data="bc:f:age"),
         InlineKeyboardButton(text="📍 Город", callback_data="bc:f:city")],
        [InlineKeyboardButton(text="✝️ Конфессия", callback_data="bc:f:denom")],
        [InlineKeyboardButton(text="✅ К отправке", callback_data="bc:preview"),
         InlineKeyboardButton(text="❌ Отмена", callback_data="bc:cancel")],
    ])


def _broadcast_platform_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Все", callback_data="bc:set:platform:all")],
        [InlineKeyboardButton(text="Только TG", callback_data="bc:set:platform:tg")],
        [InlineKeyboardButton(text="Только VK", callback_data="bc:set:platform:vk")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="bc:back")],
    ])


def _broadcast_gender_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Все", callback_data="bc:set:gender:all")],
        [InlineKeyboardButton(text="Только мужчины", callback_data="bc:set:gender:M")],
        [InlineKeyboardButton(text="Только женщины", callback_data="bc:set:gender:F")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="bc:back")],
    ])


def _broadcast_denom_kb() -> InlineKeyboardMarkup:
    rows = []
    # По 2 в ряду
    for i in range(0, len(DENOMINATIONS), 2):
        rows.append([
            InlineKeyboardButton(
                text=d,
                callback_data=f"bc:set:denom:{i + j}",
            )
            for j, d in enumerate(DENOMINATIONS[i:i+2])
        ])
    rows.append([InlineKeyboardButton(text="Все", callback_data="bc:set:denom:all")])
    rows.append([InlineKeyboardButton(text="◀ Назад", callback_data="bc:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _broadcast_age_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Все", callback_data="bc:set:age:all")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="bc:back")],
    ])


def _broadcast_city_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Все", callback_data="bc:set:city:all")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="bc:back")],
    ])


def _broadcast_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, отправить", callback_data="bc:send")],
        [InlineKeyboardButton(text="◀ К фильтрам", callback_data="bc:back_to_menu")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="bc:cancel")],
    ])


@router.message(Command("rassylka"))
async def cmd_rassylka(message: Message, state: FSMContext):
    """Запуск рассылки. Доступно ТОЛЬКО root-админам (из .env)."""
    if not is_root(message.from_user.id):
        return
    await state.clear()
    await state.set_state(BroadcastForm.text)
    await message.answer(
        "📝 <b>Рассылка</b>\n\n"
        "Напиши текст (5–4000 символов).\n"
        "Можно использовать HTML: <code>&lt;b&gt;жирный&lt;/b&gt;</code>, "
        "<code>&lt;i&gt;курсив&lt;/i&gt;</code>, "
        "<code>&lt;a href='URL'&gt;ссылка&lt;/a&gt;</code>.\n\n"
        "Для VK теги уберутся автоматически.\n\n"
        "Отмена: /cancel",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(BroadcastForm.text, Command("cancel"))
@router.message(BroadcastForm.menu, Command("cancel"))
@router.message(BroadcastForm.f_age, Command("cancel"))
@router.message(BroadcastForm.f_city, Command("cancel"))
async def broadcast_cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Рассылка отменена.", reply_markup=main_menu_kb())


@router.message(BroadcastForm.text, F.text)
async def broadcast_text(message: Message, state: FSMContext):
    """Получили текст рассылки. Сохраняем и показываем главное меню."""
    text = (message.text or "").strip()
    if not (5 <= len(text) <= 4000):
        await message.answer(
            f"Длина текста {len(text)}. Допустимо: 5–4000 символов.",
        )
        return
    await state.update_data(broadcast_text=text, filters={})
    await state.set_state(BroadcastForm.menu)

    # Считаем сколько всего получателей без фильтров
    counts = await db.count_broadcast_recipients()
    preview = text if len(text) <= 200 else text[:200] + "…"
    await message.answer(
        f"📤 <b>Кому отправить?</b>\n\n"
        f"<i>Превью текста:</i>\n{preview}\n\n"
        f"👥 Всего получателей: <b>{counts['total']}</b> "
        f"(TG: {counts['tg']}, VK: {counts['vk']})",
        reply_markup=_broadcast_main_menu_kb(),
    )


async def _show_filter_menu(message_or_call, state: FSMContext, edit: bool = False):
    """Показать меню настройки фильтров с текущим состоянием."""
    data = await state.get_data()
    filters = data.get("filters", {})
    counts = await db.count_broadcast_recipients(**filters)
    text = (
        f"⚙️ <b>Фильтры рассылки</b>\n\n"
        f"{_filter_summary(filters)}\n\n"
        f"👥 Подходит: <b>{counts['total']}</b> "
        f"(TG: {counts['tg']}, VK: {counts['vk']})"
    )
    kb = _broadcast_filter_menu_kb()
    if edit and isinstance(message_or_call, CallbackQuery):
        try:
            await message_or_call.message.edit_text(text, reply_markup=kb)
        except Exception:
            await message_or_call.message.answer(text, reply_markup=kb)
    else:
        msg = message_or_call if isinstance(message_or_call, Message) else message_or_call.message
        await msg.answer(text, reply_markup=kb)


async def _show_preview(call: CallbackQuery, state: FSMContext):
    """Показать превью перед отправкой."""
    data = await state.get_data()
    bc_text = data.get("broadcast_text", "")
    filters = data.get("filters", {})
    counts = await db.count_broadcast_recipients(**filters)

    preview = bc_text if len(bc_text) <= 300 else bc_text[:300] + "…"
    text = (
        f"📋 <b>Превью рассылки</b>\n\n"
        f"<b>Текст:</b>\n{preview}\n\n"
        f"<b>Фильтры:</b>\n{_filter_summary(filters)}\n\n"
        f"👥 <b>Получателей: {counts['total']}</b> "
        f"(TG: {counts['tg']}, VK: {counts['vk']})\n\n"
        f"Отправить?"
    )
    await state.set_state(BroadcastForm.confirm)
    try:
        await call.message.edit_text(text, reply_markup=_broadcast_confirm_kb())
    except Exception:
        await call.message.answer(text, reply_markup=_broadcast_confirm_kb())


@router.callback_query(F.data.startswith("bc:"))
async def broadcast_callback(call: CallbackQuery, state: FSMContext):
    if not is_root(call.from_user.id):
        await call.answer()
        return
    cur_state = await state.get_state()
    data = call.data

    if data == "bc:cancel":
        await state.clear()
        await call.answer("Отменено")
        try:
            await call.message.edit_text("❌ Рассылка отменена.")
        except Exception:
            pass
        return

    if data == "bc:all":
        # "Всем" — сразу превью без захода в фильтры
        await call.answer()
        await _show_preview(call, state)
        return

    if data == "bc:setup":
        await call.answer()
        await state.set_state(BroadcastForm.menu)
        await _show_filter_menu(call, state, edit=True)
        return

    if data == "bc:preview":
        await call.answer()
        await _show_preview(call, state)
        return

    if data == "bc:back_to_menu":
        await call.answer()
        await state.set_state(BroadcastForm.menu)
        await _show_filter_menu(call, state, edit=True)
        return

    if data == "bc:back":
        await call.answer()
        await state.set_state(BroadcastForm.menu)
        await _show_filter_menu(call, state, edit=True)
        return

    # Открытие конкретного фильтра
    if data.startswith("bc:f:"):
        await call.answer()
        field = data.split(":")[2]
        if field == "platform":
            await call.message.edit_text(
                "📱 <b>Платформа:</b> кому отправить?",
                reply_markup=_broadcast_platform_kb(),
            )
        elif field == "gender":
            await call.message.edit_text(
                "👤 <b>Пол:</b> кому отправить?",
                reply_markup=_broadcast_gender_kb(),
            )
        elif field == "age":
            await state.set_state(BroadcastForm.f_age)
            await call.message.edit_text(
                "📅 <b>Возраст</b>\n\n"
                "Напиши диапазон в формате <code>25-40</code> "
                "(возраст с 25 до 40 включительно).\n\n"
                "Или нажми «Все», чтобы убрать фильтр.",
                reply_markup=_broadcast_age_kb(),
            )
        elif field == "city":
            await state.set_state(BroadcastForm.f_city)
            cities = await db.list_distinct_cities(min_users=3)
            cities_hint = ", ".join(cities[:15]) if cities else "(нет данных)"
            await call.message.edit_text(
                f"📍 <b>Город</b>\n\n"
                f"Напиши название города (точное совпадение, без учёта регистра).\n\n"
                f"<i>Популярные города:</i> {cities_hint}\n\n"
                f"Или нажми «Все», чтобы убрать фильтр.",
                reply_markup=_broadcast_city_kb(),
            )
        elif field == "denom":
            await call.message.edit_text(
                "✝️ <b>Конфессия</b>",
                reply_markup=_broadcast_denom_kb(),
            )
        return

    # Установка значения фильтра
    if data.startswith("bc:set:"):
        await call.answer()
        parts = data.split(":")
        # bc:set:platform:tg / bc:set:gender:M / bc:set:age:all / bc:set:denom:0 / bc:set:city:all
        field = parts[2]
        value = parts[3]
        st = await state.get_data()
        filters = st.get("filters", {})

        if field == "platform":
            filters["platform"] = value
        elif field == "gender":
            filters["gender"] = value
        elif field == "age":
            if value == "all":
                filters.pop("age_min", None)
                filters.pop("age_max", None)
        elif field == "city":
            if value == "all":
                filters.pop("city", None)
        elif field == "denom":
            if value == "all":
                filters.pop("denomination", None)
            else:
                idx = int(value)
                if 0 <= idx < len(DENOMINATIONS):
                    filters["denomination"] = DENOMINATIONS[idx]

        await state.update_data(filters=filters)
        await state.set_state(BroadcastForm.menu)
        await _show_filter_menu(call, state, edit=True)
        return

    if data == "bc:send":
        # Запуск рассылки
        await call.answer("Запускаю…")
        await _run_broadcast(call, state)
        return


@router.message(BroadcastForm.f_age, F.text)
async def broadcast_age_input(message: Message, state: FSMContext):
    """Парсим диапазон возраста: 25-40, 25 40, 25..40."""
    txt = (message.text or "").strip().replace("..", "-").replace(" ", "-")
    m = _re_bc.match(r"^(\d{2,3})-(\d{2,3})$", txt)
    if not m:
        await message.answer(
            "Не понял. Напиши в формате <code>25-40</code> "
            "или нажми «Все».",
            reply_markup=_broadcast_age_kb(),
        )
        return
    age_min, age_max = int(m.group(1)), int(m.group(2))
    if not (18 <= age_min <= age_max <= 99):
        await message.answer("Допустимо 18–99 и минимум ≤ максимум.")
        return
    data = await state.get_data()
    filters = data.get("filters", {})
    filters["age_min"] = age_min
    filters["age_max"] = age_max
    await state.update_data(filters=filters)
    await state.set_state(BroadcastForm.menu)
    await _show_filter_menu(message, state)


@router.message(BroadcastForm.f_city, F.text)
async def broadcast_city_input(message: Message, state: FSMContext):
    city = (message.text or "").strip()
    if not (2 <= len(city) <= 50):
        await message.answer("Название города от 2 до 50 символов.")
        return
    # Сразу проверим, есть ли вообще кто-то в этом городе
    counts = await db.count_broadcast_recipients(city=city)
    if counts["total"] == 0:
        await message.answer(
            f"В городе «{city}» никого не нашёл. "
            f"Проверь название (точное совпадение), либо нажми «Все».",
            reply_markup=_broadcast_city_kb(),
        )
        return
    data = await state.get_data()
    filters = data.get("filters", {})
    filters["city"] = city
    await state.update_data(filters=filters)
    await state.set_state(BroadcastForm.menu)
    await _show_filter_menu(message, state)


async def _run_broadcast(call: CallbackQuery, state: FSMContext):
    """Сама отправка рассылки."""
    data = await state.get_data()
    bc_text = data.get("broadcast_text", "")
    filters = data.get("filters", {})

    if not bc_text:
        await call.message.answer("Текст рассылки пуст — отменяю.")
        await state.clear()
        return

    recipients = await db.get_broadcast_recipients(**filters)
    total = len(recipients)
    if total == 0:
        await call.message.answer("Никого не нашёл по фильтрам — отменяю.")
        await state.clear()
        return

    # Разделяем на TG и VK
    tg_recipients = [r for r in recipients if r["user_id"] > 0]
    vk_recipients = [r for r in recipients if r["user_id"] < 0]

    # Для VK заранее кладём в очередь — VK-бот их сам разошлёт
    batch_id = int(__import__("time").time())  # уникальный id
    if vk_recipients:
        vk_ids = [db.db_id_to_vk_id(r["user_id"]) for r in vk_recipients]
        vk_text = _strip_html_for_vk(bc_text)
        await db.queue_broadcast_chunk(vk_ids, vk_text, batch_id)

    # Очищаем состояние СРАЗУ — чтобы FSM не блокировала админа во время отправки
    await state.clear()

    # Прогресс-сообщение, которое будем редактировать
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    progress_msg = await call.message.answer(
        f"⏳ Запустил рассылку…\n"
        f"TG: 0/{len(tg_recipients)}\n"
        f"VK: 0/{len(vk_recipients)} (через очередь)"
    )

    # ---- Отправка в Telegram ----
    sent_tg = 0
    failed_tg = 0
    blocked = 0
    deleted = 0

    for i, r in enumerate(tg_recipients):
        try:
            await bot.send_message(r["user_id"], bc_text)
            sent_tg += 1
        except Exception as e:
            err = str(e).lower()
            failed_tg += 1
            if "blocked" in err or "deactivated" in err:
                blocked += 1
            elif "user_deactivated" in err or "chat not found" in err:
                deleted += 1
            logging.warning(f"Broadcast TG to {r['user_id']} failed: {e}")
        # Лимит ~10 сообщений/сек — безопасно, Telegram не ругается
        await asyncio.sleep(0.1)
        # Обновляем прогресс каждые 25 (чтобы не упереться в rate limit edit'ов)
        if (i + 1) % 25 == 0:
            try:
                await progress_msg.edit_text(
                    f"⏳ Рассылка в процессе…\n"
                    f"TG: {sent_tg + failed_tg}/{len(tg_recipients)} "
                    f"(✅ {sent_tg}, ❌ {failed_tg})\n"
                    f"VK: в очереди ({len(vk_recipients)} запланировано)"
                )
            except Exception:
                pass

    # ---- VK-статистика (запросим разок, не ждём завершения) ----
    vk_stats_now = await db.get_broadcast_stats(batch_id) if vk_recipients else {"delivered": 0, "pending": 0}

    # ---- Финал ----
    try:
        await progress_msg.edit_text(
            f"✅ <b>Рассылка завершена</b>\n\n"
            f"<b>Telegram:</b>\n"
            f"• Отправлено: {sent_tg}\n"
            f"• Не удалось: {failed_tg}"
            + (f" (заблокировали бота: {blocked})" if blocked else "")
            + (f" (удалили аккаунт: {deleted})" if deleted else "")
            + "\n\n"
            + f"<b>ВКонтакте:</b>\n"
            + f"• В очереди: {len(vk_recipients)}\n"
            + f"• Уже отправлено: {vk_stats_now['delivered']}\n"
            + f"• Ждут отправки: {vk_stats_now['pending']}\n\n"
            + f"<i>VK-рассылка идёт фоном (~20 сообщений/сек). "
              f"Проверь через несколько минут.</i>"
        )
    except Exception:
        pass

    logging.info(
        f"Broadcast batch {batch_id} done. TG sent: {sent_tg}, "
        f"TG failed: {failed_tg}, VK queued: {len(vk_recipients)}"
    )


# ============= ПЛАНИРОВЩИК УВЕДОМЛЕНИЙ =============
# Раз в час бот смотрит на текущее время и, если пора — рассылает уведомления.
# TG-получателям шлём напрямую, VK — через очередь (VK-бот сам разошлёт).

from datetime import datetime, timezone, timedelta

MOSCOW_TZ = timezone(timedelta(hours=3))


async def _dispatch_notif(user_id: int, text: str, kind: str = "system_message"):
    """Универсальный отправитель уведомления: TG напрямую, VK через очередь."""
    if db.is_tg_user(user_id):
        try:
            await bot.send_message(user_id, text)
        except Exception as e:
            logging.warning(f"notif TG to {user_id} failed: {e}")
    else:
        # В очередь как system_message — VK-бот разошлёт
        await db.queue_system_message(user_id, text)


async def send_weekly_new_profiles_notif():
    """Суббота 21:00 МСК: «X новых анкет за неделю в твоём возрасте»."""
    recipients = await db.get_weekly_notif_recipients()
    if not recipients:
        logging.info("Weekly notif: получателей 0")
        return
    logging.info(f"Weekly notif: получателей {len(recipients)}")
    notified_ids = []
    for r in recipients:
        n = r["new_count"]
        # Правильное склонение слова
        if n == 1:
            word = "новая анкета"
        elif 2 <= n <= 4:
            word = "новые анкеты"
        else:
            word = "новых анкет"
        text = (
            f"✨ За эту неделю у нас {n} {word} "
            f"(возраст {r['a_min']}–{r['a_max']}).\n\n"
            f"Загляни в ленту — может кого-то встретишь! 🕊"
        )
        await _dispatch_notif(r["user_id"], text)
        notified_ids.append(r["user_id"])
        await asyncio.sleep(0.05)  # rate limit
    await db.mark_weekly_notified(notified_ids)
    logging.info(f"Weekly notif: отправлено {len(notified_ids)}")


async def send_daily_likes_notif():
    """Ежедневно 21:00 МСК: «Сегодня тебя лайкнули X человек»."""
    recipients = await db.get_daily_likes_recipients()
    if not recipients:
        logging.info("Daily likes notif: получателей 0")
        return
    logging.info(f"Daily likes notif: получателей {len(recipients)}")
    notified_ids = []
    for r in recipients:
        n = r["likes_count"]
        # Склонение
        if n == 1:
            word = "человек лайкнул"
        elif 2 <= n <= 4:
            word = "человека лайкнули"
        else:
            word = "человек лайкнули"
        text = (
            f"❤️ Сегодня твою анкету {word} — <b>{n}</b>.\n\n"
            f"Загляни в «💌 Мои матчи» — возможно, кто-то уже мэтч!"
        )
        await _dispatch_notif(r["user_id"], text)
        notified_ids.append(r["user_id"])
        await asyncio.sleep(0.05)
    await db.mark_daily_likes_notified(notified_ids)
    logging.info(f"Daily likes notif: отправлено {len(notified_ids)}")


async def send_hidden_30days_prompt():
    """Ежедневно проверяем: у кого скрытие ≥30 дней — предложить вернуться."""
    users = await db.get_users_hidden_30_days()
    if not users:
        return
    logging.info(f"Hidden 30d prompt: {len(users)} юзеров")
    for u in users:
        uid = u["user_id"]
        text = (
            "👋 Прошёл месяц с тех пор как ты скрыл(а) анкету.\n\n"
            "Что хочешь сделать?"
        )
        if db.is_tg_user(uid):
            try:
                await bot.send_message(uid, text,
                                       reply_markup=_return_prompt_kb())
            except Exception as e:
                logging.warning(f"hidden prompt TG to {uid} failed: {e}")
        else:
            # VK-юзеру шлём просто текст-напоминание, а разберётся через
            # своё меню (в VK инлайн-кнопки для команд типа «return» сложнее
            # синхронизировать — юзер просто пишет «вернуть анкету»)
            await db.queue_system_message(
                uid,
                "👋 Прошёл месяц с тех пор как ты скрыл(а) анкету.\n"
                "Напиши «вернуть» чтобы вернуть анкету, «удалить» чтобы "
                "удалить навсегда, или ничего — тогда останется скрытой.",
            )
        # Продлеваем скрытие на 30 дней, чтобы завтра снова не спросить
        # (если пользователь ответит — extend/return сработают заново)
        await db.extend_hide(uid)
        await asyncio.sleep(0.05)


# Флаги, чтобы не запускать одну и ту же задачу дважды за одну итерацию
_last_weekly_run: str = ""
_last_daily_likes_run: str = ""
_last_hidden_prompt_run: str = ""


# ============= АДМИН-МЕНЮ («👑 Админ») =============
# Кнопка «👑 Админ» открывает подменю с inline-кнопками.
# Наполнение зависит от роли:
#   root      : Статистика, Рассылка, Назначить админа, Назначить модератора,
#               Список админов, Список модераторов
#   admin     : Статистика, Назначить модератора, Список модераторов
#   moderator : Статистика

async def _admin_menu_kb(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура админ-меню для конкретного юзера — по его роли."""
    level = await get_admin_level(user_id)
    rows = [[InlineKeyboardButton(text="📊 Статистика",
                                   callback_data="adm:stats")]]

    if level == "root":
        rows.append([InlineKeyboardButton(text="📢 Рассылка",
                                          callback_data="adm:broadcast")])
        rows.append([
            InlineKeyboardButton(text="👑 Назначить админа",
                                 callback_data="adm:assign:admin"),
            InlineKeyboardButton(text="🛡 Назначить модератора",
                                 callback_data="adm:assign:moderator"),
        ])
        rows.append([
            InlineKeyboardButton(text="📋 Админы",
                                 callback_data="adm:list:admin"),
            InlineKeyboardButton(text="📋 Модераторы",
                                 callback_data="adm:list:moderator"),
        ])
    elif level == "admin":
        rows.append([InlineKeyboardButton(text="🛡 Назначить модератора",
                                          callback_data="adm:assign:moderator")])
        rows.append([InlineKeyboardButton(text="📋 Модераторы",
                                          callback_data="adm:list:moderator")])

    # Модератору доступна только статистика — доп. кнопок нет.
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text == "👑 Админ")
async def admin_menu_button(message: Message, state: FSMContext):
    """Обработчик кнопки главного меню."""
    if not await is_moderator(message.from_user.id):
        # Тихо игнорируем — у обычного юзера этой кнопки и не должно быть,
        # но на всякий случай (мог руками написать)
        return
    await state.clear()
    level = await get_admin_level(message.from_user.id)
    level_label = {"root": "🔴 Root", "admin": "🟠 Админ",
                    "moderator": "🟡 Модератор"}[level]
    await message.answer(
        f"👑 <b>Админ-меню</b>\nТвоя роль: {level_label}\n\n"
        "Выбери действие:",
        reply_markup=await _admin_menu_kb(message.from_user.id),
    )


@router.message(Command("admin"))
async def cmd_admin_menu(message: Message, state: FSMContext):
    """Аналог кнопки «👑 Админ» — команда."""
    await admin_menu_button(message, state)


@router.callback_query(F.data == "adm:stats")
async def admin_menu_stats(call: CallbackQuery):
    if not await is_moderator(call.from_user.id):
        await call.answer()
        return
    await call.answer("Загружаю…")
    # Вызываем существующую /stats — она отправит статистику новым сообщением
    fake_msg = call.message.model_copy(update={"from_user": call.from_user})
    await cmd_stats(fake_msg)


@router.callback_query(F.data == "adm:broadcast")
async def admin_menu_broadcast(call: CallbackQuery, state: FSMContext):
    """Открывает рассылку — только root."""
    if not is_root(call.from_user.id):
        await call.answer("Только root-админ может рассылать.", show_alert=True)
        return
    await call.answer()
    # Запускаем FSM рассылки как /rassylka
    await state.clear()
    await state.set_state(BroadcastForm.text)
    await bot.send_message(
        call.from_user.id,
        "📝 <b>Рассылка</b>\n\n"
        "Напиши текст (5–4000 символов).\n"
        "Можно HTML: <code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>, "
        "<code>&lt;a href='URL'&gt;</code>. В VK теги убираются.\n\n"
        "Отмена: /cancel",
        reply_markup=ReplyKeyboardRemove(),
    )


# ---- Назначение роли ----

@router.callback_query(F.data.startswith("adm:assign:"))
async def admin_menu_assign(call: CallbackQuery, state: FSMContext):
    """Кнопка «Назначить админа/модератора» — вход в FSM."""
    role = call.data.split(":")[2]  # 'admin' | 'moderator'

    # Проверка прав
    if role == "admin" and not is_root(call.from_user.id):
        await call.answer("Только root может назначать админов.", show_alert=True)
        return
    if role == "moderator" and not await is_admin(call.from_user.id):
        # is_admin включает root + admin — оба могут назначать модератора
        await call.answer("Только root или админ могут назначать модераторов.",
                          show_alert=True)
        return

    await call.answer()
    await state.set_state(AssignRoleForm.waiting_target)
    await state.update_data(assign_role=role)
    role_label = "админа" if role == "admin" else "модератора"
    await bot.send_message(
        call.from_user.id,
        f"👑 <b>Назначить {role_label}</b>\n\n"
        f"Пришли одно из:\n"
        f"• <code>123456</code> — TG ID\n"
        f"• <code>-123456</code> — VK ID (со знаком минус)\n"
        f"• <code>vk.com/id123456</code> — ссылка VK\n"
        f"• <code>@username</code> или <code>t.me/username</code> — TG\n\n"
        f"Юзер должен быть зарегистрирован в боте.\n\n"
        f"Отмена: /cancel",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(AssignRoleForm.waiting_target, Command("cancel"))
async def assign_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Назначение отменено.",
                         reply_markup=await menu_for(message.from_user.id))


@router.message(AssignRoleForm.waiting_target, F.text)
async def assign_target_input(message: Message, state: FSMContext):
    data = await state.get_data()
    role = data.get("assign_role")  # 'admin' | 'moderator'
    target_id = await parse_target_id((message.text or "").strip())
    if target_id is None:
        await message.answer(
            "Не распознал ID. Пришли ещё раз в правильном формате, "
            "или /cancel."
        )
        return

    # Проверка что юзер существует в БД
    target_user = await db.get_user(target_id)
    if not target_user:
        await message.answer(
            "❌ Этот пользователь не найден в боте.\n"
            "Он должен сначала зарегистрироваться (напишет /start в боте).\n\n"
            "Попробуй ещё раз или /cancel."
        )
        return

    # Проверка на root — root в БД не хранится, но лучше не путать
    if is_root(target_id):
        await message.answer("Это уже root-админ (из .env), назначать не нужно.")
        await state.clear()
        return

    # Назначаем
    await db.set_role(target_id, role, message.from_user.id)
    await state.clear()

    target_name = target_user.get("name") or f"id {target_id}"
    role_label = "админом" if role == "admin" else "модератором"
    await message.answer(
        f"✅ <b>{target_name}</b> (id {target_id}) назначен(а) {role_label}.",
        reply_markup=await menu_for(message.from_user.id),
    )

    # Уведомляем нового
    notify_text = (
        f"🎉 Тебя назначили <b>{role_label}</b> бота «Ковчег»!\n\n"
        f"Доступные команды:\n"
        f"• /stats — статистика\n"
        f"• /reports — жалобы\n"
        f"• /baninfo <ID> — анкета\n"
        f"• /userinfo <ID> — подробно\n"
        f"• /ban <ID> причина — забанить\n"
        f"• /unban <ID> — разбанить\n"
        f"• /banlist — список банов\n"
        f"• /admin — открыть меню"
    )
    if db.is_tg_user(target_id):
        try:
            await bot.send_message(target_id, notify_text)
        except Exception:
            pass
    else:
        # VK — через очередь
        # Упростим текст (VK не понимает HTML)
        vk_text = notify_text.replace("<b>", "").replace("</b>", "")
        await db.queue_system_message(target_id, vk_text)


# ---- Список админов/модераторов + разжалование ----

@router.callback_query(F.data.startswith("adm:list:"))
async def admin_menu_list(call: CallbackQuery):
    """Показать список админов/модераторов с кнопками разжалования."""
    role = call.data.split(":")[2]  # 'admin' | 'moderator'

    # Проверка прав на просмотр:
    # - список админов — только root
    # - список модераторов — root или admin
    if role == "admin" and not is_root(call.from_user.id):
        await call.answer("Только root видит список админов.", show_alert=True)
        return
    if role == "moderator" and not await is_admin(call.from_user.id):
        await call.answer("Только root или админ.", show_alert=True)
        return

    await call.answer()
    items = await db.list_admins_by_role(role)
    role_label = "Администраторы" if role == "admin" else "Модераторы"

    if not items:
        try:
            await call.message.edit_text(
                f"📋 <b>{role_label}</b>\n\n"
                "Список пуст.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="◀ Назад",
                                          callback_data="adm:back"),
                ]]),
            )
        except Exception:
            pass
        return

    # Строим кнопки разжалования
    rows = []
    lines = [f"📋 <b>{role_label}</b> ({len(items)}):", ""]
    for it in items:
        name = it.get("name") or "(без имени)"
        uid = it["user_id"]
        platform = "🌐 VK" if db.is_vk_user(uid) else "📱 TG"
        lines.append(f"• {platform} {name} — <code>{uid}</code>")
        # Кнопка разжаловать — только root, или admin (если разжаловать модератора)
        can_demote = is_root(call.from_user.id) or (
            role == "moderator" and await is_admin(call.from_user.id)
        )
        if can_demote:
            rows.append([InlineKeyboardButton(
                text=f"❌ Разжаловать {name}",
                callback_data=f"adm:demote:{uid}",
            )])
    rows.append([InlineKeyboardButton(text="◀ Назад", callback_data="adm:back")])

    try:
        await call.message.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("adm:demote:"))
async def admin_menu_demote(call: CallbackQuery):
    target_id = int(call.data.split(":")[2])

    # Проверка прав на разжалование:
    #   root разжалует любого
    #   admin разжалует только модератора
    target_role = await db.get_role(target_id)
    if target_role is None:
        await call.answer("Уже разжалован.", show_alert=True)
        return

    if is_root(call.from_user.id):
        pass  # root может всё
    elif await is_admin(call.from_user.id) and target_role == "moderator":
        pass  # admin разжалует модератора
    else:
        await call.answer("Нет прав.", show_alert=True)
        return

    await db.remove_role(target_id)
    await call.answer("Разжалован")

    target_user = await db.get_user(target_id)
    target_name = target_user.get("name") if target_user else f"id {target_id}"
    role_label = "админа" if target_role == "admin" else "модератора"
    try:
        await call.message.edit_text(
            f"❌ Разжаловал(а) {target_name} — больше не {role_label}.",
        )
    except Exception:
        pass

    # Уведомляем разжалованного
    notify_text = f"ℹ️ Тебя разжаловали из {role_label} бота «Ковчег»."
    if db.is_tg_user(target_id):
        try:
            await bot.send_message(target_id, notify_text)
        except Exception:
            pass
    else:
        await db.queue_system_message(target_id, notify_text)


@router.callback_query(F.data == "adm:back")
async def admin_menu_back(call: CallbackQuery):
    """Кнопка «Назад» — возврат в главное админ-меню."""
    if not await is_moderator(call.from_user.id):
        await call.answer()
        return
    await call.answer()
    level = await get_admin_level(call.from_user.id)
    level_label = {"root": "🔴 Root", "admin": "🟠 Админ",
                    "moderator": "🟡 Модератор"}[level]
    try:
        await call.message.edit_text(
            f"👑 <b>Админ-меню</b>\nТвоя роль: {level_label}\n\n"
            "Выбери действие:",
            reply_markup=await _admin_menu_kb(call.from_user.id),
        )
    except Exception:
        pass


# ============= РЕДАКТИРОВАНИЕ АНКЕТЫ =============
# Юзер жмёт «✏️ Редактировать анкету» → появляется меню с полями.
# Выбирает поле → бот просит новое значение → сохраняет → возвращается в меню.

def _edit_menu_kb() -> InlineKeyboardMarkup:
    """Клавиатура меню редактирования — список полей."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Имя", callback_data="edit:name"),
         InlineKeyboardButton(text="🎂 Возраст", callback_data="edit:age")],
        [InlineKeyboardButton(text="🔎 Возраст партнёра",
                              callback_data="edit:age_range")],
        [InlineKeyboardButton(text="📍 Город", callback_data="edit:city"),
         InlineKeyboardButton(text="✝️ Конфессия", callback_data="edit:denomination")],
        [InlineKeyboardButton(text="⛪ Церковь", callback_data="edit:church"),
         InlineKeyboardButton(text="🙏 Служение", callback_data="edit:church_role")],
        [InlineKeyboardButton(text="💼 Работа/учёба", callback_data="edit:job")],
        [InlineKeyboardButton(text="💍 Семейное", callback_data="edit:marital"),
         InlineKeyboardButton(text="👶 Дети", callback_data="edit:children")],
        [InlineKeyboardButton(text="📝 О себе", callback_data="edit:hobbies")],
        [InlineKeyboardButton(text="📷 Загрузить фото заново",
                              callback_data="edit:photos")],
        [InlineKeyboardButton(text="✅ Готово", callback_data="edit:close")],
    ])


def _edit_cancel_kb() -> InlineKeyboardMarkup:
    """Кнопка «Отмена» на любом шаге редактирования — вернуться в меню."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀ Отмена", callback_data="edit:cancel_field"),
    ]])


@router.callback_query(F.data == "edit:open")
async def edit_open(call: CallbackQuery, state: FSMContext):
    """Открытие меню редактирования — из «Моя анкета»."""
    u = await db.get_user(call.from_user.id)
    if not u:
        await call.answer("Сначала заполни анкету: /start", show_alert=True)
        return
    await call.answer()
    await state.clear()
    await bot.send_message(
        call.from_user.id,
        "✏️ <b>Редактирование анкеты</b>\n\n"
        "Выбери, что изменить:",
        reply_markup=_edit_menu_kb(),
    )


@router.callback_query(F.data == "edit:close")
async def edit_close(call: CallbackQuery, state: FSMContext):
    """Закрытие меню редактирования."""
    await call.answer("Готово")
    await state.clear()
    try:
        await call.message.edit_text("✅ Редактирование завершено.")
    except Exception:
        pass


@router.callback_query(F.data == "edit:cancel_field")
async def edit_cancel_field(call: CallbackQuery, state: FSMContext):
    """Отмена ввода конкретного поля — возврат в меню."""
    await call.answer()
    await state.clear()
    try:
        await call.message.edit_text(
            "✏️ <b>Редактирование анкеты</b>\n\nВыбери, что изменить:",
            reply_markup=_edit_menu_kb(),
        )
    except Exception:
        # если старое сообщение уже не редактируется — шлём новое
        await bot.send_message(
            call.from_user.id,
            "✏️ <b>Редактирование анкеты</b>\n\nВыбери, что изменить:",
            reply_markup=_edit_menu_kb(),
        )


# ---- Открытие каждого поля ----

@router.callback_query(F.data == "edit:name")
async def edit_field_name(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(EditProfileForm.edit_name)
    await bot.send_message(
        call.from_user.id,
        "✏️ Пришли <b>новое имя</b> (2-30 символов):",
        reply_markup=_edit_cancel_kb(),
    )


@router.callback_query(F.data == "edit:age")
async def edit_field_age(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(EditProfileForm.edit_age)
    await bot.send_message(
        call.from_user.id,
        "🎂 Пришли <b>новый возраст</b> (18-99):",
        reply_markup=_edit_cancel_kb(),
    )


@router.callback_query(F.data == "edit:age_range")
async def edit_field_age_range(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(EditProfileForm.edit_age_min)
    await bot.send_message(
        call.from_user.id,
        "🔎 Диапазон возраста партнёра.\n\n"
        "Сначала пришли <b>минимальный возраст</b> (например, 22):",
        reply_markup=_edit_cancel_kb(),
    )


@router.callback_query(F.data == "edit:city")
async def edit_field_city(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(EditProfileForm.edit_city)
    await bot.send_message(
        call.from_user.id,
        "📍 Пришли <b>новый город</b> (2-50 символов):",
        reply_markup=_edit_cancel_kb(),
    )


def _edit_denom_kb() -> InlineKeyboardMarkup:
    """Клавиатура выбора конфессии."""
    rows = []
    for i in range(0, len(DENOMINATIONS), 2):
        row = [InlineKeyboardButton(text=DENOMINATIONS[i],
                                     callback_data=f"edit:d:{i}")]
        if i + 1 < len(DENOMINATIONS):
            row.append(InlineKeyboardButton(
                text=DENOMINATIONS[i + 1],
                callback_data=f"edit:d:{i+1}",
            ))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="Другое (написать своё)",
                                       callback_data="edit:d:other")])
    rows.append([InlineKeyboardButton(text="◀ Отмена",
                                       callback_data="edit:cancel_field")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "edit:denomination")
async def edit_field_denomination(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await bot.send_message(
        call.from_user.id,
        "✝️ Выбери <b>конфессию</b>:",
        reply_markup=_edit_denom_kb(),
    )


@router.callback_query(F.data.startswith("edit:d:"))
async def edit_denom_selected(call: CallbackQuery, state: FSMContext):
    val = call.data.split(":", 2)[2]
    if val == "other":
        await call.answer()
        await state.set_state(EditProfileForm.edit_denomination_other)
        await bot.send_message(
            call.from_user.id,
            "Напиши <b>название конфессии</b> (2-50 символов):",
            reply_markup=_edit_cancel_kb(),
        )
        return
    # Выбрана стандартная
    try:
        idx = int(val)
        denom = DENOMINATIONS[idx]
    except Exception:
        await call.answer("Ошибка")
        return
    await call.answer(f"Сохранено: {denom}")
    await db.update_profile_fields(call.from_user.id, denomination=denom)
    await _edit_back_to_menu(call)


@router.callback_query(F.data == "edit:church")
async def edit_field_church(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(EditProfileForm.edit_church)
    await bot.send_message(
        call.from_user.id,
        "⛪ Пришли <b>название церкви</b> (2-100 символов):",
        reply_markup=_edit_cancel_kb(),
    )


@router.callback_query(F.data == "edit:church_role")
async def edit_field_church_role(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(EditProfileForm.edit_church_role)
    await bot.send_message(
        call.from_user.id,
        "🙏 Пришли <b>своё служение в церкви</b> (2-100 символов):",
        reply_markup=_edit_cancel_kb(),
    )


@router.callback_query(F.data == "edit:job")
async def edit_field_job(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(EditProfileForm.edit_job)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Убрать (не указывать)",
                              callback_data="edit:job:clear")],
        [InlineKeyboardButton(text="◀ Отмена", callback_data="edit:cancel_field")],
    ])
    await bot.send_message(
        call.from_user.id,
        "💼 Пришли <b>кем работаешь или на кого учишься</b> (2-100 символов).\n\n"
        "Или нажми «🗑 Убрать», чтобы поле осталось пустым:",
        reply_markup=kb,
    )


@router.callback_query(F.data == "edit:job:clear")
async def edit_job_clear(call: CallbackQuery, state: FSMContext):
    await call.answer("Убрано")
    await db.update_profile_fields(call.from_user.id, job=None)
    await _edit_back_to_menu(call)


def _edit_marital_kb(gender: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора семейного положения (с учётом пола)."""
    single_label = "Не женат" if gender == "M" else "Не замужем"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=single_label,
                              callback_data=f"edit:m:{single_label}")],
        [InlineKeyboardButton(text="В разводе",
                              callback_data="edit:m:В разводе")],
        [InlineKeyboardButton(text="Вдовец / Вдова",
                              callback_data="edit:m:Вдовец / Вдова")],
        [InlineKeyboardButton(text="◀ Отмена", callback_data="edit:cancel_field")],
    ])


@router.callback_query(F.data == "edit:marital")
async def edit_field_marital(call: CallbackQuery, state: FSMContext):
    u = await db.get_user(call.from_user.id)
    if not u:
        await call.answer()
        return
    await call.answer()
    await bot.send_message(
        call.from_user.id,
        "💍 Выбери <b>семейное положение</b>:",
        reply_markup=_edit_marital_kb(u["gender"]),
    )


@router.callback_query(F.data.startswith("edit:m:"))
async def edit_marital_selected(call: CallbackQuery, state: FSMContext):
    value = call.data.split(":", 2)[2]
    await call.answer(f"Сохранено: {value}")
    await db.update_profile_fields(call.from_user.id, marital=value)
    await _edit_back_to_menu(call)


@router.callback_query(F.data == "edit:children")
async def edit_field_children(call: CallbackQuery, state: FSMContext):
    await call.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Нет детей", callback_data="edit:c:Нет детей")],
        [InlineKeyboardButton(text="Есть, живут со мной",
                              callback_data="edit:c:Есть, живут со мной")],
        [InlineKeyboardButton(text="Есть, живут отдельно",
                              callback_data="edit:c:Есть, живут отдельно")],
        [InlineKeyboardButton(text="◀ Отмена", callback_data="edit:cancel_field")],
    ])
    await bot.send_message(
        call.from_user.id,
        "👶 Выбери:",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("edit:c:"))
async def edit_children_selected(call: CallbackQuery, state: FSMContext):
    value = call.data.split(":", 2)[2]
    await call.answer(f"Сохранено")
    await db.update_profile_fields(call.from_user.id, children=value)
    await _edit_back_to_menu(call)


@router.callback_query(F.data == "edit:hobbies")
async def edit_field_hobbies(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(EditProfileForm.edit_hobbies)
    await bot.send_message(
        call.from_user.id,
        "📝 Пришли <b>новое описание</b> «О себе» — минимум <b>10 слов</b>:",
        reply_markup=_edit_cancel_kb(),
    )


@router.callback_query(F.data == "edit:photos")
async def edit_field_photos(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(EditProfileForm.edit_photos)
    await state.update_data(new_photos=[])
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀ Отмена", callback_data="edit:cancel_field"),
    ]])
    await bot.send_message(
        call.from_user.id,
        "📷 <b>Загрузить фото заново</b>\n\n"
        "Пришли <b>2-5 фотографий</b> (по одной или сразу).\n"
        "Старые фото будут <b>заменены</b> на новые.\n\n"
        "Когда закончишь — нажми «✅ Готово» (появится после 2-х фото).",
        reply_markup=kb,
    )


async def _edit_back_to_menu(call: CallbackQuery):
    """Возврат в меню редактирования после сохранения одного поля."""
    try:
        await bot.send_message(
            call.from_user.id,
            "✅ Сохранено. Что ещё изменить?",
            reply_markup=_edit_menu_kb(),
        )
    except Exception:
        pass


# ---- Обработчики ввода текста для каждого поля ----

@router.message(EditProfileForm.edit_name, F.text)
async def edit_input_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not (2 <= len(name) <= 30):
        await message.answer("Имя от 2 до 30 символов. Попробуй ещё раз.",
                             reply_markup=_edit_cancel_kb())
        return
    await db.update_profile_fields(message.from_user.id, name=name)
    await state.clear()
    await message.answer(f"✅ Имя изменено на «{name}». Что ещё?",
                         reply_markup=_edit_menu_kb())


@router.message(EditProfileForm.edit_age, F.text)
async def edit_input_age(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    if not txt.isdigit():
        await message.answer("Возраст — целое число. Попробуй ещё раз.",
                             reply_markup=_edit_cancel_kb())
        return
    age = int(txt)
    if not (18 <= age <= 99):
        await message.answer("Возраст должен быть от 18 до 99.",
                             reply_markup=_edit_cancel_kb())
        return
    await db.update_profile_fields(message.from_user.id, age=age)
    await state.clear()
    await message.answer(f"✅ Возраст изменён на {age}. Что ещё?",
                         reply_markup=_edit_menu_kb())


@router.message(EditProfileForm.edit_age_min, F.text)
async def edit_input_age_min(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    if not txt.isdigit() or not (18 <= int(txt) <= 99):
        await message.answer("Возраст — число от 18 до 99. Попробуй ещё раз.",
                             reply_markup=_edit_cancel_kb())
        return
    await state.update_data(age_min=int(txt))
    await state.set_state(EditProfileForm.edit_age_max)
    await message.answer(
        f"Минимум: {txt}.\n\nТеперь пришли <b>максимальный возраст</b>:",
        reply_markup=_edit_cancel_kb(),
    )


@router.message(EditProfileForm.edit_age_max, F.text)
async def edit_input_age_max(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    if not txt.isdigit() or not (18 <= int(txt) <= 99):
        await message.answer("Возраст — число от 18 до 99.",
                             reply_markup=_edit_cancel_kb())
        return
    max_age = int(txt)
    data = await state.get_data()
    min_age = data.get("age_min", 18)
    if max_age < min_age:
        await message.answer(f"Максимум не может быть меньше минимума ({min_age}).",
                             reply_markup=_edit_cancel_kb())
        return
    await db.update_profile_fields(message.from_user.id,
                                    partner_age_min=min_age,
                                    partner_age_max=max_age)
    await state.clear()
    await message.answer(
        f"✅ Диапазон: {min_age}–{max_age}. Что ещё?",
        reply_markup=_edit_menu_kb(),
    )


@router.message(EditProfileForm.edit_city, F.text)
async def edit_input_city(message: Message, state: FSMContext):
    city = (message.text or "").strip()
    if not (2 <= len(city) <= 50):
        await message.answer("Город от 2 до 50 символов.",
                             reply_markup=_edit_cancel_kb())
        return
    await db.update_profile_fields(message.from_user.id, city=city)
    await state.clear()
    await message.answer(f"✅ Город: {city}. Что ещё?",
                         reply_markup=_edit_menu_kb())


@router.message(EditProfileForm.edit_denomination_other, F.text)
async def edit_input_denom_other(message: Message, state: FSMContext):
    denom = (message.text or "").strip()
    if not (2 <= len(denom) <= 50):
        await message.answer("От 2 до 50 символов.",
                             reply_markup=_edit_cancel_kb())
        return
    await db.update_profile_fields(message.from_user.id, denomination=denom)
    await state.clear()
    await message.answer(f"✅ Конфессия: {denom}. Что ещё?",
                         reply_markup=_edit_menu_kb())


@router.message(EditProfileForm.edit_church, F.text)
async def edit_input_church(message: Message, state: FSMContext):
    ch = (message.text or "").strip()
    if not (2 <= len(ch) <= 100):
        await message.answer("От 2 до 100 символов.",
                             reply_markup=_edit_cancel_kb())
        return
    await db.update_profile_fields(message.from_user.id, church=ch)
    await state.clear()
    await message.answer(f"✅ Церковь: {ch}. Что ещё?",
                         reply_markup=_edit_menu_kb())


@router.message(EditProfileForm.edit_church_role, F.text)
async def edit_input_church_role(message: Message, state: FSMContext):
    role = (message.text or "").strip()
    if not (2 <= len(role) <= 100):
        await message.answer("От 2 до 100 символов.",
                             reply_markup=_edit_cancel_kb())
        return
    await db.update_profile_fields(message.from_user.id, church_role=role)
    await state.clear()
    await message.answer(f"✅ Служение: {role}. Что ещё?",
                         reply_markup=_edit_menu_kb())


@router.message(EditProfileForm.edit_job, F.text)
async def edit_input_job(message: Message, state: FSMContext):
    job = (message.text or "").strip()
    if not (2 <= len(job) <= 100):
        await message.answer("От 2 до 100 символов.",
                             reply_markup=_edit_cancel_kb())
        return
    await db.update_profile_fields(message.from_user.id, job=job)
    await state.clear()
    await message.answer(f"✅ Работа/учёба: {job}. Что ещё?",
                         reply_markup=_edit_menu_kb())


@router.message(EditProfileForm.edit_hobbies, F.text)
async def edit_input_hobbies(message: Message, state: FSMContext):
    hobbies = (message.text or "").strip()
    if not (10 <= len(hobbies) <= 500):
        await message.answer("От 10 до 500 символов.",
                             reply_markup=_edit_cancel_kb())
        return
    word_count = db.count_meaningful_words(hobbies, db.HOBBIES_MIN_WORD_LEN)
    if word_count < db.HOBBIES_MIN_WORDS:
        await message.answer(
            f"Слишком коротко — минимум <b>{db.HOBBIES_MIN_WORDS} слов</b> "
            f"(у тебя {word_count}).\n\n"
            f"Расскажи подробнее о себе, работе, увлечениях, что важно "
            f"в отношениях.",
            reply_markup=_edit_cancel_kb(),
        )
        return
    await db.update_profile_fields(message.from_user.id, hobbies=hobbies)
    await state.clear()
    await message.answer("✅ Описание обновлено. Что ещё?",
                         reply_markup=_edit_menu_kb())


# ---- Загрузка фото заново ----

@router.message(EditProfileForm.edit_photos, F.photo)
async def edit_input_photos(message: Message, state: FSMContext):
    """Приём фото при редактировании — накапливаем во временном списке."""
    data = await state.get_data()
    new_photos = data.get("new_photos", [])
    if len(new_photos) >= 5:
        await message.answer("Уже 5 фото — больше не нужно. Нажми «✅ Готово».")
        return

    file_id = message.photo[-1].file_id
    user_id = message.from_user.id

    # Скачиваем во временную папку, потом переместим
    tmp_dir = _os.path.join("/tmp", f"edit_photos_{user_id}")
    _os.makedirs(tmp_dir, exist_ok=True)
    pos = len(new_photos)
    tmp_path = _os.path.join(tmp_dir, f"{pos}.jpg")

    ok = await photo_utils.download_tg_photo(bot, file_id, tmp_path)
    if not ok:
        logging.warning(f"edit: не смог скачать фото {file_id}")
        tmp_path = None

    new_photos.append({"photo_id": file_id, "file_path": tmp_path})
    await state.update_data(new_photos=new_photos)

    count = len(new_photos)
    if count < 2:
        text = f"Принято {count}/5. Нужно ещё минимум одно."
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀ Отмена",
                                  callback_data="edit:cancel_field"),
        ]])
    else:
        text = f"Принято {count}/5. Можно добавить ещё или нажать «Готово»."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✅ Готово ({count}/5)",
                                  callback_data="edit:photos_done")],
            [InlineKeyboardButton(text="◀ Отмена",
                                  callback_data="edit:cancel_field")],
        ])
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "edit:photos_done")
async def edit_photos_done(call: CallbackQuery, state: FSMContext):
    """Финализация — перемещаем фото в основную папку и обновляем БД."""
    data = await state.get_data()
    new_photos = data.get("new_photos", [])
    if len(new_photos) < 2:
        await call.answer("Нужно минимум 2 фото!", show_alert=True)
        return

    user_id = call.from_user.id
    await call.answer("Сохраняю…")

    # 1. Удаляем старые файлы из основной папки
    import shutil
    main_dir = db.user_photos_dir(user_id)
    if _os.path.exists(main_dir):
        try:
            for f in _os.listdir(main_dir):
                fp = _os.path.join(main_dir, f)
                if _os.path.isfile(fp):
                    _os.remove(fp)
        except Exception as e:
            logging.warning(f"edit photos: не смог очистить старые файлы: {e}")
    _os.makedirs(main_dir, exist_ok=True)

    # 2. Перемещаем новые из /tmp в основную папку
    final_photos = []
    for idx, p in enumerate(new_photos):
        src = p.get("file_path")
        target = _os.path.join(main_dir, f"{idx}.jpg")
        if src and _os.path.exists(src):
            try:
                shutil.move(src, target)
                final_photos.append({"photo_id": p["photo_id"],
                                      "file_path": target})
            except Exception as e:
                logging.warning(f"edit photos move failed: {e}")
                final_photos.append({"photo_id": p["photo_id"],
                                      "file_path": None})
        else:
            final_photos.append({"photo_id": p["photo_id"], "file_path": None})

    # 3. Удаляем tmp-папку
    tmp_dir = _os.path.join("/tmp", f"edit_photos_{user_id}")
    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception:
        pass

    # 4. Обновляем записи в БД (user_photos)
    # Нам нужна функция которая заменит фото у юзера — используем raw SQL
    await db.replace_user_photos(user_id, final_photos)

    await state.clear()
    try:
        await call.message.edit_text(
            f"✅ Фото обновлены ({len(final_photos)} шт.). Что ещё?",
            reply_markup=_edit_menu_kb(),
        )
    except Exception:
        await bot.send_message(
            user_id,
            f"✅ Фото обновлены ({len(final_photos)} шт.). Что ещё?",
            reply_markup=_edit_menu_kb(),
        )


@router.message(EditProfileForm.edit_photos)
async def edit_photos_fallback(message: Message, state: FSMContext):
    """Не-фото сообщение в состоянии загрузки фото."""
    await message.answer("Пришли <b>фотографию</b> (не текст).")


async def scheduler_loop():
    """Раз в минуту проверяем текущее московское время. Если пора — запускаем.
    Флаги дней (например, 2026-07-19) предотвращают повтор в одном дне."""
    global _last_weekly_run, _last_daily_likes_run, _last_hidden_prompt_run
    logging.info("Планировщик уведомлений запущен.")
    while True:
        try:
            now = datetime.now(MOSCOW_TZ)
            today_str = now.strftime("%Y-%m-%d")

            # Суббота = weekday() == 5 в питоне. 21:00 МСК.
            if (now.weekday() == 5 and now.hour == 21
                    and _last_weekly_run != today_str):
                _last_weekly_run = today_str
                logging.info("Планировщик: запуск weekly новых анкет")
                asyncio.create_task(send_weekly_new_profiles_notif())

            # Ежедневно 21:00 МСК — итог лайков
            if now.hour == 21 and _last_daily_likes_run != today_str:
                _last_daily_likes_run = today_str
                logging.info("Планировщик: запуск daily лайков")
                asyncio.create_task(send_daily_likes_notif())

            # Ежедневно 12:00 МСК — напоминание тем кто скрыл ≥30 дней
            if now.hour == 12 and _last_hidden_prompt_run != today_str:
                _last_hidden_prompt_run = today_str
                logging.info("Планировщик: запуск hidden 30d")
                asyncio.create_task(send_hidden_30days_prompt())

        except Exception as e:
            logging.exception(f"scheduler_loop error: {e}")
        await asyncio.sleep(60)


# ----------- Фоновая задача: доставка кросс-платформенных уведомлений -----------
async def deliver_pending_notifications():
    """Раз в 3 секунды смотрим в очередь pending_notifications.
    TG-бот доставляет:
      - match для TG-получателей (VK-получателей возьмёт VK-бот)
      - admin_report (админы всегда в Telegram)"""
    while True:
        try:
            notifications = await db.get_pending_notifications()
            for n in notifications:
                try:
                    if n["kind"] == "match":
                        recipient = n["recipient_id"]
                        if recipient is None or not db.is_tg_user(recipient):
                            # Это для VK-юзера — пропускаем, доставит VK-бот
                            continue
                        partner_id = n["partner_id"]
                        partner = await db.get_user(partner_id)
                        if partner:
                            text = (
                                f"🎉 <b>Вы понравились друг другу!</b>\n\n"
                                f"<b>{partner['name']}, {partner['age']}</b>\n"
                                f'Контакт: <a href="{db.contact_link(partner)}">'
                                f'{partner.get("name") or "написать"}</a>'
                            )
                            src = photo_source(partner.get("photo_path"),
                                                partner["photo_id"])
                            try:
                                await bot.send_photo(recipient, src, caption=text)
                            except Exception as e:
                                logging.warning(f"send_photo failed: {e}")
                                try:
                                    await bot.send_message(recipient, text)
                                except Exception:
                                    pass
                        await db.mark_notification_delivered(n["id"])

                    elif n["kind"] == "admin_report":
                        import json
                        payload = json.loads(n["payload"] or "{}")
                        reporter_id = payload.get("reporter_id")
                        target_id = payload.get("target_id")
                        total = payload.get("total", 0)
                        reason = payload.get("reason")
                        await notify_admins_about_report(
                            reporter_id, target_id, total, reason=reason,
                        )
                        await db.mark_notification_delivered(n["id"])

                    elif n["kind"] == "system_message":
                        recipient = n["recipient_id"]
                        # TG-бот обрабатывает только своих получателей.
                        # VK-получателей возьмёт VK-бот.
                        if recipient is None or not db.is_tg_user(recipient):
                            continue
                        import json
                        payload = json.loads(n["payload"] or "{}")
                        text = payload.get("text", "")
                        if text:
                            try:
                                await bot.send_message(recipient, text)
                            except Exception as e:
                                logging.warning(
                                    f"system_message TG to {recipient} failed: {e}"
                                )
                        await db.mark_notification_delivered(n["id"])

                    elif n["kind"] == "broadcast":
                        recipient = n["recipient_id"]
                        if recipient is None or not db.is_tg_user(recipient):
                            continue
                        import json
                        payload = json.loads(n["payload"] or "{}")
                        text = payload.get("text", "")
                        if text:
                            try:
                                await bot.send_message(recipient, text)
                            except Exception as e:
                                logging.warning(
                                    f"broadcast TG to {recipient} failed: {e}"
                                )
                        await asyncio.sleep(0.05)  # лимит ~20/сек
                        await db.mark_notification_delivered(n["id"])
                except Exception as e:
                    logging.exception(f"Доставка уведомления {n['id']} упала: {e}")
                    await db.mark_notification_delivered(n["id"])
        except Exception as e:
            logging.exception(f"deliver_pending_notifications loop error: {e}")
        await asyncio.sleep(3)


# ----------- Запуск -----------
async def main():
    await db.init_db()
    db.ensure_photos_dir()
    logging.info("База данных готова. Запускаем бота…")
    await bot.delete_webhook(drop_pending_updates=True)
    # Фоновая задача доставки кросс-платформенных уведомлений
    asyncio.create_task(deliver_pending_notifications())
    # Планировщик еженедельных / ежедневных уведомлений
    asyncio.create_task(scheduler_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
