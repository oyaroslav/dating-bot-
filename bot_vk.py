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

# ID администраторов в ВК (через запятую в .env).
# Например: VK_ADMIN_IDS=99046865,12345678
# Эти пользователи могут запускать команду «rassylka» в ВК.
VK_ADMIN_IDS: set[int] = set()
for _x in os.getenv("VK_ADMIN_IDS", "").split(","):
    _x = _x.strip()
    if _x.isdigit():
        VK_ADMIN_IDS.add(int(_x))

if not VK_TOKEN or not VK_GROUP_ID:
    raise RuntimeError(
        "VK_TOKEN или VK_GROUP_ID не заданы в .env."
    )

VK_GROUP_ID_INT = int(VK_GROUP_ID)


def vk_is_admin(vk_user_id: int) -> bool:
    return vk_user_id in VK_ADMIN_IDS

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
    "city",
    "denomination", "denomination_other",
    "church",
    "church_role", "job",
    "marital", "children", "hobbies", "photo",
    # Дозаполнение старых анкет:
    "legacy_denomination", "legacy_denomination_other",
    "legacy_church_role", "legacy_job",
    # Жалоба:
    "report_reason",
    # Рассылка (только для админов):
    "bc_text", "bc_age", "bc_city",
)


# Список конфессий (синхронизировано с TG-ботом)
DENOMINATIONS = [
    "Баптисты", "Пятидесятники", "АСД",
    "Евангельские Христиане", "Лютеране", "Православные",
    "Католики", "Методисты", "Пресвитериане",
]


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


def denomination_keyboard() -> VkKeyboard:
    """Клавиатура с конфессиями. VK позволяет до 5 кнопок в ряду —
    но безопасней по 2 в ряд (длинные названия)."""
    kb = VkKeyboard(one_time=True)
    for i, d in enumerate(DENOMINATIONS):
        kb.add_button(d)
        # По 2 кнопки в ряду
        if i % 2 == 1 and i < len(DENOMINATIONS) - 1:
            kb.add_line()
    kb.add_line()
    kb.add_button("Другое")
    return kb


def skip_keyboard() -> VkKeyboard:
    """Клавиатура с одной кнопкой «Пропустить» (для job)."""
    kb = VkKeyboard(one_time=True)
    kb.add_button("Пропустить")
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
    set_state(vk_user_id, "denomination")
    send_message(
        vk_user_id,
        "Какой конфессии ты принадлежишь? Выбери из кнопок.",
        keyboard=denomination_keyboard(),
    )


async def handle_form_denomination(vk_user_id: int, text: str):
    text = text.strip()
    if text == "Другое":
        set_state(vk_user_id, "denomination_other")
        send_message(
            vk_user_id, "Напиши свою конфессию (2–50 символов).",
            keyboard=empty_keyboard(),
        )
        return
    if text not in DENOMINATIONS:
        send_message(vk_user_id, "Выбери из кнопок.",
                     keyboard=denomination_keyboard())
        return
    update_data(vk_user_id, denomination=text)
    set_state(vk_user_id, "church")
    send_message(
        vk_user_id,
        "Как называется твоя церковь?\n"
        "Например: «Вифания», «Дом благодати», «Свет Спасения».",
        keyboard=empty_keyboard(),
    )


async def handle_form_denomination_other(vk_user_id: int, text: str):
    text = text.strip()
    if not (2 <= len(text) <= 50):
        send_message(vk_user_id, "Название конфессии от 2 до 50 символов.")
        return
    update_data(vk_user_id, denomination=text)
    set_state(vk_user_id, "church")
    send_message(
        vk_user_id,
        "Как называется твоя церковь?\n"
        "Например: «Вифания», «Дом благодати», «Свет Спасения».",
    )


async def handle_form_church(vk_user_id: int, text: str):
    church = text.strip()
    if not (1 <= len(church) <= 100):
        send_message(vk_user_id, "Слишком короткое или длинное. От 1 до 100 символов.")
        return
    update_data(vk_user_id, church=church)
    set_state(vk_user_id, "church_role")
    send_message(
        vk_user_id,
        "Какое у тебя служение в церкви?\n"
        "Например: «прихожанин», «диакон», «лидер молодёжи», «руководитель прославления».",
    )


