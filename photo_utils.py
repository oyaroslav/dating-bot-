"""
Утилиты для скачивания фото с серверов мессенджеров и сохранения локально.
Часть мульти-платформенной архитектуры — фото лежат на нашем VPS,
оба бота читают их с диска и отправляют через свой API.
"""
import asyncio
import logging
import os
from typing import Optional

import aiohttp

import database as db

log = logging.getLogger("photo_utils")


# Максимальный размер фото — 10 МБ (защита от диверсии)
MAX_PHOTO_SIZE = 10 * 1024 * 1024


async def download_tg_photo(bot, file_id: str, save_path: str) -> bool:
    """Скачивает фото из Telegram по file_id и сохраняет на диск.

    Использует aiogram-бот для получения URL файла (метод getFile),
    затем напрямую скачивает через aiohttp (без проксирования через aiogram).

    Возвращает True при успехе.
    """
    try:
        tg_file = await bot.get_file(file_id)
        # У Telegram есть метод download_file, но мы качаем напрямую через aiohttp
        # — так быстрее и не зависит от внутренностей aiogram
        token = bot.token
        url = f"https://api.telegram.org/file/bot{token}/{tg_file.file_path}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    log.warning(f"TG file download failed: HTTP {resp.status}")
                    return False
                # Проверяем размер
                size_header = resp.headers.get("Content-Length")
                if size_header and int(size_header) > MAX_PHOTO_SIZE:
                    log.warning(f"TG file too big: {size_header} bytes")
                    return False

                # Создаём папку под файл, если её нет
                os.makedirs(os.path.dirname(save_path), exist_ok=True)

                # Скачиваем порциями (чтобы не съесть память на больших файлах)
                downloaded = 0
                with open(save_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        downloaded += len(chunk)
                        if downloaded > MAX_PHOTO_SIZE:
                            log.warning("TG file exceeds size limit, aborting")
                            f.close()
                            os.remove(save_path)
                            return False
                        f.write(chunk)
        log.info(f"TG photo saved: {save_path} ({downloaded} bytes)")
        return True
    except Exception as e:
        log.exception(f"download_tg_photo failed for {file_id}: {e}")
        return False


async def download_vk_photo(url: str, save_path: str) -> bool:
    """Скачивает фото из ВК по прямому URL (ВК отдаёт URL'ы в Photo.sizes).
    Сохраняет на диск.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    log.warning(f"VK photo download failed: HTTP {resp.status}")
                    return False
                size_header = resp.headers.get("Content-Length")
                if size_header and int(size_header) > MAX_PHOTO_SIZE:
                    log.warning(f"VK photo too big: {size_header} bytes")
                    return False

                os.makedirs(os.path.dirname(save_path), exist_ok=True)

                downloaded = 0
                with open(save_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        downloaded += len(chunk)
                        if downloaded > MAX_PHOTO_SIZE:
                            f.close()
                            os.remove(save_path)
                            return False
                        f.write(chunk)
        log.info(f"VK photo saved: {save_path} ({downloaded} bytes)")
        return True
    except Exception as e:
        log.exception(f"download_vk_photo failed for {url}: {e}")
        return False


# ============= Миграция существующих TG-фото =============

async def migrate_all_tg_photos(bot, progress_callback=None) -> dict:
    """Проходит по всем фото в user_photos и users, у которых нет file_path,
    и скачивает их с серверов Telegram. Заполняет file_path в БД.

    Работает только для платформы 'tg' — для VK фото загружаются при регистрации.

    Возвращает статистику {total, success, failed, skipped}.
    progress_callback(done, total) вызывается после каждого скачивания.
    """
    import aiosqlite

    db.ensure_photos_dir()
    stats = {"total": 0, "success": 0, "failed": 0, "skipped": 0}

    # Сначала собираем список всех фото к миграции
    tasks = []  # список (user_id, position, photo_id, target_path)

    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row

        # user_photos — все фотки пользователей TG, без file_path
        cur = await conn.execute("""
            SELECT up.user_id, up.position, up.photo_id
            FROM user_photos up
            JOIN users u ON u.user_id = up.user_id
            WHERE up.file_path IS NULL
              AND u.platform = 'tg'
              AND u.user_id > 0
            ORDER BY up.user_id, up.position
        """)
        for row in await cur.fetchall():
            user_id, pos, photo_id = row["user_id"], row["position"], row["photo_id"]
            folder = db.user_photos_dir(user_id)
            target = os.path.join(folder, f"{pos}.jpg")
            tasks.append(("user_photos", user_id, pos, photo_id, target))

        # users — обложки (на случай если кто-то регистрировался ещё до таблицы user_photos)
        cur = await conn.execute("""
            SELECT user_id, photo_id
            FROM users
            WHERE photo_path IS NULL
              AND platform = 'tg'
              AND user_id > 0
              AND NOT EXISTS (
                  SELECT 1 FROM user_photos WHERE user_photos.user_id = users.user_id
              )
        """)
        for row in await cur.fetchall():
            user_id, photo_id = row["user_id"], row["photo_id"]
            folder = db.user_photos_dir(user_id)
            target = os.path.join(folder, "0.jpg")
            tasks.append(("users", user_id, 0, photo_id, target))

    stats["total"] = len(tasks)
    log.info(f"Миграция фото: к обработке {stats['total']} файлов")

    for i, (source_table, user_id, pos, photo_id, target) in enumerate(tasks):
        # Если файл уже скачан (мы запускали миграцию ранее) — пропускаем,
        # просто обновляем БД
        if os.path.exists(target) and os.path.getsize(target) > 0:
            ok = True
            stats["skipped"] += 1
        else:
            ok = await download_tg_photo(bot, photo_id, target)
            if ok:
                stats["success"] += 1
            else:
                stats["failed"] += 1

        if ok:
            # Записываем путь в БД
            async with aiosqlite.connect(db.DB_PATH) as conn:
                if source_table == "user_photos":
                    await conn.execute(
                        "UPDATE user_photos SET file_path = ? "
                        "WHERE user_id = ? AND position = ?",
                        (target, user_id, pos),
                    )
                # В таблице users — обновляем обложку (всегда позиция 0)
                if pos == 0:
                    await conn.execute(
                        "UPDATE users SET photo_path = ? WHERE user_id = ?",
                        (target, user_id),
                    )
                await conn.commit()

        if progress_callback:
            await progress_callback(i + 1, stats["total"])

        # Небольшая пауза, чтобы не упереться в rate limit Telegram
        await asyncio.sleep(0.05)

    log.info(
        f"Миграция завершена: всего {stats['total']}, "
        f"успех {stats['success']}, пропущено {stats['skipped']}, "
        f"ошибок {stats['failed']}"
    )
    return stats
