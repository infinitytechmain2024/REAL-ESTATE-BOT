# Шифрованный раздел для профилей браузера (D20, P2.10)

Решение D20: каталог `/profiles` сервиса `browser` (`docker-compose.yml:161-194`, volume `browser_profiles:/profiles`
на `:178-180`) лежит на томе LUKS2. В v3 там cookie четырёх соцсетей каждого пользователя (до 10 человек), то есть
вход в их аккаунты. Цель: копия диска VPS (снимок у провайдера, украденный бэкап диска) не даёт этих cookie.

Команды выполняет владелец на VPS под root в `/opt/real-estate-bot`. Пароль тома вводит только сам владелец, в
командах он нигде не печатается. ⛔ Стоп 3: шаги 3-5 и 8 меняют данные, перед каждым нужно отдельное «ок» владельца.

Имена в этом документе:

| Что | Значение |
|---|---|
| файл-контейнер | `/var/lib/rebot-profiles.luks` |
| открытый том | `/dev/mapper/rebot-profiles` |
| точка монтирования | `/srv/rebot-profiles` |
| каталог профилей (bind в `/profiles`) | `/srv/rebot-profiles/profiles`, владелец `10002:10002` (`browseruser`, `docker/browser-session.Dockerfile:10`), права 0700 |
| старый volume | `real-estate-monitor_browser_profiles` |

Профиль монтирует только `browser`. `facebook-collector`, `facebook-runner`, `telegram`, `campaign-runner` ходят в
него по HTTP (`http://browser:8090`): томов профилей у них нет, но во время переноса они не должны запускать сбор.

## 1. Предусловия и проверка (только чтение)

```bash
cd /opt/real-estate-bot
df -h / /var/lib/docker                              # свободно на диске
free -m; nproc                                       # RAM: argon2id в luksFormat берёт до 1 ГБ
cryptsetup --version                                 # нужна 2.x (LUKS2)
docker compose version                               # нужна v2+ (bind.create_host_path)
docker volume ls --filter name=real-estate-monitor_browser_profiles
docker volume inspect -f '{{ .Mountpoint }}' real-estate-monitor_browser_profiles
du -sh "$(docker volume inspect -f '{{ .Mountpoint }}' real-estate-monitor_browser_profiles)"
ls -la "$(docker volume inspect -f '{{ .Mountpoint }}' real-estate-monitor_browser_profiles)"
ls -la docker-compose.override.yml 2>/dev/null || echo "override нет"
grep -E '^COMPOSE_FILE=' .env || echo "COMPOSE_FILE не задан"
git check-ignore -v docker-compose.override.yml || echo "override не игнорируется git"
docker compose --env-file .env exec -T postgres sh -c \
  'psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select profile_name, platform, state from browser_profiles where deleted_at is null"'
```

Норма:
- `cryptsetup 2.x`, Compose `v2.x` или новее;
- volume один, `Mountpoint` вида `/var/lib/docker/volumes/real-estate-monitor_browser_profiles/_data`;
- свободно не меньше `размер файла-контейнера + 2 × du профилей` (по умолчанию файл 20 ГБ, см. шаг 3);
- override ещё нет, `COMPOSE_FILE` не задан (иначе Compose не подхватит `docker-compose.override.yml` сам, и
  схема из шага 4 не сработает: тогда сначала разобраться, откуда взялся `COMPOSE_FILE`);
- профили в `ready`, ни одного `in_use`.

Если что-то не так, дальше не идти. Откат: не нужен, ничего не менялось.

## 2. Выбор схемы

**По умолчанию: файл-контейнер LUKS2 на существующем диске** (loop-устройство). Отдельный диск не нужен,
размер задаётся файлом, при нехватке файл можно увеличить.

**Альтернатива: отдельный блочный том у провайдера VPS.** Те же команды, только вместо файла `/dev/<диск>` (без
`fallocate` и `losetup`). Плюс: не занимает место на системном диске. Минус: платный, зависит от провайдера.

