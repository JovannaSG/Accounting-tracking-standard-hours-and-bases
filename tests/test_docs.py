"""
Целостность текстовых файлов репозитория.

Единственная цель этих тестов — не дать повториться порче документации,
из-за которой руководство администратора сократилось с 622 до 20 строк
(коммит 7b10e78): текст сохранили не в UTF-8, прочитали с потерей символов,
и все кириллические буквы превратились в U+FFFD. Восстановить оригинальные
байты невозможно — каждый потерянный символ заменён одним U+FFFD без
сохранения позиционной информации. Единственным источником правды осталась
история Git.

Тест намеренно строгий: он проверяет не «похоже ли на русский текст», а
конкретные искажения, которые ломают чтение и правку.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Каталоги, которые не проверяем: виртуальное окружение, кэши, VCS.
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules"}

TEXT_SUFFIXES = {
    ".py", ".md", ".json", ".txt", ".yml", ".yaml", ".sh", ".cfg", ".ini",
    ".toml", ".example", ".sql", ".csv",
}

# Локальные файлы с секретами и машинные артефакты: их содержимое не должно
# влиять на тесты (они в .gitignore и у разработчика каждый раз свои).
LOCAL_ONLY_NAMES = {
    "users.json", "user.json", "client_databases.json", "login_data.txt",
}
LOCAL_ONLY_SUFFIXES = {".db", ".sqlite3", ".xlsx", ".log", ".env"}

# Документация, которую администраторы читают в первую очередь.
REQUIRED_DOCS = [
    "README.md",
    "CHANGELOG.md",
    "docs/Руководство_администратора_и_пользователя.md",
]


def _text_files() -> list[Path]:
    found = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(ROOT).parts
        if SKIP_DIRS & set(rel_parts):
            continue
        if path.name in LOCAL_ONLY_NAMES:
            continue
        if path.suffix.lower() in LOCAL_ONLY_SUFFIXES:
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name == "Dockerfile":
            found.append(path)
    return sorted(found)


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def _read(path: Path) -> str:
    # encoding="utf-8" намеренно жёсткий: битый файл должен падать здесь,
    # а не молча превращаться в мусор через errors="replace".
    return path.read_text(encoding="utf-8")


def test_docs_are_valid_utf8_without_replacement_chars():
    """Ни в одном текстовом файле нет U+FFFD — документы не повреждены.

    Именно этот символ заменил всю кириллицу в руководстве в 7b10e78.
    """
    broken = {}
    for path in _text_files():
        try:
            text = _read(path)
        except UnicodeDecodeError as e:
            broken[_rel(path)] = f"невалидный UTF-8: {e}"
            continue
        count = text.count("\ufffd")
        if count:
            broken[_rel(path)] = f"{count} символов U+FFFD"
    assert not broken, "Повреждённые текстовые файлы:\n" + "\n".join(
        f"  {name}: {why}" for name, why in sorted(broken.items())
    )


def test_docs_have_no_bom_and_end_with_newline():
    """Нет BOM в начале файла; у Markdown есть завершающий перевод строки.

    BOM в начале файла без явного указания кодировки часть редакторов и
    скриптов показывает как невидимый символ в первой строке.

    Требование перевода строки в конце распространено только на Markdown:
    для него это влияет на рендеринг и на diff, а выравнивать хвосты
    всех служебных файлов репозитория — лишний шум в изменениях.

    Проверяется только начало файла: U+FEFF внутри файла — допустимые
    тестовые данные (tests/test_db.py проверяет разбор файлов с BOM).
    """
    problems = []
    for path in _text_files():
        text = _read(path)
        rel = _rel(path)
        if text.startswith("\ufeff"):
            problems.append(f"{rel}: BOM в начале файла")
        if path.suffix.lower() == ".md" and text and not text.endswith("\n"):
            problems.append(f"{rel}: нет перевода строки в конце")
    assert not problems, "Файлы с BOM или без финального перевода строки:\n" + "\n".join(
        f"  {p}" for p in problems
    )


def test_docs_have_no_invisible_control_chars():
    """Нет невидимых управляющих символов (мягкий перенос, LRM и т.п.).

    Мягкий перенос или знак направления текста не видны в редакторе, но
    ломают поиск по слову и выглядят как опечатка в выдаче.

    U+FEFF исключён: он проверяется отдельно, только в начале файла, а
    внутри файла встречается как осознанные тестовые данные.
    """
    allowed = {"\n", "\t", "\ufeff"}
    problems = []
    for path in _text_files():
        text = _read(path)
        for i, char in enumerate(text):
            if char in allowed or ord(char) < 0x20:
                continue
            if unicodedata.category(char) in {"Cf", "Co", "Cn"}:
                line = text.count("\n", 0, i) + 1
                problems.append(
                    f"{_rel(path)}:{line} {hex(ord(char))} "
                    f"{unicodedata.name(char, '?')}"
                )
    assert not problems, "Невидимые символы в текстовых файлах:\n" + "\n".join(
        f"  {p}" for p in problems
    )


@pytest.mark.parametrize("rel_path", REQUIRED_DOCS)
def test_required_docs_exist_and_are_readable(rel_path):
    path = ROOT / rel_path
    assert path.is_file(), f"Отсутствует обязательный документ: {rel_path}"
    text = _read(path)
    assert len(text.splitlines()) > 10, f"{rel_path} подозрительно короткий"


def test_admin_guide_is_complete():
    """Руководство содержит все разделы.

    Регрессия на случай, когда файл останется «похожим на здоровый», но
    потеряет половину разделов, как в 7b10e78 (20 строк вместо 622).
    """
    text = _read(ROOT / "docs/Руководство_администратора_и_пользователя.md")
    lines = text.splitlines()
    assert len(lines) > 400, (
        f"Руководство сократилось до {len(lines)} строк — "
        "похоже на повреждение документации"
    )
    for heading in (
        "## 1. Роли и права доступа",
        "## 2. Установка и запуск",
        "## 3. Настройка для администратора",
        "## 4. Работа пользователя",
        "## 5. Частые вопросы",
        "## 6. Структура данных",
        "## 7. Что не покрыто MVP",
    ):
        assert heading in text, f"В руководстве нет раздела: {heading!r}"
    # Кириллица должна составлять основную часть текста.
    cyrillic = sum(1 for c in text if "\u0400" <= c <= "\u04ff")
    assert cyrillic > 10000, f"Слишком мало кириллицы ({cyrillic} символов)"


def test_editorconfig_declares_utf8():
    """.editorconfig фиксирует UTF-8 — редактор не даст сохранить иначе."""
    text = _read(ROOT / ".editorconfig")
    assert "charset = utf-8" in text


def test_json_files_are_parseable():
    """Все JSON читаются: русские ключи и значения не должны ломать json."""
    broken = []
    for path in ROOT.rglob("*.json"):
        if SKIP_DIRS & set(path.relative_to(ROOT).parts):
            continue
        try:
            json.loads(_read(path))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            broken.append(f"{_rel(path)}: {e}")
    assert not broken, "Некорректные JSON:\n" + "\n".join(f"  {b}" for b in broken)
