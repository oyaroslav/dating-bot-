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
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                name        TEXT NOT NULL,
                age         INTEGER NOT NULL,
                gender      TEXT NOT NULL,           -- 'M' / 'F'
                looking_for TEXT NOT NULL,           -- 'M' / 'F' (только противоположный пол)
                city        TEXT NOT NULL,
                church      TEXT NOT NULL,
                marital     TEXT NOT NULL,
                children    TEXT NOT NULL,
                hobbies     TEXT NOT NULL,
                photo_id    TEXT NOT NULL,           -- главное фото (обложка)
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Все фото пользователя (1..5 штук) с порядком
            CREATE TABLE IF NOT EXISTS user_photos (
                user_id    INTEGER NOT NULL,
                position   INTEGER NOT NULL,         -- 0,1,2,3,4
                photo_id   TEXT NOT NULL,
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
                reported_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (reporter_id, target_id)
            );
            CREATE INDEX IF NOT EXISTS idx_reports_target
                ON reports (target_id);
            CREATE INDEX IF NOT EXISTS idx_reports_time
                ON reports (reported_at DESC);
        """)
        await conn.commit()


async def save_user(*, user_id, username, name, age, gender, looking_for,
                    city, church, marital, children, hobbies, photos):
    """Сохраняем пользователя и его фотографии.
    photos — список photo_id (1..5 штук). Первый = обложка."""
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
    photo_id = photos[0]  # обложка

    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("""
            INSERT INTO users (user_id, username, name, age, gender, looking_for,
                               city, church, marital, children, hobbies, photo_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                name=excluded.name,
                age=excluded.age,
                gender=excluded.gender,
                looking_for=excluded.looking_for,
                city=excluded.city,
                church=excluded.church,
                marital=excluded.marital,
                children=excluded.children,
                hobbies=excluded.hobbies,
                photo_id=excluded.photo_id
        """, (user_id, username, name, age, gender, looking_for,
              city, church, marital, children, hobbies, photo_id))

        # Перезаписываем список фото — старые удаляем, новые вставляем
        await conn.execute("DELETE FROM user_photos WHERE user_id = ?", (user_id,))
        for pos, pid in enumerate(photos[:5]):  # максимум 5
            await conn.execute(
                "INSERT INTO user_photos (user_id, position, photo_id) VALUES (?, ?, ?)",
                (user_id, pos, pid),
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


async def get_next_profile(user_id: int):
    """Возвращает следующую подходящую анкету.
    Логика: показываем только анкеты противоположного пола, у которых
    looking_for совпадает с моим полом, и которые я ещё не свайпал."""
    me = await get_user(user_id)
    if not me:
        return None

    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row

        # Ищем анкеты:
        #   - не моя
        #   - пол = тот, кого я ищу (looking_for)
        #   - этот человек ищет мой пол
        #   - я ещё не нажимал на эту анкету
        sql = """
            SELECT u.* FROM users u
            WHERE u.user_id != ?
              AND u.gender = ?
              AND u.looking_for = ?
              AND u.user_id NOT IN (
                  SELECT to_user FROM swipes WHERE from_user = ?
              )
              AND u.user_id NOT IN (SELECT user_id FROM banned)
            ORDER BY RANDOM()
            LIMIT 1
        """
        args = (user_id, me["looking_for"], me["gender"], user_id)

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

async def add_report(reporter_id: int, target_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("""
            INSERT OR IGNORE INTO reports (reporter_id, target_id)
            VALUES (?, ?)
        """, (reporter_id, target_id))
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
    """Последние жалобы с именами участников."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("""
            SELECT
                r.reporter_id, r.target_id, r.reported_at,
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