**Почему не автоматическая разблокировка ключом на том же диске** (keyfile в `/etc/crypttab`, ключ в `.env`, в
скрипте): ключ лежит рядом с данными. Тот, у кого копия диска, получает и файл-контейнер, и ключ, и открывает его.
Шифрование тогда защищает только от того, чего не бывает. TPM/сетевой ключ (Tang/Clevis) на типовом VPS недоступен
или требует второй сервер, поэтому сейчас не рассматривается.

**Значит, разблокировка ручная**: после каждой перезагрузки владелец заходит по SSH и вводит пароль
(`cryptsetup open`, шаг 6). Чем это грозит:
- пока том закрыт, `browser` не стартует: сбор через браузер (Facebook в браузерном режиме, проверки, окно `/login`)
  не работает. Остальные сервисы и Apify-источники работают;
- `scripts/update.sh` при закрытом томе падает на шаге `Restart`: `up -d --force-recreate` не может создать
  `browser` (источник bind отсутствует, `create_host_path: false`), `set -e` обрывает скрипт до `Cleanup`/`Status`.
  Поэтому перед каждым деплоем: `mountpoint -q /srv/rebot-profiles && echo открыт`.

**Проверка и алерт.** Каталог `/srv/rebot-profiles/profiles` существует только внутри открытого тома, а bind
с `create_host_path: false` не даёт Docker создать пустую замену. Поэтому при закрытом томе контейнер не стартует
(а не стартует с пустыми профилями на незашифрованном диске). Что увидит watchdog (`bot/ops/watchdog.py:32`,
`:150`, нужен `COMPOSE_PROFILES=ops` и `WATCHDOG_DOCKER_SOCKET`, см. `docs/v2/OPS.md` «Watchdog»):
- внезапная перезагрузка (без `down`): Docker пытается поднять старый контейнер, старт не проходит, `browser` в
  `exited`, приходит алерт `Сервис browser: exited`. **Не `unhealthy`**: до healthcheck дело не доходит;
- перезагрузка по `docs/v2/OPS.md` (с `down`) или прерванный `update.sh`: контейнера `browser` нет вовсе, watchdog
  отсутствующий контейнер не видит, алерта нет. В этих случаях владелец и так за консолью; проверка ниже обязательна.

```bash
mountpoint -q /srv/rebot-profiles && echo "том открыт" || echo "ТОМ ЗАКРЫТ"
docker compose --env-file .env ps -a browser            # норма: Up ... (healthy)
```

Предложение (не сделано, вне задачи): научить watchdog алертить, если контейнера ожидаемого сервиса нет вовсе.

## 3. Создание тома ⛔ «ок» владельца

Перед началом бэкап БД (скилл `deploy-vps`, п. 1), хотя БД здесь не меняется:

```bash
cd /opt/real-estate-bot && set -o pipefail && ./scripts/backup.sh && ls -lh /root/backups | tail -1
```

Создание. Размер `20G` подходит для 40 профилей (10 пользователей × 4 сети) с запасом; если `du` из шага 1
больше 5 ГБ, взять `max(20G, 4 × du)`.

```bash
set -euo pipefail
fallocate -l 20G /var/lib/rebot-profiles.luks
chmod 600 /var/lib/rebot-profiles.luks
cryptsetup luksFormat --type luks2 /var/lib/rebot-profiles.luks
#   отвечает: YES (заглавными), затем пароль дважды: <пароль из менеджера паролей, см. шаг 9>
#   мало RAM (меньше 2 ГБ) и падает по памяти: добавить --pbkdf-memory 262144
cryptsetup open /var/lib/rebot-profiles.luks rebot-profiles     # пароль: <...>
mkfs.ext4 -L rebot-profiles /dev/mapper/rebot-profiles
mkdir -p /srv/rebot-profiles
chmod 700 /srv/rebot-profiles
chattr +i /srv/rebot-profiles          # пустая точка монтирования неизменяема: при закрытом томе в неё не записать
mount /dev/mapper/rebot-profiles /srv/rebot-profiles
mkdir /srv/rebot-profiles/profiles
chown 10002:10002 /srv/rebot-profiles/profiles
chmod 700 /srv/rebot-profiles/profiles
```

