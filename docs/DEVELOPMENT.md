# Разработка

## Требования

- Windows 10/11
- Python 3.10+ (проект проверен на Python 3.14)
- openOMSI с поддержкой Lua-плагинов — только для живой проверки

## Окружение

```bat
py -3 -m venv .venv
.venv\Scripts\activate
py -3 -m pip install -r requirements.txt
```

## Запуск

```bat
py -3 AutoInformant.py
```

Приложение читает и создаёт `autoinformant_pro_settings.json` рядом с `AutoInformant.py`.
Этот файл содержит локальные пути и пользовательские настройки и **не должен попадать в git**.

## Тесты

```bat
py -3 -m py_compile AutoInformant.py informant_core.py openomsi_bridge.py pack_system.py yandex_music_service.py test_autoinformant.py test_yandex_music.py
py -3 -m unittest -q test_autoinformant.py test_yandex_music.py
```

Ожидаемый результат — 95 тестов, на Windows один тест DPAPI пропускается (он проверяет
поведение вне Windows). Тесты не требуют запущенного openOMSI,
но часть проверок Яндекс Музыки работает на локальном тестовом аудио и не
подтверждает доступ к конкретному аккаунту или треку.

## Структура

```
AutoInformant.py            точка входа, главное окно, вкладки
informant_core.py           аудиобиблиотека, остановки, расписания
pack_system.py              .aipack: импорт, экспорт, установка, категории
openomsi_bridge.py          UDP-мост и подготовка аудио для Lua-плагина
yandex_music_service.py     адаптер Яндекс Музыки, DPAPI-хранилище токена
BusStopMaker.pyw            вспомогательная утилита
plugin/AutoInformantPro/    Lua-плагин openOMSI
```

## Соглашения

- Кодировка файлов — UTF-8, переводы строк в Python — LF (`run_app.bat` — CRLF).
- Пользовательские пути и токены никогда не коммитятся.
- Схема `.aipack` v1 и протокол Lua-плагина v2 считаются стабильными: изменения
  формата требуют отдельной миграции и обновления документации.
- Для новых версий обновляйте `VERSION.txt`, `CHANGELOG.md` и тег релиза.

## Релиз

1. Обновите `VERSION.txt` и `CHANGELOG.md`.
2. Соберите standalone-архив с исходниками, плагином и пустыми `packs/`, `maps/`.
3. Создайте тег и релиз:

```bat
git tag v2.0.2
git push origin v2.0.2
gh release create v2.0.2 AutoInformantPro_Standalone_v2_0_2.zip --title "AutoInformant Pro 2.0.2" --notes-file RELEASE_NOTES.md
```
