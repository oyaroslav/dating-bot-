"""
VK Dating Bot — ВКонтакте версия бота знакомств «Ковчег».

Работает в паре с Telegram-ботом через общую базу данных.
Пользователи могут регистрироваться в любой платформе — анкеты в общем котле,
матчи возможны между TG и VK.

Архитектура:
  - VK user_id хранится в БД как ОТРИЦАТЕЛЬНОЕ число.
  - Фото скачиваются с серверов ВК и лежат в /root/dating_bot/photos/<abs(db_id)>/.
  - Long Poll в фоновом потоке, события через asyncio.Queue.
"""
import asyncio
import json
import logging
import os
import random
import threading
import queue
from typing import Optional

import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
from dotenv import load_dotenv

import database as db
import photo_utils


# ----------- Настройка -----------
load_dotenv()
VK_TOKEN = os.getenv("VK_TOKEN", "").strip()
VK_GROUP_ID = os.getenv("VK_GROUP_ID", "").strip()
VK_REQUIRED_GROUP = os.getenv("VK_REQUIRED_GROUP", "").strip()

if not VK_TOKEN or not VK_GROUP_ID:
    raise RuntimeError(
        "VK_TOKEN или VK_GROUP_ID не заданы в .env."
    )

VK_GROUP_ID_INT = int(VK_GROUP_ID)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [VK] %(levelname)s: %(message)s",
)
log = logging.getLogger("vk_bot")