async def handle_form_church_role(vk_user_id: int, text: str):
    role = text.strip()
    if not (2 <= len(role) <= 100):
        send_message(vk_user_id, "От 2 до 100 символов.")
        return
    update_data(vk_user_id, church_role=role)
    set_state(vk_user_id, "job")
    send_message(
        vk_user_id,
        "Кем работаешь или на кого учишься?\n"
        "Одной строкой. Можно пропустить.",
        keyboard=skip_keyboard(),
    )


async def handle_form_job(vk_user_id: int, text: str):
    text = text.strip()
    if text == "Пропустить":
        job = None
    else:
        if not (2 <= len(text) <= 100):
            send_message(vk_user_id, "От 2 до 100 символов, либо «Пропустить».",
                         keyboard=skip_keyboard())
            return
        job = text
    update_data(vk_user_id, job=job)
    set_state(vk_user_id, "marital")
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
            denomination=data.get("denomination"),
            church_role=data.get("church_role"),
            job=data.get("job"),
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
    # Команда рассылки — только для админов
    if lower in ("rassylka", "/rassylka", "рассылка"):
        if not vk_is_admin(vk_user_id):
            return  # тихо игнорируем, не дразним обычных юзеров
        await handle_rassylka_start(vk_user_id)
        return
    # /cancel — отмена FSM админом (в т.ч. рассылки)
    if lower in ("/cancel", "отмена"):
        if state and state.startswith("bc_"):
            set_state(vk_user_id, None)
            send_message(vk_user_id, "❌ Рассылка отменена.",
                         keyboard=main_menu_keyboard())
            return

    # Кнопки главного меню (только если не в FSM)
    if state is None:
        if text == "🔍 Смотреть анкеты":
            if not await require_consent(vk_user_id):
                return
            if not await require_subscription(vk_user_id):
                return
            if not await require_fillin_vk(vk_user_id):
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
            if not await require_fillin_vk(vk_user_id):
                return
            partner = "девушки" if u["gender"] == "M" else "молодого человека"
            # Текст анкеты — новые поля только если заполнены
            profile_lines = [
                f"👤 Твоя анкета:\n",
                f"{u['name']}, {u['age']}",
                f"📍 {u['city']}",
            ]
            if u.get("denomination"):
                profile_lines.append(f"✝️ {u['denomination']}")
            profile_lines.append(f"⛪ {u['church']}")
            if u.get("church_role"):
                profile_lines.append(f"🙏 {u['church_role']}")
            if u.get("job"):
                profile_lines.append(f"💼 {u['job']}")
            profile_lines.append(f"💍 {u['marital']}")
            profile_lines.append(f"👶 {u['children']}")
            profile_lines.append(f"\nО себе:\n{u['hobbies']}")
            profile_lines.append(
                f"\n🔎 Ищу возраст {partner}: "
                f"{u['partner_age_min']}–{u['partner_age_max']} лет"
            )
            send_message(vk_user_id, "\n".join(profile_lines),
                         keyboard=main_menu_keyboard())
            return
        if text == "💌 Мои матчи":
            if not await require_consent(vk_user_id):
                return
            if not await require_fillin_vk(vk_user_id):
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
    elif state == "denomination":
        await handle_form_denomination(vk_user_id, text)
    elif state == "denomination_other":
        await handle_form_denomination_other(vk_user_id, text)
    elif state == "church":
        await handle_form_church(vk_user_id, text)
    elif state == "church_role":
        await handle_form_church_role(vk_user_id, text)
    elif state == "job":
        await handle_form_job(vk_user_id, text)
    elif state == "marital":
        await handle_form_marital(vk_user_id, text)
    elif state == "children":
        await handle_form_children(vk_user_id, text)
    elif state == "hobbies":
        await handle_form_hobbies(vk_user_id, text)
    elif state == "report_reason":
        await handle_report_reason_text(vk_user_id, text)
    # Дозаполнение старых анкет
    elif state == "legacy_denomination":
        await handle_legacy_denomination(vk_user_id, text)
    elif state == "legacy_denomination_other":
        await handle_legacy_denomination_other(vk_user_id, text)
    elif state == "legacy_church_role":
        await handle_legacy_church_role(vk_user_id, text)
    elif state == "legacy_job":
        await handle_legacy_job(vk_user_id, text)
    # Рассылка
    elif state == "bc_text":
        await handle_rassylka_text(vk_user_id, text)
    elif state == "bc_age":
        await handle_rassylka_age(vk_user_id, text)
    elif state == "bc_city":
        await handle_rassylka_city(vk_user_id, text)
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

    elif cmd == "report_cancel":
        set_state(vk_user_id, None)
        send_message(vk_user_id, "❌ Жалоба отменена.",
                     keyboard=main_menu_keyboard())

    elif cmd in ("swipe_like", "swipe_dislike", "swipe_next", "swipe_prev",
                 "swipe_report", "swipe_stop", "swipe_noop"):
        action = cmd.replace("swipe_", "")
        await handle_swipe_action(vk_user_id, action)

    elif cmd and cmd.startswith("bc_"):
        # Рассылка (только админ)
        await handle_rassylka_callback(vk_user_id, payload)

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
    """Текст анкеты для отправки в VK (без HTML — VK его не понимает).
    Новые поля (denomination, church_role, job) показываются только если заполнены."""
    lines = [f"{u['name']}, {u['age']}",
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
    return "\n".join(lines) + f"\n\nО себе:\n{u['hobbies']}"


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


async def handle_report_reason_text(vk_user_id: int, text: str):
    """Обработка текста причины жалобы (FSM state=report_reason)."""
    reason = (text or "").strip()
    if not (30 <= len(reason) <= 200):
        cancel_kb = VkKeyboard(inline=True)
        cancel_kb.add_callback_button(
            "❌ Отмена", color=VkKeyboardColor.SECONDARY,
            payload={"cmd": "report_cancel"},
        )
        send_message(
            vk_user_id,
            f"Причина должна быть от 30 до 200 символов "
            f"(сейчас {len(reason)}). Попробуй ещё раз или нажми «Отмена».",
            keyboard=cancel_kb,
        )
        return

    data = get_data(vk_user_id)
    target_id = data.get("report_target_id")
    db_id = db.vk_id_to_db_id(vk_user_id)

    if not target_id:
        set_state(vk_user_id, None)
        send_message(vk_user_id, "Что-то пошло не так. Попробуй ещё раз.",
                     keyboard=main_menu_keyboard())
        return

    already = await db.has_reported(db_id, target_id)
    if already:
        set_state(vk_user_id, None)
        send_message(vk_user_id, "Ты уже жаловался на эту анкету.",
                     keyboard=main_menu_keyboard())
        return

    await db.add_report(db_id, target_id, reason=reason)
    total = await db.count_reports(target_id)
    set_state(vk_user_id, None)
    send_message(
        vk_user_id,
        "✅ Жалоба отправлена администрации. Спасибо!",
        keyboard=main_menu_keyboard(),
    )
    await notify_admins_about_report_vk(db_id, target_id, total, reason=reason)


async def require_fillin_vk(vk_user_id: int) -> bool:
    """Проверяет нужно ли пользователю VK дозаполнить новые поля.
    Если да — запускает FSM legacy_* и возвращает False.
    Если нет — True."""
    db_id = db.vk_id_to_db_id(vk_user_id)
    if not await db.needs_legacy_fillin(db_id):
        return True
    set_state(vk_user_id, "legacy_denomination")
    send_message(
        vk_user_id,
        "👋 У нас обновление!\n\n"
        "Ответь на пару вопросов — это поможет другим узнать о тебе больше, "
        "и сможешь снова листать анкеты.\n\n"
        "Какой конфессии ты принадлежишь?",
        keyboard=denomination_keyboard(),
    )
    return False


async def handle_legacy_denomination(vk_user_id: int, text: str):
    text = text.strip()
    if text == "Другое":
        set_state(vk_user_id, "legacy_denomination_other")
        send_message(vk_user_id, "Напиши свою конфессию (2–50 символов).",
                     keyboard=empty_keyboard())
        return
    if text not in DENOMINATIONS:
        send_message(vk_user_id, "Выбери из кнопок.",
                     keyboard=denomination_keyboard())
        return
    update_data(vk_user_id, legacy_denomination=text)
    set_state(vk_user_id, "legacy_church_role")
    send_message(
        vk_user_id,
        "Какое у тебя служение в церкви?\n"
        "Например: «прихожанин», «диакон», «лидер молодёжи».",
        keyboard=empty_keyboard(),
    )


async def handle_legacy_denomination_other(vk_user_id: int, text: str):
    text = text.strip()
    if not (2 <= len(text) <= 50):
        send_message(vk_user_id, "Название конфессии от 2 до 50 символов.")
        return
    update_data(vk_user_id, legacy_denomination=text)
    set_state(vk_user_id, "legacy_church_role")
    send_message(
        vk_user_id,
        "Какое у тебя служение в церкви?\n"
        "Например: «прихожанин», «диакон», «лидер молодёжи».",
    )


async def handle_legacy_church_role(vk_user_id: int, text: str):
    role = text.strip()
    if not (2 <= len(role) <= 100):
        send_message(vk_user_id, "Опиши служение в 2–100 символов.")
        return
    update_data(vk_user_id, legacy_church_role=role)
    set_state(vk_user_id, "legacy_job")
    send_message(
        vk_user_id,
        "Кем работаешь или на кого учишься?\n"
        "Одной строкой. Можно пропустить.",
        keyboard=skip_keyboard(),
    )


async def handle_legacy_job(vk_user_id: int, text: str):
    text = text.strip()
    if text == "Пропустить":
        job = None
    else:
        if not (2 <= len(text) <= 100):
            send_message(vk_user_id, "От 2 до 100 символов, либо «Пропустить».",
                         keyboard=skip_keyboard())
            return
        job = text
    data = get_data(vk_user_id)
    db_id = db.vk_id_to_db_id(vk_user_id)
    await db.update_profile_fields(
        db_id,
        denomination=data["legacy_denomination"],
        church_role=data["legacy_church_role"],
        job=job,
    )
    set_state(vk_user_id, None)
    send_message(
        vk_user_id,
        "✅ Спасибо! Анкета обновлена. Можешь дальше пользоваться ботом.",
        keyboard=main_menu_keyboard(),
    )


# ============= РАССЫЛКА (команда rassylka) =============
# В VK инлайн-меню с кнопками работает иначе чем в TG, поэтому делаем
# проще: бот ведёт диалог пошагово через VkKeyboard (callback_button + payload).

import re as _re_bc_vk


def _bc_strip_html(text: str) -> str:
    """Чистим HTML — VK его не понимает."""
    if not text:
        return text
    text = _re_bc_vk.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=_re_bc_vk.IGNORECASE)
    text = _re_bc_vk.sub(r"<[^>]+>", "", text)
    text = (text.replace("&amp;", "&").replace("&lt;", "<")
                 .replace("&gt;", ">").replace("&quot;", '"')
                 .replace("&#39;", "'"))
    return text


