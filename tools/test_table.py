# -*- coding: utf-8 -*-
"""
Тестовый скрипт SQLite3
Назначение: чтение ExcelDB.db в текущем каталоге и вывод всех имён таблиц в базе данных
"""

import sqlite3
import os
import sys

def list_tables(db_path: str) -> list:
    """Получить список всех таблиц в базе данных SQLite"""
    # Проверить, существует ли файл
    if not os.path.isfile(db_path):
        raise FileNotFoundError(f"Файл базы данных не найден: {db_path}")

    # Подключиться к базе данных
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        # sqlite_master — системная таблица SQLite, хранящая информацию о структуре БД
        cursor.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name;"
        )
        # Извлечь имя каждой таблицы (первый столбец результата)
        tables = [row[0] for row in cursor.fetchall()]
        return tables
    finally:
        conn.close()



def main():
    # Путь к файлу базы данных: ExcelDB.db в каталоге текущего скрипта
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ExcelDB.db")

    print(f"Путь к файлу базы данных: {db_path}")
    print("-" * 50)

    try:
        tables = list_tables(db_path)
        if tables:
            print(f"Всего найдено таблиц: {len(tables)}\n")
            for idx, name in enumerate(tables, start=1):
                print(f"  {idx:>3}. {name}")
        else:
            print("В базе данных нет пользовательских таблиц.")
    except Exception as e:
        print(f"Произошла ошибка: {e}", file=sys.stderr)
        sys.exit(1)



if __name__ == "__main__":
    main()
