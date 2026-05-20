"""
VK Dating Bot — ВКонтакте версия бота знакомств «Ковчег».

Работает в паре с Telegram-ботом (bot.py) через общую базу данных.
Пользователи могут регистрироваться в любой из платформ — анкеты видны
обеим сторонам, матчи возможны между TG и VK.

Архитектура:
  - VK user_id хранится в БД как ОТРИЦАТЕЛЬНОЕ число
    (так не конфликтует с положительными Telegram-id).
  - Используем vk_api с Long Poll для бота сообщества.
  - Состояние диалогов (FSM) — в памяти процесса (простой dict).

Это ЭТАП 2: только базовый каркас — старт, согласие ПДн,
команды /privacy, /agreement. Регистрация, свайпы — в следующих этапах.
"""
import asyncio
import logging
import os
import random
from typing import Optional

import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
from dotenv import load_dotenv

import database as db


# ----------- Настройка -----------
load_dotenv()
VK_TOKEN = os.getenv("VK_TOKEN", "").strip()
VK_GROUP_ID = os.getenv("VK_GROUP_ID", "").strip()
VK_REQUIRED_GROUP = os.getenv("VK_REQUIRED_GROUP", "").strip()

if not VK_TOKEN or not VK_GROUP_ID:
    raise RuntimeError(
        "VK_TOKEN или VK_GROUP_ID не заданы в .env. "
        "Получи их в настройках сообщества ВК → Работа с API."
    )

VK_GROUP_ID_INT = int(VK_GROUP_ID)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [VK] %(levelname)s: %(message)s",
)
log = logging.getLogger("vk_bot")

# ----------- Тексты документов (читаем из тех же файлов, что и TG-бот) -----------
def _load_doc(filename: str, fallback: str) -> str:
    path = os.path.join(os.path.dirname(__file__), filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        log.warning(f"Файл {filename} не найден.")
        return fallback


PRIVACY_POLICY_TEXT = _load_doc(
    "PRIVACY_POLICY.md",
    "Текст политики не настроен. Свяжись с администратором."
)
USER_AGREEMENT_TEXT = _load_doc(
    "USER_AGREEMENT.md",
    "Текст соглашения не настроен. Свяжись с администратором."
)

# Лимит сообщения VK — 4096 символов
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


# ----------- FSM в памяти (простой словарь) -----------
# {vk_user_id: {"state": "...", "data": {...}}}
user_states: dict[int, dict] = {}


def get_state(vk_user_id: int) -> Optional[str]:
    return user_states.get(vk_user_id, {}).get("state")


def set_state(vk_user_id: int, state: Optional[str], data: Optional[dict] = None):
    if state is None:
        user_states.pop(vk_user_id, None)
        return
    existing = user_states.get(vk_user_id, {"data": {}})
    existing["state"] = state
    if data:
        existing["data"].update(data)
    user_states[vk_user_id] = existing


def get_data(vk_user_id: int) -> dict:
    return user_states.get(vk_user_id, {}).get("data", {})


# ----------- Отправка сообщений -----------
def send_message(vk_user_id: int, text: str, keyboard=None, attachment=None):
    """Универсальная отправка сообщения VK-пользователю."""
    params = {
        "user_id": vk_user_id,
        "message": text,
        "random_id": random.randint(1, 2**31 - 1),
    }
    if keyboard is not None:
        params["keyboard"] = keyboard.get_keyboard() if hasattr(keyboard, "get_keyboard") else keyboard
    if attachment:
        params["attachment"] = attachment
    try:
        vk.messages.send(**params)
    except vk_api.exceptions.ApiError as e:
        log.warning(f"Не смог отправить {vk_user_id}: {e}")


def send_long(vk_user_id: int, text: str, keyboard=None):
    """Отправка длинного сообщения с разбивкой."""
    parts = split_for_vk(text)
    for i, p in enumerate(parts):
        # Клавиатуру цепляем только к последнему куску
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


def consent_keyboard() -> VkKeyboard:
    """Клавиатура для согласия на обработку ПДн."""
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
    """Удаление клавиатуры."""
    return VkKeyboard.get_empty_keyboard()


# ----------- Получение данных о пользователе из VK -----------
def get_vk_user_info(vk_user_id: int) -> dict:
    """Возвращает имя, screen_name пользователя из VK."""
    try:
        users = vk.users.get(
            user_ids=vk_user_id,
            fields="screen_name,first_name,last_name",
        )
        if users:
            return users[0]
    except vk_api.exceptions.ApiError as e:
        log.warning(f"Не смог получить инфо о {vk_user_id}: {e}")
    return {"id": vk_user_id, "first_name": "Пользователь", "last_name": ""}


# ----------- Проверка согласия и подписки -----------
async def require_consent(vk_user_id: int) -> bool:
    """Если согласие есть — True. Если нет — показывает окно и False."""
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
    """Проверка, что пользователь подписан на обязательное сообщество."""
    if not VK_REQUIRED_GROUP:
        return True
    try:
        result = vk.groups.isMember(
            group_id=VK_REQUIRED_GROUP, user_id=vk_user_id,
        )
        return bool(result)
    except vk_api.exceptions.ApiError as e:
        log.warning(f"Не смог проверить подписку {vk_user_id}: {e}")
        return True  # лучше пропустить, чем заблокировать всех


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


# ----------- Обработка событий -----------
async def handle_start(vk_user_id: int):
    """Команда /start или 'Начать'."""
    set_state(vk_user_id, None)  # сбрасываем FSM

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
            "👋 Здравствуй! Это бот для знакомства верующих людей.\n\n"
            "Регистрация анкет будет добавлена в следующих обновлениях.\n"
            "Пока ты можешь просмотреть документы или дождаться полной версии.",
            keyboard=main_menu_keyboard(),
        )