def _bc_filter_summary(filters: dict) -> str:
    p = filters.get("platform", "all")
    p_label = {"all": "Все", "tg": "Только TG", "vk": "Только VK"}.get(p, "Все")
    g = filters.get("gender", "all")
    g_label = {"all": "Все", "M": "Только М", "F": "Только Ж"}.get(g, "Все")
    lines = [
        f"📱 Платформа: {p_label}",
        f"👤 Пол: {g_label}",
    ]
    if filters.get("age_min") and filters.get("age_max"):
        lines.append(f"📅 Возраст: {filters['age_min']}–{filters['age_max']}")
    else:
        lines.append("📅 Возраст: Все")
    lines.append(f"📍 Город: {filters.get('city') or 'Все'}")
    lines.append(f"✝️ Конфессия: {filters.get('denomination') or 'Все'}")
    return "\n".join(lines)


def _bc_main_kb() -> VkKeyboard:
    kb = VkKeyboard(inline=True)
    kb.add_callback_button("👥 Всем", color=VkKeyboardColor.POSITIVE,
                           payload={"cmd": "bc_all"})
    kb.add_line()
    kb.add_callback_button("⚙️ Настроить фильтры", color=VkKeyboardColor.PRIMARY,
                           payload={"cmd": "bc_setup"})
    kb.add_line()
    kb.add_callback_button("❌ Отмена", color=VkKeyboardColor.NEGATIVE,
                           payload={"cmd": "bc_cancel"})
    return kb


