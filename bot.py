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


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


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
    if is_admin(user_id):
        return []  # админам не мешаем

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
        if user and not is_admin(user.id):
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
    church = State()
    marital = State()
    children = State()
    hobbies = State()
    photo = State()


class ReportForm(StatesGroup):
    """Отдельный FSM для жалобы — пользователь нажал 🚩 и теперь пишет причину."""
    reason = State()


# ----------- Клавиатуры -----------
def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Смотреть анкеты")],
            [KeyboardButton(text="👤 Моя анкета"), KeyboardButton(text="💌 Мои матчи")],
            [KeyboardButton(text="✏️ Заполнить заново")],
        ],
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
            reply_markup=main_menu_kb(),
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
            reply_markup=main_menu_kb(),
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
                reply_markup=main_menu_kb(),
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
        "<b>Какую церковь посещаешь?</b>\n"
        "Можно указать название и/или конфессию (например: «Слово жизни, г. Москва» "
        "или «Баптистская церковь Благодать»)."
    )
    await state.set_state(Form.church)


@router.message(Form.church)
async def form_church(message: Message, state: FSMContext):
    church = (message.text or "").strip()
    if not (2 <= len(church) <= 100):
        await message.answer("Название церкви от 2 до 100 символов.")
        return
    await state.update_data(church=church)
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
        "<b>Расскажи о своих хобби и увлечениях</b> (1–500 символов).\n"
        "Например: «Люблю читать, играю на гитаре в служении, увлекаюсь походами».",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(Form.hobbies)


@router.message(Form.hobbies)
async def form_hobbies(message: Message, state: FSMContext):
    hobbies = (message.text or "").strip()
    if not (1 <= len(hobbies) <= 500):
        await message.answer("От 1 до 500 символов. Попробуй ещё раз.")
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
        reply_markup=main_menu_kb(),
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
    return (
        f"<b>{u['name']}, {u['age']}</b>\n"
        f"📍 {u['city']}\n"
        f"⛪ {u['church']}\n"
        f"💍 {u['marital']}\n"
        f"👶 {u['children']}\n\n"
        f"<b>О себе:</b>\n{u['hobbies']}"
    )


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


@router.message(F.text == "🔍 Смотреть анкеты")
async def browse_profiles(message: Message):
    if not await require_subscription(message):
        return
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала заполни анкету: /start")
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
async def my_profile(message: Message):
    u = await db.get_user(message.from_user.id)
    if not u:
        await message.answer("Анкеты ещё нет. /start чтобы создать.")
        return
    photos = await db.get_user_photos_with_paths(message.from_user.id)
    if not photos:
        photos = [{"photo_id": u["photo_id"], "file_path": u.get("photo_path")}]

    # К стандартной подписи добавляем личный фильтр по возрасту партнёра
    # (его видит только сам пользователь — другим это не показывается)
    caption = format_profile(u)
    age_min = u.get("partner_age_min")
    age_max = u.get("partner_age_max")
    if age_min and age_max:
        partner = "девушки" if u["gender"] == "M" else "молодого человека"
        caption += f"\n\n🔎 <b>Ищу возраст {partner}:</b> {age_min}–{age_max} лет"

    if len(photos) == 1:
        src = photo_source_from_dict(photos[0])
        await message.answer_photo(src, caption=caption,
                                   reply_markup=main_menu_kb())
    else:
        # Медиа-группа: подпись на первом фото
        media = [InputMediaPhoto(media=photo_source_from_dict(photos[0]),
                                 caption=caption, parse_mode="HTML")]
        for p in photos[1:]:
            media.append(InputMediaPhoto(media=photo_source_from_dict(p)))
        await message.answer_media_group(media)
        await message.answer(f"Загружено фото: {len(photos)}.",
                             reply_markup=main_menu_kb())


# ----------- Список матчей -----------
@router.message(F.text == "💌 Мои матчи")
async def my_matches(message: Message):
    matches = await db.get_matches(message.from_user.id)
    if not matches:
        await message.answer("Пока ни одного матча. Лайкай анкеты — найдёшь!",
                             reply_markup=main_menu_kb())
        return
    lines = ["<b>Твои матчи:</b>\n"]
    for m in matches:
        contact = (f'@{m["username"]}' if m["username"]
                   else f'(без username, id {m["user_id"]})')
        lines.append(f"• {m['name']}, {m['age']} — {contact}")
    await message.answer("\n".join(lines), reply_markup=main_menu_kb())


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
    if not is_admin(message.from_user.id):
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
    if not is_admin(message.from_user.id):
        return
    s = await db.get_stats()

    def fmt_top(items: list, label: str) -> str:
        if not items:
            return f"<i>нет {label}</i>"
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

        "⛪ <b>Топ церквей</b>\n"
        f"{fmt_top(s['top_churches'], 'данных')}\n\n"

        "🛡 <b>Модерация</b>\n"
        f"  Жалоб всего: {s['reports_total']}\n"
        f"  Жалоб за 24 часа: <b>{s['reports_24h']}</b>\n"
        f"  Забанено: {s['banned_total']}"
    )
    await message.answer(text)


@router.message(Command("ban"))
async def cmd_ban(message: Message):
    if not is_admin(message.from_user.id):
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

    if is_admin(target_id):
        await message.answer("⚠️ Нельзя забанить админа.")
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
    if not is_admin(message.from_user.id):
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
    if not is_admin(message.from_user.id):
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
    if not is_admin(message.from_user.id):
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
        f"⛪ {u['church']}\n"
        f"💍 {u['marital']}\n"
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
    if not is_admin(message.from_user.id):
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
            f"⛪ {u['church']}\n"
            f"💍 {u['marital']}\n"
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
    if not is_admin(message.from_user.id):
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
    if not is_admin(message.from_user.id):
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
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