async def handle_message(event):
    """Обработка нового входящего сообщения."""
    msg = event.obj.message
    text = (msg.get("text") or "").strip()
    vk_user_id = msg["from_id"]

    # Игнорируем сообщения от сообществ и ботов
    if vk_user_id < 0:
        return

    log.info(f"[msg] {vk_user_id}: {text[:80]}")

    # Команды-триггеры запуска
    lower = text.lower()
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

    # Кнопки главного меню
    if text == "🔍 Смотреть анкеты":
        if not await require_consent(vk_user_id):
            return
        if not await require_subscription(vk_user_id):
            return
        send_message(
            vk_user_id,
            "🔍 Просмотр анкет будет в следующих обновлениях бота.",
            keyboard=main_menu_keyboard(),
        )
        return

    if text == "👤 Моя анкета":
        if not await require_consent(vk_user_id):
            return
        send_message(
            vk_user_id,
            "👤 Моя анкета — будет доступна после реализации регистрации.",
            keyboard=main_menu_keyboard(),
        )
        return

    if text == "💌 Мои матчи":
        if not await require_consent(vk_user_id):
            return
        send_message(
            vk_user_id,
            "💌 Список матчей будет доступен после реализации регистрации.",
            keyboard=main_menu_keyboard(),
        )
        return

    if text == "✏️ Заполнить заново":
        if not await require_consent(vk_user_id):
            return
        send_message(
            vk_user_id,
            "✏️ Регистрация будет добавлена в следующих обновлениях.",
            keyboard=main_menu_keyboard(),
        )
        return

    # Если в FSM — будут добавлены обработчики на следующих этапах
    state = get_state(vk_user_id)
    if state:
        send_message(vk_user_id, "Идёт регистрация — этап в разработке.")
        return

    # По умолчанию — отвечаем приветствием
    await handle_start(vk_user_id)