def _bc_filter_kb() -> VkKeyboard:
    kb = VkKeyboard(inline=True)
    kb.add_callback_button("📱 Платформа", payload={"cmd": "bc_f_platform"})
    kb.add_callback_button("👤 Пол", payload={"cmd": "bc_f_gender"})
    kb.add_line()
    kb.add_callback_button("📅 Возраст", payload={"cmd": "bc_f_age"})
    kb.add_callback_button("📍 Город", payload={"cmd": "bc_f_city"})
    kb.add_line()
    kb.add_callback_button("✝️ Конфессия", payload={"cmd": "bc_f_denom"})
    kb.add_line()
    kb.add_callback_button("✅ К отправке", color=VkKeyboardColor.POSITIVE,
                           payload={"cmd": "bc_preview"})
    kb.add_callback_button("❌ Отмена", color=VkKeyboardColor.NEGATIVE,
                           payload={"cmd": "bc_cancel"})
    return kb


def _bc_platform_kb() -> VkKeyboard:
    kb = VkKeyboard(inline=True)
    kb.add_callback_button("Все", payload={"cmd": "bc_set", "field": "platform", "value": "all"})
    kb.add_line()
    kb.add_callback_button("Только TG", payload={"cmd": "bc_set", "field": "platform", "value": "tg"})
    kb.add_line()
    kb.add_callback_button("Только VK", payload={"cmd": "bc_set", "field": "platform", "value": "vk"})
    kb.add_line()
    kb.add_callback_button("◀ Назад", payload={"cmd": "bc_back"})
    return kb


