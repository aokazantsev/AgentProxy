# AgentProxy

Прокси-сниффер HTTP(S) для Windows вместо Charles, которым ИИ-агент управляет сам. Телефон, эмулятор или браузер
шлют трафик через AgentProxy, каждый завершённый запрос попадает в историю, а Claude Code читает её, ищет нужные
запросы и ждёт новые через MCP-сервер `agentproxy` — без кликов в окне сниффера.

- Движок — [mitmproxy](https://mitmproxy.org): перехват HTTPS, HTTP/2, WebSocket.
- Интерфейс минимальный и только информативный: значок в трее, журнал, монитор запросов в браузере.
- Ставится в папку пользователя без прав администратора.

## Установка силами Claude

Этот раздел — инструкция для ИИ-агента. Дай Claude ссылку на репозиторий и попроси поставить AgentProxy.

1. Скачать установщик последней версии:
   ```bash
   curl -fL -o "$TEMP/AgentProxySetup.exe" https://github.com/aokazantsev/AgentProxy/releases/latest/download/AgentProxySetup.exe
   ```
2. Сверить контрольную сумму с опубликованной (поле `digest` у файла в последнем релизе). Команда печатает `OK`
   или `MISMATCH`; при `MISMATCH` или пустом `digest` установщик не запускать и сказать пользователю:
   ```bash
   expected=$(curl -s https://api.github.com/repos/aokazantsev/AgentProxy/releases/latest | grep -o '"digest": *"sha256:[0-9a-f]*"' | grep -o '[0-9a-f]\{64\}'); actual=$(sha256sum "$TEMP/AgentProxySetup.exe" | cut -d' ' -f1); [ -n "$expected" ] && [ "$expected" = "$actual" ] && echo OK || echo MISMATCH
   ```
3. Поставить молча, права администратора не нужны:
   ```bash
   "$TEMP/AgentProxySetup.exe" --silent
   ```
   Код выхода 0 — успех, журнал установки — `%TEMP%\AgentProxy-setup.log`. Установщик кладёт программу в
   `%LOCALAPPDATA%\Programs\AgentProxy`, включает автозапуск, запускает программу и подключает Claude Code:
   MCP-сервер `agentproxy`, скилл `agentproxy` и хуки. Ключи: `--noautostart`, `--dir="<папка>"`,
   `--option:claude=0` (не подключать Claude Code; поверх установки с подключённой интеграцией — снять её), `--option:crashReports=1` (отправлять автору отчёты о сбоях — только если пользователь прямо согласился:
   в отчёт уходит журнал программы).
   Ключи пишутся и через `/` (`/silent`), но из Git Bash — только через `--`: Git Bash превращает `/silent` в путь.
4. Проверить:
   ```bash
   "$LOCALAPPDATA/Programs/AgentProxy/agentproxy.cmd" status --wait 30
   ```
   Команда ждёт до 30 секунд, пока движок поднимется после установки. `"running": true` и код 0 — движок работает, `claude.mcp`, `claude.skill`, `claude.hooks` — интеграция на месте.
5. Сказать пользователю: MCP-сервер появится в новых сессиях Claude Code; скилл `agentproxy` подхватывается сразу и
   описывает подключение устройства.

Обновление: пользователь нажимает в трее «Проверить обновление…» → «Установить сейчас». Интеграция с Claude Code
обновляется сама при первом запуске новой версии, а хуки сообщают открытым сессиям, что нового.

## Подключение устройства

Прокси слушает только `127.0.0.1:8890`, устройство подключается через `adb reverse` — по USB или Wi-Fi-отладке,
без открытия портов в брандмауэре.

```bash
adb reverse tcp:8890 tcp:8890
adb shell settings put global http_proxy 127.0.0.1:8890
```

Корневой сертификат ставится на устройство один раз: с включённым прокси открыть в браузере устройства
`http://mitm.it` и следовать инструкции для Android. Приложение увидит HTTPS, если доверяет пользовательским
сертификатам (`network_security_config` с `<certificates src="user"/>` в debug-сборке).

После работы прокси на устройстве выключается, иначе без запущенного AgentProxy у него пропадёт интернет:

```bash
adb shell settings put global http_proxy :0
adb reverse --remove tcp:8890
```

## Что умеет Claude

| Инструмент MCP | Что делает |
|---|---|
| `proxy_status` | версия, порт, число записанных запросов, ссылки на сертификат |
| `list_flows` | сводка запросов, свежие сверху, фильтры по хосту, пути, методу, коду, ошибкам |
| `get_flow` | запрос целиком: заголовки и тела, JSON отформатирован |
| `wait_for_flow` | ждёт запрос, который приложение отправит после вызова |
| `clear_flows` | очищает историю |

То же доступно по HTTP: `GET /api/status`, `GET /api/flows?host=...`, `GET /api/flows/<id>`,
`GET /api/flows/wait?path=...`, `DELETE /api/flows`, сертификат — `/api/ca.pem`, `/api/ca.cer`. Монитор для
человека — `http://127.0.0.1:8891/`.

## Интеграция с Claude Code

| Что | Где | Зачем |
|---|---|---|
| MCP-сервер `agentproxy` | `~/.claude.json`, `mcpServers` | инструменты из таблицы выше |
| Скилл `agentproxy` | `~/.claude/skills/agentproxy/SKILL.md` | как подключить устройство, как искать запросы, что делать, если трафика не видно |
| Хук `SessionStart` | `~/.claude/settings.json` | в начале сессии Claude узнаёт, работает ли прокси и на каком порту; после обновления — что нового |
| Хук `UserPromptSubmit` | `~/.claude/settings.json` | уже открытая сессия узнаёт об обновлении программы на следующем сообщении; запрос к `127.0.0.1`, процесс не запускается |

Подключить и отключить — пункт трея или `agentproxy.cmd claude install|uninstall|status`. Чужие хуки, серверы и
настройки не трогаются; файл `settings.json`, который является ссылкой, остаётся ссылкой.

## Ограничения

AgentProxy рассчитан на ПК с одним пользователем Windows. API и MCP на `127.0.0.1` не требуют пароля: любой
процесс на этом ПК, в том числе другого пользователя Windows, может прочитать историю запросов. Пока движок
выключен, программа, занявшая его порт API, получит запросы хука `UserPromptSubmit` Claude Code. На ПК, где
работают несколько людей, AgentProxy не ставить или отключать интеграцию с Claude Code, когда он не нужен.

Через прокси к его собственным портам не пройти: запросы к ним с устройства отклоняются, а адрес соединения
проверяется после разрешения имени.

## Файлы

| Файл | Что |
|---|---|
| `%LOCALAPPDATA%\Programs\AgentProxy` | программа, встроенный Python и движок |
| `%LOCALAPPDATA%\AgentProxy\settings.json` | порты (`proxy_port`, `api_port`), `verify_upstream`, `max_flows`, `max_body_bytes` |
| `%LOCALAPPDATA%\AgentProxy\log.txt` | журнал программы в трее |
| `%LOCALAPPDATA%\AgentProxy\engine.log` | журнал движка |
| `%LOCALAPPDATA%\AgentProxy\cli.log` | журнал командной строки и хука старта сессии |
| `%LOCALAPPDATA%\AgentProxy\flows.db` | история запросов (SQLite, последние 5000) |
| `%LOCALAPPDATA%\AgentProxy\certs` | корневой сертификат прокси |

Удаление — «Приложения» Windows или `Uninstall.exe --silent` из папки программы: снимает интеграцию с Claude Code,
автозапуск и удаляет данные вместе с сертификатом.

## Разработка

Нужны Python 3.12+ (`py`) для сборки и .NET Framework 4.8 (есть в Windows).

| Задача | Команда |
|---|---|
| Встроенный Python и зависимости | `py tools\runtime.py fetch` |
| Обновить зафиксированные версии | `py tools\runtime.py lock` |
| Пересобрать значок `src\app.ico` | `py tools\make_icon.py` (нужен Pillow: `py -m pip install pillow`) |
| Все тесты (движок и C#) | `test.cmd` |
| Только тесты движка | `py tools\test.py` |
| Программа без запуска | `build.cmd nostart` |
| Установщик | `build-installer.cmd` → `dist\AgentProxySetup.exe` |

Зависимости движка зафиксированы с хешами (`engine\requirements.lock`), встроенный Python — по хешу
(`engine\python-embed.sha256`). Устройство и правила работы — `CLAUDE.md`.

## Лицензия

MIT. mitmproxy — MIT, Python — PSF License.
