"""
Telegram Dating Bot — бот знакомств.
Анкета: Имя, Возраст, Пол, Кого ищет, Город, Церковь,
        Семейное положение, Дети, Хобби, Фото.
Логика: лайки/дизлайки → при взаимном лайке оба получают контакт другого.
"""
import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove,
)
from dotenv import load_dotenv

import database as db

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


def swipe_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="◀", callback_data="swipe:prev"),
            InlineKeyboardButton(text="❌", callback_data="swipe:dislike"),
            InlineKeyboardButton(text="❤️", callback_data="swipe:like"),
            InlineKeyboardButton(text="▶", callback_data="swipe:next"),
        ],
        [InlineKeyboardButton(text="⏹ Стоп", callback_data="swipe:stop")],
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
    await message.answer("<b>Пришли своё фото</b> (одно).")
    await state.set_state(Form.photo)


@router.message(Form.photo, F.photo)
async def form_photo(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    data = await state.get_data()
    await db.save_user(
        user_id=message.from_user.id,
        username=message.from_user.username,
        name=data["name"],
        age=data["age"],
        gender=data["gender"],
        looking_for=data["looking_for"],
        city=data["city"],
        church=data["church"],
        marital=data["marital"],
        children=data["children"],
        hobbies=data["hobbies"],
        photo_id=photo_id,
    )
    await state.clear()
    await message.answer(
        "✅ Анкета сохранена!\n\n"
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


# ----------- Показ анкет -----------
async def show_next_profile(user_id: int, chat_id: int):
    profile = await db.get_next_profile(user_id)
    if not profile:
        await bot.send_message(
            chat_id,
            "🤷 Анкеты закончились. Загляни позже — появятся новые!",
            reply_markup=main_menu_kb(),
        )
        return
    await bot.send_photo(
        chat_id,
        photo=profile["photo_id"],
        caption=format_profile(profile),
        reply_markup=swipe_kb(),
    )


@router.message(F.text == "🔍 Смотреть анкеты")
async def browse_profiles(message: Message):
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала заполни анкету: /start")
        return
    await show_next_profile(message.from_user.id, message.chat.id)


# ----------- Свайпы и листание -----------
@router.callback_query(F.data.startswith("swipe:"))
async def on_swipe(call: CallbackQuery):
    action = call.data.split(":")[1]
    await call.answer()

    if action == "stop":
        await call.message.edit_reply_markup(reply_markup=None)
        await bot.send_message(call.from_user.id, "Окей, остановились.",
                               reply_markup=main_menu_kb())
        return

    target_id = await db.get_last_shown(call.from_user.id)

    # Стрелки — просто листание без отметки в базе
    if action == "next":
        await call.message.edit_reply_markup(reply_markup=None)
        await show_next_profile(call.from_user.id, call.message.chat.id)
        return

    if action == "prev":
        await call.message.edit_reply_markup(reply_markup=None)
        prev = await db.get_prev_profile(call.from_user.id)
        if not prev:
            await bot.send_message(
                call.from_user.id,
                "Это первая анкета — назад уже некуда.",
                reply_markup=main_menu_kb(),
            )
            return
        await bot.send_photo(
            call.message.chat.id,
            photo=prev["photo_id"],
            caption=format_profile(prev),
            reply_markup=swipe_kb(),
        )
        return

    # Лайк/дизлайк — отмечаем выбор и идём дальше
    if not target_id:
        await call.message.edit_reply_markup(reply_markup=None)
        await show_next_profile(call.from_user.id, call.message.chat.id)
        return

    if action == "like":
        is_match = await db.add_like(call.from_user.id, target_id)
        if is_match:
            await notify_match(call.from_user.id, target_id)
    elif action == "dislike":
        await db.add_dislike(call.from_user.id, target_id)

    await call.message.edit_reply_markup(reply_markup=None)
    await show_next_profile(call.from_user.id, call.message.chat.id)


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
    await message.answer_photo(
        u["photo_id"], caption=format_profile(u), reply_markup=main_menu_kb(),
    )


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


# ----------- Запуск -----------
async def main():
    await db.init_db()
    logging.info("База данных готова. Запускаем бота…")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