def _bc_gender_kb() -> VkKeyboard:
    kb = VkKeyboard(inline=True)
    kb.add_callback_button("Все", payload={"cmd": "bc_set", "field": "gender", "value": "all"})
    kb.add_line()
    kb.add_callback_button("Только мужчины", payload={"cmd": "bc_set", "field": "gender", "value": "M"})
    kb.add_line()
    kb.add_callback_button("Только женщины", payload={"cmd": "bc_set", "field": "gender", "value": "F"})
    kb.add_line()
    kb.add_callback_button("◀ Назад", payload={"cmd": "bc_back"})
    return kb


def _bc_denom_kb() -> VkKeyboard:
    kb = VkKeyboard(inline=True)
    for i, d in enumerate(DENOMINATIONS):
        kb.add_callback_button(
            d, payload={"cmd": "bc_set", "field": "denom", "value": str(i)},
        )
        if i % 2 == 1 and i < len(DENOMINATIONS) - 1:
            kb.add_line()
    kb.add_line()
    kb.add_callback_button("Все", payload={"cmd": "bc_set", "field": "denom", "value": "all"})
    kb.add_line()
    kb.add_callback_button("◀ Назад", payload={"cmd": "bc_back"})
    return kb


def _bc_back_kb() -> VkKeyboard:
    """Минимальная клавиатура «Все / Назад» для age и city."""
    kb = VkKeyboard(inline=True)
    kb.add_callback_button("Все (убрать фильтр)",
                            payload={"cmd": "bc_set", "field": "age_or_city_all"})
    kb.add_line()
    kb.add_callback_button("◀ Назад", payload={"cmd": "bc_back"})
    return kb


def _bc_confirm_kb() -> VkKeyboard:
    kb = VkKeyboard(inline=True)
    kb.add_callback_button("✅ Да, отправить", color=VkKeyboardColor.POSITIVE,
                            payload={"cmd": "bc_send"})
    kb.add_line()
    kb.add_callback_button("◀ К фильтрам", payload={"cmd": "bc_back_to_menu"})
    kb.add_line()
    kb.add_callback_button("❌ Отмена", color=VkKeyboardColor.NEGATIVE,
                            payload={"cmd": "bc_cancel"})
    return kb


async def handle_rassylka_start(vk_user_id: int):
    """Запуск рассылки (только для админов)."""
    set_state(vk_user_id, "bc_text", filters={}, broadcast_text=None)
    send_message(
        vk_user_id,
        "📝 Рассылка\n\n"
        "Напиши текст (5–4000 символов).\n"
        "В VK теги HTML не отображаются (TG-получателям отправлю как есть).\n\n"
        "Отмена: «отмена» или /cancel",
        keyboard=empty_keyboard(),
    )


