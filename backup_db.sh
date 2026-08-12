#!/bin/bash
# ============================================================
# Скрипт еженедельного бэкапа БД бота Ковчег.
# Запускается по cron раз в неделю (см. ниже как настроить).
#
# Что делает:
# 1. Копирует sqlite базу через sqlite3 .backup (безопасно
#    даже если бот прямо сейчас пишет — не заблокирует).
# 2. Сжимает gzip'ом (обычно в 3-4 раза меньше).
# 3. Хранит последние 12 бэкапов (≈ 3 месяца при недельной ротации).
# 4. Опционально копирует в Яндекс.Диск через WebDAV.
# ============================================================

set -e  # выйти при любой ошибке

DB_PATH="/root/dating_bot/dating.db"
BACKUP_DIR="/root/backups"
KEEP_LAST=12  # сколько бэкапов хранить

# Настройки Яндекс.Диска (заполни если хочешь использовать)
# Пароль надо получить в https://id.yandex.ru/security/app-passwords
# YANDEX_USER="your_login@yandex.ru"
# YANDEX_APP_PASSWORD="password_here"
# YANDEX_REMOTE_DIR="/kovcheg-backups"

mkdir -p "$BACKUP_DIR"

# Имя бэкапа с датой
STAMP=$(date +%Y-%m-%d_%H-%M)
BACKUP_NAME="dating_$STAMP.db"
BACKUP_PATH="$BACKUP_DIR/$BACKUP_NAME"

echo "[$(date +'%F %T')] Начинаю бэкап..."

# 1. Безопасное копирование через SQLite (не блокирует пишущего)
sqlite3 "$DB_PATH" ".backup '$BACKUP_PATH'"
echo "[$(date +'%F %T')] База скопирована в $BACKUP_PATH"

# 2. Сжатие
gzip "$BACKUP_PATH"
BACKUP_PATH_GZ="${BACKUP_PATH}.gz"
BACKUP_SIZE=$(du -h "$BACKUP_PATH_GZ" | cut -f1)
echo "[$(date +'%F %T')] Сжато: $BACKUP_PATH_GZ ($BACKUP_SIZE)"

# 3. Удаление старых (кроме последних $KEEP_LAST)
cd "$BACKUP_DIR"
ls -1t dating_*.db.gz 2>/dev/null | tail -n +$((KEEP_LAST + 1)) | xargs -r rm -f
COUNT=$(ls -1 dating_*.db.gz 2>/dev/null | wc -l)
echo "[$(date +'%F %T')] Всего бэкапов в папке: $COUNT"

# 4. (Опция) Загрузка на Яндекс.Диск через WebDAV
if [ -n "$YANDEX_APP_PASSWORD" ]; then
    echo "[$(date +'%F %T')] Загружаю на Яндекс.Диск..."
    # Создаём папку если её нет (MKCOL)
    curl -s -u "$YANDEX_USER:$YANDEX_APP_PASSWORD" \
         -X MKCOL "https://webdav.yandex.ru$YANDEX_REMOTE_DIR" > /dev/null 2>&1 || true
    # Загружаем файл
    HTTP_CODE=$(curl -s -u "$YANDEX_USER:$YANDEX_APP_PASSWORD" \
        -T "$BACKUP_PATH_GZ" \
        -w "%{http_code}" \
        "https://webdav.yandex.ru$YANDEX_REMOTE_DIR/$BACKUP_NAME.gz")
    if [ "$HTTP_CODE" = "201" ] || [ "$HTTP_CODE" = "204" ]; then
        echo "[$(date +'%F %T')] Успешно загружен на Яндекс.Диск"
    else
        echo "[$(date +'%F %T')] Ошибка загрузки на Яндекс.Диск: HTTP $HTTP_CODE"
    fi
fi

echo "[$(date +'%F %T')] Бэкап завершён."
