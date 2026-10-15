# AgentProxy — инструкции для Claude

Поведение, установка и файлы — в `README.md`, прочитать перед правкой. Здесь только правила работы.

## Устройство

- Две части. **Программа в трее** (`src\*.cs`, C# 5, .NET Framework 4.8): значок, меню, журнал, настройки, обновление,
  отчёты о сбоях, присмотр за движком (`EngineSupervisor`: запуск, проверка `/api/status` каждые 5 с, перезапуск с
  паузами `RestartPolicy`). **Движок** (`engine\agentproxy`, Python 3.13 встроенный): mitmproxy, история запросов в
  SQLite, HTTP API и MCP на tornado, интеграция с Claude Code, командная строка.
- Движок запускается как `runtime\python\python.exe -I -m agentproxy --parent-pid <pid трея>` и сам выходит, когда
  трей умер. Коды выхода: 0 — штатно, 1 — падение, 3 — порт занят, 4 — нет папки данных.
- `Recorder` пишет завершённые запросы, сбои `CONNECT` и отказ клиента от сертификата прокси; SSE и ответы больше
  `max_body_bytes` пропускает потоком и сохраняет только начало тела. `OwnPortsGuard` не пускает через прокси запросы к
  его же портам: иначе любой клиент прокси читал бы историю через API.
- MCP-инструменты и HTTP API — один набор (`service.TOOLS`): новый инструмент — запись в `TOOLS`, метод в
  `ProxyService`, строка в `SKILL.md` и README (тест сверяет `SKILL.md` с `TOOLS`).
- API слушает только `127.0.0.1` и отклоняет чужие `Host` и `Origin` (защита от DNS rebinding и запросов из браузера).
  Остановка движка по API — только с токеном из `engine-runtime.json`.
- Интеграция с Claude Code (`engine\agentproxy\claude`): MCP-сервер в `~/.claude.json` (через `claude mcp add`, если
  CLI найден, иначе правкой файла), скилл из шаблона `SKILL.md`, хуки в `~/.claude/settings.json`. Наши хуки узнаются по
  `agentproxy.cli` в аргументах и по адресу `/claude/hooks/user-prompt-submit`. Состояние — `claude-integration.json` в
  папке данных; `refresh` при старте трея переустанавливает интеграцию после обновления и запоминает прежнюю версию.
- Уведомление об обновлении: хук старта сессии показывает «что нового» 3 дня после обновления; хук запроса
  (`UserPromptSubmit`, HTTP к движку) — один раз в каждой уже открытой сессии, которая видела прежнюю версию
  (`claude-sessions.json`). Текст берётся из `CHANGELOG.md` папки установки.

## Версии

- 0.x — каждое опубликованное состояние получает новый номер; 1.0 — первая рабочая версия.
- Номер версии в пяти местах: `engine\agentproxy\__init__.py`, `src\AppIdentity.cs`, три `AssemblyInfo.cs`; плюс
  верхняя запись `CHANGELOG.md`. Тест `test_version_consistency.py` ловит расхождение.
- `CHANGELOG.md` читает сам Claude у пользователя: после обновления записи версии приходят в сессию. Писать их для
  агента: что изменилось в инструментах, скилле и поведении, без внутренностей.
- До 1.0 Claude ставит, удаляет и переустанавливает программу сам; рабочую копию пользователь обновляет через трей.

## Сборка и проверка

- `py tools\runtime.py fetch` — встроенный Python и зависимости по хешам в `runtime\` (не в git). `lock` — пересобрать
  `requirements*.lock` после правки `requirements*.in`.
- `test.cmd` — тесты движка (pytest, включая сквозные с настоящим mitmproxy) и C# (`tests\csharp`). Сквозные тесты
  запускают движок на свободных портах во временной папке.
- `build.cmd nostart` — программа; `build-installer.cmd` — установщик `dist\AgentProxySetup.exe` и его sha256.
- Установщик — без прав администратора. Тихо: `AgentProxySetup.exe --silent` (из Git Bash ключи только через `--`,
  иначе Git Bash превращает `/silent` в путь). Удаление: `Uninstall.exe --silent`. Журналы — `%TEMP%\AgentProxy-setup.log`,
  `%TEMP%\AgentProxy-uninstall.log`.
- Перед релизом — установка поверх предыдущего релиза, проверка `agentproxy.cmd status` и журналов.
- Интеграцию с Claude Code проверять на копии конфигурации через `CLAUDE_CONFIG_DIR`, а установку — с
  `--option:claude=0`, пока не нужна настоящая.

## Правила кода

- C#: штатный `csc.exe`, **C# 5** — нет `$"..."`, `?.`, `nameof`, `=>`-членов. Комментариев нет, один класс — один файл.
- Python: стандартная библиотека и то, что приезжает с mitmproxy; новые зависимости — только через `requirements.in` и
  `lock`. Комментариев нет.
- Журналы и отчёты о сбоях не содержат тел и заголовков запросов: только шаги, порты, ошибки.
- `.cmd` — CRLF и только ASCII; писать их python-скриптом или инструментом записи, не `sed`.

## Общее с другими pet-проектами

Проект — один из четырёх pet-проектов (AgentVpnGuard, DevMonitor, AgentGlow, AgentProxy), они устроены одинаково.

- **Общие файлы** взяты у AgentGlow с заменой `namespace`: `installer\setup\{SetupForm, Installation, InstallTarget,
  Payload, SetupOption, SetupField, InstallRequest, SetupLog}.cs`, `installer\common\RunningApp.cs`,
  `src\{AboutForm, CrashReportConsent, CrashUploader, CrashReport, AppLog, UserDataPaths, Autostart}.cs`,
  `installer\make-payload.ps1`.
- **Осознанные отличия:** установка без прав (папка пользователя, `UninstallRegistration` пишет в HKCU, манифесты
  `asInvoker`); тихий режим установщика и деинсталлятора (`SilentSetup`, `CommandLine`); `UpdateChecker` сверяет
  sha256 с полем `digest` релиза, `UpdateForm` ставит скачанную версию кнопкой «Установить сейчас». Последние два —
  кандидаты на перенос в остальные проекты.
- **Своё у проекта:** `src\AppIdentity.cs`, `installer\setup\SetupProfile.cs`, `installer\uninstall\UninstallProfile.cs`,
  `installer\payload.txt`, всё про движок.
- Меню трея: состояние; Claude Code; «Открыть монитор», «Подключить/Отключить Claude Code», «Перезапустить движок»;
  «Настройки…», «Журнал», «Проверить обновление…», «Запускать при входе в Windows», «О приложении…»; «Выход».
- Любая правка доводится до публикации: версия, `CHANGELOG.md`, тесты, установщик, проверка Защитником, коммит и push,
  Release с `AgentProxySetup.exe`, при необходимости README, скриншоты и страница на aokazantsev.ru/pets.