async def handle_rassylka_text(vk_user_id: int, text: str):
    text = (text or "").strip()
    if not (5 <= len(text) <= 4000):
        send_message(vk_user_id,
                     f"Длина {len(text)}. Допустимо: 5–4000 символов.")
        return
    update_data(vk_user_id, broadcast_text=text)
    set_state(vk_user_id, None)  # выходим из bc_text — дальше через кнопки

    counts = await db.count_broadcast_recipients()
    preview = text if len(text) <= 200 else text[:200] + "…"
    send_message(
        vk_user_id,
        f"📤 Кому отправить?\n\n"
        f"Превью текста:\n{preview}\n\n"
        f"👥 Всего получателей: {counts['total']} "
        f"(TG: {counts['tg']}, VK: {counts['vk']})",
        keyboard=_bc_main_kb(),
    )


async def _bc_show_menu(vk_user_id: int):
    data = get_data(vk_user_id)
    filters = data.get("filters", {})
    counts = await db.count_broadcast_recipients(**filters)
    send_message(
        vk_user_id,
        f"⚙️ Фильтры рассылки\n\n"
        f"{_bc_filter_summary(filters)}\n\n"
        f"👥 Подходит: {counts['total']} "
        f"(TG: {counts['tg']}, VK: {counts['vk']})",
        keyboard=_bc_filter_kb(),
    )


async def _bc_show_preview(vk_user_id: int):
    data = get_data(vk_user_id)
    bc_text = data.get("broadcast_text", "")
    filters = data.get("filters", {})
    counts = await db.count_broadcast_recipients(**filters)
    preview = bc_text if len(bc_text) <= 300 else bc_text[:300] + "…"
    send_message(
        vk_user_id,
        f"📋 Превью рассылки\n\n"
        f"Текст:\n{preview}\n\n"
        f"Фильтры:\n{_bc_filter_summary(filters)}\n\n"
        f"👥 Получателей: {counts['total']} "
        f"(TG: {counts['tg']}, VK: {counts['vk']})\n\n"
        f"Отправить?",
        keyboard=_bc_confirm_kb(),
    )


async def handle_rassylka_age(vk_user_id: int, text: str):
    txt = (text or "").strip().replace("..", "-").replace(" ", "-")
    m = _re_bc_vk.match(r"^(\d{2,3})-(\d{2,3})$", txt)
    if not m:
        send_message(vk_user_id,
                     "Не понял. Напиши в формате 25-40 или нажми «Все».",
                     keyboard=_bc_back_kb())
        return
    age_min, age_max = int(m.group(1)), int(m.group(2))
    if not (18 <= age_min <= age_max <= 99):
        send_message(vk_user_id, "Допустимо 18–99 и минимум ≤ максимум.")
        return
    data = get_data(vk_user_id)
    filters = data.get("filters", {})
    filters["age_min"] = age_min
    filters["age_max"] = age_max
    update_data(vk_user_id, filters=filters)
    set_state(vk_user_id, None)
    await _bc_show_menu(vk_user_id)


async def handle_rassylka_city(vk_user_id: int, text: str):
    city = (text or "").strip()
    if not (2 <= len(city) <= 50):
        send_message(vk_user_id, "Название города от 2 до 50 символов.")
        return
    counts = await db.count_broadcast_recipients(city=city)
    if counts["total"] == 0:
        send_message(vk_user_id,
                     f"В городе «{city}» никого не нашёл. "
                     f"Проверь название или нажми «Все».",
                     keyboard=_bc_back_kb())
        return
    data = get_data(vk_user_id)
    filters = data.get("filters", {})
    filters["city"] = city
    update_data(vk_user_id, filters=filters)
    set_state(vk_user_id, None)
    await _bc_show_menu(vk_user_id)


