## Что и зачем / What and why

<!-- Что меняет этот PR и зачем он нужен? Укажите issue, если есть. / What does this change do, and why is it needed? Link the issue if there is one. -->

## Как проверено / How it was tested

<!-- Команды, ручные проверки, скриншоты для UI. / Commands you ran, manual checks, screenshots for UI changes. -->

## Чек-лист / Checklist

- [ ] Я прочитал(а) [CONTRIBUTING.ru.md](../CONTRIBUTING.ru.md) / [CONTRIBUTING.md](../CONTRIBUTING.md) и подписал(а) [CLA](../CLA.md) (бот попросит при первом PR) / I have read the contributing guide and signed the CLA (the bot will ask on the first pull request)
- [ ] Тесты добавлены или обновлены; проходят `pytest`, `ruff check`, `ruff format --check`, `mypy` / Tests added or updated; all checks pass
- [ ] Строки для пользователей добавлены в файлы локализации (минимум ru и en) / User-visible strings are in the locale files (at least Russian and English)
- [ ] Для изменений схемы есть миграция Alembic (SQLite, PostgreSQL, MySQL) / Schema changes come with an Alembic migration that works on SQLite, PostgreSQL and MySQL
- [ ] Нет секретов, токенов, адресов серверов и данных клиентов в коде, тестах, документации и скриншотах / No secrets, real tokens, server addresses or customer data in code, tests, docs or screenshots
- [ ] Документация обновлена, если изменилось поведение или настройки / Docs updated if behavior or settings changed
