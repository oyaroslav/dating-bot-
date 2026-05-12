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
    TelegramObject, InputMediaPhoto,
)
from typing import Any, Awaitable, Callable, Dict
from dotenv import load_dotenv

import database as db

# ----------- Настройка -----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден. Создай файл .env и положи туда токен.")

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

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# ----------- Состояния анкеты (FSM) -----------
class Form(StatesGroup):
    name = State()
    age = State()
    gender = State()
    looking_for = State()
    city = State()
    church = State()
    marital = State()
    children = State()
    hobbies = State()
    photo = State()


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


def looking_for_kb(gender: str) -> ReplyKeyboardMarkup:
    """Показываем только противоположный пол.
    Мужчине — кнопку «Женщин», женщине — кнопку «Мужчин»."""
    button = KeyboardButton(text="Женщин") if gender == "M" else KeyboardButton(text="Мужчин")
    return ReplyKeyboardMarkup(
        keyboard=[[button]],
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
    # Проверка обязательной подписки на каналы
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
    await state.update_data(gender=gender)
    # Автоматически устанавливаем поиск противоположного пола.
    # Однополый поиск в этом боте не предусмотрен.
    opposite = "F" if gender == "M" else "M"
    await state.update_data(looking_for=opposite)
    prompt = ("<b>Кого ищешь?</b>\nДля подтверждения нажми кнопку ниже."
              if gender == "M"
              else "<b>Кого ищешь?</b>\nДля подтверждения нажми кнопку ниже.")
    await message.answer(prompt, reply_markup=looking_for_kb(gender))
    await state.set_state(Form.looking_for)


@router.message(Form.looking_for)
async def form_looking_for(message: Message, state: FSMContext):
    data = await state.get_data()
    gender = data.get("gender")
    expected = "Женщин" if gender == "M" else "Мужчин"
    if message.text != expected:
        await message.answer("Нажми на кнопку.", reply_markup=looking_for_kb(gender))
        return
    # looking_for уже установлен в form_gender, просто переходим дальше
    await message.answer("<b>Из какого ты города?</b>",
                         reply_markup=ReplyKeyboardRemove())
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
    photos.append(message.photo[-1].file_id)
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
        photos.pop()
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
    await db.save_user(
        user_id=call.from_user.id,
        username=call.from_user.username,
        name=data["name"],
        age=data["age"],
        gender=data["gender"],
        looking_for=data["looking_for"],
        city=data["city"],
        church=data["church"],
        marital=data["marital"],
        children=data["children"],
        hobbies=data["hobbies"],
        photos=photos,
    )
    await state.clear()
    await call.message.edit_reply_markup(reply_markup=None)
    await bot.send_message(
        call.from_user.id,
        f"✅ Анкета сохранена! Загружено фото: {len(photos)}.\n\n"
        "Жми «🔍 Смотреть анкеты», чтобы начать знакомиться. "
        "Если кто-то ответит взаимностью — я пришлю тебе его контакт.",
        reply_markup=main_menu_kb(),
    )


@router.message(Form.photo)
async def form_photo_invalid(message: Message):
    await message.answer("Это не фото. Пришли изображением, не файлом.")


# ----------- Формат анкеты для показа -----------
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
    photos = await db.get_user_photos(target_user_id)
    if not photos:
        photos = [profile["photo_id"]]
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

    if edit_message is not None:
        try:
            await edit_message.edit_media(
                media=InputMediaPhoto(media=photos[photo_idx], caption=caption,
                                      parse_mode="HTML"),
                reply_markup=kb,
            )
            return True
        except Exception as e:
            # если не получилось отредактировать (старое сообщение и т.п.) — шлём новое
            logging.warning(f"edit_media failed: {e}")

    await bot.send_photo(chat_id, photo=photos[photo_idx],
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
async def on_swipe(call: CallbackQuery):
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

    # Жалоба — записываем в базу и уведомляем админов
    if action == "report":
        if not target_id:
            await call.answer("Сначала открой анкету.", show_alert=True)
            return
        already = await db.has_reported(user_id, target_id)
        if already:
            await call.answer("Ты уже жаловался на эту анкету.", show_alert=True)
            return
        await db.add_report(user_id, target_id)
        total = await db.count_reports(target_id)
        await call.answer("🚩 Жалоба принята. Спасибо!", show_alert=True)
        await notify_admins_about_report(user_id, target_id, total)
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
    """Уведомляем обоих о взаимном лайке."""
    a = await db.get_user(user_a_id)
    b = await db.get_user(user_b_id)
    if not a or not b:
        return

    def contact_link(u):
        if u["username"]:
            return f'<a href="https://t.me/{u["username"]}">@{u["username"]}</a>'
        return f'<a href="tg://user?id={u["user_id"]}">написать в Telegram</a>'

    text_for_a = (
        f"🎉 <b>Вы понравились друг другу!</b>\n\n"
        f"<b>{b['name']}, {b['age']}</b>\n"
        f"Контакт: {contact_link(b)}"
    )
    text_for_b = (
        f"🎉 <b>Вы понравились друг другу!</b>\n\n"
        f"<b>{a['name']}, {a['age']}</b>\n"
        f"Контакт: {contact_link(a)}"
    )
    try:
        await bot.send_photo(user_a_id, b["photo_id"], caption=text_for_a)
    except Exception as e:
        logging.warning(f"Не смог уведомить {user_a_id}: {e}")
    try:
        await bot.send_photo(user_b_id, a["photo_id"], caption=text_for_b)
    except Exception as e:
        logging.warning(f"Не смог уведомить {user_b_id}: {e}")


# ----------- Моя анкета -----------
@router.message(F.text == "👤 Моя анкета")
async def my_profile(message: Message):
    u = await db.get_user(message.from_user.id)
    if not u:
        await message.answer("Анкеты ещё нет. /start чтобы создать.")
        return
    photos = await db.get_user_photos(message.from_user.id)
    if not photos:
        photos = [u["photo_id"]]

    caption = format_profile(u)
    if len(photos) == 1:
        await message.answer_photo(photos[0], caption=caption,
                                   reply_markup=main_menu_kb())
    else:
        # Медиа-группа: подпись на первом фото
        media = [InputMediaPhoto(media=photos[0], caption=caption,
                                 parse_mode="HTML")]
        for p in photos[1:]:
            media.append(InputMediaPhoto(media=p))
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
async def notify_admins_about_report(reporter_id: int, target_id: int, total: int):
    """Шлём всем админам уведомление о новой жалобе."""
    if not ADMIN_IDS:
        return
    target = await db.get_user(target_id)
    reporter = await db.get_user(reporter_id)
    target_name = target["name"] if target else f"id {target_id}"
    reporter_name = reporter["name"] if reporter else f"id {reporter_id}"
    text = (
        f"🚩 <b>Новая жалоба</b>\n\n"
        f"От: <b>{reporter_name}</b> (id {reporter_id})\n"
        f"На: <b>{target_name}</b> (id {target_id})\n"
        f"Всего жалоб на этого пользователя: <b>{total}</b>\n\n"
        f"Чтобы посмотреть анкету: <code>/baninfo {target_id}</code>\n"
        f"Чтобы забанить: <code>/ban {target_id} причина</code>"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception as e:
            logging.warning(f"Не смог уведомить админа {admin_id}: {e}")


# ----------- Админ-команды -----------
@router.message(Command("admin"))
async def cmd_admin(message: Message):
    """Справка по командам админа."""
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "<b>Админ-команды:</b>\n\n"
        "<code>/ban ID причина</code> — забанить пользователя\n"
        "<code>/unban ID</code> — разбанить\n"
        "<code>/banlist</code> — список забаненных\n"
        "<code>/baninfo ID</code> — посмотреть анкету и жалобы\n"
        "<code>/reports</code> — последние жалобы\n\n"
        f"Твой ID: <code>{message.from_user.id}</code>"
    )


@router.message(Command("ban"))
async def cmd_ban(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: <code>/ban ID причина</code>")
        return
    target_id = int(parts[1])
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

    # Сообщаем забаненному
    try:
        await bot.send_message(
            target_id,
            f"⛔ Ты заблокирован администратором.\n"
            f"Причина: {reason}\n\n"
            f"Если считаешь это ошибкой — напиши администратору."
        )
    except Exception:
        pass  # пользователь мог заблокировать бота


@router.message(Command("unban"))
async def cmd_unban(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: <code>/unban ID</code>")
        return
    target_id = int(parts[1])
    ok = await db.unban_user(target_id)
    if ok:
        await message.answer(f"✅ Пользователь id {target_id} разбанен.")
        try:
            await bot.send_message(target_id, "✅ Тебя разбанили. Можешь снова пользоваться ботом.")
        except Exception:
            pass
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
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: <code>/baninfo ID</code>")
        return
    target_id = int(parts[1])
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
        await message.answer_photo(u["photo_id"], caption=caption)
    except Exception:
        await message.answer(caption)


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
        lines.append(
            f"• <b>{target_name}</b> (id {r['target_id']}) ← от {reporter_name}"
        )
    lines.append("\nПодробнее: <code>/baninfo ID</code>")
    await message.answer("\n".join(lines))


# ----------- Запуск -----------
async def main():
    await db.init_db()
    logging.info("База данных готова. Запускаем бота…")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