async def handle_rassylka_callback(vk_user_id: int, payload: dict):
    """Обработка кнопок рассылки (callback из inline-клавиатуры)."""
    cmd = payload.get("cmd")
    if not vk_is_admin(vk_user_id):
        return

    if cmd == "bc_cancel":
        set_state(vk_user_id, None)
        send_message(vk_user_id, "❌ Рассылка отменена.",
                     keyboard=main_menu_keyboard())
        return

    if cmd == "bc_all":
        # Сразу превью без фильтров
        await _bc_show_preview(vk_user_id)
        return

    if cmd == "bc_setup":
        await _bc_show_menu(vk_user_id)
        return

    if cmd == "bc_preview":
        await _bc_show_preview(vk_user_id)
        return

    if cmd == "bc_back_to_menu" or cmd == "bc_back":
        await _bc_show_menu(vk_user_id)
        return

    if cmd == "bc_f_platform":
        send_message(vk_user_id, "📱 Платформа:", keyboard=_bc_platform_kb())
        return

    if cmd == "bc_f_gender":
        send_message(vk_user_id, "👤 Пол:", keyboard=_bc_gender_kb())
        return

    if cmd == "bc_f_age":
        set_state(vk_user_id, "bc_age")
        send_message(
            vk_user_id,
            "📅 Возраст\n\n"
            "Напиши диапазон, например: 25-40.\n"
            "Или нажми «Все», чтобы убрать фильтр.",
            keyboard=_bc_back_kb(),
        )
        return

    if cmd == "bc_f_city":
        set_state(vk_user_id, "bc_city")
        cities = await db.list_distinct_cities(min_users=3)
        cities_hint = ", ".join(cities[:15]) if cities else "(нет данных)"
        send_message(
            vk_user_id,
            f"📍 Город\n\n"
            f"Напиши название города (точное совпадение).\n\n"
            f"Популярные: {cities_hint}\n\n"
            f"Или нажми «Все».",
            keyboard=_bc_back_kb(),
        )
        return

    if cmd == "bc_f_denom":
        send_message(vk_user_id, "✝️ Конфессия:", keyboard=_bc_denom_kb())
        return

    if cmd == "bc_set":
        field = payload.get("field")
        value = payload.get("value")
        data = get_data(vk_user_id)
        filters = data.get("filters", {})

        if field == "platform":
            filters["platform"] = value
        elif field == "gender":
            filters["gender"] = value
        elif field == "denom":
            if value == "all":
                filters.pop("denomination", None)
            else:
                idx = int(value)
                if 0 <= idx < len(DENOMINATIONS):
                    filters["denomination"] = DENOMINATIONS[idx]
        elif field == "age_or_city_all":
            # Кнопка «Все» в разделе age или city
            cur_st = get_state(vk_user_id)
            if cur_st == "bc_age":
                filters.pop("age_min", None)
                filters.pop("age_max", None)
            elif cur_st == "bc_city":
                filters.pop("city", None)
            set_state(vk_user_id, None)

        update_data(vk_user_id, filters=filters)
        await _bc_show_menu(vk_user_id)
        return

    if cmd == "bc_send":
        await _bc_run(vk_user_id)
        return