Норма:

```bash
cryptsetup luksDump /var/lib/rebot-profiles.luks | grep -E '^Version|Cipher|PBKDF'   # Version: 2, aes-xts-plain64, argon2id
cryptsetup status rebot-profiles | grep -E 'type|loop'                               # LUKS2, loop:
findmnt /srv/rebot-profiles                                                          # /dev/mapper/rebot-profiles ext4
stat -c '%U:%G %a %n' /srv/rebot-profiles/profiles                                   # 10002:10002 700 (или имя пользователя)
```

В `/etc/fstab` и `/etc/crypttab` ничего не добавлять: автоматическое открытие невозможно без ключа рядом с данными,
а запись без `noauto` может остановить загрузку VPS на запросе пароля.

Откат (до шага 4 сервис не затронут):

```bash
umount /srv/rebot-profiles; cryptsetup close rebot-profiles
chattr -i /srv/rebot-profiles; rmdir /srv/rebot-profiles
rm -f /var/lib/rebot-profiles.luks
```

## 4. Перенос ⛔ «ок» владельца

4.1. Нет активных сессий (все профили не `in_use`, нет идущего сбора):

```bash
cd /opt/real-estate-bot
docker compose --env-file .env exec -T postgres sh -c \
  'psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select profile_name, state from browser_profiles where deleted_at is null"'
docker compose --env-file .env ps -a facebook-collector facebook-runner
```

Норма: `in_use` нет, `facebook-collector` не `Up` (one-shot, профиль `collector`).

4.2. Остановка всех, кто держит профиль или может его запросить:

```bash
docker compose --env-file .env stop facebook-runner browser
docker compose --env-file .env ps -a browser facebook-runner        # оба Exited
```

(`facebook-collector` уже не запущен по 4.1; если `Up`, дождаться окончания, не убивать.)

4.3. Копия со старого volume:

```bash
src="$(docker volume inspect -f '{{ .Mountpoint }}' real-estate-monitor_browser_profiles)"
rsync -aHAX --numeric-ids "$src"/ /srv/rebot-profiles/profiles/
chown 10002:10002 /srv/rebot-profiles/profiles && chmod 700 /srv/rebot-profiles/profiles
diff -rq --no-dereference "$src" /srv/rebot-profiles/profiles && echo "копия совпадает"
du -sh "$src" /srv/rebot-profiles/profiles
```

Норма: `копия совпадает`, размеры близки. Каталоги профилей внутри 0700 (`bot/browser_session/manager.py:175-182`);
`.session.lock` копируется как есть, менеджер берёт на нём блокировку заново.

4.4. Override на VPS (не в основной `docker-compose.yml`). Compose подхватывает `docker-compose.override.yml` из
каталога проекта сам, если не задан `-f`/`COMPOSE_FILE`; все скрипты (`update.sh`, `backup.sh`, `browser_login.sh`)
вызывают `docker compose --env-file .env` без `-f`, значит override действует везде. Тома сливаются по `target`:
`/profiles` заменяется, `browser_screenshots` остаётся.

```bash
cat > docker-compose.override.yml <<'YAML'
# Local to this VPS, not in git. D20: browser profiles on the LUKS volume.
# docs/v3/VPS_ENCRYPTED_PROFILES.md
services:
  browser:
    volumes:
      - type: bind
        source: /srv/rebot-profiles/profiles
        target: /profiles
        bind:
          create_host_path: false
YAML
chmod 600 docker-compose.override.yml
echo docker-compose.override.yml >> .git/info/exclude       # локальное игнорирование, в репозиторий не попадает
git status --porcelain                                      # override не виден
docker compose --env-file .env config | awk '/^  browser:/,/^  facebook-collector:/' | grep -A4 'target: /profiles' -B3
```

Норма: в `config` у `browser` `type: bind`, `source: /srv/rebot-profiles/profiles`, `target: /profiles`,
`create_host_path: false`.

