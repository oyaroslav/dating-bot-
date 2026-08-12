"""
Слой работы с базой данных SQLite.
Таблицы:
  users   — анкеты пользователей (с полями для христианского бота знакомств)
  swipes  — кто кому ставил like/dislike
  shown   — последняя показанная анкета (чтобы знать, на кого нажал like)
"""
import aiosqlite

DB_PATH = "dating.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id          INTEGER PRIMARY KEY,
                username         TEXT,
                name             TEXT NOT NULL,
                age              INTEGER NOT NULL,
                gender           TEXT NOT NULL,           -- 'M' / 'F'
                looking_for      TEXT NOT NULL,           -- 'M' / 'F' (только противоположный пол)
                partner_age_min  INTEGER NOT NULL DEFAULT 18,
                partner_age_max  INTEGER NOT NULL DEFAULT 99,
                city             TEXT NOT NULL,
                church           TEXT NOT NULL,
                denomination     TEXT,                    -- конфессия (новое, может быть NULL у старых)
                church_role      TEXT,                    -- служение в церкви (новое)
                job              TEXT,                    -- работа/учёба (новое, может быть NULL)
                marital          TEXT NOT NULL,
                children         TEXT NOT NULL,
                hobbies          TEXT NOT NULL,
                photo_id         TEXT NOT NULL,           -- главное фото (обложка, TG file_id для старых)
                photo_path       TEXT,                    -- локальный путь к обложке (новая система)
                platform         TEXT NOT NULL DEFAULT 'tg',  -- 'tg' / 'vk'
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Все фото пользователя (1..5 штук) с порядком
            CREATE TABLE IF NOT EXISTS user_photos (
                user_id    INTEGER NOT NULL,
                position   INTEGER NOT NULL,         -- 0,1,2,3,4
                photo_id   TEXT NOT NULL,            -- TG file_id (старые) или маркер
                file_path  TEXT,                     -- локальный путь к файлу
                PRIMARY KEY (user_id, position)
            );

            CREATE TABLE IF NOT EXISTS swipes (
                from_user   INTEGER NOT NULL,
                to_user     INTEGER NOT NULL,
                action      TEXT NOT NULL,           -- 'like' / 'dislike'
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (from_user, to_user)
            );

            CREATE TABLE IF NOT EXISTS shown (
                user_id   INTEGER PRIMARY KEY,
                target_id INTEGER NOT NULL
            );

            -- История просмотров: позволяет листать назад стрелкой «◀»
            CREATE TABLE IF NOT EXISTS view_history (
                user_id    INTEGER NOT NULL,
                target_id  INTEGER NOT NULL,
                viewed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, target_id, viewed_at)
            );
            CREATE INDEX IF NOT EXISTS idx_view_history
                ON view_history (user_id, viewed_at DESC);

            -- Бан-лист: забаненные не могут пользоваться ботом
            CREATE TABLE IF NOT EXISTS banned (
                user_id    INTEGER PRIMARY KEY,
                reason     TEXT,
                banned_by  INTEGER,
                banned_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Жалобы пользователей друг на друга
            CREATE TABLE IF NOT EXISTS reports (
                reporter_id  INTEGER NOT NULL,
                target_id    INTEGER NOT NULL,
                reason       TEXT,
                reported_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (reporter_id, target_id)
            );
            CREATE INDEX IF NOT EXISTS idx_reports_target
                ON reports (target_id);
            CREATE INDEX IF NOT EXISTS idx_reports_time
                ON reports (reported_at DESC);

            -- Согласие на обработку персональных данных (152-ФЗ).
            -- Храним сам факт и время — это нужно для подтверждения, что
            -- пользователь видел политику и согласился на обработку.
            CREATE TABLE IF NOT EXISTS consents (
                user_id     INTEGER PRIMARY KEY,
                accepted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                version     TEXT NOT NULL DEFAULT 'v1'
            );

            -- Динамическая ролевая модель.
            -- Root-админы задаются через .env (ADMIN_IDS / VK_ADMIN_IDS)
            -- и в эту таблицу НЕ попадают.
            -- Здесь только admin и moderator, назначенные через бота.
            -- user_id может быть как TG (положительный), так и VK (отрицательный).
            CREATE TABLE IF NOT EXISTS admins (
                user_id      INTEGER PRIMARY KEY,
                role         TEXT NOT NULL,         -- 'admin' | 'moderator'
                assigned_by  INTEGER NOT NULL,      -- кто назначил (для истории)
                assigned_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Очередь уведомлений для кросс-платформенной доставки.
            -- VK-бот не может слать в Telegram напрямую, поэтому кладёт сюда.
            -- TG-бот раз в несколько секунд читает и доставляет.
            CREATE TABLE IF NOT EXISTS pending_notifications (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                kind         TEXT NOT NULL,
                recipient_id INTEGER,
                partner_id   INTEGER,
                payload      TEXT,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                delivered    INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_pending_undelivered
                ON pending_notifications (delivered, kind);
        """)

        # ---- МИГРАЦИЯ для уже существующих баз ----
        # Если у пользователя база была создана до появления полей
        # partner_age_min / partner_age_max — добавляем их с разумными значениями.
        cur = await conn.execute("PRAGMA table_info(users)")
        existing_cols = {row[1] for row in await cur.fetchall()}

        if "partner_age_min" not in existing_cols:
            await conn.execute(
                "ALTER TABLE users ADD COLUMN partner_age_min INTEGER NOT NULL DEFAULT 18"
            )
            # для уже зарегистрированных: ±10 лет от собственного возраста
            await conn.execute("""
                UPDATE users SET partner_age_min = MAX(18, age - 10)
                WHERE partner_age_min = 18
            """)

        if "partner_age_max" not in existing_cols:
            await conn.execute(
                "ALTER TABLE users ADD COLUMN partner_age_max INTEGER NOT NULL DEFAULT 99"
            )
            await conn.execute("""
                UPDATE users SET partner_age_max = MIN(99, age + 10)
                WHERE partner_age_max = 99
            """)

        # Платформа: 'tg' для Telegram, 'vk' для ВКонтакте.
        # Существующие пользователи — Telegram (мы только начали делать ВК).
        if "platform" not in existing_cols:
            await conn.execute(
                "ALTER TABLE users ADD COLUMN platform TEXT NOT NULL DEFAULT 'tg'"
            )

        # Локальный путь к фото-обложке (для мультиплатформенности).
        # Если NULL — значит используется старый photo_id (TG-нативный).
        if "photo_path" not in existing_cols:
            await conn.execute(
                "ALTER TABLE users ADD COLUMN photo_path TEXT"
            )

        # То же для таблицы user_photos
        cur = await conn.execute("PRAGMA table_info(user_photos)")
        photos_cols = {row[1] for row in await cur.fetchall()}
        if "file_path" not in photos_cols:
            await conn.execute(
                "ALTER TABLE user_photos ADD COLUMN file_path TEXT"
            )

        # Поле reason в reports (добавилось позже, миграция для старых баз)
        cur = await conn.execute("PRAGMA table_info(reports)")
        reports_cols = {row[1] for row in await cur.fetchall()}
        if "reason" not in reports_cols:
            await conn.execute(
                "ALTER TABLE reports ADD COLUMN reason TEXT"
            )

        # Новые поля анкеты: конфессия, служение, работа.
        # У существующих ~500 анкет будут NULL — при следующем заходе
        # бот попросит их дозаполнить (см. блок «перехват старых юзеров»).
        if "denomination" not in existing_cols:
            await conn.execute(
                "ALTER TABLE users ADD COLUMN denomination TEXT"
            )
        if "church_role" not in existing_cols:
            await conn.execute(
                "ALTER TABLE users ADD COLUMN church_role TEXT"
            )
        if "job" not in existing_cols:
            await conn.execute(
                "ALTER TABLE users ADD COLUMN job TEXT"
            )

        # Скрытая анкета (юзер поставил на паузу).
        # is_hidden = 1 — не показывать анкету другим, не пускать листать самому.
        if "is_hidden" not in existing_cols:
            await conn.execute(
                "ALTER TABLE users ADD COLUMN is_hidden INTEGER NOT NULL DEFAULT 0"
            )
        # Когда скрыл — чтобы через 30 дней спросить «вернуть?»
        if "hidden_at" not in existing_cols:
            await conn.execute(
                "ALTER TABLE users ADD COLUMN hidden_at TIMESTAMP"
            )
        # Когда последний раз отправляли еженедельное уведомление —
        # чтобы не отправлять дважды за одну субботу
        if "last_weekly_notif" not in existing_cols:
            await conn.execute(
                "ALTER TABLE users ADD COLUMN last_weekly_notif TIMESTAMP"
            )
        # Когда последний раз отправляли итог лайков — чтобы не дублировать
        if "last_daily_likes_notif" not in existing_cols:
            await conn.execute(
                "ALTER TABLE users ADD COLUMN last_daily_likes_notif TIMESTAMP"
            )

        await conn.commit()


async def save_user(*, user_id, username, name, age, gender, looking_for,
                    partner_age_min, partner_age_max,
                    city, church, marital, children, hobbies, photos,
                    denomination=None, church_role=None, job=None,
                    platform="tg"):
    """Сохраняем пользователя и его фотографии.
    photos — список фотографий. Каждый элемент может быть:
      - строкой (старый формат, photo_id) — тогда file_path = None
      - словарём {photo_id, file_path}
    Первый элемент = обложка.
    denomination — конфессия (обязательно для НОВЫХ анкет, может быть None для старых)
    church_role — служение в церкви (обязательно для НОВЫХ)
    job — работа/учёба (может быть None — пользователь пропустил)
    """
    if gender not in ("M", "F"):
        raise ValueError("gender должен быть 'M' или 'F'")
    expected_lf = "F" if gender == "M" else "M"
    if looking_for != expected_lf:
        raise ValueError(
            f"Недопустимая комбинация: gender={gender}, looking_for={looking_for}. "
            f"Разрешено только разнополое знакомство."
        )
    if not photos:
        raise ValueError("Нужно хотя бы одно фото")
    if not (18 <= partner_age_min <= partner_age_max <= 99):
        raise ValueError("Некорректный диапазон возраста партнёра")
    if platform not in ("tg", "vk"):
        raise ValueError(f"platform должен быть 'tg' или 'vk', получено: {platform}")

    # Нормализуем формат photos в список словарей
    normalized = []
    for p in photos[:5]:
        if isinstance(p, str):
            normalized.append({"photo_id": p, "file_path": None})
        elif isinstance(p, dict):
            normalized.append({
                "photo_id": p.get("photo_id") or "",
                "file_path": p.get("file_path"),
            })
        else:
            raise ValueError(f"Неподдерживаемый формат фото: {type(p)}")

    cover = normalized[0]  # обложка

    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("""
            INSERT INTO users (user_id, username, name, age, gender, looking_for,
                               partner_age_min, partner_age_max,
                               city, church, denomination, church_role, job,
                               marital, children, hobbies,
                               photo_id, photo_path, platform)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                name=excluded.name,
                age=excluded.age,
                gender=excluded.gender,
                looking_for=excluded.looking_for,
                partner_age_min=excluded.partner_age_min,
                partner_age_max=excluded.partner_age_max,
                city=excluded.city,
                church=excluded.church,
                denomination=excluded.denomination,
                church_role=excluded.church_role,
                job=excluded.job,
                marital=excluded.marital,
                children=excluded.children,
                hobbies=excluded.hobbies,
                photo_id=excluded.photo_id,
                photo_path=excluded.photo_path,
                platform=excluded.platform
        """, (user_id, username, name, age, gender, looking_for,
              partner_age_min, partner_age_max,
              city, church, denomination, church_role, job,
              marital, children, hobbies,
              cover["photo_id"], cover["file_path"], platform))

        # Перезаписываем список фото — старые удаляем, новые вставляем
        await conn.execute("DELETE FROM user_photos WHERE user_id = ?", (user_id,))
        for pos, p in enumerate(normalized):
            await conn.execute(
                "INSERT INTO user_photos (user_id, position, photo_id, file_path) "
                "VALUES (?, ?, ?, ?)",
                (user_id, pos, p["photo_id"], p["file_path"]),
            )
        await conn.commit()


async def get_user_photos(user_id: int):
    """Возвращает список photo_id пользователя в правильном порядке."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT photo_id FROM user_photos WHERE user_id = ? ORDER BY position",
            (user_id,),
        )
        rows = await cur.fetchall()
        if rows:
            return [r[0] for r in rows]
        # Совместимость со старыми анкетами без user_photos
        cur = await conn.execute(
            "SELECT photo_id FROM users WHERE user_id = ?", (user_id,)
        )
        r = await cur.fetchone()
        return [r[0]] if r and r[0] else []


async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_user_by_username(username: str):
    """Поиск пользователя по username (для @username в админ-командах).
    Сравнение регистронезависимое."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_next_profile(user_id: int):
    """Возвращает следующую подходящую анкету.

    Логика подбора:
      - противоположный пол
      - попадает в МОЙ диапазон возраста (partner_age_min..partner_age_max)
        — ЭТО ОДНОСТОРОННИЙ фильтр: только мой выбор. Каждый сам решает,
        кого видеть, поэтому НЕ требуем, чтобы я попадал в их диапазон.
      - я ещё не свайпал (или свайпнул давно — см. ниже)
      - не забанен
      - НЕ скрыл свою анкету

    Про восстановление пропущенных: если я поставил ❌ более 30 дней назад,
    эта анкета снова появится в ленте. Лайки НЕ восстанавливаем — они однократны.
    """
    me = await get_user(user_id)
    if not me:
        return None

    # На случай старой базы / пропавших значений — берём широкий диапазон
    my_min = me.get("partner_age_min") or 18
    my_max = me.get("partner_age_max") or 99

    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row

        # Логика подбора:
        #   - анкета не моя
        #   - противоположный пол
        #   - у неё НЕ включён режим "скрыть"
        #   - я не свайпал ЛИБО свайпнул дизлайк старше 30 дней
        #   - не забанен
        #   - И ОДНО ИЗ:
        #       а) попадает в мой диапазон возраста
        #       б) уже меня лайкнула — тогда показать вне зависимости от возраста
        sql = """
            SELECT u.* FROM users u
            WHERE u.user_id != ?
              AND u.gender = ?
              AND u.looking_for = ?
              AND COALESCE(u.is_hidden, 0) = 0
              AND u.user_id NOT IN (
                  SELECT to_user FROM swipes
                  WHERE from_user = ?
                    AND (
                        action = 'like'
                        OR datetime(created_at) > datetime('now', '-30 days')
                    )
              )
              AND u.user_id NOT IN (SELECT user_id FROM banned)
              AND (
                  u.age BETWEEN ? AND ?
                  OR EXISTS (
                      SELECT 1 FROM swipes likes_me
                      WHERE likes_me.from_user = u.user_id
                        AND likes_me.to_user = ?
                        AND likes_me.action = 'like'
                  )
              )
            ORDER BY RANDOM()
            LIMIT 1
        """
        args = (user_id, me["looking_for"], me["gender"], user_id,
                my_min, my_max, user_id)

        cur = await conn.execute(sql, args)
        row = await cur.fetchone()
        if not row:
            return None

        await conn.execute("""
            INSERT INTO shown (user_id, target_id) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET target_id = excluded.target_id
        """, (user_id, row["user_id"]))
        # Записываем в историю — это позволит листать стрелкой «◀»
        await conn.execute("""
            INSERT INTO view_history (user_id, target_id) VALUES (?, ?)
        """, (user_id, row["user_id"]))
        await conn.commit()
        return dict(row)


async def get_prev_profile(user_id: int):
    """Возвращает предыдущую просмотренную анкету (стрелка «◀»).
    Логика: смотрим в историю, берём предпоследнюю запись,
    делаем её текущей."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row

        # Берём две последние записи: текущую и ту, что до неё
        cur = await conn.execute("""
            SELECT target_id, viewed_at FROM view_history
            WHERE user_id = ?
            ORDER BY viewed_at DESC
            LIMIT 2
        """, (user_id,))
        rows = await cur.fetchall()
        if len(rows) < 2:
            return None  # листать некуда

        # Удаляем текущую запись из истории, чтобы при следующем «назад»
        # можно было уйти ещё глубже
        await conn.execute("""
            DELETE FROM view_history
            WHERE user_id = ? AND viewed_at = ?
        """, (user_id, rows[0]["viewed_at"]))

        prev_id = rows[1]["target_id"]
        # Делаем предыдущую анкету «текущей» — чтобы лайк/дизлайк сработали на неё
        await conn.execute("""
            INSERT INTO shown (user_id, target_id) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET target_id = excluded.target_id
        """, (user_id, prev_id))
        await conn.commit()

        # Достаём анкету
        cur = await conn.execute("SELECT * FROM users WHERE user_id = ?", (prev_id,))
        prof = await cur.fetchone()
        return dict(prof) if prof else None


async def get_last_shown(user_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("SELECT target_id FROM shown WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else None


async def set_last_shown(user_id: int, target_id: int):
    """Запомнить, какую анкету пользователь сейчас смотрит."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("""
            INSERT INTO shown (user_id, target_id) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET target_id = excluded.target_id
        """, (user_id, target_id))
        await conn.commit()


async def add_like(from_user: int, to_user: int) -> bool:
    """Сохраняет лайк. Возвращает True, если случился взаимный матч."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("""
            INSERT OR REPLACE INTO swipes (from_user, to_user, action)
            VALUES (?, ?, 'like')
        """, (from_user, to_user))
        await conn.commit()

        cur = await conn.execute("""
            SELECT 1 FROM swipes
            WHERE from_user = ? AND to_user = ? AND action = 'like'
        """, (to_user, from_user))
        return await cur.fetchone() is not None


async def add_dislike(from_user: int, to_user: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("""
            INSERT OR REPLACE INTO swipes (from_user, to_user, action)
            VALUES (?, ?, 'dislike')
        """, (from_user, to_user))
        await conn.commit()


async def get_matches(user_id: int):
    """Все взаимные лайки данного пользователя (забаненные исключены)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("""
            SELECT u.* FROM users u
            WHERE u.user_id IN (
                SELECT s1.to_user FROM swipes s1
                WHERE s1.from_user = ? AND s1.action = 'like'
                  AND EXISTS (
                      SELECT 1 FROM swipes s2
                      WHERE s2.from_user = s1.to_user
                        AND s2.to_user = ?
                        AND s2.action = 'like'
                  )
            )
              AND u.user_id NOT IN (SELECT user_id FROM banned)
            ORDER BY u.name
        """, (user_id, user_id))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


# ============= БАН-ЛИСТ =============

async def ban_user(user_id: int, reason: str, banned_by: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("""
            INSERT INTO banned (user_id, reason, banned_by)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                reason = excluded.reason,
                banned_by = excluded.banned_by,
                banned_at = CURRENT_TIMESTAMP
        """, (user_id, reason, banned_by))
        await conn.commit()


async def unban_user(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("DELETE FROM banned WHERE user_id = ?", (user_id,))
        await conn.commit()
        return cur.rowcount > 0


async def get_ban(user_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM banned WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_all_bans():
    """Список забаненных c их именами (если есть анкета)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("""
            SELECT b.user_id, b.reason, b.banned_at, u.name
            FROM banned b
            LEFT JOIN users u ON u.user_id = b.user_id
            ORDER BY b.banned_at DESC
        """)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


# ============= ЖАЛОБЫ =============

async def add_report(reporter_id: int, target_id: int, reason: str = None):
    """Записывает жалобу с причиной."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("""
            INSERT INTO reports (reporter_id, target_id, reason)
            VALUES (?, ?, ?)
            ON CONFLICT(reporter_id, target_id) DO UPDATE SET
                reason = COALESCE(excluded.reason, reports.reason),
                reported_at = CURRENT_TIMESTAMP
        """, (reporter_id, target_id, reason))
        await conn.commit()


async def has_reported(reporter_id: int, target_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("""
            SELECT 1 FROM reports
            WHERE reporter_id = ? AND target_id = ?
        """, (reporter_id, target_id))
        return await cur.fetchone() is not None


async def count_reports(target_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM reports WHERE target_id = ?", (target_id,)
        )
        row = await cur.fetchone()
        return row[0] if row else 0


async def get_recent_reports(limit: int = 20):
    """Последние жалобы с именами участников и причинами."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("""
            SELECT
                r.reporter_id, r.target_id, r.reported_at, r.reason,
                ur.name AS reporter_name,
                ut.name AS target_name
            FROM reports r
            LEFT JOIN users ur ON ur.user_id = r.reporter_id
            LEFT JOIN users ut ON ut.user_id = r.target_id
            ORDER BY r.reported_at DESC
            LIMIT ?
        """, (limit,))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_reports_on(target_id: int, limit: int = 20):
    """Жалобы на конкретного пользователя с причинами."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("""
            SELECT r.reporter_id, r.reason, r.reported_at, ur.name AS reporter_name
            FROM reports r
            LEFT JOIN users ur ON ur.user_id = r.reporter_id
            WHERE r.target_id = ?
            ORDER BY r.reported_at DESC
            LIMIT ?
        """, (target_id, limit))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


# ============= СТАТИСТИКА =============

async def get_stats() -> dict:
    """Собираем сводную статистику для админа.
    Возвращаем dict со всеми ключевыми метриками."""
    async with aiosqlite.connect(DB_PATH) as conn:
        async def _one(sql: str, params: tuple = ()) -> int:
            cur = await conn.execute(sql, params)
            row = await cur.fetchone()
            return row[0] if row and row[0] is not None else 0

        async def _list(sql: str, params: tuple = ()):
            cur = await conn.execute(sql, params)
            return await cur.fetchall()

        stats: dict = {}

        # ---- ПОЛЬЗОВАТЕЛИ ----
        stats["users_total"] = await _one("SELECT COUNT(*) FROM users")
        stats["users_male"] = await _one(
            "SELECT COUNT(*) FROM users WHERE gender = 'M'"
        )
        stats["users_female"] = await _one(
            "SELECT COUNT(*) FROM users WHERE gender = 'F'"
        )
        stats["users_24h"] = await _one(
            "SELECT COUNT(*) FROM users WHERE created_at >= datetime('now', '-1 day')"
        )
        stats["users_7d"] = await _one(
            "SELECT COUNT(*) FROM users WHERE created_at >= datetime('now', '-7 day')"
        )
        stats["users_30d"] = await _one(
            "SELECT COUNT(*) FROM users WHERE created_at >= datetime('now', '-30 day')"
        )
        stats["users_active_7d"] = await _one("""
            SELECT COUNT(DISTINCT from_user) FROM swipes
            WHERE created_at >= datetime('now', '-7 day')
        """)

        # ---- СВАЙПЫ ----
        stats["swipes_total"] = await _one("SELECT COUNT(*) FROM swipes")
        stats["likes_total"] = await _one(
            "SELECT COUNT(*) FROM swipes WHERE action = 'like'"
        )
        stats["dislikes_total"] = await _one(
            "SELECT COUNT(*) FROM swipes WHERE action = 'dislike'"
        )
        stats["swipes_24h"] = await _one(
            "SELECT COUNT(*) FROM swipes WHERE created_at >= datetime('now', '-1 day')"
        )
        if stats["swipes_total"] > 0:
            stats["like_rate"] = round(
                100 * stats["likes_total"] / stats["swipes_total"], 1
            )
        else:
            stats["like_rate"] = 0.0

        # ---- МАТЧИ ----
        # Делим на 2, потому что каждая пара считается дважды (a->b и b->a)
        stats["matches_total"] = await _one("""
            SELECT COUNT(*) / 2 FROM swipes s1
            WHERE s1.action = 'like'
              AND EXISTS (
                  SELECT 1 FROM swipes s2
                  WHERE s2.from_user = s1.to_user
                    AND s2.to_user = s1.from_user
                    AND s2.action = 'like'
              )
        """)
        # Новые матчи: считаем по более позднему лайку в паре
        stats["matches_24h"] = await _one("""
            SELECT COUNT(*) FROM swipes s1
            WHERE s1.action = 'like'
              AND s1.created_at >= datetime('now', '-1 day')
              AND EXISTS (
                  SELECT 1 FROM swipes s2
                  WHERE s2.from_user = s1.to_user
                    AND s2.to_user = s1.from_user
                    AND s2.action = 'like'
                    AND s2.created_at <= s1.created_at
              )
        """)
        stats["matches_7d"] = await _one("""
            SELECT COUNT(*) FROM swipes s1
            WHERE s1.action = 'like'
              AND s1.created_at >= datetime('now', '-7 day')
              AND EXISTS (
                  SELECT 1 FROM swipes s2
                  WHERE s2.from_user = s1.to_user
                    AND s2.to_user = s1.from_user
                    AND s2.action = 'like'
                    AND s2.created_at <= s1.created_at
              )
        """)

        # ---- ТОП ГОРОДОВ И ЦЕРКВЕЙ ----
        rows = await _list("""
            SELECT city, COUNT(*) as cnt FROM users
            GROUP BY LOWER(TRIM(city))
            ORDER BY cnt DESC LIMIT 5
        """)
        stats["top_cities"] = [(r[0], r[1]) for r in rows]

        rows = await _list("""
            SELECT church, COUNT(*) as cnt FROM users
            GROUP BY LOWER(TRIM(church))
            ORDER BY cnt DESC LIMIT 5
        """)
        stats["top_churches"] = [(r[0], r[1]) for r in rows]

        # ---- МОДЕРАЦИЯ ----
        stats["reports_total"] = await _one("SELECT COUNT(*) FROM reports")
        stats["reports_24h"] = await _one(
            "SELECT COUNT(*) FROM reports WHERE reported_at >= datetime('now', '-1 day')"
        )
        stats["banned_total"] = await _one("SELECT COUNT(*) FROM banned")

        return stats


async def get_user_info(user_id: int) -> dict:
    """Полная информация о пользователе для админа:
    анкета + активность (свайпы, жалобы, матчи, статус бана)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row

        async def _one(sql: str, params: tuple = ()) -> int:
            cur = await conn.execute(sql, params)
            row = await cur.fetchone()
            return row[0] if row and row[0] is not None else 0

        info: dict = {"user_id": user_id}

        # Анкета
        cur = await conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        u = await cur.fetchone()
        info["profile"] = dict(u) if u else None

        # Статус бана
        cur = await conn.execute("SELECT * FROM banned WHERE user_id = ?", (user_id,))
        ban = await cur.fetchone()
        info["ban"] = dict(ban) if ban else None

        # Активность пользователя (что ОН делал)
        info["my_likes"] = await _one(
            "SELECT COUNT(*) FROM swipes WHERE from_user = ? AND action = 'like'",
            (user_id,)
        )
        info["my_dislikes"] = await _one(
            "SELECT COUNT(*) FROM swipes WHERE from_user = ? AND action = 'dislike'",
            (user_id,)
        )
        info["my_reports"] = await _one(
            "SELECT COUNT(*) FROM reports WHERE reporter_id = ?", (user_id,)
        )

        # Что получил ОТ других
        info["likes_received"] = await _one(
            "SELECT COUNT(*) FROM swipes WHERE to_user = ? AND action = 'like'",
            (user_id,)
        )
        info["dislikes_received"] = await _one(
            "SELECT COUNT(*) FROM swipes WHERE to_user = ? AND action = 'dislike'",
            (user_id,)
        )
        info["reports_against"] = await _one(
            "SELECT COUNT(*) FROM reports WHERE target_id = ?", (user_id,)
        )

        # Матчи (взаимные лайки)
        info["matches"] = await _one("""
            SELECT COUNT(*) FROM swipes s1
            WHERE s1.from_user = ? AND s1.action = 'like'
              AND EXISTS (
                  SELECT 1 FROM swipes s2
                  WHERE s2.from_user = s1.to_user
                    AND s2.to_user = s1.from_user
                    AND s2.action = 'like'
              )
        """, (user_id,))

        # Последняя активность
        cur = await conn.execute(
            "SELECT MAX(created_at) FROM swipes WHERE from_user = ?", (user_id,)
        )
        row = await cur.fetchone()
        info["last_active"] = row[0] if row else None

        return info


async def reset_user_swipes(user_id: int):
    """Сбросить ЛИЧНУЮ историю взаимодействий пользователя.
    Удаляем:
      - его свайпы (лайки/дизлайки от него) — чтобы ленту начать сначала
      - его историю просмотров (для стрелки «◀»)
      - запись о последней показанной анкете

    НЕ трогаем:
      - чужие свайпы В ЕГО адрес (это чужие данные)
      - его матчи (если у него уже были взаимные лайки, и обе стороны
        стояли — после сброса односторонняя «нить» с его стороны порвётся,
        но это нормально: при следующей встрече он сможет лайкнуть заново)
      - бан, жалобы на него или от него (модерация неприкосновенна)
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("DELETE FROM swipes WHERE from_user = ?", (user_id,))
        await conn.execute("DELETE FROM view_history WHERE user_id = ?", (user_id,))
        await conn.execute("DELETE FROM shown WHERE user_id = ?", (user_id,))
        await conn.commit()


# ============= СОГЛАСИЕ НА ОБРАБОТКУ ПДн (152-ФЗ) =============

async def has_consent(user_id: int) -> bool:
    """Принял ли пользователь согласие на обработку персональных данных."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT 1 FROM consents WHERE user_id = ?", (user_id,)
        )
        return await cur.fetchone() is not None


async def grant_consent(user_id: int, version: str = "v1"):
    """Записываем факт согласия. Важно для подтверждения, что у нас
    есть основание обрабатывать данные пользователя по 152-ФЗ."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("""
            INSERT INTO consents (user_id, version) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                accepted_at = CURRENT_TIMESTAMP,
                version = excluded.version
        """, (user_id, version))
        await conn.commit()


async def revoke_consent(user_id: int):
    """Отозвать согласие. Используется при /forget или если
    пользователь явно отказывается от обработки данных."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("DELETE FROM consents WHERE user_id = ?", (user_id,))
        await conn.commit()


async def delete_user_completely(user_id: int):
    """Полное удаление пользователя по запросу (право на забвение, 152-ФЗ ст.14).
    Чистим:
      - анкету (users)
      - фотографии (user_photos) — записи в БД
      - физические файлы фото с диска
      - все его свайпы (от него и к нему)
      - историю просмотров
      - shown
      - жалобы (от него и на него)
      - бан (если был)
      - согласие
    """
    import shutil
    # Сначала удаляем физические файлы — пока ещё знаем путь
    photo_dir = _os.path.join(PHOTOS_DIR, str(abs(user_id)))
    if _os.path.exists(photo_dir):
        try:
            shutil.rmtree(photo_dir)
        except Exception as e:
            # Не критично — удаление данных в БД важнее
            import logging
            logging.warning(f"Не смог удалить папку {photo_dir}: {e}")

    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        await conn.execute("DELETE FROM user_photos WHERE user_id = ?", (user_id,))
        await conn.execute(
            "DELETE FROM swipes WHERE from_user = ? OR to_user = ?",
            (user_id, user_id),
        )
        await conn.execute("DELETE FROM view_history WHERE user_id = ?", (user_id,))
        await conn.execute("DELETE FROM shown WHERE user_id = ?", (user_id,))
        await conn.execute(
            "DELETE FROM reports WHERE reporter_id = ? OR target_id = ?",
            (user_id, user_id),
        )
        await conn.execute("DELETE FROM banned WHERE user_id = ?", (user_id,))
        await conn.execute("DELETE FROM consents WHERE user_id = ?", (user_id,))
        await conn.commit()


# ============= МУЛЬТИПЛАТФОРМЕННОСТЬ =============

def vk_id_to_db_id(vk_user_id: int) -> int:
    """Преобразует VK user_id (положительное число) в наш db_id (отрицательное).
    Так VK-пользователи не конфликтуют с Telegram в общей таблице."""
    if vk_user_id <= 0:
        raise ValueError(f"VK user_id должен быть > 0, получено: {vk_user_id}")
    return -vk_user_id


def db_id_to_vk_id(db_user_id: int) -> int:
    """Обратное преобразование."""
    if db_user_id >= 0:
        raise ValueError(f"db_user_id для VK должен быть < 0, получено: {db_user_id}")
    return -db_user_id


def is_vk_user(db_user_id: int) -> bool:
    """Проверяет, что пользователь из VK (по знаку id)."""
    return db_user_id < 0


def is_tg_user(db_user_id: int) -> bool:
    """Проверяет, что пользователь из Telegram."""
    return db_user_id > 0


def contact_link(user_row: dict) -> str:
    """Формирует ссылку для связи в зависимости от платформы.
    user_row — запись из таблицы users (содержит platform и username/user_id)."""
    platform = user_row.get("platform", "tg")
    username = user_row.get("username")
    user_id = user_row["user_id"]

    if platform == "vk":
        vk_id = db_id_to_vk_id(user_id) if user_id < 0 else user_id
        # username в VK — это короткое имя страницы (screen_name), если есть
        if username:
            return f"https://vk.com/{username}"
        return f"https://vk.com/id{vk_id}"
    else:  # tg
        if username:
            return f"https://t.me/{username}"
        return f"tg://user?id={user_id}"


# ============= УНИВЕРСАЛЬНЫЙ ДОСТУП К ФОТО =============

import os as _os

# Папка для локального хранения фотографий
PHOTOS_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "photos")


def ensure_photos_dir():
    """Создаём папку для фото, если её нет."""
    _os.makedirs(PHOTOS_DIR, exist_ok=True)


def user_photos_dir(db_user_id: int) -> str:
    """Папка под фото конкретного пользователя.
    Используем абсолютное значение id для имени папки —
    отрицательные VK-id превращаются в положительные."""
    folder = _os.path.join(PHOTOS_DIR, str(abs(db_user_id)))
    _os.makedirs(folder, exist_ok=True)
    return folder


async def get_user_photos_with_paths(user_id: int):
    """Возвращает список словарей вместо просто id.
    Каждый элемент: {photo_id, file_path}.
    file_path может быть None — тогда придётся использовать photo_id (старая система).
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT photo_id, file_path FROM user_photos "
            "WHERE user_id = ? ORDER BY position",
            (user_id,),
        )
        rows = await cur.fetchall()
        if rows:
            return [{"photo_id": r[0], "file_path": r[1]} for r in rows]
        # Совместимость со старыми анкетами
        cur = await conn.execute(
            "SELECT photo_id, photo_path FROM users WHERE user_id = ?",
            (user_id,),
        )
        r = await cur.fetchone()
        if r and r[0]:
            return [{"photo_id": r[0], "file_path": r[1]}]
        return []


# ============= КРОСС-ПЛАТФОРМЕННЫЕ УВЕДОМЛЕНИЯ =============

# Очередь уведомлений, которые должны быть доставлены через другой бот.
# Например, VK-юзер лайкнул TG-юзера → матч. VK-бот не может отправить
# в Telegram напрямую, поэтому кладёт запись сюда. TG-бот раз в несколько
# секунд читает очередь и доставляет.
# Аналогично для уведомлений админам о жалобах от VK-юзеров.

async def _ensure_notifications_table():
    """Создаёт таблицу очереди уведомлений (вызывается из init_db и при первом использовании)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript("""
            CREATE TABLE IF NOT EXISTS pending_notifications (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                kind         TEXT NOT NULL,       -- 'match' | 'admin_report'
                recipient_id INTEGER,             -- кому доставить (для match)
                partner_id   INTEGER,             -- для match — id матча
                payload      TEXT,                -- JSON с доп.данными (для admin_report)
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                delivered    INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_pending_undelivered
                ON pending_notifications (delivered, kind);
        """)
        await conn.commit()


async def queue_match_notification(recipient_id: int, partner_id: int):
    """Уведомление о матче для TG-юзера, инициированное из VK-бота."""
    await _ensure_notifications_table()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO pending_notifications (kind, recipient_id, partner_id) "
            "VALUES ('match', ?, ?)",
            (recipient_id, partner_id),
        )
        await conn.commit()


async def queue_admin_report(reporter_id: int, target_id: int, total: int,
                              reason: str = None):
    """Жалоба от VK-юзера на кого-то — нужно уведомить TG-админов."""
    import json
    await _ensure_notifications_table()
    payload = json.dumps({
        "reporter_id": reporter_id,
        "target_id": target_id,
        "total": total,
        "reason": reason,
    })
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO pending_notifications (kind, payload) VALUES ('admin_report', ?)",
            (payload,),
        )
        await conn.commit()


async def queue_system_message(recipient_id: int, text: str):
    """Системное сообщение в VK или TG (например, уведомление о бане).
    Кладём в общую очередь — соответствующий бот разошлёт."""
    import json
    await _ensure_notifications_table()
    payload = json.dumps({"text": text})
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO pending_notifications (kind, recipient_id, payload) "
            "VALUES ('system_message', ?, ?)",
            (recipient_id, payload),
        )
        await conn.commit()


async def get_pending_notifications(
        kinds: tuple = ("match", "admin_report", "system_message", "broadcast"),
        limit: int = 100):
    """Возвращает недоставленные уведомления. Бот будет вызывать эту
    функцию из фоновой задачи."""
    placeholders = ",".join("?" for _ in kinds)
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            f"SELECT * FROM pending_notifications "
            f"WHERE delivered = 0 AND kind IN ({placeholders}) "
            f"ORDER BY id LIMIT ?",
            (*kinds, limit),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def mark_notification_delivered(notification_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE pending_notifications SET delivered = 1 WHERE id = ?",
            (notification_id,),
        )
        await conn.commit()


async def update_profile_fields(user_id: int, **fields):
    """Обновить отдельные поля анкеты не трогая остальное.
    Используется для дозаполнения старых анкет: конфессия, служение, работа.
    Допустимые поля: denomination, church_role, job, и любые другие из users.
    """
    if not fields:
        return
    # Защита: разрешаем обновлять только безопасные поля
    allowed = {
        "denomination", "church_role", "job",
        "name", "age", "city", "church", "hobbies",
        "marital", "children",
        "partner_age_min", "partner_age_max",
    }
    set_parts = []
    values = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        set_parts.append(f"{k} = ?")
        values.append(v)
    if not set_parts:
        return
    values.append(user_id)
    sql = f"UPDATE users SET {', '.join(set_parts)} WHERE user_id = ?"
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(sql, values)
        await conn.commit()


async def needs_legacy_fillin(user_id: int) -> bool:
    """Возвращает True, если у пользователя есть анкета, но ещё не заполнены
    новые поля (denomination, church_role). job не считаем — он опционален."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT denomination, church_role FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        if not row:
            return False  # анкеты нет — ничего дозаполнять
        denom, role = row
        return not denom or not role


# ============= РАССЫЛКА (/rassylka) =============

async def get_broadcast_recipients(
    platform: str = "all",       # "all" | "tg" | "vk"
    gender: str = "all",          # "all" | "M" | "F"
    age_min: int | None = None,   # None = без ограничения
    age_max: int | None = None,
    city: str | None = None,      # None = любой
    denomination: str | None = None,
):
    """Возвращает список (user_id, name) — кому отправлять рассылку.
    Исключаем забаненных и тех, кто не давал согласие."""
    where = ["1=1"]
    params = []

    # Фильтр по платформе через знак user_id (положительный=TG, отрицательный=VK)
    if platform == "tg":
        where.append("u.user_id > 0")
    elif platform == "vk":
        where.append("u.user_id < 0")

    if gender in ("M", "F"):
        where.append("u.gender = ?")
        params.append(gender)

    if age_min is not None:
        where.append("u.age >= ?")
        params.append(age_min)
    if age_max is not None:
        where.append("u.age <= ?")
        params.append(age_max)

    if city:
        where.append("LOWER(u.city) = LOWER(?)")
        params.append(city.strip())

    if denomination:
        where.append("LOWER(u.denomination) = LOWER(?)")
        params.append(denomination.strip())

    # Только незабаненные
    where.append(
        "NOT EXISTS (SELECT 1 FROM banned b WHERE b.user_id = u.user_id)"
    )
    # Только давшие согласие
    where.append(
        "EXISTS (SELECT 1 FROM consents c WHERE c.user_id = u.user_id)"
    )

    sql = f"""
        SELECT u.user_id, u.name
        FROM users u
        WHERE {' AND '.join(where)}
        ORDER BY u.user_id
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(sql, params)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def count_broadcast_recipients(**filters) -> dict:
    """Возвращает разбивку по платформам: {'total': N, 'tg': X, 'vk': Y}.
    Принимает те же фильтры что и get_broadcast_recipients."""
    recipients = await get_broadcast_recipients(**filters)
    tg = sum(1 for r in recipients if r["user_id"] > 0)
    vk = sum(1 for r in recipients if r["user_id"] < 0)
    return {"total": len(recipients), "tg": tg, "vk": vk}


async def list_distinct_cities(min_users: int = 1) -> list[str]:
    """Список городов где есть хотя бы min_users пользователей. Для админа — справочно."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("""
            SELECT city, COUNT(*) as cnt FROM users
            WHERE EXISTS (SELECT 1 FROM consents c WHERE c.user_id = users.user_id)
              AND NOT EXISTS (SELECT 1 FROM banned b WHERE b.user_id = users.user_id)
            GROUP BY LOWER(city) HAVING cnt >= ?
            ORDER BY cnt DESC
        """, (min_users,))
        rows = await cur.fetchall()
        return [r[0] for r in rows]


# ============= ОЧЕРЕДЬ РАССЫЛОК (для VK) =============
# VK-бот опрашивает очередь и отправляет тем VK-юзерам, которых ему назначил TG-бот.
# Так мы не дублируем код отправки в двух процессах.

async def queue_broadcast_chunk(vk_user_ids: list[int], text: str,
                                 batch_id: int):
    """Кладёт партию VK-получателей в очередь. VK-бот их разошлёт.
    batch_id — общий идентификатор рассылки (для статистики)."""
    import json
    await _ensure_notifications_table()
    payload = json.dumps({"text": text, "batch_id": batch_id})
    async with aiosqlite.connect(DB_PATH) as conn:
        for vk_id in vk_user_ids:
            await conn.execute(
                "INSERT INTO pending_notifications (kind, recipient_id, payload) "
                "VALUES ('broadcast', ?, ?)",
                (vk_id, payload),
            )
        await conn.commit()


async def get_broadcast_stats(batch_id: int) -> dict:
    """Сколько уже доставлено для конкретной рассылки (VK-часть).
    Возвращает {'delivered': X, 'pending': Y}"""
    import json
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("""
            SELECT delivered, COUNT(*) FROM pending_notifications
            WHERE kind = 'broadcast'
              AND json_extract(payload, '$.batch_id') = ?
            GROUP BY delivered
        """, (batch_id,))
        rows = await cur.fetchall()
        result = {"delivered": 0, "pending": 0}
        for delivered, count in rows:
            if delivered:
                result["delivered"] = count
            else:
                result["pending"] = count
        return result


# ============= СКРЫТИЕ АНКЕТЫ («Скрыть меня») =============

async def is_hidden(user_id: int) -> bool:
    """Скрыта ли анкета этим юзером."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT COALESCE(is_hidden, 0) FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        return bool(row and row[0])


async def hide_user(user_id: int):
    """Скрывает анкету — она не показывается другим и не пускает листать."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE users SET is_hidden = 1, hidden_at = CURRENT_TIMESTAMP "
            "WHERE user_id = ?",
            (user_id,),
        )
        await conn.commit()


async def unhide_user(user_id: int):
    """Возвращает анкету в ленту."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE users SET is_hidden = 0, hidden_at = NULL "
            "WHERE user_id = ?",
            (user_id,),
        )
        await conn.commit()


async def extend_hide(user_id: int):
    """Продлить скрытие ещё на 30 дней — сбрасываем hidden_at на сейчас."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE users SET hidden_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (user_id,),
        )
        await conn.commit()


async def get_users_hidden_30_days() -> list[dict]:
    """Юзеры, у которых скрытие ≥ 30 дней и мы им ещё не отправляли напоминание.
    Возвращаем всех подходящих — напоминание шлётся раз, поскольку после
    extend_hide() поле hidden_at обновится и таймер начнётся заново."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("""
            SELECT user_id, name, hidden_at FROM users
            WHERE is_hidden = 1
              AND hidden_at IS NOT NULL
              AND datetime(hidden_at) < datetime('now', '-30 days')
        """)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


# ============= ИТОГ ЛАЙКОВ ЗА ДЕНЬ =============

async def get_daily_likes_recipients() -> list[dict]:
    """Возвращает список тех, кому нужно отправить итог лайков.
    Условия:
      - за последние 24 часа получил ≥ 1 лайк
      - анкета не скрыта, не забанен
      - последнее отправленное «итог лайков» уведомление было > 20 часов назад
        (чтобы не задваивать, если бот перезапустился)

    Каждая запись: {user_id, name, likes_count}."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("""
            SELECT u.user_id, u.name, COUNT(s.from_user) AS likes_count
            FROM users u
            JOIN swipes s ON s.to_user = u.user_id
                          AND s.action = 'like'
                          AND datetime(s.created_at) > datetime('now', '-24 hours')
            WHERE COALESCE(u.is_hidden, 0) = 0
              AND u.user_id NOT IN (SELECT user_id FROM banned)
              AND (
                  u.last_daily_likes_notif IS NULL
                  OR datetime(u.last_daily_likes_notif) < datetime('now', '-20 hours')
              )
              -- отправляем только тем от кого лайк пришёл после последнего уведомления
              AND (
                  u.last_daily_likes_notif IS NULL
                  OR datetime(s.created_at) > datetime(u.last_daily_likes_notif)
              )
            GROUP BY u.user_id
            HAVING likes_count > 0
        """)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def mark_daily_likes_notified(user_ids: list[int]):
    """Отмечаем что этим юзерам мы уже отправили итог."""
    if not user_ids:
        return
    async with aiosqlite.connect(DB_PATH) as conn:
        placeholders = ",".join("?" for _ in user_ids)
        await conn.execute(
            f"UPDATE users SET last_daily_likes_notif = CURRENT_TIMESTAMP "
            f"WHERE user_id IN ({placeholders})",
            user_ids,
        )
        await conn.commit()


# ============= ЕЖЕНЕДЕЛЬНОЕ УВЕДОМЛЕНИЕ О НОВЫХ АНКЕТАХ =============

async def get_weekly_notif_recipients() -> list[dict]:
    """Считает для каждого активного юзера, сколько новых анкет
    противоположного пола появилось за неделю в его возрастном диапазоне.

    Возвращаем только тех, у кого фактически ≥ 1 новая анкета."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("""
            SELECT u.user_id, u.name, u.gender,
                   COALESCE(u.partner_age_min, 18) AS a_min,
                   COALESCE(u.partner_age_max, 99) AS a_max,
                   (
                     SELECT COUNT(*) FROM users n
                     WHERE n.gender = u.looking_for
                       AND n.looking_for = u.gender
                       AND n.age BETWEEN COALESCE(u.partner_age_min, 18)
                                     AND COALESCE(u.partner_age_max, 99)
                       AND COALESCE(n.is_hidden, 0) = 0
                       AND n.user_id NOT IN (SELECT user_id FROM banned)
                       AND datetime(n.created_at) > datetime('now', '-7 days')
                       AND n.user_id != u.user_id
                   ) AS new_count
            FROM users u
            WHERE COALESCE(u.is_hidden, 0) = 0
              AND u.user_id NOT IN (SELECT user_id FROM banned)
              -- не отправляли в этот раз (в этот вызов):
              AND (
                  u.last_weekly_notif IS NULL
                  OR datetime(u.last_weekly_notif) < datetime('now', '-5 days')
              )
        """)
        rows = await cur.fetchall()
        return [dict(r) for r in rows if r["new_count"] > 0]


async def mark_weekly_notified(user_ids: list[int]):
    if not user_ids:
        return
    async with aiosqlite.connect(DB_PATH) as conn:
        placeholders = ",".join("?" for _ in user_ids)
        await conn.execute(
            f"UPDATE users SET last_weekly_notif = CURRENT_TIMESTAMP "
            f"WHERE user_id IN ({placeholders})",
            user_ids,
        )
        await conn.commit()


# ============= РОЛЕВАЯ МОДЕЛЬ (admin / moderator) =============
# Root-админы — из .env, в этой таблице их нет.
# Все проверки прав в боте склеивают ".env-роот" + "запись в admins".

async def get_role(user_id: int) -> str | None:
    """Возвращает роль юзера из таблицы admins: 'admin' | 'moderator' | None.
    NB: root-статус (из .env) здесь НЕ учитывается — его проверяет сам бот."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT role FROM admins WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def set_role(user_id: int, role: str, assigned_by: int):
    """Назначить роль (создать или перезаписать). role: 'admin' | 'moderator'."""
    if role not in ("admin", "moderator"):
        raise ValueError(f"Недопустимая роль: {role}")
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("""
            INSERT INTO admins (user_id, role, assigned_by) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                role = excluded.role,
                assigned_by = excluded.assigned_by,
                assigned_at = CURRENT_TIMESTAMP
        """, (user_id, role, assigned_by))
        await conn.commit()


async def remove_role(user_id: int) -> bool:
    """Разжаловать (удалить запись). Возвращает True если запись была."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "DELETE FROM admins WHERE user_id = ?", (user_id,)
        )
        await conn.commit()
        return cur.rowcount > 0


async def list_admins_by_role(role: str) -> list[dict]:
    """Список юзеров с указанной ролью, с их именами (из users, если есть)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("""
            SELECT a.user_id, a.role, a.assigned_at, a.assigned_by,
                   u.name, u.username
            FROM admins a
            LEFT JOIN users u ON u.user_id = a.user_id
            WHERE a.role = ?
            ORDER BY a.assigned_at DESC
        """, (role,))
        return [dict(r) for r in await cur.fetchall()]


async def get_all_admin_ids() -> set[int]:
    """Все user_id с ролью admin (для быстрой проверки в боте)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT user_id FROM admins WHERE role = 'admin'"
        )
        return {r[0] for r in await cur.fetchall()}


async def get_all_moderator_ids() -> set[int]:
    """Все user_id с ролью moderator."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT user_id FROM admins WHERE role = 'moderator'"
        )
        return {r[0] for r in await cur.fetchall()}
