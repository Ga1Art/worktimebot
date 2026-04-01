import psycopg2
from datetime import date
from config import DB_HOST, DB_NAME, DB_USER, DB_PASSWORD, DB_PORT


def get_connection():
    """
    Создаёт подключение к PostgreSQL.
    Каждый вызов — новое соединение (для MVP ок).
    """
    return psycopg2.connect(
        host=DB_HOST,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        port=DB_PORT,
        sslmode="require"  # важно для Supabase
    )


def get_or_create_worker(full_name, chat_id):
    """
    Ищем пользователя по chat_id.
    Если нет — создаём.
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id FROM workers WHERE chat_id = %s
    """, (chat_id,))
    result = cursor.fetchone()

    if result:
        worker_id = result[0]
    else:
        cursor.execute("""
            INSERT INTO workers (full_name, chat_id)
            VALUES (%s, %s)
            RETURNING id
        """, (full_name, chat_id))

        worker_id = cursor.fetchone()[0]
        conn.commit()

    cursor.close()
    conn.close()

    return worker_id


def save_to_db(data: dict, name: str, chat_id: int):
    """
    Главная функция сохранения данных.
    Вызывается из handlers.
    """

    conn = get_connection()
    cursor = conn.cursor()

    worker_id = get_or_create_worker(name, chat_id)

    # формируем дату
    work_date = date.today().replace(day=int(data.get("date")))

    place = data.get("place")

    # ⏱ Работа
    if place in ["Монтаж", "Смена"]:
        work_type = "install" if place == "Монтаж" else "shift"

        cursor.execute("""
            INSERT INTO work_logs (worker_id, work_type, work_date, hours)
            VALUES (%s, %s, %s, %s)
        """, (
            worker_id,
            work_type,
            work_date,
            data.get("hours")
        ))

    # 💸 Расходы
    elif place == "Расходы":
        cursor.execute("""
            INSERT INTO expenses (worker_id, expense_date, amount, description)
            VALUES (%s, %s, %s, %s)
        """, (
            worker_id,
            work_date,
            data.get("amount"),
            data.get("expense_type")
        ))

    conn.commit()
    cursor.close()
    conn.close()