Почему так с git: `update.sh` проверяет только отслеживаемые файлы (`git status --porcelain --untracked-files=no`),
неотслеживаемый override деплой не блокирует, а `git checkout`/`merge --ff-only` его не трогают. Сломать может одно:
если `docker-compose.override.yml` когда-нибудь появится в репозитории, `checkout` откажется перезаписывать локальный
файл и `update.sh` упадёт на шаге `Code`. Поэтому файл в `.git/info/exclude`; добавить его в общий `.gitignore` можно
отдельным PR (предложение, не сделано). `git clean` на VPS не запускать.

4.5. Старт:

```bash
docker compose --env-file .env up -d browser
sleep 130                                                   # start_period браузера 120 с
docker compose --env-file .env ps browser                   # Up ... (healthy)
docker compose --env-file .env exec -T browser sh -c 'grep " /profiles " /proc/self/mountinfo; ls -la /profiles; touch /profiles/.probe && rm /profiles/.probe && echo write ok'
docker compose --env-file .env logs --tail=50 browser | grep -iE "error|traceback|permission" || echo ok
docker compose --env-file .env up -d facebook-runner
docker compose --env-file .env ps facebook-runner           # Up ... (healthy)
```

Норма: в `mountinfo` строка с `/dev/mapper/rebot-profiles` (или `dm-N`) и `/profiles`; `write ok`; логи `ok`.

4.6. Профиль `ready` и вход жив. По каждому профилю (имя и сеть из шага 1) открыть окно входа: в Telegram `/login`
или

```bash
bash scripts/browser_login.sh facebook facebook-main        # для остальных: <платформа> <имя профиля>
```

Норма: страница сети открыта под пользователем, вход заново не просит; на вопрос скрипта ответить `y` (профиль
остаётся `ready`). Если просит войти: не входить, ответить `N` и сделать откат (шаг 8): копия не та.

4.7. Проверка алерта (по желанию, 2-3 минуты простоя браузера; нужен включённый watchdog):

```bash
docker compose --env-file .env stop browser
umount /srv/rebot-profiles && cryptsetup close rebot-profiles
docker compose --env-file .env start browser                # ожидаемо ошибка: bind source path does not exist
docker compose --env-file .env ps -a browser                # Exited; через 1-2 мин алерт "Сервис browser: exited"
# вернуть: шаг 6
```

Откат шага 4: шаг 8.

## 5. Удаление старой копии ⛔ отдельное «ок» владельца

Это удаление данных, после него шаг 8 (откат) невозможен. Не раньше чем через **14 дней** после успешного 4.6 и
хотя бы одной перезагрузки по шагу 6. Отдельным шагом, не вместе с переносом.

Сначала только посмотреть:

```bash
cd /opt/real-estate-bot
docker compose --env-file .env config | grep -c browser_profiles     # 0: основной compose + override его не используют
docker ps -a --filter volume=real-estate-monitor_browser_profiles    # пусто: ни один контейнер его не держит
src="$(docker volume inspect -f '{{ .Mountpoint }}' real-estate-monitor_browser_profiles)"; echo "$src"
ls -la "$src"; du -sh "$src"
```

Норма: `0`, список контейнеров пуст, путь `/var/lib/docker/volumes/real-estate-monitor_browser_profiles/_data`.

Удаление (только этот volume; Docker откажет, если он кем-то используется):

```bash
docker volume rm real-estate-monitor_browser_profiles
docker volume ls --filter name=real-estate-monitor_browser_profiles  # пусто
```

Честно о пределе: удалённые файлы остаются в свободных блоках диска (и в снимках провайдера, сделанных раньше),
`shred` на ext4/SSD этого не гарантирует. Старые cookie перестают быть опасными только когда сессии истекут или
будут отозваны. Полная гарантия: после переноса в каждой сети завершить другие сеансы и войти заново (окно `/login`);
это решение владельца, пользователям придётся войти снова.

Откат: нет (данные удалены). Профили остаются на LUKS-томе.

## 6. После перезагрузки VPS