async def _bc_run(vk_user_id: int):
    """Запуск самой отправки от VK-админа."""
    data = get_data(vk_user_id)
    bc_text = data.get("broadcast_text", "")
    filters = data.get("filters", {})
    if not bc_text:
        send_message(vk_user_id, "Текст пуст — отменяю.",
                     keyboard=main_menu_keyboard())
        set_state(vk_user_id, None)
        return

    recipients = await db.get_broadcast_recipients(**filters)
    total = len(recipients)
    if total == 0:
        send_message(vk_user_id, "Никого не нашёл по фильтрам.",
                     keyboard=main_menu_keyboard())
        set_state(vk_user_id, None)
        return

    tg_recipients = [r for r in recipients if r["user_id"] > 0]
    vk_recipients = [r for r in recipients if r["user_id"] < 0]

    batch_id = int(__import__("time").time())

    # Для TG-получателей кладём в очередь как system_message — TG-бот разошлёт
    # Здесь мы в VK-процессе, у нас нет TG-API.
    if tg_recipients:
        for r in tg_recipients:
            await db.queue_system_message(r["user_id"], bc_text)

    # VK — отправляем сами, прямо здесь
    vk_text = _bc_strip_html(bc_text)
    set_state(vk_user_id, None)

    send_message(
        vk_user_id,
        f"⏳ Запустил рассылку…\n"
        f"VK: 0/{len(vk_recipients)}\n"
        f"TG: 0/{len(tg_recipients)} (через очередь)",
        keyboard=main_menu_keyboard(),
    )

    sent_vk = 0
    failed_vk = 0
    for i, r in enumerate(vk_recipients):
        vk_uid = db.db_id_to_vk_id(r["user_id"])
        try:
            send_message(vk_uid, vk_text)
            sent_vk += 1
        except Exception as e:
            failed_vk += 1
            log.warning(f"Broadcast VK to {vk_uid} failed: {e}")
        await asyncio.sleep(0.05)
        # Прогресс каждые 50 — VK не любит частые edit, поэтому шлём новые сообщения редко
        if (i + 1) % 100 == 0:
            send_message(
                vk_user_id,
                f"⏳ Прогресс: {sent_vk + failed_vk}/{len(vk_recipients)} VK "
                f"(✅ {sent_vk}, ❌ {failed_vk})"
            )

    send_message(
        vk_user_id,
        f"✅ Рассылка завершена\n\n"
        f"ВКонтакте:\n"
        f"• Отправлено: {sent_vk}\n"
        f"• Не удалось: {failed_vk}\n\n"
        f"Telegram:\n"
        f"• В очереди: {len(tg_recipients)}\n"
        f"• Отправит TG-бот в течение нескольких секунд.",
        keyboard=main_menu_keyboard(),
    )
    log.info(
        f"Broadcast (VK admin) batch {batch_id} done. VK sent: {sent_vk}, "
        f"VK failed: {failed_vk}, TG queued: {len(tg_recipients)}"
    )


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

    # Жалоба — запрашиваем причину (FSM state="report_reason")
    if action == "report":
        if not target_id:
            send_message(vk_user_id, "Сначала открой анкету.")
            return
        already = await db.has_reported(db_id, target_id)
        if already:
            send_message(vk_user_id, "Ты уже жаловался на эту анкету.")
            return
        # Сохраняем target в state.data, переводим в режим ожидания причины
        set_state(vk_user_id, "report_reason", report_target_id=target_id)
        # Удаляем текущую анкету из чата — чтобы не отвлекала
        if st and st.get("last_message_id"):
            delete_message_silently(st["last_message_id"])
        # Кнопка отмены
        cancel_kb = VkKeyboard(inline=True)
        cancel_kb.add_callback_button(
            "❌ Отмена", color=VkKeyboardColor.SECONDARY,
            payload={"cmd": "report_cancel"},
        )
        send_message(
            vk_user_id,
            "🚩 Жалоба на эту анкету\n\n"
            "Напиши причину жалобы (30–200 символов). "
            "Это поможет администраторам разобраться.\n\n"
            "Если нажал случайно — нажми «❌ Отмена».",
            keyboard=cancel_kb,
        )
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


async def notify_admins_about_report_vk(reporter_id: int, target_id: int,
                                          total: int, reason: str = None):
    """Уведомление админов о жалобе (от VK-юзера) — через Telegram-бота.
    Нам нужно «попросить» TG-бот разослать админам, но из VK-процесса
    напрямую отправить в TG мы не можем. Решение: кладём в очередь."""
    await db.queue_admin_report(reporter_id, target_id, total, reason=reason)


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
    VK-бот доставляет:
      - match для VK-получателей (TG-получателей возьмёт TG-бот)
      - system_message для VK-получателей (бан/разбан и т.п.)
      - broadcast для VK-получателей (рассылка от админа)"""
    while True:
        try:
            notifications = await db.get_pending_notifications(
                kinds=("match", "system_message", "broadcast"),
            )
            for n in notifications:
                recipient = n["recipient_id"]
                if recipient is None or not db.is_vk_user(recipient):
                    # Не наш — не трогаем (доставит TG-бот)
                    continue
                try:
                    vk_uid = db.db_id_to_vk_id(recipient)

                    if n["kind"] == "match":
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
                        attachment = None
                        if partner.get("photo_path") and os.path.exists(partner["photo_path"]):
                            attachment = upload_photo_to_messages(
                                partner["photo_path"], vk_uid,
                            )
                        send_message(vk_uid, text, attachment=attachment)

                    elif n["kind"] == "system_message":
                        import json
                        payload = json.loads(n["payload"] or "{}")
                        text = payload.get("text", "")
                        if text:
                            send_message(vk_uid, text)

                    elif n["kind"] == "broadcast":
                        import json
                        payload = json.loads(n["payload"] or "{}")
                        text = payload.get("text", "")
                        if text:
                            send_message(vk_uid, text)
                        # Лимит ~20 сообщений/сек на доставку рассылок
                        await asyncio.sleep(0.05)

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