# ----------- Тексты документов -----------
def _load_doc(filename: str, fallback: str) -> str:
    path = os.path.join(os.path.dirname(__file__), filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        log.warning(f"Файл {filename} не найден.")
        return fallback


PRIVACY_POLICY_TEXT = _load_doc(
    "PRIVACY_POLICY.md", "Текст политики не настроен."
)
USER_AGREEMENT_TEXT = _load_doc(
    "USER_AGREEMENT.md", "Текст соглашения не настроен."
)

VK_MSG_LIMIT = 4000


def split_for_vk(text: str, limit: int = VK_MSG_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts, buf, cur_len = [], [], 0
    for line in text.split("\n"):
        if cur_len + len(line) + 1 > limit and buf:
            parts.append("\n".join(buf))
            buf, cur_len = [], 0
        buf.append(line)
        cur_len += len(line) + 1
    if buf:
        parts.append("\n".join(buf))
    return parts


# ----------- Подключение к VK -----------
vk_session = vk_api.VkApi(token=VK_TOKEN)
vk = vk_session.get_api()
longpoll = VkBotLongPoll(vk_session, VK_GROUP_ID_INT)


# ----------- FSM в памяти -----------
# {vk_user_id: {"state": "name|age|gender|...", "data": {...}}}
user_states: dict[int, dict] = {}

# Состояния регистрации
STATES = (
    "name", "age", "gender",
    "partner_age_min", "partner_age_max",
    "city", "church", "marital", "children", "hobbies", "photo",
)


def get_state(vk_user_id: int) -> Optional[str]:
    return user_states.get(vk_user_id, {}).get("state")


def set_state(vk_user_id: int, state: Optional[str], **data):
    if state is None:
        user_states.pop(vk_user_id, None)
        return
    existing = user_states.get(vk_user_id, {"data": {}})
    existing["state"] = state
    existing["data"].update(data)
    user_states[vk_user_id] = existing


def update_data(vk_user_id: int, **kwargs):
    existing = user_states.get(vk_user_id, {"state": None, "data": {}})
    existing["data"].update(kwargs)
    user_states[vk_user_id] = existing


def get_data(vk_user_id: int) -> dict:
    return user_states.get(vk_user_id, {}).get("data", {})


# ----------- Отправка сообщений -----------
def send_message(vk_user_id: int, text: str, keyboard=None, attachment=None):
    """Отправляет сообщение пользователю VK. Возвращает message_id (int) или None.
    message_id нужен, чтобы потом удалить сообщение через messages.delete."""
    params = {
        "user_id": vk_user_id,
        "message": text,
        "random_id": random.randint(1, 2**31 - 1),
    }
    if keyboard is not None:
        if hasattr(keyboard, "get_keyboard"):
            params["keyboard"] = keyboard.get_keyboard()
        else:
            params["keyboard"] = keyboard
    if attachment:
        params["attachment"] = attachment
    try:
        # vk.messages.send возвращает int — id отправленного сообщения
        return vk.messages.send(**params)
    except vk_api.exceptions.ApiError as e:
        log.warning(f"Не смог отправить {vk_user_id}: {e}")
        return None


def delete_message_silently(message_id: int):
    """Тихо удаляет сообщение бота (delete_for_all=1 — без следа в чате).
    Если не получилось (прошло >24ч, уже удалено, и т.п.) — просто игнорируем."""
    if not message_id:
        return
    try:
        vk.messages.delete(message_ids=message_id, delete_for_all=1)
    except Exception as e:
        log.debug(f"Не смог удалить сообщение {message_id}: {e}")


def send_long(vk_user_id: int, text: str, keyboard=None):
    parts = split_for_vk(text)
    for i, p in enumerate(parts):
        kb = keyboard if i == len(parts) - 1 else None
        send_message(vk_user_id, p, keyboard=kb)


# ----------- Клавиатуры -----------
def main_menu_keyboard() -> VkKeyboard:
    kb = VkKeyboard(one_time=False)
    kb.add_button("🔍 Смотреть анкеты", color=VkKeyboardColor.PRIMARY)
    kb.add_line()
    kb.add_button("👤 Моя анкета")
    kb.add_button("💌 Мои матчи")
    kb.add_line()
    kb.add_button("✏️ Заполнить заново", color=VkKeyboardColor.SECONDARY)
    return kb


def gender_keyboard() -> VkKeyboard:
    kb = VkKeyboard(one_time=True)
    kb.add_button("Мужчина", color=VkKeyboardColor.PRIMARY)
    kb.add_button("Женщина", color=VkKeyboardColor.PRIMARY)
    return kb


def marital_keyboard(gender: Optional[str] = None) -> VkKeyboard:
    """Клавиатура семейного положения.
    Для мужчин показываем «Не женат», для женщин — «Не замужем».
    Если gender не задан — показываем общий вариант (для обратной совместимости)."""
    kb = VkKeyboard(one_time=True)
    if gender == "M":
        kb.add_button("Не женат")
    elif gender == "F":
        kb.add_button("Не замужем")
    else:
        kb.add_button("Не женат / Не замужем")
    kb.add_line()
    kb.add_button("В разводе")
    kb.add_button("Вдовец / Вдова")
    return kb


def children_keyboard() -> VkKeyboard:
    kb = VkKeyboard(one_time=True)
    kb.add_button("Нет детей")
    kb.add_line()
    kb.add_button("Есть дети")
    return kb


def photo_done_keyboard(count: int) -> VkKeyboard:
    kb = VkKeyboard(inline=True)
    if count >= 2:
        kb.add_callback_button(
            f"✅ Готово ({count}/5)", color=VkKeyboardColor.POSITIVE,
            payload={"cmd": "photos_done"},
        )
        kb.add_line()
    if count > 0:
        kb.add_callback_button(
            "🗑 Удалить последнее", color=VkKeyboardColor.SECONDARY,
            payload={"cmd": "photos_undo"},
        )
    return kb


def consent_keyboard() -> VkKeyboard:
    kb = VkKeyboard(inline=True)
    kb.add_callback_button("📋 Читать политику", color=VkKeyboardColor.SECONDARY,
                           payload={"cmd": "show_privacy"})
    kb.add_line()
    kb.add_callback_button("📜 Читать соглашение", color=VkKeyboardColor.SECONDARY,
                           payload={"cmd": "show_agreement"})
    kb.add_line()
    kb.add_callback_button("✅ Принимаю", color=VkKeyboardColor.POSITIVE,
                           payload={"cmd": "consent_accept"})
    kb.add_line()
    kb.add_callback_button("❌ Отказываюсь", color=VkKeyboardColor.NEGATIVE,
                           payload={"cmd": "consent_decline"})
    return kb


def empty_keyboard() -> str:
    return VkKeyboard.get_empty_keyboard()


# ----------- Информация о пользователе VK -----------
def get_vk_user_info(vk_user_id: int) -> dict:
    try:
        users = vk.users.get(
            user_ids=vk_user_id,
            fields="screen_name,first_name,last_name",
        )
        if users:
            return users[0]
    except vk_api.exceptions.ApiError as e:
        log.warning(f"users.get failed for {vk_user_id}: {e}")
    return {"id": vk_user_id, "first_name": "Пользователь", "last_name": ""}


# ----------- Проверка согласия / подписки -----------
async def require_consent(vk_user_id: int) -> bool:
    db_id = db.vk_id_to_db_id(vk_user_id)
    if await db.has_consent(db_id):
        return True
    send_message(
        vk_user_id,
        "📋 Согласие на обработку персональных данных\n\n"
        "Для пользования ботом необходимо согласие на обработку "
        "персональных данных в соответствии с ФЗ № 152-ФЗ.\n\n"
        "Бот собирает: имя, возраст, пол, город, церковь, семейное положение, "
        "наличие детей, описание, фотографии. Эти данные показываются другим "
        "пользователям бота для знакомства.\n\n"
        "Ознакомься с документами по кнопкам ниже, потом нажми «Принимаю».",
        keyboard=consent_keyboard(),
    )
    return False


def is_subscribed_to_group(vk_user_id: int) -> bool:
    if not VK_REQUIRED_GROUP:
        return True
    try:
        result = vk.groups.isMember(
            group_id=VK_REQUIRED_GROUP, user_id=vk_user_id,
        )
        return bool(result)
    except vk_api.exceptions.ApiError as e:
        log.warning(f"groups.isMember failed for {vk_user_id}: {e}")
        return True


async def require_subscription(vk_user_id: int) -> bool:
    if is_subscribed_to_group(vk_user_id):
        return True
    kb = VkKeyboard(inline=True)
    kb.add_openlink_button(
        "📢 Открыть сообщество",
        link=f"https://vk.com/{VK_REQUIRED_GROUP}",
    )
    kb.add_line()
    kb.add_callback_button(
        "✅ Я подписался",
        color=VkKeyboardColor.POSITIVE,
        payload={"cmd": "check_sub"},
    )
    send_message(
        vk_user_id,
        "📢 Для пользования ботом нужна подписка на наше сообщество.\n\n"
        "Подпишись и нажми «✅ Я подписался».",
        keyboard=kb,
    )
    return False


# ============= РЕГИСТРАЦИЯ =============

async def start_registration(vk_user_id: int):
    """Запуск анкеты с нуля. Подставляем имя из VK как подсказку."""
    info = get_vk_user_info(vk_user_id)
    default_name = info.get("first_name", "")
    # Удаляем старые фото пользователя если есть (для регистрации заново)
    folder = db.user_photos_dir(db.vk_id_to_db_id(vk_user_id))
    import shutil
    if os.path.exists(folder):
        try:
            shutil.rmtree(folder)
            os.makedirs(folder, exist_ok=True)
        except Exception as e:
            log.warning(f"Не смог очистить {folder}: {e}")

    set_state(vk_user_id, "name", default_name=default_name)
    send_message(
        vk_user_id,
        f"Заполним анкету.\n\n"
        f"Как тебя зовут?\n"
        f"(в VK у тебя «{default_name}» — можешь просто написать «{default_name}» или другое имя)",
        keyboard=empty_keyboard(),
    )


async def handle_form_name(vk_user_id: int, text: str):
    name = text.strip()
    if not (1 <= len(name) <= 30):
        send_message(vk_user_id, "Имя слишком короткое или длинное. От 1 до 30 символов.")
        return
    update_data(vk_user_id, name=name)
    set_state(vk_user_id, "age")
    send_message(vk_user_id, "Сколько тебе лет? (от 18 до 99)")


async def handle_form_age(vk_user_id: int, text: str):
    if not text.strip().isdigit():
        send_message(vk_user_id, "Напиши число, например: 28")
        return
    age = int(text.strip())
    if not (18 <= age <= 99):
        send_message(vk_user_id, "Возраст должен быть от 18 до 99.")
        return
    update_data(vk_user_id, age=age)
    set_state(vk_user_id, "gender")
    send_message(vk_user_id, "Твой пол?", keyboard=gender_keyboard())


async def handle_form_gender(vk_user_id: int, text: str):
    if text not in ("Мужчина", "Женщина"):
        send_message(vk_user_id, "Выбери из кнопок.", keyboard=gender_keyboard())
        return
    gender = "M" if text == "Мужчина" else "F"
    opposite = "F" if gender == "M" else "M"
    update_data(vk_user_id, gender=gender, looking_for=opposite)
    set_state(vk_user_id, "partner_age_min")
    partner = "девушки" if gender == "M" else "молодого человека"
    send_message(
        vk_user_id,
        f"Минимальный возраст {partner}?\nНапример: 22",
        keyboard=empty_keyboard(),
    )


async def handle_form_partner_age_min(vk_user_id: int, text: str):
    if not text.strip().isdigit():
        send_message(vk_user_id, "Напиши число, например: 22")
        return
    age_min = int(text.strip())
    if not (18 <= age_min <= 99):
        send_message(vk_user_id, "Возраст должен быть от 18 до 99.")
        return
    update_data(vk_user_id, partner_age_min=age_min)
    set_state(vk_user_id, "partner_age_max")
    data = get_data(vk_user_id)
    partner = "девушки" if data.get("gender") == "M" else "молодого человека"
    send_message(vk_user_id, f"Максимальный возраст {partner}?\nНапример: 40")


async def handle_form_partner_age_max(vk_user_id: int, text: str):
    if not text.strip().isdigit():
        send_message(vk_user_id, "Напиши число, например: 40")
        return
    age_max = int(text.strip())
    if not (18 <= age_max <= 99):
        send_message(vk_user_id, "Возраст должен быть от 18 до 99.")
        return
    data = get_data(vk_user_id)
    age_min = data.get("partner_age_min", 18)
    if age_max < age_min:
        send_message(
            vk_user_id,
            f"Максимум не может быть меньше минимума ({age_min}). Введи от {age_min}."
        )
        return
    update_data(vk_user_id, partner_age_max=age_max)
    set_state(vk_user_id, "city")
    send_message(vk_user_id, "Из какого ты города?")


async def handle_form_city(vk_user_id: int, text: str):
    city = text.strip()
    if not (1 <= len(city) <= 50):
        send_message(vk_user_id, "Название города от 1 до 50 символов.")
        return
    update_data(vk_user_id, city=city)
    set_state(vk_user_id, "church")
    send_message(vk_user_id, "Какую церковь / общину посещаешь?")


async def handle_form_church(vk_user_id: int, text: str):
    church = text.strip()
    if not (1 <= len(church) <= 100):
        send_message(vk_user_id, "Слишком короткое или длинное. От 1 до 100 символов.")
        return
    update_data(vk_user_id, church=church)
    set_state(vk_user_id, "marital")
    # Кнопку «Не женат / Не замужем» показываем по полу пользователя
    gender = get_data(vk_user_id).get("gender")
    send_message(vk_user_id, "Семейное положение?",
                 keyboard=marital_keyboard(gender))


async def handle_form_marital(vk_user_id: int, text: str):
    # Принимаем все варианты: оба гендерных + общий старый + развод/вдовство
    valid = ("Не женат", "Не замужем", "Не женат / Не замужем",
             "В разводе", "Вдовец / Вдова")
    gender = get_data(vk_user_id).get("gender")
    if text not in valid:
        send_message(vk_user_id, "Выбери из кнопок.",
                     keyboard=marital_keyboard(gender))
        return
    # Сохраняем буквально то, что выбрал пользователь — гендерно-корректно.
    # «Не женат / Не замужем» (если вдруг придёт) преобразуем по полу.
    if text == "Не женат / Не замужем":
        text = "Не замужем" if gender == "F" else "Не женат"
    update_data(vk_user_id, marital=text)
    set_state(vk_user_id, "children")
    send_message(vk_user_id, "Есть ли дети?", keyboard=children_keyboard())


async def handle_form_children(vk_user_id: int, text: str):
    if text not in ("Нет детей", "Есть дети"):
        send_message(vk_user_id, "Выбери из кнопок.", keyboard=children_keyboard())
        return
    update_data(vk_user_id, children=text)
    set_state(vk_user_id, "hobbies")
    send_message(
        vk_user_id,
        "Расскажи о себе и своих интересах (до 500 символов).",
        keyboard=empty_keyboard(),
    )


async def handle_form_hobbies(vk_user_id: int, text: str):
    hobbies = text.strip()
    if not (1 <= len(hobbies) <= 500):
        send_message(vk_user_id, "От 1 до 500 символов.")
        return
    update_data(vk_user_id, hobbies=hobbies, photos=[])
    set_state(vk_user_id, "photo")
    send_message(
        vk_user_id,
        "📸 Пришли свои фото — от 2 до 5 штук. По одной фотке в одном сообщении.\n"
        "После 2-й фотки появится кнопка «✅ Готово».",
    )


# ----------- Загрузка фото из VK -----------

def extract_largest_photo_url(photo_attachment: dict) -> Optional[str]:
    """Из вложения 'photo' достаём URL самого большого размера."""
    photo = photo_attachment.get("photo", {})
    sizes = photo.get("sizes", [])
    if not sizes:
        return None
    # Сортируем по площади (width*height), берём самое большое
    best = max(sizes, key=lambda s: s.get("width", 0) * s.get("height", 0))
    return best.get("url")


async def handle_form_photo(vk_user_id: int, message: dict):
    """Обработка сообщения с фото на этапе регистрации."""
    data = get_data(vk_user_id)
    photos = data.get("photos", [])
    if len(photos) >= 5:
        send_message(vk_user_id, "Максимум 5 фото. Жми ✅ Готово.",
                     keyboard=photo_done_keyboard(len(photos)))
        return

    # Извлекаем все фото из вложений
    attachments = message.get("attachments", [])
    photo_attachments = [a for a in attachments if a.get("type") == "photo"]
    if not photo_attachments:
        send_message(
            vk_user_id,
            "Пришли фотографией (через скрепку), а не текстом или файлом."
        )
        return

    db_id = db.vk_id_to_db_id(vk_user_id)
    folder = db.user_photos_dir(db_id)

    for att in photo_attachments:
        if len(photos) >= 5:
            break
        url = extract_largest_photo_url(att)
        if not url:
            continue
        pos = len(photos)
        target = os.path.join(folder, f"{pos}.jpg")
        ok = await photo_utils.download_vk_photo(url, target)
        if ok:
            # photo_id в нашей БД для VK — это URL фото (нам понадобится
            # как fallback, если файл удалят)
            photos.append({"photo_id": url, "file_path": target})

    update_data(vk_user_id, photos=photos)

    if len(photos) < 2:
        text = (f"📷 Принято фото {len(photos)}/5.\n"
                f"Нужно ещё минимум {2 - len(photos)}.")
    elif len(photos) < 5:
        text = (f"📷 Принято фото {len(photos)}/5.\n"
                f"Можно добавить ещё или нажать ✅ Готово.")
    else:
        text = f"📷 Принято фото {len(photos)}/5. Это максимум — жми ✅ Готово."

    send_message(vk_user_id, text, keyboard=photo_done_keyboard(len(photos)))


async def handle_photos_undo(vk_user_id: int):
    """Кнопка «Удалить последнее»."""
    data = get_data(vk_user_id)
    photos = data.get("photos", [])
    if photos:
        removed = photos.pop()
        if isinstance(removed, dict) and removed.get("file_path"):
            try:
                if os.path.exists(removed["file_path"]):
                    os.remove(removed["file_path"])
            except Exception as e:
                log.warning(f"Не смог удалить {removed['file_path']}: {e}")
        update_data(vk_user_id, photos=photos)

    if not photos:
        send_message(vk_user_id, "Удалил все фото. Пришли первое заново.")
        return

    if len(photos) >= 2:
        text = (f"Удалил последнее. Сейчас фото: {len(photos)}/5.\n"
                f"Пришли ещё или жми ✅ Готово.")
    else:
        text = (f"Удалил последнее. Сейчас фото: {len(photos)}/5.\n"
                f"Нужно ещё минимум одно.")
    send_message(vk_user_id, text, keyboard=photo_done_keyboard(len(photos)))


async def handle_photos_done(vk_user_id: int):
    """Кнопка «Готово» — финал регистрации."""
    data = get_data(vk_user_id)
    photos = data.get("photos", [])
    if len(photos) < 2:
        send_message(vk_user_id, "⚠️ Нужно минимум 2 фото.",
                     keyboard=photo_done_keyboard(len(photos)))
        return

    db_id = db.vk_id_to_db_id(vk_user_id)
    info = get_vk_user_info(vk_user_id)
    screen_name = info.get("screen_name") or f"id{vk_user_id}"

    try:
        await db.save_user(
            user_id=db_id,
            username=screen_name,
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
            platform="vk",
        )
    except Exception as e:
        log.exception(f"save_user failed for vk {vk_user_id}")
        send_message(vk_user_id, f"❌ Ошибка сохранения: {e}")
        return

    # При перезаполнении — обнулим лайки/историю, чтобы лента началась заново
    is_restart = data.get("is_restart", False)
    extra = ""
    if is_restart:
        await db.reset_user_swipes(db_id)
        extra = "\nИстория свайпов сброшена."

    set_state(vk_user_id, None)
    send_message(
        vk_user_id,
        f"✅ Анкета сохранена! Загружено фото: {len(photos)}.{extra}\n\n"
        "Просмотр анкет в ВК будет добавлен в следующем обновлении бота.",
        keyboard=main_menu_keyboard(),
    )


# ============= ОБРАБОТКА СОБЫТИЙ =============

async def handle_start(vk_user_id: int):
    set_state(vk_user_id, None)
    if not await require_consent(vk_user_id):
        return
    if not await require_subscription(vk_user_id):
        return

    db_id = db.vk_id_to_db_id(vk_user_id)
    user = await db.get_user(db_id)
    if user:
        send_message(
            vk_user_id,
            f"С возвращением, {user['name']}! Что будем делать?",
            keyboard=main_menu_keyboard(),
        )
    else:
        send_message(
            vk_user_id,
            "👋 Здравствуй! Это бот для знакомства верующих людей.\n"
            "Давай заполним анкету.",
            keyboard=empty_keyboard(),
        )
        await start_registration(vk_user_id)


async def handle_forget(vk_user_id: int):
    kb = VkKeyboard(inline=True)
    kb.add_callback_button("🗑 Да, удалить", color=VkKeyboardColor.NEGATIVE,
                           payload={"cmd": "forget_confirm"})
    kb.add_line()
    kb.add_callback_button("❌ Отмена", color=VkKeyboardColor.SECONDARY,
                           payload={"cmd": "forget_cancel"})
    send_message(
        vk_user_id,
        "🗑 Удаление всех данных\n\n"
        "Будут удалены: анкета, фотографии, все лайки и матчи, согласие. "
        "Это необратимо.\n\nТочно удалить?",
        keyboard=kb,
    )


async def handle_message(event):
    msg = event.obj.message
    text = (msg.get("text") or "").strip()
    vk_user_id = msg["from_id"]
    if vk_user_id < 0:
        return

    log.info(f"[msg] {vk_user_id}: {text[:80]}")

    lower = text.lower()

    # Команды-триггеры (вне регистрации)
    state = get_state(vk_user_id)

    # Если идёт этап загрузки фото — нужно обработать вложения, даже если текста нет
    if state == "photo" and msg.get("attachments"):
        await handle_form_photo(vk_user_id, msg)
        return

    # Системные команды доступны всегда
    if lower in ("start", "/start", "начать", "старт"):
        await handle_start(vk_user_id)
        return
    if lower in ("/privacy", "политика", "privacy"):
        send_long(vk_user_id, PRIVACY_POLICY_TEXT)
        return
    if lower in ("/agreement", "соглашение", "agreement"):
        send_long(vk_user_id, USER_AGREEMENT_TEXT)
        return
    if lower in ("/forget", "удалить", "забыть"):
        await handle_forget(vk_user_id)
        return

    # Кнопки главного меню (только если не в FSM)
    if state is None:
        if text == "🔍 Смотреть анкеты":
            if not await require_consent(vk_user_id):
                return
            if not await require_subscription(vk_user_id):
                return
            await show_next_profile(vk_user_id)
            return
        if text == "👤 Моя анкета":
            if not await require_consent(vk_user_id):
                return
            db_id = db.vk_id_to_db_id(vk_user_id)
            u = await db.get_user(db_id)
            if not u:
                send_message(vk_user_id, "Анкеты ещё нет. Напиши «Начать», чтобы создать.")
                return
            # Покажем хотя бы текстом (фото в чате VK через бот — задача шага 5)
            partner = "девушки" if u["gender"] == "M" else "молодого человека"
            text_profile = (
                f"👤 Твоя анкета:\n\n"
                f"{u['name']}, {u['age']}\n"
                f"📍 {u['city']}\n"
                f"⛪ {u['church']}\n"
                f"💍 {u['marital']}\n"
                f"👶 {u['children']}\n\n"
                f"О себе:\n{u['hobbies']}\n\n"
                f"🔎 Ищу возраст {partner}: "
                f"{u['partner_age_min']}–{u['partner_age_max']} лет"
            )
            send_message(vk_user_id, text_profile, keyboard=main_menu_keyboard())
            return
        if text == "💌 Мои матчи":
            if not await require_consent(vk_user_id):
                return
            await show_my_matches(vk_user_id)
            return
        if text == "✏️ Заполнить заново":
            if not await require_consent(vk_user_id):
                return
            update_data(vk_user_id, is_restart=True)
            await start_registration(vk_user_id)
            return

    # Если в процессе регистрации — диспетчер по состояниям
    if state == "name":
        await handle_form_name(vk_user_id, text)
    elif state == "age":
        await handle_form_age(vk_user_id, text)
    elif state == "gender":
        await handle_form_gender(vk_user_id, text)
    elif state == "partner_age_min":
        await handle_form_partner_age_min(vk_user_id, text)
    elif state == "partner_age_max":
        await handle_form_partner_age_max(vk_user_id, text)
    elif state == "city":
        await handle_form_city(vk_user_id, text)
    elif state == "church":
        await handle_form_church(vk_user_id, text)
    elif state == "marital":
        await handle_form_marital(vk_user_id, text)
    elif state == "children":
        await handle_form_children(vk_user_id, text)
    elif state == "hobbies":
        await handle_form_hobbies(vk_user_id, text)
    elif state == "photo":
        # Текст на этапе фото — подсказка
        send_message(
            vk_user_id,
            "Сейчас нужно прислать фото (через скрепку, не текстом).",
            keyboard=photo_done_keyboard(len(get_data(vk_user_id).get("photos", []))),
        )
    else:
        # По умолчанию — приветствие
        await handle_start(vk_user_id)


async def handle_callback(event):
    payload = event.obj.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    cmd = payload.get("cmd")
    vk_user_id = event.obj["user_id"]
    log.info(f"[callback] {vk_user_id}: {cmd}")

    if cmd == "show_privacy":
        send_long(vk_user_id, PRIVACY_POLICY_TEXT)
        send_message(vk_user_id, "👆 Это политика. Теперь выбери:",
                     keyboard=consent_keyboard())

    elif cmd == "show_agreement":
        send_long(vk_user_id, USER_AGREEMENT_TEXT)
        send_message(vk_user_id, "👆 Это соглашение. Теперь выбери:",
                     keyboard=consent_keyboard())

    elif cmd == "consent_accept":
        db_id = db.vk_id_to_db_id(vk_user_id)
        await db.grant_consent(db_id, "v1")
        send_message(vk_user_id, "✅ Согласие принято. Спасибо!")
        if not await require_subscription(vk_user_id):
            return
        # Дальше — регистрация или главное меню
        user = await db.get_user(db_id)
        if user:
            send_message(
                vk_user_id,
                f"С возвращением, {user['name']}!",
                keyboard=main_menu_keyboard(),
            )
        else:
            await start_registration(vk_user_id)

    elif cmd == "consent_decline":
        send_message(
            vk_user_id,
            "Без согласия пользоваться ботом нельзя. Если передумаешь — "
            "напиши «Начать».",
        )

    elif cmd == "check_sub":
        if is_subscribed_to_group(vk_user_id):
            send_message(vk_user_id, "✅ Подписка подтверждена.")
            await handle_start(vk_user_id)
        else:
            send_message(vk_user_id, "⚠️ Ты ещё не подписан. Подпишись и нажми снова.")

    elif cmd == "photos_done":
        await handle_photos_done(vk_user_id)

    elif cmd == "photos_undo":
        await handle_photos_undo(vk_user_id)

    elif cmd == "forget_confirm":
        db_id = db.vk_id_to_db_id(vk_user_id)
        await db.delete_user_completely(db_id)
        set_state(vk_user_id, None)
        send_message(
            vk_user_id,
            "🗑 Все твои данные удалены. Если захочешь вернуться — напиши «Начать».",
            keyboard=empty_keyboard(),
        )

    elif cmd == "forget_cancel":
        send_message(vk_user_id, "Отмена. Данные сохранены.",
                     keyboard=main_menu_keyboard())

    elif cmd in ("swipe_like", "swipe_dislike", "swipe_next", "swipe_prev",
                 "swipe_report", "swipe_stop", "swipe_noop"):
        action = cmd.replace("swipe_", "")
        await handle_swipe_action(vk_user_id, action)

    # Подтверждаем нажатие, чтобы убрать «крутилку»
    try:
        vk.messages.sendMessageEventAnswer(
            event_id=event.obj["event_id"],
            user_id=vk_user_id,
            peer_id=event.obj["peer_id"],
        )
    except Exception as e:
        log.warning(f"sendMessageEventAnswer failed: {e}")


# ============= ПРОСМОТР АНКЕТ / СВАЙПЫ =============

# Состояние просмотра в памяти, аналогично TG-боту.
# {vk_user_id: {"target_id": db_id, "photo_idx": int, "photos": [{photo_id, file_path}]}}
viewer_state: dict[int, dict] = {}


def upload_photo_to_messages(file_path: str, peer_id: int) -> Optional[str]:
    """Загружает локальный файл фото в ВК для отправки в сообщение.
    Возвращает строку attachment вида 'photo<owner>_<id>' для использования
    в messages.send.

    ВК требует двухэтапной загрузки: получить сервер → загрузить файл →
    сохранить в messages.saveMessagesPhoto.
    """
    try:
        import requests
        # 1. Получаем upload-сервер
        upload = vk.photos.getMessagesUploadServer(peer_id=peer_id)
        upload_url = upload["upload_url"]

        # 2. Грузим файл
        with open(file_path, "rb") as f:
            resp = requests.post(
                upload_url,
                files={"photo": ("photo.jpg", f, "image/jpeg")},
                timeout=30,
            )
            resp.raise_for_status()
            upload_result = resp.json()

        # 3. Сохраняем
        saved = vk.photos.saveMessagesPhoto(
            photo=upload_result["photo"],
            server=upload_result["server"],
            hash=upload_result["hash"],
        )
        if not saved:
            return None
        item = saved[0]
        return f"photo{item['owner_id']}_{item['id']}"
    except Exception as e:
        log.warning(f"upload_photo_to_messages failed: {e}")
        return None


def swipe_keyboard(photo_idx: int, total_photos: int) -> VkKeyboard:
    """Клавиатура под анкетой в ленте."""
    kb = VkKeyboard(inline=True)
    counter = f"{photo_idx + 1}/{total_photos}" if total_photos > 1 else "📷"
    kb.add_callback_button("◀", color=VkKeyboardColor.SECONDARY,
                           payload={"cmd": "swipe_prev"})
    kb.add_callback_button(counter, color=VkKeyboardColor.SECONDARY,
                           payload={"cmd": "swipe_noop"})
    kb.add_callback_button("▶", color=VkKeyboardColor.SECONDARY,
                           payload={"cmd": "swipe_next"})
    kb.add_line()
    kb.add_callback_button("❌", color=VkKeyboardColor.NEGATIVE,
                           payload={"cmd": "swipe_dislike"})
    kb.add_callback_button("❤️", color=VkKeyboardColor.POSITIVE,
                           payload={"cmd": "swipe_like"})
    kb.add_line()
    kb.add_callback_button("🚩 Жалоба", color=VkKeyboardColor.SECONDARY,
                           payload={"cmd": "swipe_report"})
    kb.add_callback_button("⏹ Стоп", color=VkKeyboardColor.SECONDARY,
                           payload={"cmd": "swipe_stop"})
    return kb


def format_profile_text(u: dict) -> str:
    """Текст анкеты для отправки в VK (без HTML — VK его не понимает)."""
    return (
        f"{u['name']}, {u['age']}\n"
        f"📍 {u['city']}\n"
        f"⛪ {u['church']}\n"
        f"💍 {u['marital']}\n"
        f"👶 {u['children']}\n\n"
        f"О себе:\n{u['hobbies']}"
    )


async def send_profile_to_vk(vk_user_id: int, profile: dict,
                             photos: list, photo_idx: int = 0):
    """Отправляет одну анкету пользователю VK с фото и кнопками свайпа.
    Перед отправкой удаляет предыдущее сообщение бота (если было),
    чтобы чат не забивался — в нём всегда видна только текущая анкета.
    Сохраняет id нового сообщения в viewer_state, чтобы удалить его при показе
    следующей анкеты или при выходе из ленты."""
    photo_idx = max(0, min(photo_idx, len(photos) - 1))
    p = photos[photo_idx]

    # Прикрепляем фото. Если есть локальный файл — загружаем его.
    attachment = None
    if p.get("file_path") and os.path.exists(p["file_path"]):
        attachment = upload_photo_to_messages(p["file_path"], vk_user_id)
    if not attachment:
        log.warning(f"Нет файла для фото у user_id={profile['user_id']}")

    text = format_profile_text(profile)

    # Удаляем предыдущее сообщение бота с анкетой (если было) — чтобы чат
    # оставался чистым. Работает в течение 24ч после отправки.
    st = viewer_state.get(vk_user_id)
    if st and st.get("last_message_id"):
        delete_message_silently(st["last_message_id"])

    msg_id = send_message(
        vk_user_id, text,
        keyboard=swipe_keyboard(photo_idx, len(photos)),
        attachment=attachment,
    )

    # Запоминаем id нового сообщения в viewer_state, чтобы удалить его
    # при показе следующей анкеты
    if st is not None:
        st["last_message_id"] = msg_id


async def show_next_profile(vk_user_id: int):
    """Показать следующую подходящую анкету в ленте."""
    db_id = db.vk_id_to_db_id(vk_user_id)
    me = await db.get_user(db_id)
    if not me:
        send_message(vk_user_id, "Сначала заполни анкету. Напиши «Начать».")
        return

    profile = await db.get_next_profile(db_id)
    if not profile:
        # Анкеты закончились — удалим последнюю показанную, чтобы чат был чист
        st = viewer_state.pop(vk_user_id, None)
        if st and st.get("last_message_id"):
            delete_message_silently(st["last_message_id"])
        send_message(
            vk_user_id,
            "🤷 Анкеты закончились. Загляни позже — появятся новые!",
            keyboard=main_menu_keyboard(),
        )
        return

    photos = await db.get_user_photos_with_paths(profile["user_id"])
    if not photos:
        photos = [{"photo_id": profile["photo_id"],
                   "file_path": profile.get("photo_path")}]

    # Сохраняем last_message_id из предыдущего состояния — send_profile_to_vk
    # сам его удалит и перезапишет на id нового сообщения.
    prev_last = viewer_state.get(vk_user_id, {}).get("last_message_id")
    viewer_state[vk_user_id] = {
        "target_id": profile["user_id"],
        "photo_idx": 0,
        "photos": photos,
        "last_message_id": prev_last,
    }
    await db.set_last_shown(db_id, profile["user_id"])

    await send_profile_to_vk(vk_user_id, profile, photos, 0)


async def handle_swipe_action(vk_user_id: int, action: str):
    """Обработка ❤️ ❌ ◀ ▶ 🚩 ⏹ внутри ленты."""
    db_id = db.vk_id_to_db_id(vk_user_id)
    st = viewer_state.get(vk_user_id)

    if action == "noop":
        return

    if action == "stop":
        # Удаляем последнюю показанную анкету — чтобы чат остался чистым после выхода
        if st and st.get("last_message_id"):
            delete_message_silently(st["last_message_id"])
        viewer_state.pop(vk_user_id, None)
        send_message(vk_user_id, "Окей, остановились.",
                     keyboard=main_menu_keyboard())
        return

    target_id = st["target_id"] if st else await db.get_last_shown(db_id)

    # Жалоба
    if action == "report":
        if not target_id:
            send_message(vk_user_id, "Сначала открой анкету.")
            return
        already = await db.has_reported(db_id, target_id)
        if already:
            send_message(vk_user_id, "Ты уже жаловался на эту анкету.")
            return
        await db.add_report(db_id, target_id)
        total = await db.count_reports(target_id)
        send_message(vk_user_id, "🚩 Жалоба принята. Спасибо!")
        await notify_admins_about_report_vk(db_id, target_id, total)
        return

    # Стрелки — листание фото внутри анкеты или переход к соседней
    if action == "next":
        if st and st["photo_idx"] + 1 < len(st["photos"]):
            new_idx = st["photo_idx"] + 1
            profile = await db.get_user(st["target_id"])
            if profile:
                st["photo_idx"] = new_idx
                await send_profile_to_vk(vk_user_id, profile, st["photos"], new_idx)
            return
        # последнее фото — к следующей анкете
        await show_next_profile(vk_user_id)
        return

    if action == "prev":
        if st and st["photo_idx"] > 0:
            new_idx = st["photo_idx"] - 1
            profile = await db.get_user(st["target_id"])
            if profile:
                st["photo_idx"] = new_idx
                await send_profile_to_vk(vk_user_id, profile, st["photos"], new_idx)
            return
        # на первом фото — к предыдущей анкете
        prev = await db.get_prev_profile(db_id)
        if not prev:
            send_message(vk_user_id, "Это первая анкета — назад уже некуда.")
            return
        photos = await db.get_user_photos_with_paths(prev["user_id"])
        if not photos:
            photos = [{"photo_id": prev["photo_id"],
                       "file_path": prev.get("photo_path")}]
        # Сохраняем last_message_id — send_profile_to_vk удалит предыдущее
        prev_last = viewer_state.get(vk_user_id, {}).get("last_message_id")
        viewer_state[vk_user_id] = {
            "target_id": prev["user_id"],
            "photo_idx": 0,
            "photos": photos,
            "last_message_id": prev_last,
        }
        await db.set_last_shown(db_id, prev["user_id"])
        await send_profile_to_vk(vk_user_id, prev, photos, 0)
        return

    # ❤️ / ❌
    if not target_id:
        await show_next_profile(vk_user_id)
        return

    if action == "like":
        is_match = await db.add_like(db_id, target_id)
        if is_match:
            send_message(vk_user_id, "🎉 Матч!")
            await notify_match(db_id, target_id)
        else:
            send_message(vk_user_id, "❤️ Лайк отправлен")
    elif action == "dislike":
        await db.add_dislike(db_id, target_id)
        send_message(vk_user_id, "❌ Пропущено")

    # Дальше — следующая анкета
    await show_next_profile(vk_user_id)


# ============= МАТЧИ =============

async def notify_match(user_a_id: int, user_b_id: int):
    """Кросс-платформенное уведомление о матче.
    user_a — тот, кто только что лайкнул (источник — VK-юзер).
    user_b — кого лайкнул (любая платформа).
    Сообщения отправляются через соответствующий API.

    Эта функция работает от лица VK-бота. Telegram-юзера уведомит TG-бот
    (см. helper в database — notify_match_for_tg будет вызвана отдельно).
    """
    a = await db.get_user(user_a_id)
    b = await db.get_user(user_b_id)
    if not a or not b:
        return

    text_for_a = (
        f"🎉 Вы понравились друг другу!\n\n"
        f"{b['name']}, {b['age']}\n"
        f"Контакт: {db.contact_link(b)}"
    )
    text_for_b = (
        f"🎉 Вы понравились друг другу!\n\n"
        f"{a['name']}, {a['age']}\n"
        f"Контакт: {db.contact_link(a)}"
    )

    # A — это VK (мы внутри VK-бота). Отправляем напрямую.
    if db.is_vk_user(user_a_id):
        try:
            send_message(db.db_id_to_vk_id(user_a_id), text_for_a)
        except Exception as e:
            log.warning(f"VK match notify to A failed: {e}")

    # B — может быть VK или TG. Если VK — мы отправляем сами.
    # Если TG — кладём в очередь, TG-бот заберёт и отправит.
    if db.is_vk_user(user_b_id):
        try:
            send_message(db.db_id_to_vk_id(user_b_id), text_for_b)
        except Exception as e:
            log.warning(f"VK match notify to B failed: {e}")
    else:
        # TG-юзер — записываем уведомление в БД, TG-бот их разошлёт
        await db.queue_match_notification(user_b_id, user_a_id)


async def notify_admins_about_report_vk(reporter_id: int, target_id: int, total: int):
    """Уведомление админов о жалобе (от VK-юзера) — через Telegram-бота.
    Нам нужно «попросить» TG-бот разослать админам, но из VK-процесса
    напрямую отправить в TG мы не можем. Решение: кладём в очередь."""
    await db.queue_admin_report(reporter_id, target_id, total)


async def show_my_matches(vk_user_id: int):
    """Список матчей VK-юзера."""
    db_id = db.vk_id_to_db_id(vk_user_id)
    matches = await db.get_matches(db_id)
    if not matches:
        send_message(
            vk_user_id,
            "💌 У тебя пока нет матчей. Лайкай анкеты — может, кто-то ответит!",
            keyboard=main_menu_keyboard(),
        )
        return
    send_message(vk_user_id, f"💌 Твои матчи ({len(matches)}):")
    for m in matches:
        text = (f"{m['name']}, {m['age']}\n"
                f"📍 {m['city']}\n"
                f"Контакт: {db.contact_link(m)}")
        # Фото обложки — если есть локальный файл
        attachment = None
        if m.get("photo_path") and os.path.exists(m["photo_path"]):
            attachment = upload_photo_to_messages(m["photo_path"], vk_user_id)
        send_message(vk_user_id, text, attachment=attachment)



async def deliver_pending_notifications_vk():
    """Раз в 3 секунды смотрим очередь pending_notifications.
    VK-бот доставляет матчи, где получатель — VK-юзер.
    Записи для TG-юзеров обрабатывает TG-бот."""
    while True:
        try:
            notifications = await db.get_pending_notifications(kinds=("match",))
            for n in notifications:
                recipient = n["recipient_id"]
                if recipient is None or not db.is_vk_user(recipient):
                    # Не наш — не трогаем (доставит TG-бот)
                    continue
                try:
                    partner_id = n["partner_id"]
                    partner = await db.get_user(partner_id)
                    if not partner:
                        await db.mark_notification_delivered(n["id"])
                        continue
                    text = (
                        f"🎉 Вы понравились друг другу!\n\n"
                        f"{partner['name']}, {partner['age']}\n"
                        f"Контакт: {db.contact_link(partner)}"
                    )
                    vk_uid = db.db_id_to_vk_id(recipient)
                    attachment = None
                    if partner.get("photo_path") and os.path.exists(partner["photo_path"]):
                        attachment = upload_photo_to_messages(partner["photo_path"], vk_uid)
                    send_message(vk_uid, text, attachment=attachment)
                    await db.mark_notification_delivered(n["id"])
                except Exception as e:
                    log.exception(f"Доставка VK-уведомления {n['id']} упала: {e}")
                    await db.mark_notification_delivered(n["id"])
        except Exception as e:
            log.exception(f"deliver loop error: {e}")
        await asyncio.sleep(3)


async def migrate_marital_by_gender():
    """Одноразовая миграция: старые анкеты со значением «Не женат / Не замужем»
    переписываем на гендерно-корректное."""
    import aiosqlite
    async with aiosqlite.connect(db.DB_PATH) as conn:
        # Мужчинам — «Не женат»
        cur = await conn.execute(
            "UPDATE users SET marital = 'Не женат' "
            "WHERE marital = 'Не женат / Не замужем' AND gender = 'M'"
        )
        m_count = cur.rowcount
        # Женщинам — «Не замужем»
        cur = await conn.execute(
            "UPDATE users SET marital = 'Не замужем' "
            "WHERE marital = 'Не женат / Не замужем' AND gender = 'F'"
        )
        f_count = cur.rowcount
        await conn.commit()
        if m_count or f_count:
            log.info(f"Миграция marital: М→{m_count}, Ж→{f_count}")


async def process_pending_conversations():
    """При старте обрабатываем «висящие» сообщения — те, что пришли пока бот лежал.
    Идём по диалогам с непрочитанными от пользователя и обрабатываем последнее как
    обычное событие message_new."""
    try:
        # filter=unanswered — диалоги, где последнее сообщение от пользователя,
        # а бот ещё не отвечал
        resp = vk.messages.getConversations(
            filter="unanswered",
            count=200,  # хватит на любой реальный объём
            extended=0,
        )
    except vk_api.exceptions.ApiError as e:
        log.warning(f"getConversations failed: {e}")
        return

    items = resp.get("items", [])
    if not items:
        log.info("Висящих диалогов нет.")
        return

    log.info(f"Найдено {len(items)} висящих диалогов — обрабатываю…")
    handled = 0
    for it in items:
        last_msg = it.get("last_message") or {}
        from_id = last_msg.get("from_id")
        # Только сообщения от пользователей (положительный id, не от сообществ)
        if not from_id or from_id < 0:
            continue
        # Имитируем структуру события Long Poll, чтобы переиспользовать handle_message
        class FakeEvent:
            pass
        fake = FakeEvent()
        fake.obj = type("Obj", (), {"message": last_msg})()
        try:
            await handle_message(fake)
            handled += 1
        except Exception as e:
            log.exception(f"Не смог обработать висящее сообщение от {from_id}: {e}")
    log.info(f"Обработано висящих диалогов: {handled}")


async def main():
    await db.init_db()
    db.ensure_photos_dir()

    # Миграция семейного положения по полу (одноразовая, идемпотентная)
    await migrate_marital_by_gender()

    log.info(f"VK-бот запущен. Group ID: {VK_GROUP_ID_INT}")
    log.info(f"Обязательная группа: {VK_REQUIRED_GROUP or '(нет)'}")

    # Фоновая задача доставки уведомлений
    asyncio.create_task(deliver_pending_notifications_vk())

    # Обрабатываем сообщения, которые пришли пока бот не работал
    await process_pending_conversations()

    event_queue: queue.Queue = queue.Queue()
    stop_flag = threading.Event()

    def background_listen():
        """Long Poll с автоматическим переподключением при разрывах сети."""
        import time
        while not stop_flag.is_set():
            try:
                for event in longpoll.listen():
                    if stop_flag.is_set():
                        break
                    event_queue.put(event)
            except Exception as e:
                # Таймауты Long Poll, разрывы сети — нормальное явление.
                # Просто пере-подключаемся через 2 секунды.
                log.warning(f"Long Poll переподключение после ошибки: {e}")
                time.sleep(2)

    thread = threading.Thread(target=background_listen, daemon=True)
    thread.start()

    log.info("Начинаю обработку событий…")
    try:
        while True:
            try:
                event = event_queue.get(timeout=1.0)
            except queue.Empty:
                await asyncio.sleep(0.1)
                continue
            try:
                if event.type == VkBotEventType.MESSAGE_NEW:
                    await handle_message(event)
                elif event.type == VkBotEventType.MESSAGE_EVENT:
                    await handle_callback(event)
            except Exception as e:
                log.exception(f"Ошибка при обработке события: {e}")
    finally:
        stop_flag.set()


if __name__ == "__main__":
    asyncio.run(main())
