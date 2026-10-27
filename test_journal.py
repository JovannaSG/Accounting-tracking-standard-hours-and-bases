import pandas as pd
from core import db
from core.api_client import OneCClient

# Укажите точное имя базы из дашборда и нужный период
BASE_NAME = "7;Говса Р.К. ИП"
START_DATE = "2026-01-01"
END_DATE = "2026-12-31"
OUTPUT_FILE = "journal_export.xlsx"

def main():
    # Ищем базу в локальной SQLite
    bases = [b for b in db.list_bases() if b["name"] == BASE_NAME]
    if not bases:
        print(f"База '{BASE_NAME}' не найдена в системе.")
        return
    base = bases[0]
    
    # Подключаемся к 1С
    client = OneCClient(base["url"], base["login"], base["password"])
    
    print("Ищем Журнал операций в OData...")
    sets = client.fetch_metadata_entity_sets()
    journal_name = client._resolve_operation_journal(sets)
    
    if not journal_name:
        print("Журнал операций не опубликован для этой базы.")
        return
        
    print(f"Скачиваем данные из {journal_name} (это займет немного времени)...")
    fields = ["Date", "Ref_Type", "Posted"]
    
    records = []
    # Вытягиваем документы итератором, чтобы не перегружать память
    for rec in client.fetch_documents_iter(journal_name, START_DATE, END_DATE, select=fields):
        records.append({
            "Дата (Date)": rec.get("Date"),
            "Тип документа (Ref_Type)": rec.get("Ref_Type"),
            "Проведен (Posted)": rec.get("Posted")
        })
        
    # Сохраняем в Excel
    df = pd.DataFrame(records)
    # Сортируем по типу документа для удобства чтения
    df.sort_values(by="Тип документа (Ref_Type)", inplace=True)
    df.to_excel(OUTPUT_FILE, index=False)
    
    print(f"Успешно! Сохранено {len(df)} записей в файл {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
