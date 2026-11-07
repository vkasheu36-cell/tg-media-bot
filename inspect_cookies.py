# -*- coding: utf-8 -*-
"""Проверка cookies.txt без вывода значений.

Использование:
    python inspect_cookies.py cookies.txt

Печатает домены, имена cookie, размер значений и признаки входа
(SID/HSID/LOGIN_INFO/__Secure-*PSID). Сами значения не показываются.
"""
from __future__ import annotations

import sys
from pathlib import Path

LOGIN_KEYS = {
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "LOGIN_INFO",
    "__Secure-1PSID",
    "__Secure-3PSID",
    "__Secure-1PSIDTS",
    "__Secure-3PSIDTS",
    "__Secure-1PAPISID",
    "__Secure-3PAPISID",
    "SIDCC",
}


def parse(path: Path) -> list[tuple[str, str, int]]:
    rows: list[tuple[str, str, int]] = []
    for raw in path.read_text("utf-8", "replace").splitlines():
        line = raw
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            print(f"НЕ РАЗОБРАНА СТРОКА (нет табов?): {raw[:90]!r}")
            continue
        domain, _sub, _path, _secure, _expiry, name, value = parts[:7]
        rows.append((domain, name, len(value)))
    return rows


def main() -> int:
    if len(sys.argv) != 2:
        print("Использование: python inspect_cookies.py cookies.txt")
        return 2
    path = Path(sys.argv[1])
    if not path.exists():
        print(f"Файл не найден: {path}")
        return 1

    rows = parse(path)
    if not rows:
        print("Cookie не найдены — файл пустой или не в формате Netscape.")
        return 1

    print(f"Файл: {path} ({path.stat().st_size} байт)")
    print("Домены:", ", ".join(sorted({row[0] for row in rows})))
    print(f"Всего cookie: {len(rows)}")
    print("\nИмена cookie:")
    for domain, name, size in rows:
        print(f"  {domain:28} {name:34} значение: {size} симв.")

    names = {row[1] for row in rows}
    found = sorted(names & LOGIN_KEYS)
    print("\nКлючи входа:", ", ".join(found) if found else "НЕ НАЙДЕНЫ")
    missing = sorted(LOGIN_KEYS - names)
    if missing:
        print("Нет из типичного набора:", ", ".join(missing))
    if not {"SID", "HSID", "APISID"} <= names:
        print(
            "\nВнимание: не хватает SID/HSID/APISID. Экспорт из Cookie-Editor\n"
            "часто теряет HttpOnly-пары — соберите файл через\n"
            "python -m yt_dlp --cookies-from-browser chrome --cookies cookies.txt ..."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
