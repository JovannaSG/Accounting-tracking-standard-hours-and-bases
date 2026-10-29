import os
import sys
import argparse
import json

# Добавляем корень проекта в sys.path для импорта core
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from core.api_client import OneCClient


def main():
    """
    python test_employees.py --url "https://msk1.1cfresh.com/a/ea/000000" --login "odata.user" --password "ваш_пароль"
    """

    parser = argparse.ArgumentParser(description="Дамп справочника пользователей из 1С")
    parser.add_argument("--url", required=True, help="URL базы OData (например: https://msk1.1cfresh.com/a/ea/12345)")
    parser.add_argument("--login", required=True, help="Логин OData")
    parser.add_argument("--password", required=True, help="Пароль OData")
    args = parser.parse_args()

    print(f"Подключение к {args.url}...")
    client = OneCClient(args.url, args.login, args.password)

    endpoint = f"{client.base_url}/odata/standard.odata/Catalog_Пользователи"
    params = {
        "$format": "json",
        # Запрашиваем GUID, Имя и флаги удаления/недействительности (если они есть в 1С)
        "$select": "Ref_Key,Description,DeletionMark,Недействителен",
        "$top": 1000
    }

    try:
        print("Скачивание Catalog_Пользователи...\n")
        # Используем встроенный в OneCClient метод пагинации
        users = client._paginate(endpoint, params)
        
        if not users:
            print("Справочник пуст или у пользователя OData нет прав на чтение Catalog_Пользователи.")
            return

        print(f"Найдено учетных записей: {len(users)}\n")
        print(f"{'GUID':<38} | {'Статус':<15} | {'Точное имя в 1С (Description)'}")
        print("-" * 100)
        
        # Сортируем по имени для удобства поиска
        for user in sorted(users, key=lambda x: str(x.get('Description', ''))):
            guid = user.get("Ref_Key", "")
            name = user.get("Description", "<ПУСТОЕ ИМЯ>")
            
            # Проверяем статусы (удален или заблокирован)
            status = "Активен"
            if user.get("DeletionMark"):
                status = "Удален (X)"
            elif user.get("Недействителен"):
                status = "Недействителен"
                
            print(f"{guid:<38} | {status:<15} | {name}")
            
    except Exception as e:
        print(f"\nКритическая ошибка: {e}")


if __name__ == "__main__":
    main()