async def handle_callback(event):
    """Обработка нажатий на inline-кнопки (с payload)."""
    payload = event.obj.get("payload") or {}
    if isinstance(payload, str):
        import json
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    cmd = payload.get("cmd")
    vk_user_id = event.obj["user_id"]
    log.info(f"[callback] {vk_user_id}: {cmd}")

    if cmd == "show_privacy":
        send_long(vk_user_id, PRIVACY_POLICY_TEXT)
        send_message(
            vk_user_id,
            "👆 Это была политика. Теперь выбери:",
            keyboard=consent_keyboard(),
        )
    elif cmd == "show_agreement":
        send_long(vk_user_id, USER_AGREEMENT_TEXT)
        send_message(
            vk_user_id,
            "👆 Это было соглашение. Теперь выбери:",
            keyboard=consent_keyboard(),
        )
    elif cmd == "consent_accept":
        db_id = db.vk_id_to_db_id(vk_user_id)
        await db.grant_consent(db_id, "v1")
        send_message(
            vk_user_id,
            "✅ Согласие принято. Спасибо!",
            keyboard=main_menu_keyboard(),
        )
        if not await require_subscription(vk_user_id):
            return
        # Дальше — нужно вести в регистрацию (будет в следующих этапах)
        send_message(
            vk_user_id,
            "Регистрация анкет в ВК будет добавлена в следующих обновлениях.",
            keyboard=main_menu_keyboard(),
        )
    elif cmd == "consent_decline":
        send_message(
            vk_user_id,
            "Без согласия пользоваться ботом нельзя. Если передумаешь — "
            "напиши «Начать».",
        )
    elif cmd == "check_sub":
        if is_subscribed_to_group(vk_user_id):
            send_message(
                vk_user_id,
                "✅ Подписка подтверждена.",
                keyboard=main_menu_keyboard(),
            )
            await handle_start(vk_user_id)
        else:
            send_message(
                vk_user_id,
                "⚠️ Ты ещё не подписан. Подпишись на сообщество и нажми снова.",
            )

    # Подтверждаем нажатие, чтобы убрать «крутилку»
    try:
        vk.messages.sendMessageEventAnswer(
            event_id=event.obj["event_id"],
            user_id=vk_user_id,
            peer_id=event.obj["peer_id"],
        )
    except Exception as e:
        log.warning(f"sendMessageEventAnswer failed: {e}")


async def handle_forget(vk_user_id: int):
    """Удаление всех данных пользователя по запросу (152-ФЗ)."""
    db_id = db.vk_id_to_db_id(vk_user_id)
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


# Дополнительные обработчики callback'ов для /forget — добавляем в handle_callback
# (объединено выше для простоты — но добавим обработку forget_confirm/cancel)
ORIGINAL_HANDLE_CALLBACK = handle_callback


async def handle_callback(event):  # переопределяем
    payload = event.obj.get("payload") or {}
    if isinstance(payload, str):
        import json
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    cmd = payload.get("cmd")
    vk_user_id = event.obj["user_id"]

    if cmd == "forget_confirm":
        db_id = db.vk_id_to_db_id(vk_user_id)
        await db.delete_user_completely(db_id)
        send_message(
            vk_user_id,
            "🗑 Все твои данные удалены. Если когда-нибудь захочешь "
            "вернуться — напиши «Начать».",
            keyboard=empty_keyboard(),
        )
        try:
            vk.messages.sendMessageEventAnswer(
                event_id=event.obj["event_id"],
                user_id=vk_user_id,
                peer_id=event.obj["peer_id"],
            )
        except Exception:
            pass
        return

    if cmd == "forget_cancel":
        send_message(vk_user_id, "Отмена. Данные сохранены.",
                     keyboard=main_menu_keyboard())
        try:
            vk.messages.sendMessageEventAnswer(
                event_id=event.obj["event_id"],
                user_id=vk_user_id,
                peer_id=event.obj["peer_id"],
            )
        except Exception:
            pass
        return

    # Остальные кнопки — старый обработчик
    await ORIGINAL_HANDLE_CALLBACK(event)


# ----------- Главный цикл -----------
async def main():
    await db.init_db()
    log.info(f"VK-бот запущен. Group ID: {VK_GROUP_ID_INT}")
    log.info(f"Обязательная группа: {VK_REQUIRED_GROUP or '(нет)'}")

    # VkBotLongPoll — синхронный, оборачиваем в asyncio через run_in_executor
    loop = asyncio.get_event_loop()

    def listen_sync():
        """Синхронный цикл прослушивания, возвращает события по одному."""
        for event in longpoll.listen():
            yield event

    # Запускаем синхронный цикл в фоновом потоке
    # и обрабатываем события асинхронно
    import threading
    import queue

    event_queue: queue.Queue = queue.Queue()
    stop_flag = threading.Event()

    def background_listen():
        try:
            for event in longpoll.listen():
                if stop_flag.is_set():
                    break
                event_queue.put(event)
        except Exception as e:
            log.error(f"Фоновый поток упал: {e}")

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
