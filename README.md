# HQ Access Control Bot

Бот-привратник на aiogram 3.x. Сверяет заявку на вступление в скрытую группу
с белым списком проекта и автоматически одобряет или отклоняет.

## Состав папки

| Файл              | Назначение                                                        |
|-------------------|-------------------------------------------------------------------|
| `bot.py`          | Сам бот. Токен не зашит, берётся из `.env`.                        |
| `whitelist.json`  | Белые списки по группам. Читается на каждую заявку, без рестарта.  |
| `.env.example`    | Шаблон секретов. Скопировать в `.env`, вписать токен и свой ID.    |
| `requirements.txt`| Зависимости (`aiogram`).                                          |
| `hqbot.service`   | systemd-юнит для запуска 24/7 с авто-рестартом.                    |
| `.gitignore`      | Прячет `.env` и мусор от git.                                      |

---

## Шаг 0. Безопасность токена (обязательно)

Старый токен засвечен в переписке - он мёртв для прода. В @BotFather:
`/revoke` -> выбрать `@hq_access_control_bot` -> получить НОВЫЙ токен.
Новый токен идёт только в `.env`, в код его не вписывать.

---

## Шаг 1. Тест на своём компьютере (5 минут)

Убедись, что бот живой, до всякого сервера.

```bash
cd hq_access_control_bot
python -m venv venv
# Windows:
venv\Scripts\pip install -r requirements.txt
# Linux/Mac:
./venv/bin/pip install -r requirements.txt

# создай .env из шаблона и впиши новый токен
copy .env.example .env        # Windows
cp .env.example .env          # Linux/Mac
```

Запуск (переменные из .env нужно подгрузить; проще сразу проверить на сервере
через systemd, но локально можно так):

```bash
# Linux/Mac:
set -a; . ./.env; set +a; ./venv/bin/python bot.py
# Windows PowerShell:
Get-Content .env | ForEach-Object { if ($_ -match '^\s*([^#=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim()) } }; venv\Scripts\python bot.py
```

В логе должно появиться `Бот запущен`. Если ругается на пустой `BOT_TOKEN` -
не подхватился `.env`.

---

## Шаг 2. Собрать chat_id групп и user_id людей

**chat_id каждой из 4 групп.** Запусти бота, в каждой группе отправь
`/id@hq_access_control_bot`. Бот ответит числом вида `-100...`. Если молчит -
у бота включён Privacy Mode: @BotFather -> `/setprivacy` -> Disable, либо всегда
дёргай команду с @упоминанием бота. Заявки на вступление приходят боту
независимо от Privacy Mode, это касается только чтения команд в группе.

**user_id людей.** Каждый пишет боту в ЛС `/me` - бот вернёт числовой ID.
Альтернатива: оставь списки пустыми, пусть стучатся, смотри в логе строки
`DECLINED id=...` и переноси нужные ID в whitelist.

---

## Шаг 3. Заполнить whitelist.json

Подставь реальные chat_id ключами и ID/username участников. Соответствие
ключ-проект держи у себя (в JSON комментарии запрещены синтаксисом):

```
-100... (1-й ключ) = Brierly
-100... (2-й ключ) = VaultForge
-100... (3-й ключ) = PTTC
-100... (4-й ключ) = Scentio
```

Формат:

```json
{
  "-1001234567890": {"ids": [111111111, 222222222], "usernames": ["alice", "bob"]},
  "-1009876543210": {"ids": [333333333], "usernames": ["charlie"]}
}
```

`ids` - числовые Telegram ID, основной способ: ID не меняется, не зависит от
username. `usernames` - запас, без собачки, регистр любой. Достаточно совпадения
по любому из двух списков. Группа, которой нет в файле, отклоняется по умолчанию.

Правку файла бот подхватывает на лету, рестарт не нужен. Проверить, что подхватил:
напиши боту `/reload` (работает только с твоего ADMIN_IDS).

---

## Шаг 4. Бесплатный хост 24/7: Oracle Cloud Always Free

100% бесплатный вариант, где токен остаётся у тебя, а не у чужого сервиса.
Oracle даёт постоянную бесплатную виртуалку (Always Free, не пробный период).
Для верификации нужна карта, но списаний по Always Free нет. Точные текущие
условия и регион проверь на cloud.oracle.com перед регистрацией, тарифы меняются.

> Не используй сайты «бесплатный хостинг ботов» (панели, куда вставляешь токен).
> Для бота, который управляет доступом в твои группы, отдавать токен чужому
> сервису - дыра. Только своя виртуалка.

Альтернатива с тем же подходом: Google Cloud `e2-micro` Free tier.

### Создание виртуалки

1. Зарегистрируйся на cloud.oracle.com, регион выбери поближе.
2. Compute -> Instances -> Create Instance.
3. Image: **Canonical Ubuntu** (22.04 или новее). Shape: любой из помеченных
   **Always Free eligible**.
4. Скачай приватный SSH-ключ при создании (он понадобится для входа).
5. После запуска запиши **Public IP** инстанса.

### Деплой на виртуалку

С локального компьютера залей файлы (подставь свой ключ и IP; пользователь у
Ubuntu-образов Oracle обычно `ubuntu`):

```bash
ssh -i ssh-key.key ubuntu@PUBLIC_IP "sudo mkdir -p /opt/hqbot && sudo chown ubuntu /opt/hqbot"
scp -i ssh-key.key bot.py whitelist.json requirements.txt .env hqbot.service ubuntu@PUBLIC_IP:/opt/hqbot/
```

Подключись и подними окружение:

```bash
ssh -i ssh-key.key ubuntu@PUBLIC_IP
cd /opt/hqbot
sudo apt update && sudo apt install -y python3-venv
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

Поставь сервис на автозапуск:

```bash
sudo cp /opt/hqbot/hqbot.service /etc/systemd/system/hqbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now hqbot
sudo systemctl status hqbot      # ждём active (running)
journalctl -u hqbot -f           # живой лог: APPROVED / DECLINED
```

Готово. Бот крутится 24/7, переживает перезагрузку сервера и сам поднимается
после сбоя (`Restart=always`).

---

## Эксплуатация

- **Один процесс на один токен.** Две запущенные копии -> `409 Conflict`, бот
  падает. Не держи локальную копию запущенной, когда работает серверная.
- **Правка списков:** меняешь `whitelist.json` на сервере (`nano whitelist.json`),
  сохраняешь - бот применяет сразу. `/reload` в ЛС покажет, что он видит.
- **Смена токена:** правишь `.env`, затем `sudo systemctl restart hqbot`.
- **Логи:** `journalctl -u hqbot -e` (последние) или `-f` (поток).

## Известное ограничение

Текст «Access denied» отклонённому почти всегда упадёт с 403: Telegram не даёт
боту писать первым тому, кто не нажал Start. Заявка при этом отклоняется
корректно. Фикс: в топике-витрине добавь шаг «сначала нажми Start у
@hq_access_control_bot, потом подавай заявку» - тогда ЛС дойдёт.