Порядок `docs/v2/OPS.md` «Перезагрузка VPS» тот же, между шагами 3 и 4 открыть том:

```bash
cryptsetup open /var/lib/rebot-profiles.luks rebot-profiles          # пароль: <...>
mount /dev/mapper/rebot-profiles /srv/rebot-profiles
ls -ld /srv/rebot-profiles/profiles                                  # drwx------ 10002 10002
cd /opt/real-estate-bot && docker compose --env-file .env up -d      # при внезапной перезагрузке хватит: up -d browser
sleep 130
docker compose --env-file .env ps                                    # browser Up (healthy), остальные тоже
docker compose --env-file .env logs --since=5m browser | grep -iE "error|traceback" || echo ok
```

Норма: `browser` `Up (healthy)`, логи `ok`, `/login` открывает профиль без нового входа. `cryptsetup open` с
ошибкой `No key available with this passphrase`: неверный пароль, повторить (данные не меняются).

Перед каждым `BRANCH=main ./scripts/update.sh`: `mountpoint -q /srv/rebot-profiles && echo открыт`.

Закрыть том вручную (например, перед долгим простоем):

```bash
docker compose --env-file .env stop browser facebook-runner
umount /srv/rebot-profiles && cryptsetup close rebot-profiles
```

## 7. Бэкап

`scripts/backup.sh` делает только `pg_dump` БД; профили браузера в бэкап не входят, и так должно остаться.

Рекомендация: **профили не бэкапить**. При потере тома (сломался диск, забыт пароль) пользователь просто входит в
сеть заново через `/login`; записи `browser_profiles` в БД остаются, их состояние сменится на
`human_verification_required` при первой проверке. Если бэкап всё же нужен, то **только зашифрованным** (например,
копия самого файла `/var/lib/rebot-profiles.luks` при закрытом томе, или `tar | gpg --symmetric` с отдельным
паролем) и не в `/root/backups` рядом с дампами. Незашифрованная копия профилей обнуляет смысл шифрования.

По желанию (защита от порчи заголовка LUKS, без него данные не открыть):

```bash
cryptsetup luksHeaderBackup /var/lib/rebot-profiles.luks --header-backup-file /root/rebot-profiles.header
# скачать файл к себе в менеджер паролей (вложение), затем на VPS:
rm -f /root/rebot-profiles.header
```

## 8. Откат на старый volume

Возможен, пока не выполнен шаг 5.

```bash
cd /opt/real-estate-bot
docker compose --env-file .env stop facebook-runner browser
mv docker-compose.override.yml docker-compose.override.yml.disabled
docker compose --env-file .env config | awk '/^  browser:/,/^  facebook-collector:/' | grep -B1 -A1 'target: /profiles'   # source: browser_profiles
docker compose --env-file .env up -d browser facebook-runner
sleep 130 && docker compose --env-file .env ps browser facebook-runner                            # Up (healthy)
```

Норма: `browser` снова на `browser_profiles`, `Up (healthy)`. В старом volume состояние на момент переноса: если
сеть с тех пор обновила cookie, может понадобиться войти заново (`/login`). LUKS-том можно оставить открытым или
закрыть (шаг 6, «Закрыть том»); удалить его: откат шага 3.

Вернуться на LUKS: `mv docker-compose.override.yml.disabled docker-compose.override.yml`, затем 4.3 (новая копия,
если работали на старом volume), 4.5, 4.6.

## 9. Где хранить пароль тома

- В менеджере паролей владельца (запись «VPS rebot-profiles LUKS»), там же при желании вложение с бэкапом заголовка.
- Не на VPS (ни в файле, ни в `.env`, ни в cron, ни в истории shell: пароль вводится только в запрос `cryptsetup`).
- Не в чате, не в Telegram боту, не в тикетах и не в репозитории.
- Пароль длинный (не меньше 20 случайных символов или 6 слов). Потеря пароля = потеря профилей (см. шаг 7: входят
  заново). Сменить пароль: `cryptsetup luksChangeKey /var/lib/rebot-profiles.luks` (старый и новый вводятся в запрос).
