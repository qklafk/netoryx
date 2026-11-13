#!/usr/bin/env bash
# Netoryx installer.
#
#   curl -fsSL https://raw.githubusercontent.com/qklafk/netoryx/main/install.sh | sudo bash
#   sudo bash install.sh --bot-token <token> --admin-id <telegram id> [--domain <name>]
#        [--panel xui|remnawave|marzban|pasarguard|marzneshin|none] [--yes]
#
# Installs Docker, the Netoryx stack (app, PostgreSQL, Caddy, HAProxy edge) and a VPN panel (3x-ui by default) into
# /opt/netoryx. On 3x-ui it also prepares a Telegram-only subscription for the owner (only Telegram uses the VPN).
# The legacy Telegram proxies (--with-tgproxy, --with-socks) are still available but are no longer offered. Every step is safe to repeat: run the script again to upgrade or to resume after an
# error. Secrets are generated once and never printed.
#
# Options (see --help for the full list); useful environment variables:
#   NETORYX_DIR      install directory (default /opt/netoryx)
#   NETORYX_REPO     git URL of the code (default https://github.com/qklafk/netoryx.git)
#   NETORYX_REF      branch or tag to install (default main)
#   NETORYX_SOURCE   copy the code from this local directory instead of cloning
set -euo pipefail

# The whole script is read before anything runs (main is called on the last line), so it can be replaced on disk
# (netoryx update) or piped from curl without surprises.

readonly SUPPORTED_OS="Ubuntu 22.04 / 24.04 / 26.04, Debian 12 / 13, AlmaLinux 9, Rocky Linux 9 (x86_64 or aarch64)"
readonly XUI_PORT=2053
readonly TELEMT_INTERNAL_PORT=993
readonly SUPPORTED_PANELS="xui remnawave marzban pasarguard marzneshin none"
readonly DOCKER_RANGE="172.16.0.0/12"

INSTALL_DIR="${NETORYX_DIR:-/opt/netoryx}"
SECRETS_DIR="${NETORYX_SECRETS_DIR:-/root/netoryx}"
REPO_URL="${NETORYX_REPO:-https://github.com/qklafk/netoryx.git}"
REPO_REF="${NETORYX_REF:-main}"
SOURCE_DIR="${NETORYX_SOURCE:-}"
OS_RELEASE_FILE="${NETORYX_OS_RELEASE:-/etc/os-release}"
LOG_FILE="${NETORYX_LOG:-/var/log/netoryx-install.log}"

DRY=0
ASSUME_YES=0
UPDATE_ONLY=0
FORCE_OS=0
NO_FIREWALL=0
BOT_TOKEN_ARG=""
ADMIN_ID_ARG=""
DOMAIN_ARG=""
WITH_XUI=""
PANEL_ARG=""
PANEL="xui"      # xui | remnawave | marzban | pasarguard | marzneshin | none
HAS_PANEL=0
WITH_TGPROXY=""
WITH_SOCKS=""
WITH_SS=""
SS_PORT=8388
BRAND_ARG=""
BRAND="VPN"
SOCKS_PORT=2080
REALITY_PORT_DEFAULT=8445
TG_DOMAIN="petrovich.ru"

OS_ID=""
OS_VERSION=""
OS_CODENAME=""
OS_FAMILY=""      # apt | dnf
DOCKER_REPO=""    # ubuntu | debian | centos
FIREWALL=""       # ufw | firewalld | none
ARCH=""
SERVER_IP=""
DOMAIN=""
BOT_USERNAME=""
STEP=0
STEPS_TOTAL=9
STEP_OPEN=0
STEP_TITLE=""
TMP_DIR=""

# --------------------------------------------------------------------------------------------------------------
# Messages: every text the installer shows to a person lives here, in Russian and English.
# The language comes from --lang ru|en, otherwise from LANG (ru* -> Russian, anything else -> English).
# --------------------------------------------------------------------------------------------------------------
LANG_UI=""

load_messages() {
  if [ -z "$LANG_UI" ]; then
    case "${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}" in
      ru*) LANG_UI="ru" ;;
      *) LANG_UI="en" ;;
    esac
  fi
  if [ "$LANG_UI" = ru ]; then
    # Step titles
    M_S_CHECK="Проверяю систему"
    M_S_SETTINGS="Собираю настройки"
    M_S_DOCKER="Устанавливаю Docker и нужные пакеты"
    M_S_CODE="Загружаю Netoryx и пишу конфигурацию"
    M_S_FIREWALL="Настраиваю файрвол"
    M_S_START="Собираю и запускаю сервисы (первая сборка занимает несколько минут)"
    M_S_VPN="Настраиваю VPN и прокси"
    M_S_HTTPS="Жду HTTPS-сертификат (до 1-2 минут)"
    M_S_FINISH="Завершаю установку"
    M_S_UPD_CHECK="Проверяю систему"
    M_S_UPD_CODE="Загружаю новую версию"
    M_S_UPD_BUILD="Пересобираю приложение (VPN-соединения не прерываются)"
    M_S_UPD_DONE="Готово"
    M_OK="готово"
    M_FAILED="Не удалось выполнить шаг"
    M_FAIL_LOG="Подробности в журнале"
    M_FAIL_RETRY="Ничего не потеряно: устраните причину и запустите установщик ещё раз, выполненные шаги будут пропущены"
    M_FAIL_RETRY_CMD="Повторить"
    # Questions
    M_P_TOKEN_HELP="Токен бота. Откройте @BotFather в Telegram -> /newbot -> скопируйте строку вида 123456789:AAH... (ввод скрыт: просто вставьте и нажмите Enter)."
    M_P_TOKEN="Токен бота"
    M_E_TOKEN_FORMAT="Это не похоже на токен. Он выглядит как 123456789:AAH-... (число, двоеточие, длинный набор символов). Скопируйте его из @BotFather целиком."
    M_E_TOKEN_REJECTED="Telegram не принял этот токен. Проверьте, что скопировали его полностью и бот не удалён (в @BotFather: /mybots)."
    M_W_TOKEN_NOCHECK="Не удалось проверить токен через Telegram (нет ответа). Продолжаю без проверки."
    M_I_BOT_OK="Бот найден"
    M_P_ADMIN_HELP="Ваш Telegram ID - число, по которому бот узнает владельца. Напишите @userinfobot, он ответит числом (например 123456789). Несколько ID - через запятую."
    M_P_ADMIN="Ваш Telegram ID"
    M_E_ADMIN="Нужно число (или несколько чисел через запятую), например 123456789. Его пришлёт @userinfobot."
    M_P_DOMAIN_HELP="Домен для сайта и ссылок подписки. Если домена нет - просто нажмите Enter, будет адрес вида 1-2-3-4.sslip.io (работает сразу, без настройки DNS)."
    M_P_DOMAIN="Домен"
    M_E_DOMAIN_FORMAT="Это не похоже на домен. Пример: vpn.example.com. Или оставьте пустым и нажмите Enter."
    M_Q_DOMAIN_NORESOLVE="Домен %s не указывает на этот сервер (%s). Создайте A-запись на этот адрес и подождите несколько минут. Использовать вместо него адрес sslip.io?"
    M_W_DOMAIN_NORESOLVE="Домен %s не указывает на этот сервер (%s): HTTPS не запустится, пока не создана A-запись."
    M_P_BRAND_HELP="Название вашего сервиса: его увидят клиенты в боте, Mini App и приложениях (например MyVPN). Не знаете - нажмите Enter, будет VPN; потом можно изменить в админке."
    M_P_BRAND="Название сервиса"
    M_E_BRAND="Название должно быть от 1 до 40 символов, без кавычек и переводов строки."
    M_P_PANEL_HELP="Какую панель управления VPN установить? Бот создаёт и продлевает доступы клиентов через неё.
  1) 3x-ui       - простая и лёгкая, проверена на практике (рекомендуется; только с ней работают подписка «только Telegram» и Shadowsocks)
  2) Remnawave   - панель для нескольких серверов и узлов (бета)
  3) Marzban     - популярная панель на Xray (бета)
  4) PasarGuard  - преемник Marzban (бета)
  5) Marzneshin  - панель с узлами marznode (бета)
  6) Без панели  - панель у вас уже есть на другом сервере: подключите её позже в веб-админке (Hiddify тоже подключается так)"
    M_P_PANEL="Номер панели"
    M_E_PANEL="Введите число от 1 до 6 (или нажмите Enter для 3x-ui)."
    M_W_PANEL_FEATURES="SOCKS5 и Shadowsocks работают только с панелью 3x-ui: с панелью %s они пропущены. Подписка «только Telegram» тоже пока доступна только на 3x-ui."
    M_I_ADBLOCK_XUI="Блокировка рекламы работает только с панелью 3x-ui; с другими панелями её можно настроить в самой панели."
    M_X_PANEL_ADMIN="Панель %s: логин и пароль администратора - в файле %s"
    M_X_PANEL_UI="Панель доступна только с сервера. Откройте её через SSH-туннель:"
    M_X_PANEL_UI2="затем в браузере: http://127.0.0.1:%s%s"
    M_Q_XUI="Установить VPN (панель 3x-ui и Reality на порту 443)? Это сам VPN для ваших клиентов."
    M_I_DOMAIN="Домен"
    M_I_COMPONENTS="Состав"
    # Summary
    M_X_TITLE="Netoryx установлен"
    M_X_DRY="(пробный запуск: ничего не изменено)"
    M_X_BOT="Бот"
    M_X_SUB="Адрес подписок"
    M_X_ADMIN="Веб-админка (одноразовая ссылка, действует несколько минут)"
    M_X_ADMIN_NONE="Ссылка на веб-админку: выполните  netoryx admin-link"
    M_X_PANEL_PUBLISHED="Панель 3x-ui: https://panel.%s/ (секретный путь - в файле с доступами)"
    M_X_PANEL_TUNNEL="Панель 3x-ui закрыта от интернета. Откройте её через SSH-туннель:"
    M_X_PANEL_TUNNEL2="затем в браузере: http://127.0.0.1:%s/<путь из файла доступов>"
    M_X_CREDS="Логины и пароли хранятся в"
    M_X_CREDS_NOTE="(только владелец, посмотреть: sudo cat <файл>; никому не показывайте)"
    M_X_TGPROXY="Ваш прокси для Telegram (порт 443)"
    M_X_TGONLY="Общая ссылка «только Telegram»: через VPN идёт только Telegram, остальное - напрямую (её можно публиковать)"
    M_X_TGONLY_HOWTO="Делитесь адресом страницы: на ней подсказка для людей, а ссылка подписки всегда актуальна. Вручную: Happ или v2RayTun -> «Добавить подписку по ссылке» (или QR)."
    M_X_TGONLY_NONE="Подписка «только Telegram» пока не готова (нужен сервер 3x-ui в стране, где Telegram работает). Ссылка: netoryx tg-link"
    M_X_TGONLY_PAGE="Страница для людей (делитесь ей)"
    M_X_SOCKS="SOCKS5 для Telegram: порт %s; личные логины выдаёт бот (раздел Прокси)."
    M_X_NEXT="Что дальше"
    M_X_NEXT1="1) Откройте веб-админку по ссылке выше и создайте тариф (Тарифы)."
    M_X_NEXT2="2) Откройте бота и отправьте /start - проверьте пробный период."
    M_X_NEXT3="3) Установите Happ (или v2RayTun), в боте нажмите «Моя подписка» и импортируйте ссылку."
    M_X_CHEAT="Команды"
    M_X_LOG="Журнал установки"
    M_I_OS="Система: %s %s (%s) -> менеджер пакетов %s, репозиторий Docker %s, файрвол %s"
    M_D_SYSTEMD="нужен systemd"
    M_W_RAM="всего %s МБ оперативной памяти (рекомендуется не меньше 2 ГБ)"
    M_D_RAM="всего %s МБ оперативной памяти: для сборки образов нужно не меньше 2 ГБ"
    M_W_DISK="свободно всего %s МБ на диске (рекомендуется 10 ГБ)"
    M_D_DISK="свободно всего %s МБ на диске: нужно не меньше 8 ГБ"
    M_I_RESOURCES="память %s МБ, свободно на диске %s МБ"
    M_D_ROOT="запустите от имени root"
    M_D_ARCH="неподдерживаемая архитектура процессора: %s (поддерживаются: x86_64, aarch64)"
    M_D_OS_UNKNOWN="не удаётся прочитать %s: операционная система неизвестна. Поддерживаются: %s"
    M_D_OS_UNSUPPORTED="неподдерживаемая система: %s %s. Поддерживаются: %s. Флаг --force-os попробует всё равно, на ваш риск."
    M_W_OS_FORCED="неподдерживаемая система %s %s; продолжаю из-за --force-os"
    M_D_OS_MAP="не удаётся сопоставить %s с apt или dnf"
    M_D_CODENAME="в %s нет VERSION_CODENAME: не удаётся выбрать репозиторий Docker"
    M_D_DOMAIN_INVALID="некорректный домен: %s"
    M_D_NO_IP="не удаётся определить публичный IP-адрес сервера (нет доступа в интернет?)"
    M_D_NO_TOKEN="не указан --bot-token"
    M_D_BAD_TOKEN="некорректный --bot-token"
    M_D_NO_ADMIN="не указан --admin-id"
    M_D_BAD_ADMIN="некорректный --admin-id"
    M_D_SOCKS_PORT="--socks-port должен быть числом от 1024 до 65535"
    M_D_SS_PORT="--ss-port должен быть числом от 1024 до 65535"
    M_D_TG_DOMAIN="--tg-domain не является корректным доменом"
    M_D_BRAND="некорректный --brand-name"
    M_C_BOT="бот и веб-часть"
    M_W_QRENCODE="qrencode недоступен: QR-код в терминале пропущен"
    M_I_DOCKER_PRESENT="Docker и плагин compose уже установлены"
    M_I_DOCKER_REPO="репозиторий: download.docker.com/linux/%s %s"
    M_I_SWAP_NOT_NEEDED="памяти достаточно (%s МБ): файл подкачки не нужен"
    M_I_SWAP_PRESENT="подкачка уже настроена"
    M_I_SWAP_CREATE="оперативной памяти %s МБ: создаю файл подкачки 2 ГБ (/swapfile)"
    M_W_FIREWALL_SKIPPED="файрвол не тронут (--no-firewall): откройте 80/tcp и 443/tcp%s, а также разрешите сети %s доступ к tcp/%s и к порту Reality на хосте"
    M_I_SSH_PORTS="порты SSH оставлены открытыми"
    M_I_UFW_NOTE="примечание: порты, опубликованные Docker, по его устройству обходят ufw; стек публикует только 80 и 443"
    M_I_FW_TRUSTED="сеть %s доверенная (контейнеры могут обращаться к хосту), для остального действует публичная зона"
    M_I_SELINUX="SELinux включён: у bind-монтирований в docker-compose.yml есть метка :z, больше ничего делать не нужно"
    M_D_SOURCE="NETORYX_SOURCE не является каталогом: %s"
    M_I_COPYING="копирую из %s"
    M_I_CHECKOUT_UPDATE="обновляю существующий каталог с кодом до %s"
    M_W_NOT_GIT="%s существует, но это не git-копия: код оставлен как есть"
    M_D_NO_COMPOSE="после загрузки кода нет файла %s/docker-compose.yml"
    M_I_WOULD_ENV="создал бы %s (права 0600) с ключами BOT_TOKEN, ADMIN_IDS, DOMAIN, SECRET_KEY, POSTGRES_PASSWORD, REALITY_PORT, COMPOSE_PROFILES"
    M_I_PANEL_WORD="панель"
    M_I_ENV_UPDATED="%s обновлён (ключи: BOT_TOKEN ADMIN_IDS DOMAIN SECRET_KEY POSTGRES_PASSWORD REALITY_PORT COMPOSE_PROFILES NETORYX_PANEL)"
    M_I_WOULD_WRITE="записал бы"
    M_I_SNI_WRITTEN="sni.map записан"
    M_I_SNI_KEPT="sni.map правили вручную: оставлен без изменений"
    M_I_WOULD_TELEMT="записал бы %s (tls_domain %s, токен API и секреты пользователей генерируются), владелец uid 65532"
    M_I_TELEMT_WRITTEN="конфигурация telemt записана (секреты в %s)"
    M_I_TELEMT_KEPT="конфигурация telemt уже есть: оставлена как есть"
    M_I_WOULD_PANEL_CREDS="создал бы %s и файлы настроек в %s (права 0600): случайные логин и пароль администратора"
    M_D_PG_CERT="не удалось создать сертификат узла PasarGuard (openssl)"
    M_I_PANEL_WRITTEN="настройки панели записаны в %s; логин и пароль администратора: %s"
    M_D_PG_DOWN="PasarGuard не отвечает; смотрите: netoryx logs pasarguard"
    M_D_PG_KEY="не удалось получить ключ настройки от PasarGuard; смотрите: netoryx logs pasarguard"
    M_D_PG_OWNER="PasarGuard отказался создать владельца (HTTP %s); смотрите %s"
    M_I_WOULD_MZ="создал бы администратора через marzneshin-cli (import-from-env) и записал бы конфигурацию Xray для узла"
    M_D_MZ_DOWN="контейнер Marzneshin не запущен; смотрите: netoryx logs marzneshin"
    M_D_MZ_ADMIN="не удалось создать администратора Marzneshin; смотрите: netoryx logs marzneshin"
    M_D_MZ_CERT="не удалось прочитать сертификат узла Marzneshin; смотрите: netoryx logs marzneshin"
    M_I_MZ_XRAY="конфигурация Xray с входом Reality записана для marznode"
    M_I_WOULD_PG="создал бы владельца с одноразовым ключом (pasarguard-cli) и передал бы панели сертификат узла"
    M_W_PG_CERT="сертификата узла пока нет; точку входа, возможно, придётся достроить в панели"
    M_I_WOULD_RW="сохранил бы ключ узла в %s/node.env и запустил бы контейнер remnanode"
    M_D_RW_KEY="Remnawave не выдал ключ узла; запустите установщик ещё раз"
    M_I_RW_NODE="узел Remnawave запускается; он подключится к панели в течение минуты"
    M_W_NOT_READY="не дождался готовности: %s"
    M_I_WAIT_APP="жду запуска приложения"
    M_L_APP="приложение"
    M_I_APP_UP="приложение запущено"
    M_I_NOT_REQUESTED="не запрошено"
    M_I_WOULD_XUI="задал бы случайные логин, пароль и путь веб-интерфейса в контейнере 3x-ui, они хранятся в %s"
    M_I_WAIT_XUI="жду контейнер 3x-ui"
    M_D_XUI_DOWN="контейнер 3x-ui не запущен; смотрите: netoryx logs xui"
    M_I_XUI_CHANGED="логин, пароль и путь веб-интерфейса заменены на случайные"
    M_I_XUI_APPLIED="доступы уже применены"
    M_I_CREDS="доступы: %s (права 0600)"
    M_I_NO_COMPONENTS="VPN и прокси не запрошены"
    M_I_ATTEMPT="попытка %s не удалась: %s; повторяю"
    M_D_REGISTER="не удалось зарегистрировать серверы: %s. Запустите установщик ещё раз, чтобы повторить."
    M_I_REGISTERED="серверы зарегистрированы (подробности в %s)"
    M_I_WOULD_HTTPS="подождал бы https://%s/healthz (сертификат может выпускаться 1-2 минуты)"
    M_I_LE="Let's Encrypt может выпускать сертификат 1-2 минуты"
    M_I_HTTPS_UP="https://%s работает"
    M_W_HTTPS_DOWN="https://%s пока не отвечает. Проверьте DNS и доступность портов 80 и 443, затем: netoryx logs caddy"
    M_D_NO_INSTALL="установка в %s не найдена: запустите установщик без --update"
    M_U_CHANGED1="В этой версии изменилась конфигурация стека. Приложение обновлено; чтобы применить новую конфигурацию edge/прокси,"
    M_U_PLAIN="Обновлено. Перезапуск edge, прокси и панели не нужен."
    M_X_HEADER="Установщик Netoryx"
    M_C_SOCKS="SOCKS5 на порту %s"
    M_C_SS="Shadowsocks на порту %s"
    M_C_TGPROXY="MTProto-прокси"
    M_D_APP_DOWN="приложение не отвечает; смотрите: netoryx logs app"
    M_U_CHANGED2="выполните:  netoryx restart edge caddy   (открытые VPN-соединения переподключатся, некоторым приложениям нужно переключить VPN)."
    M_U_CHANGED3="Изменения в контейнерах панели применяются командой:"
    M_X_DRYRUN="пробный запуск"
    M_L_XUI="панель 3x-ui"
  else
    M_S_CHECK="Checking the system"
    M_S_SETTINGS="Collecting settings"
    M_S_DOCKER="Installing Docker and required packages"
    M_S_CODE="Downloading Netoryx and writing the configuration"
    M_S_FIREWALL="Configuring the firewall"
    M_S_START="Building and starting the services (the first build takes a few minutes)"
    M_S_VPN="Setting up the VPN and proxies"
    M_S_HTTPS="Waiting for the HTTPS certificate (up to 1-2 minutes)"
    M_S_FINISH="Finishing"
    M_S_UPD_CHECK="Checking the system"
    M_S_UPD_CODE="Downloading the new version"
    M_S_UPD_BUILD="Rebuilding the application (VPN connections are not interrupted)"
    M_S_UPD_DONE="Done"
    M_OK="done"
    M_FAILED="This step failed"
    M_FAIL_LOG="Details are in the log"
    M_FAIL_RETRY="Nothing is lost: fix the cause and run the installer again, finished steps are skipped"
    M_FAIL_RETRY_CMD="Retry"
    M_P_TOKEN_HELP="Bot token. Open @BotFather in Telegram -> /newbot -> copy the string like 123456789:AAH... (input is hidden: just paste it and press Enter)."
    M_P_TOKEN="Bot token"
    M_E_TOKEN_FORMAT="That does not look like a token. It looks like 123456789:AAH-... (digits, a colon, a long string). Copy it from @BotFather in full."
    M_E_TOKEN_REJECTED="Telegram did not accept this token. Check that you copied all of it and the bot still exists (@BotFather: /mybots)."
    M_W_TOKEN_NOCHECK="Could not check the token with Telegram (no answer). Continuing without the check."
    M_I_BOT_OK="Bot found"
    M_P_ADMIN_HELP="Your Telegram ID is the number the bot uses to recognise the owner. Message @userinfobot, it replies with a number (for example 123456789). Several IDs: comma-separated."
    M_P_ADMIN="Your Telegram ID"
    M_E_ADMIN="A number is needed (or several, comma-separated), for example 123456789. @userinfobot will send it."
    M_P_DOMAIN_HELP="Domain for the website and subscription links. No domain? Just press Enter and you get an address like 1-2-3-4.sslip.io (works at once, no DNS setup)."
    M_P_DOMAIN="Domain"
    M_E_DOMAIN_FORMAT="That does not look like a domain. Example: vpn.example.com. Or leave it empty and press Enter."
    M_Q_DOMAIN_NORESOLVE="The domain %s does not point to this server (%s). Create an A record for this address and wait a few minutes. Use an sslip.io address instead?"
    M_W_DOMAIN_NORESOLVE="The domain %s does not point to this server (%s): HTTPS cannot start until the A record exists."
    M_P_BRAND_HELP="The name of YOUR service: your customers see it in the bot, the Mini App and their apps (for example MyVPN). Not sure? Press Enter for VPN; you can change it later in the admin."
    M_P_BRAND="Service name"
    M_E_BRAND="The name must be 1 to 40 characters, without quotes or line breaks."
    M_P_PANEL_HELP="Which VPN management panel to install? The bot creates and renews your customers' access through it.
  1) 3x-ui       - simple and light, proven in practice (recommended; the only one that supports the Telegram-only subscription and Shadowsocks)
  2) Remnawave   - panel for several servers and nodes (beta)
  3) Marzban     - popular Xray panel (beta)
  4) PasarGuard  - Marzban's successor (beta)
  5) Marzneshin  - panel with marznode nodes (beta)
  6) No panel    - you already run a panel on another server: connect it later in the web admin (Hiddify is connected this way too)"
    M_P_PANEL="Panel number"
    M_E_PANEL="Enter a number from 1 to 6 (or press Enter for 3x-ui)."
    M_W_PANEL_FEATURES="SOCKS5 and Shadowsocks work with the 3x-ui panel only: they are skipped with %s. The Telegram-only subscription is 3x-ui only for now as well."
    M_I_ADBLOCK_XUI="Ad blocking works with the 3x-ui panel only; with other panels configure it in the panel itself."
    M_X_PANEL_ADMIN="%s panel: administrator login and password are in %s"
    M_X_PANEL_UI="The panel is reachable from the server only. Open it through an SSH tunnel:"
    M_X_PANEL_UI2="then in a browser: http://127.0.0.1:%s%s"
    M_Q_XUI="Install the VPN (3x-ui panel and Reality on port 443)? This is the VPN itself for your clients."
    M_I_DOMAIN="Domain"
    M_I_COMPONENTS="Components"
    M_X_TITLE="Netoryx is installed"
    M_X_DRY="(dry run: nothing was changed)"
    M_X_BOT="Bot"
    M_X_SUB="Subscription address"
    M_X_ADMIN="Web admin (one-time link, valid for a few minutes)"
    M_X_ADMIN_NONE="Web admin link: run  netoryx admin-link"
    M_X_PANEL_PUBLISHED="3x-ui panel: https://panel.%s/ (the secret path is in the credentials file)"
    M_X_PANEL_TUNNEL="The 3x-ui panel is closed to the internet. Open it through an SSH tunnel:"
    M_X_PANEL_TUNNEL2="then in a browser: http://127.0.0.1:%s/<path from the credentials file>"
    M_X_CREDS="Logins and passwords are stored in"
    M_X_CREDS_NOTE="(owner only; read with: sudo cat <file>; never share them)"
    M_X_TGPROXY="Your Telegram proxy (port 443)"
    M_X_TGONLY="Your shared Telegram-only subscription: only Telegram uses the VPN, everything else connects directly (you can publish it)"
    M_X_TGONLY_HOWTO="Share the page address: it guides people, and the subscription link on it is always current. By hand: Happ or v2RayTun -> \"Add subscription by URL\" (or the QR)."
    M_X_TGONLY_NONE="The Telegram-only subscription is not ready yet (it needs a 3x-ui server in a country where Telegram works). Link: netoryx tg-link"
    M_X_TGONLY_PAGE="Page for people (share this one)"
    M_X_SOCKS="SOCKS5 for Telegram: port %s; personal logins are issued by the bot (Proxy section)."
    M_X_NEXT="Next steps"
    M_X_NEXT1="1) Open the web admin with the link above and create a plan (Plans)."
    M_X_NEXT2="2) Open the bot and send /start - check the free trial."
    M_X_NEXT3="3) Install Happ (or v2RayTun), press \"My subscription\" in the bot and import the link."
    M_X_CHEAT="Commands"
    M_X_LOG="Installer log"
    M_I_OS="OS: %s %s (%s) -> package manager %s, Docker repository %s, firewall %s"
    M_D_SYSTEMD="systemd is required"
    M_W_RAM="only %s MB of RAM (at least 2 GB recommended)"
    M_D_RAM="only %s MB of RAM: at least 2 GB is needed to build the images"
    M_W_DISK="only %s MB of free disk space (10 GB recommended)"
    M_D_DISK="only %s MB of free disk space: at least 8 GB is needed"
    M_I_RESOURCES="memory %s MB, free disk %s MB"
    M_D_ROOT="run as root"
    M_D_ARCH="unsupported CPU architecture: %s (supported: x86_64, aarch64)"
    M_D_OS_UNKNOWN="cannot read %s: unknown operating system. Supported: %s"
    M_D_OS_UNSUPPORTED="unsupported system: %s %s. Supported: %s. Use --force-os to try anyway, at your own risk."
    M_W_OS_FORCED="unsupported system %s %s; continuing because of --force-os"
    M_D_OS_MAP="cannot map %s to apt or dnf"
    M_D_CODENAME="%s has no VERSION_CODENAME: cannot choose the Docker repository"
    M_D_DOMAIN_INVALID="the domain \"%s\" is not valid"
    M_D_NO_IP="cannot detect the public IP address of this server (no internet access?)"
    M_D_NO_TOKEN="missing --bot-token"
    M_D_BAD_TOKEN="invalid --bot-token"
    M_D_NO_ADMIN="missing --admin-id"
    M_D_BAD_ADMIN="invalid --admin-id"
    M_D_SOCKS_PORT="--socks-port must be a number between 1024 and 65535"
    M_D_SS_PORT="--ss-port must be a number between 1024 and 65535"
    M_D_TG_DOMAIN="--tg-domain is not a valid domain name"
    M_D_BRAND="invalid --brand-name"
    M_C_BOT="bot and web"
    M_W_QRENCODE="qrencode is not available: the terminal QR code is skipped"
    M_I_DOCKER_PRESENT="Docker and the compose plugin are already installed"
    M_I_DOCKER_REPO="repository: download.docker.com/linux/%s %s"
    M_I_SWAP_NOT_NEEDED="enough memory (%s MB): no swap file needed"
    M_I_SWAP_PRESENT="swap is already configured"
    M_I_SWAP_CREATE="%s MB of RAM: creating a 2 GB swap file (/swapfile)"
    M_W_FIREWALL_SKIPPED="firewall left untouched (--no-firewall): open 80/tcp and 443/tcp%s, and let %s reach tcp/%s and the Reality port on the host"
    M_I_SSH_PORTS="SSH port(s) kept open"
    M_I_UFW_NOTE="note: ports published by Docker bypass ufw by design; the stack publishes only 80 and 443"
    M_I_FW_TRUSTED="%s is trusted (containers may reach the host); everything else follows the public zone"
    M_I_SELINUX="SELinux is enforcing: bind mounts in docker-compose.yml carry the :z label, nothing else to do"
    M_D_SOURCE="NETORYX_SOURCE is not a directory: %s"
    M_I_COPYING="copying from %s"
    M_I_CHECKOUT_UPDATE="updating the existing checkout to %s"
    M_W_NOT_GIT="%s exists but is not a git checkout: leaving the code as it is"
    M_D_NO_COMPOSE="%s/docker-compose.yml is missing after fetching the code"
    M_I_WOULD_ENV="would create %s (mode 0600) with BOT_TOKEN, ADMIN_IDS, DOMAIN, SECRET_KEY, POSTGRES_PASSWORD, REALITY_PORT, COMPOSE_PROFILES"
    M_I_PANEL_WORD="panel"
    M_I_ENV_UPDATED="%s updated (keys: BOT_TOKEN ADMIN_IDS DOMAIN SECRET_KEY POSTGRES_PASSWORD REALITY_PORT COMPOSE_PROFILES NETORYX_PANEL)"
    M_I_WOULD_WRITE="would write"
    M_I_SNI_WRITTEN="sni.map written"
    M_I_SNI_KEPT="sni.map was edited by hand: left as it is"
    M_I_WOULD_TELEMT="would write %s (tls_domain %s, API token and user secrets generated), owned by uid 65532"
    M_I_TELEMT_WRITTEN="telemt config written (secrets in %s)"
    M_I_TELEMT_KEPT="telemt config already exists: kept"
    M_I_WOULD_PANEL_CREDS="would create %s and the settings files in %s (mode 0600): random administrator login and password"
    M_D_PG_CERT="could not create the PasarGuard node certificate (openssl)"
    M_I_PANEL_WRITTEN="panel settings written to %s; administrator login and password: %s"
    M_D_PG_DOWN="PasarGuard does not answer; see: netoryx logs pasarguard"
    M_D_PG_KEY="could not get a setup key from PasarGuard; see: netoryx logs pasarguard"
    M_D_PG_OWNER="PasarGuard refused to create the owner (HTTP %s); see %s"
    M_I_WOULD_MZ="would create the administrator with marzneshin-cli (import-from-env) and write the node's Xray configuration"
    M_D_MZ_DOWN="the Marzneshin container is not running; see: netoryx logs marzneshin"
    M_D_MZ_ADMIN="could not create the Marzneshin administrator; see: netoryx logs marzneshin"
    M_D_MZ_CERT="could not read the Marzneshin node certificate; see: netoryx logs marzneshin"
    M_I_MZ_XRAY="Xray configuration with the Reality inbound written for marznode"
    M_I_WOULD_PG="would create the owner with a one-time key (pasarguard-cli) and hand the node certificate to the panel"
    M_W_PG_CERT="the node certificate is not there yet; the entry point may have to be finished in the panel"
    M_I_WOULD_RW="would store the node key in %s/node.env and start the remnanode container"
    M_D_RW_KEY="Remnawave did not provide the node key; run the installer again"
    M_I_RW_NODE="the Remnawave node is starting; it connects to the panel within a minute"
    M_W_NOT_READY="did not become ready in time: %s"
    M_I_WAIT_APP="waiting for the application"
    M_L_APP="the application"
    M_I_APP_UP="the application is up"
    M_I_NOT_REQUESTED="not requested"
    M_I_WOULD_XUI="would set a random login, password and web base path on the 3x-ui container, stored in %s"
    M_I_WAIT_XUI="waiting for the 3x-ui container"
    M_D_XUI_DOWN="the 3x-ui container is not running; see: netoryx logs xui"
    M_I_XUI_CHANGED="login, password and web base path changed from the defaults"
    M_I_XUI_APPLIED="credentials already applied"
    M_I_CREDS="credentials: %s (mode 0600)"
    M_I_NO_COMPONENTS="no VPN or proxy components requested"
    M_I_ATTEMPT="attempt %s failed: %s; retrying"
    M_D_REGISTER="could not register the servers: %s. Run the installer again to retry."
    M_I_REGISTERED="servers registered (details in %s)"
    M_I_WOULD_HTTPS="would wait for https://%s/healthz (the certificate can take 1-2 minutes)"
    M_I_LE="Let's Encrypt can take 1-2 minutes"
    M_I_HTTPS_UP="https://%s is up"
    M_W_HTTPS_DOWN="https://%s does not answer yet. Check DNS and that ports 80 and 443 are reachable, then: netoryx logs caddy"
    M_D_NO_INSTALL="no installation found in %s: run the installer without --update"
    M_U_CHANGED1="The stack configuration changed in this release. The application is updated; to apply the new edge/proxy"
    M_U_PLAIN="Updated. No restart of the edge, proxy or panel is needed."
    M_X_HEADER="Netoryx installer"
    M_C_SOCKS="SOCKS5 on %s"
    M_C_SS="Shadowsocks on %s"
    M_C_TGPROXY="MTProto proxy"
    M_D_APP_DOWN="the application does not answer; see: netoryx logs app"
    M_U_CHANGED2="configuration run:  netoryx restart edge caddy   (open VPN connections reconnect, some apps need a toggle)."
    M_U_CHANGED3="Changes to the panel containers apply with:"
    M_X_DRYRUN="dry run"
    M_L_XUI="the 3x-ui panel"
  fi
}

# --------------------------------------------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------------------------------------------
log_line() {
  if [ "$DRY" = 0 ] && [ -n "$LOG_FILE" ] && [ -w "$(dirname "$LOG_FILE")" ]; then
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG_FILE" 2>/dev/null || true
  fi
}
say() { printf '%s\n' "$*"; log_line "$*"; }
info() { printf '    %s\n' "$*"; log_line "    $*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; log_line "WARNING: $*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; log_line "ERROR: $*"; exit 1; }
OK_MARK="[ok]"
FAIL_MARK="[x]"
case "${LC_ALL:-${LANG:-}}" in
  *UTF-8* | *utf8* | *UTF8*) OK_MARK="✓"; FAIL_MARK="✗" ;;
esac

step_ok() {
  if [ "$STEP_OPEN" = 1 ]; then
    printf '    %s %s\n' "$OK_MARK" "$M_OK"
    log_line "    ok: $STEP_TITLE"
    STEP_OPEN=0
  fi
}

step() {
  step_ok
  STEP=$((STEP + 1))
  STEP_TITLE="$*"
  STEP_OPEN=1
  printf '\n[%d/%d] %s...\n' "$STEP" "$STEPS_TOTAL" "$*"
  log_line "[$STEP/$STEPS_TOTAL] $*"
}

on_error() {
  local code=$? line=$1
  trap - ERR
  if [ "$STEP_OPEN" = 1 ]; then
    printf '\n%s [%d/%d] %s\n' "$FAIL_MARK" "$STEP" "$STEPS_TOTAL" "$STEP_TITLE" >&2
  fi
  printf '%s (line %s, exit code %s).\n' "$M_FAILED" "$line" "$code" >&2
  printf '%s\n' "$M_FAIL_RETRY" >&2
  if [ "$DRY" = 0 ]; then
    printf '%s: %s\n' "$M_FAIL_LOG" "$LOG_FILE" >&2
    printf '%s: curl -fsSL https://raw.githubusercontent.com/qklafk/netoryx/main/install.sh | sudo bash\n' "$M_FAIL_RETRY_CMD" >&2
    [ -f "$INSTALL_DIR/install.sh" ] && printf '%s: sudo bash %s/install.sh\n' "$M_FAIL_RETRY_CMD" "$INSTALL_DIR" >&2
  fi
  exit "$code"
}

cleanup() {
  [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ] && rm -rf "$TMP_DIR"
  return 0
}

# Run a command quietly (output goes to the log); print the end of the output if it fails.
run() {
  if [ "$DRY" = 1 ]; then
    printf '    [dry-run] %s\n' "$*"
    return 0
  fi
  log_line "+ $*"
  local out
  out="$(mktemp)"
  if "$@" >"$out" 2>&1; then
    cat "$out" >>"$LOG_FILE" 2>/dev/null || true
    rm -f "$out"
    return 0
  fi
  local code=$?
  cat "$out" >>"$LOG_FILE" 2>/dev/null || true
  printf 'Command failed (%s): %s\n' "$code" "$*" >&2
  tail -n 20 "$out" >&2
  rm -f "$out"
  return "$code"
}

have() { command -v "$1" >/dev/null 2>&1; }

usage() {
  cat <<'EOF'
Netoryx installer

Usage: install.sh [options]

  --bot-token TOKEN     Telegram bot token from @BotFather
  --admin-id ID         Telegram id of the owner (several ids: comma-separated)
  --domain NAME         public domain pointing at this server (default: <ip-with-dashes>.sslip.io)
  --panel NAME          VPN panel to install: xui (default), remnawave, marzban, pasarguard, marzneshin or none
                        (none: connect an existing external panel later in the web admin; Hiddify is connected this way)
  --with-xui            same as --panel xui
  --with-tgproxy        legacy: install the Telegram MTProto proxy (telemt); it may not work in some countries
  --with-socks          legacy: create a SOCKS5 proxy for Telegram (3x-ui only); may not work in some countries
  --socks-port PORT     public port of the SOCKS5 proxy (default 2080)
  --with-shadowsocks    add a Shadowsocks-2022 VPN inbound (fallback protocol, own port; 3x-ui only)
  --ss-port PORT        public port of the Shadowsocks inbound (default 8388)
  --tg-domain NAME      TLS name the MTProto proxy imitates (default petrovich.ru)
  --brand-name NAME     name of YOUR service that customers see in the bot and in subscriptions (default VPN)
  --lang ru|en          language of the messages (default: from LANG)
  --yes, -y             never ask questions (missing values are an error)
  --update              fetch the latest release and restart only the application
  --no-firewall         do not touch the firewall (open 80, 443 and the SOCKS port yourself)
  --force-os            try an operating system that is not on the supported list
  --dry-run             print what would be done and change nothing
  --help                this text

Supported systems: Ubuntu 22.04 / 24.04 / 26.04, Debian 12 / 13, AlmaLinux 9, Rocky Linux 9; x86_64 and aarch64.
EOF
}

# --------------------------------------------------------------------------------------------------------------
# Arguments and questions
# --------------------------------------------------------------------------------------------------------------
parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --bot-token) BOT_TOKEN_ARG="${2:?--bot-token needs a value}"; shift 2 ;;
      --bot-token=*) BOT_TOKEN_ARG="${1#*=}"; shift ;;
      --admin-id) ADMIN_ID_ARG="${2:?--admin-id needs a value}"; shift 2 ;;
      --admin-id=*) ADMIN_ID_ARG="${1#*=}"; shift ;;
      --domain) DOMAIN_ARG="${2:?--domain needs a value}"; shift 2 ;;
      --domain=*) DOMAIN_ARG="${1#*=}"; shift ;;
      --with-xui) PANEL_ARG="${PANEL_ARG:-xui}"; shift ;;
      --panel) PANEL_ARG="${2:?--panel needs a value}"; shift 2 ;;
      --panel=*) PANEL_ARG="${1#*=}"; shift ;;
      --with-tgproxy) WITH_TGPROXY=1; shift ;;
      --with-socks) WITH_SOCKS=1; shift ;;
      --with-shadowsocks) WITH_SS=1; shift ;;
      --ss-port) SS_PORT="${2:?--ss-port needs a value}"; shift 2 ;;
      --ss-port=*) SS_PORT="${1#*=}"; shift ;;
      --socks-port) SOCKS_PORT="${2:?--socks-port needs a value}"; shift 2 ;;
      --socks-port=*) SOCKS_PORT="${1#*=}"; shift ;;
      --tg-domain) TG_DOMAIN="${2:?--tg-domain needs a value}"; shift 2 ;;
      --tg-domain=*) TG_DOMAIN="${1#*=}"; shift ;;
      --brand-name) BRAND_ARG="${2:?--brand-name needs a value}"; shift 2 ;;
      --brand-name=*) BRAND_ARG="${1#*=}"; shift ;;
      --lang) LANG_UI="${2:?--lang needs ru or en}"; shift 2 ;;
      --lang=*) LANG_UI="${1#*=}"; shift ;;
      --yes | -y) ASSUME_YES=1; shift ;;
      --update) UPDATE_ONLY=1; shift ;;
      --no-firewall) NO_FIREWALL=1; shift ;;
      --force-os) FORCE_OS=1; shift ;;
      --dry-run) DRY=1; shift ;;
      -h | --help) usage; exit 0 ;;
      *) die "unknown option: $1 (see --help)" ;;
    esac
  done
  case "$LANG_UI" in "" | ru | en) ;; *) die "--lang must be ru or en" ;; esac
  if [ -n "$PANEL_ARG" ]; then
    case " $SUPPORTED_PANELS " in *" $PANEL_ARG "*) ;; *) die "--panel must be one of: $SUPPORTED_PANELS" ;; esac
  fi
  if [ -z "$PANEL_ARG" ] && { [ "$WITH_SOCKS" = 1 ] || [ "$WITH_SS" = 1 ]; }; then PANEL_ARG=xui; fi
}

# True when questions can be asked: a person is at the keyboard and --yes was not given.
interactive() {
  [ "$ASSUME_YES" = 0 ] && [ "$DRY" = 0 ] && ( : </dev/tty ) 2>/dev/null
}

# ask_input VAR HELP LABEL DEFAULT SECRET VALIDATOR
# Shows HELP, asks "LABEL [DEFAULT]:", Enter accepts the default, and repeats until VALIDATOR (which prints its own
# friendly error) accepts the answer.
ask_input() {
  local var="$1" help="$2" label="$3" default="$4" secret="$5" validator="$6" reply=""
  while true; do
    printf '\n%s\n' "$help" >/dev/tty
    if [ "$secret" = 1 ]; then
      read -r -s -p "$label${default:+ [$default]}: " reply </dev/tty || reply=""
      printf '\n' >/dev/tty
    else
      read -r -p "$label${default:+ [$default]}: " reply </dev/tty || reply=""
    fi
    reply="${reply:-$default}"
    if "$validator" "$reply" >/dev/tty; then
      printf -v "$var" '%s' "$reply"
      return 0
    fi
  done
}

# ask_yes_no VAR QUESTION DEFAULT(1|0): [Y/n] or [y/N]. Without a person at the keyboard the answer is 0, so
# components are only installed when asked for by flags.
ask_yes_no() {
  local var="$1" question="$2" default="$3" reply=""
  if ! interactive; then
    printf -v "$var" '%s' 0
    return 0
  fi
  read -r -p "$(printf '\n%s [%s] ' "$question" "$([ "$default" = 1 ] && echo Y/n || echo y/N)")" reply </dev/tty || reply=""
  case "${reply:-}" in
    [Yy]*) printf -v "$var" '%s' 1 ;;
    [Nn]*) printf -v "$var" '%s' 0 ;;
    *) printf -v "$var" '%s' "$default" ;;
  esac
}

# --------------------------------------------------------------------------------------------------------------
# Environment detection
# --------------------------------------------------------------------------------------------------------------
detect_arch() {
  local machine="${NETORYX_ARCH:-$(uname -m)}"
  case "$machine" in
    x86_64 | amd64) ARCH="x86_64" ;;
    aarch64 | arm64) ARCH="aarch64" ;;
    *) die "$(printf "$M_D_ARCH" "$machine")" ;;
  esac
}

# Sets OS_ID, OS_VERSION, OS_CODENAME, OS_FAMILY, DOCKER_REPO from an os-release file.
detect_os() {
  [ -r "$OS_RELEASE_FILE" ] || die "$(printf "$M_D_OS_UNKNOWN" "$OS_RELEASE_FILE" "$SUPPORTED_OS")"
  local id version codename like
  id="$(. "$OS_RELEASE_FILE" >/dev/null 2>&1; printf '%s' "${ID:-}")"
  version="$(. "$OS_RELEASE_FILE" >/dev/null 2>&1; printf '%s' "${VERSION_ID:-}")"
  codename="$(. "$OS_RELEASE_FILE" >/dev/null 2>&1; printf '%s' "${VERSION_CODENAME:-}")"
  like="$(. "$OS_RELEASE_FILE" >/dev/null 2>&1; printf '%s' "${ID_LIKE:-}")"
  id="$(printf '%s' "$id" | tr '[:upper:]' '[:lower:]')"
  like="$(printf '%s' "$like" | tr '[:upper:]' '[:lower:]')"
  OS_ID="$id"
  OS_VERSION="$version"
  OS_CODENAME="$codename"
  case "$id:$version" in
    ubuntu:22.04 | ubuntu:24.04 | ubuntu:26.04) OS_FAMILY="apt"; DOCKER_REPO="ubuntu" ;;
    debian:12 | debian:13) OS_FAMILY="apt"; DOCKER_REPO="debian" ;;
    almalinux:9 | almalinux:9.* | rocky:9 | rocky:9.*) OS_FAMILY="dnf"; DOCKER_REPO="centos" ;;
    *)
      if [ "$FORCE_OS" != 1 ]; then
        die "$(printf "$M_D_OS_UNSUPPORTED" "${id:-unknown}" "${version:-?}" "$SUPPORTED_OS")"
      fi
      warn "$(printf "$M_W_OS_FORCED" "${id:-unknown}" "${version:-?}")"
      case " $id $like " in
        *" ubuntu "*) OS_FAMILY="apt"; DOCKER_REPO="ubuntu" ;;
        *" debian "*) OS_FAMILY="apt"; DOCKER_REPO="debian" ;;
        *" rhel "* | *" fedora "* | *" centos "*) OS_FAMILY="dnf"; DOCKER_REPO="centos" ;;
        *) die "$(printf "$M_D_OS_MAP" "${id:-unknown}")" ;;
      esac
      ;;
  esac
  if [ "$OS_FAMILY" = apt ] && [ -z "$OS_CODENAME" ]; then
    # Ubuntu derivatives and some images only carry UBUNTU_CODENAME
    OS_CODENAME="$(. "$OS_RELEASE_FILE" >/dev/null 2>&1; printf '%s' "${UBUNTU_CODENAME:-}")"
  fi
  if [ "$OS_FAMILY" = apt ] && [ -z "$OS_CODENAME" ]; then
    die "$(printf "$M_D_CODENAME" "$OS_RELEASE_FILE")"
  fi
  if [ "$NO_FIREWALL" = 1 ]; then
    FIREWALL="none"
  elif [ "$OS_FAMILY" = apt ]; then
    FIREWALL="ufw"
  else
    FIREWALL="firewalld"
  fi
}

mem_total_mb() {
  if [ -r /proc/meminfo ]; then
    awk '/^MemTotal:/ { printf "%d", $2 / 1024 }' /proc/meminfo
  else
    printf '0'
  fi
}

free_disk_mb() {
  local dir="$1"
  while [ ! -d "$dir" ] && [ "$dir" != "/" ]; do dir="$(dirname "$dir")"; done
  df -Pm "$dir" 2>/dev/null | awk 'NR == 2 { print $4 }'
}

preflight() {
  if [ "$DRY" = 0 ] && [ "$(id -u)" != 0 ]; then
    die "$M_D_ROOT: curl -fsSL https://raw.githubusercontent.com/qklafk/netoryx/main/install.sh | sudo bash"
  fi
  detect_arch
  detect_os
  info "$(printf "$M_I_OS" "$OS_ID" "$OS_VERSION" "$ARCH" "$OS_FAMILY" "$DOCKER_REPO" "$FIREWALL")"
  if [ "$DRY" = 0 ] && ! have systemctl; then
    die "$M_D_SYSTEMD"
  fi
  local ram disk
  ram="$(mem_total_mb)"
  disk="$(free_disk_mb "$INSTALL_DIR")"
  if [ "$ram" -gt 0 ] && [ "$ram" -lt 1400 ]; then
    if [ "$DRY" = 1 ] || [ "$FORCE_OS" = 1 ]; then
      warn "$(printf "$M_W_RAM" "$ram")"
    else
      die "$(printf "$M_D_RAM" "$ram")"
    fi
  fi
  if [ -n "$disk" ] && [ "$disk" -lt 8000 ]; then
    if [ "$DRY" = 1 ]; then warn "$(printf "$M_W_DISK" "$disk")"; else
      die "$(printf "$M_D_DISK" "$disk")"; fi
  fi
  info "$(printf "$M_I_RESOURCES" "$ram" "${disk:-?}")"
}

# --------------------------------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------------------------------
detect_public_ip() {
  if [ -n "${NETORYX_PUBLIC_IP:-}" ]; then
    printf '%s' "$NETORYX_PUBLIC_IP"
    return 0
  fi
  local url ip
  for url in https://api.ipify.org https://ifconfig.me/ip https://icanhazip.com; do
    ip="$(curl -fsS4 --max-time 8 "$url" 2>/dev/null | tr -d '[:space:]' || true)"
    if printf '%s' "$ip" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$'; then
      printf '%s' "$ip"
      return 0
    fi
  done
  return 1
}

# Two-letter country code of this server (for the location flag); empty when it cannot be found.
detect_country() {
  local ip="$1" url code
  for url in "https://ipinfo.io/$ip/country" "https://ipapi.co/$ip/country/"; do
    code="$(curl -fsS --max-time 6 "$url" 2>/dev/null | tr -d '[:space:]' | tr 'a-z' 'A-Z' || true)"
    if printf '%s' "$code" | grep -Eq '^[A-Z]{2}$'; then
      printf '%s' "$code"
      return 0
    fi
  done
  return 1
}

panel_label() {
  case "$1" in
    xui) printf '3x-ui' ;;
    remnawave) printf 'Remnawave' ;;
    marzban) printf 'Marzban' ;;
    pasarguard) printf 'PasarGuard' ;;
    marzneshin) printf 'Marzneshin' ;;
    *) printf 'none' ;;
  esac
}

panel_from_choice() {
  case "$1" in
    2) echo remnawave ;;
    3) echo marzban ;;
    4) echo pasarguard ;;
    5) echo marzneshin ;;
    6) echo none ;;
    *) echo xui ;;
  esac
}

validate_panel_choice() {
  case "$1" in
    1 | 2 | 3 | 4 | 5 | 6) ;;
    *) printf '%s
' "$M_E_PANEL"; return 1 ;;
  esac
}

# Container that listens for the Reality inbound (the edge routes vpn.<domain> to it).
panel_reality_host() {
  case "$1" in
    remnawave) echo remnanode ;;
    marzban) echo marzban ;;
    pasarguard) echo pg-node.internal ;;
    marzneshin) echo marznode ;;
    # 3x-ui runs on the host network. HAProxy re-resolves names through Docker's DNS, which does not know
    # host.docker.internal (it only lives in /etc/hosts), so the edge gets the host's bridge address instead.
    *) docker_host_ip ;;
  esac
}

docker_host_ip() {
  local ip=""
  have docker && ip="$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)"
  case "$ip" in
    *.*.*.*) echo "$ip" ;;
    *) echo 172.17.0.1 ;;
  esac
}

panel_api_url() {
  case "$1" in
    remnawave) echo http://remnawave:3000 ;;
    marzban) echo http://marzban:8880 ;;
    pasarguard) echo http://pasarguard:8000 ;;
    marzneshin) echo http://marzneshin:8000 ;;
  esac
}

# Port of the panel UI published on 127.0.0.1 and the path of its dashboard.
panel_ui_port() { case "$1" in remnawave) echo 3000 ;; xui) echo "$XUI_PORT" ;; *) echo 8000 ;; esac; }
panel_ui_path() { case "$1" in marzban | pasarguard | marzneshin) echo /dashboard/ ;; *) echo / ;; esac; }

valid_domain() { printf '%s' "$1" | grep -Eq '^([A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}$'; }

env_file() { printf '%s/.env' "$INSTALL_DIR"; }

# Value of KEY in .env (empty if missing); surrounding single quotes are removed.
env_get() {
  local file value
  file="$(env_file)"
  [ -f "$file" ] || return 0
  value="$(sed -n "s/^$1=//p" "$file" | tail -n 1)"
  value="${value#\'}"
  value="${value%\'}"
  printf '%s' "$value"
}

# Quote a value for a .env file the way both Docker Compose and the application read it.
env_quote() {
  case "$1" in
    *"'"* | *$'\n'*) die "a value for .env contains a quote or a line break" ;;
  esac
  if printf '%s' "$1" | grep -Eq '^[A-Za-z0-9_@%+=:,./-]*$'; then
    printf '%s' "$1"
  else
    printf "'%s'" "$1"
  fi
}

# Set KEY=VALUE in .env (replace or append). Values are never printed.
env_set() {
  local key="$1" value="$2" file tmp line
  file="$(env_file)"
  line="$key=$(env_quote "$value")"
  tmp="$(mktemp)"
  K="$key" L="$line" awk '
    BEGIN { k = ENVIRON["K"] "="; l = ENVIRON["L"]; found = 0 }
    index($0, k) == 1 { if (!found) print l; found = 1; next }
    { print }
    END { if (!found) print l }
  ' "$file" >"$tmp"
  cat "$tmp" >"$file"
  rm -f "$tmp"
}

gen_hex() { openssl rand -hex "$1"; }

validate_token() {
  local token="$1" reply
  if ! printf '%s' "$token" | grep -Eq '^[0-9]{6,12}:[A-Za-z0-9_-]{30,}$'; then
    printf '%s\n' "$M_E_TOKEN_FORMAT"
    return 1
  fi
  # the token goes to curl through its config on stdin, never through a command line
  reply="$(printf 'url = "https://api.telegram.org/bot%s/getMe"\n' "$token" \
    | curl -sS --max-time 8 -K - 2>/dev/null || true)"
  case "$reply" in
    *'"ok":true'*)
      BOT_USERNAME="$(printf '%s' "$reply" | sed -n 's/.*"username":"\([^"]*\)".*/\1/p')"
      printf '    %s: @%s\n' "$M_I_BOT_OK" "${BOT_USERNAME:-?}"
      ;;
    *'"ok":false'*)
      printf '%s\n' "$M_E_TOKEN_REJECTED"
      return 1
      ;;
    *) printf 'WARNING: %s\n' "$M_W_TOKEN_NOCHECK" ;;
  esac
}

validate_admin() {
  if ! printf '%s' "$1" | grep -Eq '^[0-9]{3,15}(,[0-9]{3,15})*$'; then
    printf '%s\n' "$M_E_ADMIN"
    return 1
  fi
}

validate_brand() {
  if [ -z "$1" ] || [ "${#1}" -gt 40 ] || printf '%s' "$1" | grep -q "['\"]"; then
    printf '%s\n' "$M_E_BRAND"
    return 1
  fi
}

validate_domain_input() {
  if [ -n "$1" ] && ! valid_domain "$1"; then
    printf '%s\n' "$M_E_DOMAIN_FORMAT"
    return 1
  fi
}

# "ok" | "missing" | "other:<address>" for a name compared with this server's address.
dns_state() {
  local resolved
  resolved="$(getent ahostsv4 "$1" 2>/dev/null | awk 'NR == 1 { print $1 }' || true)"
  if [ -z "$resolved" ]; then
    echo missing
  elif [ "$resolved" = "$SERVER_IP" ]; then
    echo ok
  else
    echo "other:$resolved"
  fi
}

sslip_name() { printf '%s.sslip.io' "$(printf '%s' "$SERVER_IP" | tr '.' '-')"; }

choose_domain() {
  local existing="$1" answer=""
  DOMAIN="$DOMAIN_ARG"
  if [ -z "$DOMAIN" ] && [ "$existing" = 1 ]; then DOMAIN="$(env_get DOMAIN)"; fi
  if [ -z "$DOMAIN" ] && interactive; then
    while true; do
      ask_input answer "$M_P_DOMAIN_HELP" "$M_P_DOMAIN" "" 0 validate_domain_input
      if [ -z "$answer" ]; then break; fi
      if printf '%s' "$answer" | grep -q '\.sslip\.io$' || [ "$(dns_state "$answer")" = ok ]; then
        DOMAIN="$answer"
        break
      fi
      local reply=""
      ask_yes_no reply "$(printf "$M_Q_DOMAIN_NORESOLVE" "$answer" "$SERVER_IP")" 1
      if [ "$reply" = 1 ]; then break; fi
    done
  fi
  if [ -z "$DOMAIN" ]; then DOMAIN="$(sslip_name)"; fi
  valid_domain "$DOMAIN" || [ "$DRY" = 1 ] || die "$(printf "$M_D_DOMAIN_INVALID" "$DOMAIN")"
  if [ "$DRY" = 0 ] && ! printf '%s' "$DOMAIN" | grep -q '\.sslip\.io$'; then
    warn_dns "$DOMAIN"
    [ "$HAS_PANEL" != 1 ] || warn_dns "vpn.$DOMAIN"
  fi
  return 0
}

warn_dns() {
  local state
  state="$(dns_state "$1")"
  if [ "$state" != ok ]; then
    warn "$(printf "$M_W_DOMAIN_NORESOLVE" "$1" "$SERVER_IP")"
  fi
}

collect_settings() {
  local existing=0
  [ -f "$(env_file)" ] && existing=1
  local token="$BOT_TOKEN_ARG" admin="$ADMIN_ID_ARG"
  if [ -z "$token" ] && [ "$existing" = 1 ]; then token="$(env_get BOT_TOKEN)"; fi
  if [ -z "$admin" ] && [ "$existing" = 1 ]; then admin="$(env_get ADMIN_IDS)"; fi

  if ! SERVER_IP="$(detect_public_ip)"; then
    if [ "$DRY" = 1 ]; then SERVER_IP="<server-ip>"; else
      die "$M_D_NO_IP"; fi
  fi

  if [ "$DRY" = 1 ]; then
    token="${token:-<bot-token>}"
    admin="${admin:-<admin-id>}"
  elif [ -z "$token" ] && interactive; then
    ask_input token "$M_P_TOKEN_HELP" "$M_P_TOKEN" "" 1 validate_token
  elif [ -z "$token" ]; then
    die "$M_D_NO_TOKEN"
  else
    validate_token "$token" || die "$M_D_BAD_TOKEN"
  fi
  if [ "$DRY" = 0 ] && [ -z "$admin" ] && interactive; then
    ask_input admin "$M_P_ADMIN_HELP" "$M_P_ADMIN" "" 0 validate_admin
  elif [ "$DRY" = 0 ]; then
    [ -n "$admin" ] || die "$M_D_NO_ADMIN"
    validate_admin "$admin" || die "$M_D_BAD_ADMIN"
  fi
  BOT_TOKEN_ARG="$token"
  ADMIN_ID_ARG="$admin"

  if [ "$existing" = 0 ]; then
    PANEL="$PANEL_ARG"
    if [ -z "$PANEL" ] && interactive; then
      local choice=""
      ask_input choice "$M_P_PANEL_HELP" "$M_P_PANEL" 1 0 validate_panel_choice
      PANEL="$(panel_from_choice "$choice")"
    fi
    PANEL="${PANEL:-xui}"
  else
    # Re-running keeps what is installed; flags can only add components.
    local profiles
    profiles="$(env_get COMPOSE_PROFILES)"
    PANEL="$(env_get NETORYX_PANEL)"
    if [ -z "$PANEL" ]; then
      case ",$profiles," in *,xui,*) PANEL=xui ;; *) PANEL=none ;; esac
    fi
    case ",$profiles," in *,tgproxy,*) WITH_TGPROXY=1 ;; esac
    if [ -n "$(env_get SOCKS_PORT)" ]; then WITH_SOCKS=1; SOCKS_PORT="$(env_get SOCKS_PORT)"; fi
    if [ -n "$(env_get SS_PORT)" ]; then WITH_SS=1; SS_PORT="$(env_get SS_PORT)"; fi
  fi
  if [ "$PANEL" != xui ] && { [ "$WITH_SOCKS" = 1 ] || [ "$WITH_SS" = 1 ]; }; then
    # shellcheck disable=SC2059
    warn "$(printf "$M_W_PANEL_FEATURES" "$(panel_label "$PANEL")")"
    WITH_SOCKS=0
    WITH_SS=0
  fi
  [ "$PANEL" = xui ] && WITH_XUI=1
  [ "$PANEL" != none ] && HAS_PANEL=1
  WITH_SS="${WITH_SS:-0}"
  WITH_XUI="${WITH_XUI:-0}"
  WITH_TGPROXY="${WITH_TGPROXY:-0}"
  WITH_SOCKS="${WITH_SOCKS:-0}"
  printf '%s' "$SOCKS_PORT" | grep -Eq '^[0-9]+$' && [ "$SOCKS_PORT" -ge 1024 ] && [ "$SOCKS_PORT" -le 65535 ] \
    || die "$M_D_SOCKS_PORT"
  printf '%s' "$SS_PORT" | grep -Eq '^[0-9]+$' && [ "$SS_PORT" -ge 1024 ] && [ "$SS_PORT" -le 65535 ] \
    || die "$M_D_SS_PORT"
  valid_domain "$TG_DOMAIN" || die "$M_D_TG_DOMAIN"

  choose_domain "$existing"
  BRAND="$BRAND_ARG"
  if [ -z "$BRAND" ] && [ "$existing" = 1 ]; then BRAND="$(env_get BRAND_NAME)"; fi
  if [ -z "$BRAND" ] && interactive; then
    ask_input BRAND "$M_P_BRAND_HELP" "$M_P_BRAND" "VPN" 0 validate_brand
  fi
  BRAND="${BRAND:-VPN}"
  validate_brand "$BRAND" >/dev/null || die "$M_D_BRAND"
  info "$M_I_DOMAIN: $DOMAIN ($SERVER_IP)"
  info "$M_I_COMPONENTS: $M_C_BOT$([ "$HAS_PANEL" = 1 ] && echo ", $(panel_label "$PANEL") + Reality")$([ "$WITH_SOCKS" = 1 ] && echo ", $(printf "$M_C_SOCKS" "$SOCKS_PORT")")$([ "$WITH_SS" = 1 ] && echo ", $(printf "$M_C_SS" "$SS_PORT")")$([ "$WITH_TGPROXY" = 1 ] && echo ", $M_C_TGPROXY")"
  return 0
}

# --------------------------------------------------------------------------------------------------------------
# Host preparation
# --------------------------------------------------------------------------------------------------------------
install_packages() {
  if [ "$OS_FAMILY" = apt ]; then
    export DEBIAN_FRONTEND=noninteractive
    run apt-get update -y
    run apt-get install -y ca-certificates curl gnupg git openssl iproute2 iptables
    [ "$FIREWALL" = ufw ] && run apt-get install -y ufw
    have qrencode || run apt-get install -y qrencode || warn "$M_W_QRENCODE"
  else
    local missing=()
    for tool in curl git openssl; do have "$tool" || missing+=("$tool"); done
    run dnf install -y ca-certificates iproute iptables-nft
    [ "${#missing[@]}" -gt 0 ] && run dnf install -y "${missing[@]}"
    if [ "$FIREWALL" = firewalld ]; then have firewall-cmd || run dnf install -y firewalld; fi
    if ! have qrencode; then
      run dnf install -y qrencode || { run dnf install -y epel-release && run dnf install -y qrencode; } \
        || warn "$M_W_QRENCODE"
    fi
  fi
}

install_docker() {
  if [ "$DRY" = 0 ] && have docker && docker compose version >/dev/null 2>&1; then
    info "$M_I_DOCKER_PRESENT"
  elif [ "$OS_FAMILY" = apt ]; then
    # Official repository (https://docs.docker.com/engine/install/ubuntu/ and /debian/); the codename comes from
    # os-release, so new releases work as soon as Docker publishes packages for them.
    run install -m 0755 -d /etc/apt/keyrings
    if [ "$DRY" = 1 ]; then
      run curl -fsSL "https://download.docker.com/linux/$DOCKER_REPO/gpg" -o /etc/apt/keyrings/docker.asc
    else
      run curl -fsSL "https://download.docker.com/linux/$DOCKER_REPO/gpg" -o /etc/apt/keyrings/docker.asc
      chmod a+r /etc/apt/keyrings/docker.asc
      printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' \
        "$(dpkg --print-architecture)" "$DOCKER_REPO" "$OS_CODENAME" >/etc/apt/sources.list.d/docker.list
    fi
    info "$(printf "$M_I_DOCKER_REPO" "$DOCKER_REPO" "$OS_CODENAME")"
    run apt-get update -y
    run apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  else
    # Docker documents CentOS Stream 9 / RHEL; AlmaLinux and Rocky Linux 9 are binary compatible and use the
    # CentOS repository.
    run curl -fsSL https://download.docker.com/linux/centos/docker-ce.repo -o /etc/yum.repos.d/docker-ce.repo
    run dnf install -y --allowerasing docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  fi
  run systemctl enable --now docker
}

setup_swap() {
  local ram
  ram="$(mem_total_mb)"
  if [ "$ram" -eq 0 ] || [ "$ram" -ge 4096 ]; then
    info "$(printf "$M_I_SWAP_NOT_NEEDED" "$ram")"
    return 0
  fi
  if [ "$DRY" = 0 ] && [ -n "$(swapon --show --noheadings 2>/dev/null)" ]; then
    info "$M_I_SWAP_PRESENT"
    return 0
  fi
  info "$(printf "$M_I_SWAP_CREATE" "$ram")"
  if [ "$DRY" = 1 ]; then
    run fallocate -l 2G /swapfile
    return 0
  fi
  if [ ! -f /swapfile ]; then
    if ! fallocate -l 2G /swapfile 2>/dev/null; then
      run dd if=/dev/zero of=/swapfile bs=1M count=2048
    fi
    chmod 600 /swapfile
    run mkswap /swapfile
  fi
  if ! swapon /swapfile 2>/dev/null; then
    # some filesystems do not accept a preallocated file: write it out in full
    swapoff /swapfile 2>/dev/null || true
    rm -f /swapfile
    run dd if=/dev/zero of=/swapfile bs=1M count=2048
    chmod 600 /swapfile
    run mkswap /swapfile
    run swapon /swapfile
  fi
  grep -q '^/swapfile ' /etc/fstab || printf '/swapfile none swap sw 0 0\n' >>/etc/fstab
}

# Ports sshd listens on, one per line (never lock the administrator out).
ssh_ports() {
  local ports=""
  if have sshd; then ports="$(sshd -T 2>/dev/null | awk '$1 == "port" { print $2 }' || true)"; fi
  if [ -z "$ports" ]; then
    ports="$(cat /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null \
      | awk 'tolower($1) == "port" { print $2 }' || true)"
  fi
  if [ -n "${SSH_CONNECTION:-}" ]; then ports="$ports"$'\n'"$(printf '%s' "$SSH_CONNECTION" | awk '{ print $4 }')"; fi
  ports="$(printf '%s\n' "$ports" | grep -E '^[0-9]+$' | sort -un || true)"
  printf '%s\n' "${ports:-22}"
}

setup_firewall() {
  if [ "$FIREWALL" = none ]; then
    warn "$(printf "$M_W_FIREWALL_SKIPPED" "$([ "$WITH_SOCKS" = 1 ] && echo ", $SOCKS_PORT/tcp")$([ "$WITH_SS" = 1 ] && echo ", $SS_PORT/tcp+udp")" "$DOCKER_RANGE" "$XUI_PORT")"
    return 0
  fi
  local reality_port ports port
  reality_port="$(env_get REALITY_PORT)"
  reality_port="${reality_port:-$REALITY_PORT_DEFAULT}"
  ports="$(ssh_ports)"
  info "$M_I_SSH_PORTS: $(printf '%s' "$ports" | tr '\n' ' ')"
  if [ "$FIREWALL" = ufw ]; then
    for port in $ports; do run ufw allow "$port/tcp"; done
    run ufw allow 80/tcp
    run ufw allow 443/tcp
    [ "$WITH_SOCKS" = 1 ] && run ufw allow "$SOCKS_PORT/tcp"
    [ "$WITH_SS" = 1 ] && run ufw allow "$SS_PORT"
    if [ "$WITH_XUI" = 1 ]; then
      # containers reach the host-network panel and the Reality backend through the Docker bridge networks
      run ufw allow from "$DOCKER_RANGE" to any port "$XUI_PORT" proto tcp
      run ufw allow from "$DOCKER_RANGE" to any port "$reality_port" proto tcp
    fi
    run ufw default deny incoming
    run ufw default allow outgoing
    run ufw --force enable
    info "$M_I_UFW_NOTE"
    return 0
  fi
  # firewalld: services/ports in the default zone; the Docker networks' source range goes to the trusted zone so
  # containers can reach the host (panel, Reality backend). Packets from that range are matched by source before the
  # interface, so it works whichever zone Docker puts its bridges into.
  if [ "$DRY" = 1 ]; then
    run systemctl enable --now firewalld
    for port in $ports 80 443; do run firewall-cmd --permanent --add-port="$port/tcp"; done
    [ "$WITH_SOCKS" = 1 ] && run firewall-cmd --permanent --add-port="$SOCKS_PORT/tcp"
    [ "$WITH_SS" = 1 ] && run firewall-cmd --permanent --add-port="$SS_PORT/tcp" --add-port="$SS_PORT/udp"
    run firewall-cmd --permanent --zone=trusted --add-source="$DOCKER_RANGE"
    return 0
  fi
  run systemctl enable --now firewalld
  local changed=0
  fw_add_port() {
    firewall-cmd --permanent --query-port="$1/tcp" >/dev/null 2>&1 && return 0
    run firewall-cmd --permanent --add-port="$1/tcp"
    changed=1
  }
  for port in $ports 80 443; do fw_add_port "$port"; done
  [ "$WITH_SOCKS" = 1 ] && fw_add_port "$SOCKS_PORT"
  if [ "$WITH_SS" = 1 ] && ! firewall-cmd --permanent --query-port="$SS_PORT/udp" >/dev/null 2>&1; then
    run firewall-cmd --permanent --add-port="$SS_PORT/udp"
    changed=1
  fi
  [ "$WITH_SS" = 1 ] && fw_add_port "$SS_PORT"
  if ! firewall-cmd --permanent --zone=trusted --query-source="$DOCKER_RANGE" >/dev/null 2>&1; then
    run firewall-cmd --permanent --zone=trusted --add-source="$DOCKER_RANGE"
    changed=1
  fi
  if [ "$changed" = 1 ]; then
    run firewall-cmd --reload
    # firewalld drops Docker's own rules on reload
    run systemctl restart docker
  fi
  info "$(printf "$M_I_FW_TRUSTED" "$DOCKER_RANGE")"
}

selinux_note() {
  if [ "$OS_FAMILY" = dnf ] && have getenforce && [ "$(getenforce 2>/dev/null)" = Enforcing ]; then
    info "$M_I_SELINUX"
  fi
}

# --------------------------------------------------------------------------------------------------------------
# Code and configuration
# --------------------------------------------------------------------------------------------------------------
# Directories of the repository a server never needs.
readonly NOT_ON_SERVER="site docs tests .github"

# Leaves those directories out of the working tree; best effort (older git keeps a full checkout).
sparse_code() {
  [ "$DRY" = 1 ] && return 0
  local patterns=('/*') d
  for d in $NOT_ON_SERVER; do patterns+=("!/$d/"); done
  git -C "$1" sparse-checkout set --no-cone "${patterns[@]}" >>"$LOG_FILE" 2>&1 || true
}

fetch_code() {
  if [ -n "$SOURCE_DIR" ]; then
    [ -d "$SOURCE_DIR" ] || die "$(printf "$M_D_SOURCE" "$SOURCE_DIR")"
    info "$(printf "$M_I_COPYING" "$SOURCE_DIR")"
    run mkdir -p "$INSTALL_DIR"
    if [ "$DRY" = 0 ]; then
      tar -C "$SOURCE_DIR" --exclude=./.git --exclude=./.env --exclude=./.venv --exclude=./data \
        --exclude=./deploy/telemt --exclude=./deploy/haproxy/sni.map '--exclude=./deploy/caddy-sites/*.caddy' \
        --exclude=node_modules --exclude=./web/dist --exclude=__pycache__ \
        --exclude=./site --exclude=./docs --exclude=./tests --exclude=./.github -cf - . \
        | tar -C "$INSTALL_DIR" -xf -
    fi
  elif [ -d "$INSTALL_DIR/.git" ]; then
    info "$(printf "$M_I_CHECKOUT_UPDATE" "$REPO_REF")"
    run git -C "$INSTALL_DIR" fetch --depth 1 origin "$REPO_REF"
    sparse_code "$INSTALL_DIR"
    run git -C "$INSTALL_DIR" reset --hard FETCH_HEAD
  elif [ -e "$INSTALL_DIR/docker-compose.yml" ]; then
    warn "$(printf "$M_W_NOT_GIT" "$INSTALL_DIR")"
  else
    run mkdir -p "$(dirname "$INSTALL_DIR")"
    # Only what the server runs: the website, docs, tests and CI files are never downloaded (partial clone +
    # sparse checkout). A git or remote without these features falls back to a full checkout.
    if [ "$DRY" = 1 ]; then
      run git clone --depth 1 --filter=blob:none --no-checkout --branch "$REPO_REF" "$REPO_URL" "$INSTALL_DIR"
    elif git clone -q --depth 1 --filter=blob:none --no-checkout --branch "$REPO_REF" "$REPO_URL" "$INSTALL_DIR" \
      >>"$LOG_FILE" 2>&1; then
      sparse_code "$INSTALL_DIR"
      git -C "$INSTALL_DIR" checkout -q "$REPO_REF" >>"$LOG_FILE" 2>&1 \
        || run git -C "$INSTALL_DIR" checkout -q -f FETCH_HEAD
    else
      rm -rf "$INSTALL_DIR"
      run git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$INSTALL_DIR"
    fi
  fi
  if [ "$DRY" = 0 ] && [ ! -f "$INSTALL_DIR/docker-compose.yml" ]; then
    die "$(printf "$M_D_NO_COMPOSE" "$INSTALL_DIR")"
  fi
}

write_env() {
  run mkdir -p "$SECRETS_DIR"
  run chmod 700 "$SECRETS_DIR"
  if [ "$DRY" = 1 ]; then
    info "$(printf "$M_I_WOULD_ENV" "$(env_file)")"
    if installable_panel; then
      info "$M_I_PANEL_WORD $PANEL: COMPOSE_FILE=docker-compose.yml:deploy/panels/$PANEL/compose.yml, REALITY_HOST=$(panel_reality_host "$PANEL")"
    fi
    return 0
  fi
  local file profiles=""
  file="$(env_file)"
  if [ ! -f "$file" ]; then
    (umask 077; printf '# Written by the Netoryx installer. Secrets stay on this server.\n' >"$file")
  fi
  chmod 600 "$file"
  env_set BOT_TOKEN "$BOT_TOKEN_ARG"
  env_set ADMIN_IDS "$ADMIN_ID_ARG"
  env_set DOMAIN "$DOMAIN"
  env_set BRAND_NAME "$BRAND"
  [ -n "$(env_get SECRET_KEY)" ] || env_set SECRET_KEY "$(gen_hex 32)"
  [ -n "$(env_get POSTGRES_PASSWORD)" ] || env_set POSTGRES_PASSWORD "$(gen_hex 24)"
  [ -n "$(env_get REALITY_PORT)" ] || env_set REALITY_PORT "$REALITY_PORT_DEFAULT"
  [ "$WITH_XUI" = 1 ] && profiles="xui"
  [ "$WITH_TGPROXY" = 1 ] && profiles="${profiles:+$profiles,}tgproxy"
  # Remnawave's node joins once the panel has produced its key (see configure_panel)
  case ",$(env_get COMPOSE_PROFILES)," in *,panelnode,*) profiles="${profiles:+$profiles,}panelnode" ;; esac
  env_set COMPOSE_PROFILES "$profiles"
  env_set NETORYX_PANEL "$PANEL"
  case "$PANEL" in
    remnawave | marzban | pasarguard | marzneshin)
      env_set COMPOSE_FILE "docker-compose.yml:deploy/panels/$PANEL/compose.yml"
      env_set REALITY_HOST "$(panel_reality_host "$PANEL")"
      ;;
  esac
  [ "$WITH_SOCKS" = 1 ] && env_set SOCKS_PORT "$SOCKS_PORT"
  [ "$WITH_SS" = 1 ] && env_set SS_PORT "$SS_PORT"
  info "$(printf "$M_I_ENV_UPDATED" "$file")"
}

write_edge_files() {
  local map="$INSTALL_DIR/deploy/haproxy/sni.map" marker="# Managed by the installer"
  if [ "$DRY" = 1 ]; then
    info "$M_I_WOULD_WRITE $map: $([ "$HAS_PANEL" = 1 ] && echo "vpn.$DOMAIN -> reality")$([ "$HAS_PANEL$WITH_TGPROXY" = 11 ] && echo ', ')$([ "$WITH_TGPROXY" = 1 ] && echo "$TG_DOMAIN -> tgproxy")"
  elif [ ! -f "$map" ] || grep -q "^$marker" "$map"; then
    {
      printf '%s (TLS server name -> backend); other names go to the web backend.\n' "$marker"
      [ "$HAS_PANEL" = 1 ] && printf 'vpn.%s reality\n' "$DOMAIN"
      [ "$WITH_TGPROXY" = 1 ] && printf '%s tgproxy\n' "$TG_DOMAIN"
      true
    } >"$map"
    info "$M_I_SNI_WRITTEN"
  else
    info "$M_I_SNI_KEPT"
  fi
  [ "$WITH_TGPROXY" = 1 ] || return 0
  write_telemt_config
}

# Reads KEY='value' from a credentials file.
cred_get() {
  [ -f "$1" ] || return 0
  sed -n "s/^$2=//p" "$1" | tail -n 1 | sed "s/^'//; s/'\$//"
}

write_telemt_config() {
  local dir="$INSTALL_DIR/deploy/telemt" cfg creds="$SECRETS_DIR/telemt-credentials"
  cfg="$dir/config.toml"
  if [ "$DRY" = 1 ]; then
    info "$(printf "$M_I_WOULD_TELEMT" "$cfg" "$TG_DOMAIN")"
    return 0
  fi
  mkdir -p "$dir"
  if [ ! -f "$cfg" ]; then
    local token owner keep
    token="Bearer $(gen_hex 24)"
    owner="$(gen_hex 16)"
    keep="$(gen_hex 16)"
    (
      umask 077
      {
        printf "AUTH_HEADER='%s'\n" "$token"
        printf "OWNER_SECRET='%s'\n" "$owner"
        printf "TLS_DOMAIN='%s'\n" "$TG_DOMAIN"
      } >"$creds"
    )
    # the file holds the API token and the users' secrets: never create it readable by others
    (umask 077; cat >"$cfg") <<EOF
[general]
use_middle_proxy = true
log_level = "normal"

[general.modes]
classic = false
secure = false
tls = true

[general.links]
public_host = "$SERVER_IP"
public_port = 443

[server]
port = $TELEMT_INTERNAL_PORT

[server.api]
enabled = true
listen = "0.0.0.0:9091"
whitelist = ["127.0.0.1/32", "$DOCKER_RANGE"]
auth_header = "$token"

[censorship]
tls_domain = "$TG_DOMAIN"
mask = true
tls_emulation = true
tls_front_dir = "tlsfront"

# The bot adds one user per customer through the API. telemt refuses to delete its last user, hence the placeholder.
[access.users]
owner = "$owner"
keep = "$keep"
EOF
    chmod 600 "$cfg"
    info "$(printf "$M_I_TELEMT_WRITTEN" "$creds")"
  else
    info "$M_I_TELEMT_KEPT"
  fi
  chown -R 65532:65532 "$dir"
}

# --------------------------------------------------------------------------------------------------------------
# Panels other than 3x-ui (Remnawave, Marzban, PasarGuard, Marzneshin)
# --------------------------------------------------------------------------------------------------------------
installable_panel() { case "$PANEL" in remnawave | marzban | pasarguard | marzneshin) return 0 ;; *) return 1 ;; esac; }
panel_dir() { printf '%s/deploy/panels/%s' "$INSTALL_DIR" "$PANEL"; }
panel_creds() { printf '%s/%s-credentials' "$SECRETS_DIR" "$PANEL"; }
# at least 24 characters with upper and lower case letters and digits (Remnawave insists on it)
# Satisfies every panel's password rules (PasarGuard: 2 upper, 2 lower, 2 digits, a special character).
strong_password() { printf 'Nx%s-Ay19' "$(gen_hex 16)"; }
# Reads stdin into a file only the owner can read.
write_secret_file() { (umask 077; cat >"$1"); }

write_panel_files() {
  installable_panel || return 0
  local dir creds
  dir="$(panel_dir)"
  creds="$(panel_creds)"
  if [ "$DRY" = 1 ]; then
    info "$(printf "$M_I_WOULD_PANEL_CREDS" "$creds" "$dir")"
    return 0
  fi
  mkdir -p "$dir"
  if [ ! -f "$creds" ]; then
    (
      umask 077
      {
        printf "PANEL_USER='%s'\n" "a$(gen_hex 4)"
        printf "PANEL_PASS='%s'\n" "$(strong_password)"
        printf "DB_PASS='%s'\n" "$(gen_hex 16)"
        printf "APP_SECRET='%s'\n" "$(gen_hex 32)"
        printf "METRICS_PASS='%s'\n" "$(gen_hex 12)"
        printf "WEBHOOK_SECRET='%s'\n" "$(gen_hex 32)"
        printf "NODE_API_KEY='%s'\n" "$(cat /proc/sys/kernel/random/uuid)"
      } >"$creds"
    )
  fi
  local user pass db app_secret metrics webhook node_key
  user="$(cred_get "$creds" PANEL_USER)"
  pass="$(cred_get "$creds" PANEL_PASS)"
  db="$(cred_get "$creds" DB_PASS)"
  app_secret="$(cred_get "$creds" APP_SECRET)"
  metrics="$(cred_get "$creds" METRICS_PASS)"
  webhook="$(cred_get "$creds" WEBHOOK_SECRET)"
  node_key="$(cred_get "$creds" NODE_API_KEY)"
  case "$PANEL" in
    remnawave)
      write_secret_file "$dir/panel.env" <<EOF
APP_PORT=3000
METRICS_PORT=3001
API_INSTANCES=1
DATABASE_URL="postgresql://postgres:$db@remnawave-db:5432/postgres"
REDIS_SOCKET=/var/run/valkey/valkey.sock
APP_SECRET=$app_secret
IS_TELEGRAM_NOTIFICATIONS_ENABLED=false
PANEL_DOMAIN=panel.$DOMAIN
FRONT_END_DOMAIN=*
SUB_PUBLIC_DOMAIN=$DOMAIN/api/sub
METRICS_USER=admin
METRICS_PASS=$metrics
WEBHOOK_ENABLED=false
WEBHOOK_SECRET_HEADER=$webhook
POSTGRES_USER=postgres
POSTGRES_PASSWORD=$db
POSTGRES_DB=postgres
EOF
      # the node key is added after the panel has produced it; the placeholder keeps compose happy until then
      [ -f "$dir/node.env" ] || write_secret_file "$dir/node.env" <<EOF
NODE_PORT=2222
EOF
      ;;
    marzban)
      write_secret_file "$dir/panel.env" <<EOF
UVICORN_HOST=0.0.0.0
UVICORN_PORT=8000
SUDO_USERNAME=$user
SUDO_PASSWORD=$pass
SQLALCHEMY_DATABASE_URL=sqlite:////var/lib/marzban/db.sqlite3
XRAY_JSON=/var/lib/marzban/xray_config.json
XRAY_SUBSCRIPTION_URL_PREFIX=https://$DOMAIN
EOF
      ;;
    pasarguard)
      # Without TLS files the panel only binds a private address; its own container name resolves to one.
      write_secret_file "$dir/panel.env" <<EOF
UVICORN_HOST=pasarguard
UVICORN_PORT=8000
SUDO_USERNAME=$user
SUDO_PASSWORD=$pass
SQLALCHEMY_DATABASE_URL=sqlite+aiosqlite:////var/lib/pasarguard/db.sqlite3
EOF
      # The node does not create its TLS certificate itself (the upstream install script does): make a
      # self-signed one for the name the panel uses to reach it; the panel trusts exactly this certificate.
      if [ ! -s "$dir/certs/ssl_key.pem" ]         || ! openssl x509 -in "$dir/certs/ssl_cert.pem" -noout -text 2>/dev/null | grep -q 'DNS:pg-node.internal'; then
        (umask 077; mkdir -p "$dir/certs"
          openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 \
            -keyout "$dir/certs/ssl_key.pem" -out "$dir/certs/ssl_cert.pem" \
            -subj "/CN=pg-node.internal" -addext "subjectAltName=DNS:pg-node.internal" >>"$LOG_FILE" 2>&1) \
          || die "$M_D_PG_CERT"
        chmod 644 "$dir/certs/ssl_cert.pem"
      fi
      write_secret_file "$dir/node.env" <<EOF
SERVICE_PORT=62050
SERVICE_PROTOCOL=grpc
SSL_CERT_FILE=/etc/pg-node/certs/ssl_cert.pem
SSL_KEY_FILE=/etc/pg-node/certs/ssl_key.pem
API_KEY=$node_key
GENERATED_CONFIG_PATH=/var/lib/pg-node/generated
EOF
      ;;
    marzneshin)
      mkdir -p "$dir/marznode"
      chmod 0755 "$dir/xray-compat.sh"
      write_secret_file "$dir/panel.env" <<EOF
UVICORN_HOST=0.0.0.0
UVICORN_PORT=8000
SUDO_USERNAME=$user
SUDO_PASSWORD=$pass
SQLALCHEMY_DATABASE_URL=sqlite:////var/lib/marzneshin/db.sqlite3
EOF
      ;;
  esac
  info "$(printf "$M_I_PANEL_WRITTEN" "$dir" "$creds")"
}

pg_cert_ready() {
  grep -q 'BEGIN CERTIFICATE' "$(panel_dir)/certs/ssl_cert.pem" 2>/dev/null
}

# PasarGuard accepts no administrator from the environment in production: the owner is created through its setup
# API with a one-time key that only its CLI hands out.
pasarguard_owner() {
  [ -f "$SECRETS_DIR/.pasarguard-owner" ] && return 0
  local creds key="" i body resp code
  creds="$(panel_creds)"
  wait_for "PasarGuard" 40 3 curl -fsS -o /dev/null http://127.0.0.1:8000/ \
    || die "$M_D_PG_DOWN"
  for i in 1 2 3 4 5; do
    key="$(dc exec -T pasarguard python /code/pasarguard-cli.py generate-temp-key 2>>"$LOG_FILE" \
      | sed 's/\x1b\[[0-9;]*m//g' | sed -n 's/.*Temp key:[^A-Za-z0-9_-]*\([A-Za-z0-9_-]\{8,\}\).*/\1/p' | head -n 1)"
    [ -n "$key" ] && break
    sleep 5
  done
  [ -n "$key" ] || die "$M_D_PG_KEY"
  body="$(mktemp)"; resp="$(mktemp)"
  (umask 077; printf '{"key":"%s","username":"%s","password":"%s"}' "$key" \
    "$(cred_get "$creds" PANEL_USER)" "$(cred_get "$creds" PANEL_PASS)" >"$body")
  code="$(curl -sS -o "$resp" -w '%{http_code}' -X POST -H 'Content-Type: application/json' --data @"$body" \
    http://127.0.0.1:8000/api/setup/owner 2>>"$LOG_FILE" || true)"
  rm -f "$body"
  case "$code" in
    201|409) (umask 077; : >"$SECRETS_DIR/.pasarguard-owner"); rm -f "$resp" ;;
    *) cat "$resp" >>"$LOG_FILE"; rm -f "$resp"; die "$(printf "$M_D_PG_OWNER" "$code" "$LOG_FILE")" ;;
  esac
}

# Prints the certificate Marzneshin presents to its nodes (GET /api/nodes/settings), logging in with the
# administrator from the credentials file; the password travels on stdin only.
marzneshin_client_cert() {
  local creds
  creds="$(panel_creds)"
  printf '%s\n%s\n' "$(cred_get "$creds" PANEL_USER)" "$(cred_get "$creds" PANEL_PASS)" \
    | dc exec -T app /app/.venv/bin/python -c '
import json, sys, time, urllib.parse, urllib.request
user, password = sys.stdin.read().splitlines()[:2]
base = "http://marzneshin:8000"
for _ in range(20):
    try:
        form = urllib.parse.urlencode({"username": user, "password": password}).encode()
        token = json.load(urllib.request.urlopen(base + "/api/admins/token", form, timeout=10))["access_token"]
        req = urllib.request.Request(base + "/api/nodes/settings", headers={"Authorization": "Bearer " + token})
        cert = json.load(urllib.request.urlopen(req, timeout=10))["certificate"]
        if "BEGIN CERTIFICATE" in cert:
            print(cert.strip())
            sys.exit(0)
    except Exception:
        pass
    time.sleep(3)
sys.exit(1)
'
}

# Steps that must happen before the application registers the panel.
panel_pre_bootstrap() {
  installable_panel || return 0
  info "$M_I_ADBLOCK_XUI"
  local dir reality_port
  dir="$(panel_dir)"
  reality_port="$(env_get REALITY_PORT)"
  reality_port="${reality_port:-$REALITY_PORT_DEFAULT}"
  case "$PANEL" in
    marzneshin)
      if [ "$DRY" = 1 ]; then
        info "$M_I_WOULD_MZ"
        return 0
      fi
      wait_for "Marzneshin" 40 3 dc exec -T marzneshin true \
        || die "$M_D_MZ_DOWN"
      local i ok=0
      for i in 1 2 3 4 5 6 7 8 9 10; do
        if dc exec -T marzneshin python3 marzneshin-cli.py admin import-from-env -y >>"$LOG_FILE" 2>&1; then
          ok=1
          break
        fi
        sleep 6
      done
      [ "$ok" = 1 ] || die "$M_D_MZ_ADMIN"
      # The panel always talks to its nodes over TLS and the node only trusts the panel's own certificate.
      if [ ! -s "$dir/marznode/client.pem" ]; then
        (umask 077; marzneshin_client_cert >"$dir/marznode/client.pem.new") \
          || die "$M_D_MZ_CERT"
        mv "$dir/marznode/client.pem.new" "$dir/marznode/client.pem"
        run dc restart marznode
      fi
      if [ ! -f "$SECRETS_DIR/.marznode-configured" ]; then
        (umask 077; dc run --rm --no-deps -T app /app/.venv/bin/python -m app reality-config \
          --port "$reality_port" --sni "vpn.$DOMAIN" --dest caddy:8444 >"$dir/marznode/xray_config.json")
        run dc restart marznode
        (umask 077; : >"$SECRETS_DIR/.marznode-configured")
        info "$M_I_MZ_XRAY"
      fi
      ;;
    pasarguard)
      if [ "$DRY" = 1 ]; then
        info "$M_I_WOULD_PG"
        return 0
      fi
      pasarguard_owner
      wait_for "the PasarGuard node certificate" 30 4 pg_cert_ready \
        || warn "$M_W_PG_CERT"
      ;;
  esac
}

# Steps after the application has registered the panel.
panel_post_bootstrap() {
  [ "$PANEL" = remnawave ] || return 0
  local dir
  dir="$(panel_dir)"
  if [ "$DRY" = 1 ]; then
    info "$(printf "$M_I_WOULD_RW" "$dir")"
    return 0
  fi
  local key_file="$TMP_DIR/rw-node-key"
  if ! dc exec -T app cat /tmp/netoryx-rw-node-key >"$key_file" 2>/dev/null || [ ! -s "$key_file" ]; then
    die "$M_D_RW_KEY"
  fi
  write_secret_file "$dir/node.env" <<EOF
NODE_PORT=2222
SECRET_KEY=$(cat "$key_file")
EOF
  dc exec -T app rm -f /tmp/netoryx-rw-node-key || true
  local profiles
  profiles="$(env_get COMPOSE_PROFILES)"
  case ",$profiles," in
    *,panelnode,*) ;;
    *) env_set COMPOSE_PROFILES "${profiles:+$profiles,}panelnode" ;;
  esac
  run dc up -d
  info "$M_I_RW_NODE"
}

# --------------------------------------------------------------------------------------------------------------
# Stack
# --------------------------------------------------------------------------------------------------------------
dc() { (cd "$INSTALL_DIR" && docker compose "$@"); }

app_healthy() {
  dc exec -T app /app/.venv/bin/python -c \
    "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)" >/dev/null 2>&1
}

wait_for() {
  local what="$1" tries="$2" delay="$3"
  shift 3
  local i
  for ((i = 1; i <= tries; i++)); do
    if "$@"; then
      return 0
    fi
    sleep "$delay"
  done
  warn "$(printf "$M_W_NOT_READY" "$what")"
  return 1
}

start_stack() {
  if [ "$DRY" = 1 ]; then
    run docker compose up -d --build
    return 0
  fi
  run dc up -d --build
  info "$M_I_WAIT_APP"
  wait_for "$M_L_APP" 60 5 app_healthy || die "$M_D_APP_DOWN"
  info "$M_I_APP_UP"
}

configure_xui() {
  if [ "$WITH_XUI" != 1 ]; then
    info "$M_I_NOT_REQUESTED"
    return 0
  fi
  local creds="$SECRETS_DIR/xui-credentials" marker="$SECRETS_DIR/.xui-configured"
  if [ "$DRY" = 1 ]; then
    info "$(printf "$M_I_WOULD_XUI" "$creds")"
    return 0
  fi
  if [ ! -f "$creds" ]; then
    (
      umask 077
      {
        printf "XUI_USER='%s'\n" "a$(gen_hex 5)"
        printf "XUI_PASS='%s'\n" "$(gen_hex 12)"
        printf "XUI_BASE='%s'\n" "$(gen_hex 8)"
        printf "XUI_PORT='%s'\n" "$XUI_PORT"
      } >"$creds"
    )
  fi
  local user pass base
  user="$(cred_get "$creds" XUI_USER)"
  pass="$(cred_get "$creds" XUI_PASS)"
  base="$(cred_get "$creds" XUI_BASE)"
  if [ ! -f "$marker" ]; then
    info "$M_I_WAIT_XUI"
    wait_for "3x-ui" 30 3 dc exec -T xui true || die "$M_D_XUI_DOWN"
    # credentials travel through the environment of the exec, not through our own command line
    XUI_U="$user" XUI_P="$pass" XUI_B="$base" run dc exec -T -e XUI_U -e XUI_P -e XUI_B xui \
      sh -c '/app/x-ui setting -username "$XUI_U" -password "$XUI_P" -webBasePath "$XUI_B"'
    run dc restart xui
    (umask 077; : >"$marker")
    info "$M_I_XUI_CHANGED"
  else
    info "$M_I_XUI_APPLIED"
  fi
  wait_for "$M_L_XUI" 30 3 curl -fsS -o /dev/null --max-time 4 "http://127.0.0.1:$XUI_PORT/$base/" || true
  info "$(printf "$M_I_CREDS" "$creds")"
}

bootstrap_servers() {
  if [ "$HAS_PANEL" != 1 ] && [ "$WITH_TGPROXY" != 1 ]; then
    info "$M_I_NO_COMPONENTS"
    return 0
  fi
  local reality_port
  reality_port="$(env_get REALITY_PORT)"
  reality_port="${reality_port:-$REALITY_PORT_DEFAULT}"
  local args=(bootstrap-local --endpoint-address "$SERVER_IP")
  local country=""
  [ "$DRY" = 1 ] || country="$(detect_country "$SERVER_IP" || true)"
  [ -n "$country" ] && args+=(--location-country "$country")
  if [ "$WITH_XUI" = 1 ]; then
    local creds="$SECRETS_DIR/xui-credentials"
    local xui_base xui_user
    xui_base="$(cred_get "$creds" XUI_BASE)"
    xui_user="$(cred_get "$creds" XUI_USER)"
    [ "$DRY" = 1 ] && { xui_base="<generated>"; xui_user="<generated>"; }
    args+=(--xui-url "http://host.docker.internal:$XUI_PORT/$xui_base"
      --xui-user "$xui_user" --xui-pass-file /tmp/netoryx-xui-pass
      --reality-port "$reality_port" --reality-sni "vpn.$DOMAIN" --reality-dest 127.0.0.1:8444)
    [ "$WITH_SOCKS" = 1 ] && args+=(--socks-port "$SOCKS_PORT" --socks-host "$SERVER_IP")
    [ "$WITH_SS" = 1 ] && args+=(--ss-port "$SS_PORT")
  fi
  local pcreds="" puser=""
  if installable_panel; then
    pcreds="$(panel_creds)"
    puser="$(cred_get "$pcreds" PANEL_USER)"
    [ "$DRY" = 1 ] && puser="<generated>"
    args+=(--panel-kind "$PANEL" --panel-url "$(panel_api_url "$PANEL")" --panel-user "$puser"
      --panel-pass-file /tmp/netoryx-panel-pass --panel-node-address "$(panel_reality_host "$PANEL")"
      --reality-port "$reality_port" --reality-sni "vpn.$DOMAIN" --reality-dest caddy:8444)
    [ "$PANEL" = remnawave ] && args+=(--panel-node-secret-out /tmp/netoryx-rw-node-key)
    [ "$PANEL" = pasarguard ] && args+=(--panel-node-cert-file /tmp/netoryx-pg-cert --panel-node-key-file /tmp/netoryx-pg-key)
  fi
  if [ "$WITH_TGPROXY" = 1 ]; then
    args+=(--telemt-url http://telemt:9091 --telemt-token-file /tmp/netoryx-telemt-token --telemt-host "$SERVER_IP")
  fi
  if [ "$DRY" = 1 ]; then
    run docker compose exec -T app /app/.venv/bin/python -m app "${args[@]}"
    return 0
  fi
  TMP_DIR="$(mktemp -d)"
  # secrets reach the container through stdin, never through a command line
  if [ "$WITH_XUI" = 1 ]; then
    cred_get "$SECRETS_DIR/xui-credentials" XUI_PASS >"$TMP_DIR/xui-pass"
    dc exec -T app sh -c 'umask 077; cat > /tmp/netoryx-xui-pass' <"$TMP_DIR/xui-pass"
  fi
  if installable_panel; then
    cred_get "$pcreds" PANEL_PASS >"$TMP_DIR/panel-pass"
    dc exec -T app sh -c 'umask 077; cat > /tmp/netoryx-panel-pass' <"$TMP_DIR/panel-pass"
    if [ "$PANEL" = pasarguard ]; then
      cred_get "$pcreds" NODE_API_KEY >"$TMP_DIR/pg-key"
      dc exec -T app sh -c 'umask 077; cat > /tmp/netoryx-pg-key' <"$TMP_DIR/pg-key"
      if pg_cert_ready; then
        dc exec -T app sh -c 'umask 077; cat > /tmp/netoryx-pg-cert' <"$(panel_dir)/certs/ssl_cert.pem"
      fi
    fi
  fi
  if [ "$WITH_TGPROXY" = 1 ]; then
    cred_get "$SECRETS_DIR/telemt-credentials" AUTH_HEADER >"$TMP_DIR/telemt-token"
    dc exec -T app sh -c 'umask 077; cat > /tmp/netoryx-telemt-token' <"$TMP_DIR/telemt-token"
  fi
  local attempt ok=0
  for attempt in 1 2 3; do
    if dc exec -T app /app/.venv/bin/python -m app "${args[@]}" >"$TMP_DIR/result.json" 2>"$TMP_DIR/error.txt"; then
      ok=1
      break
    fi
    info "$(printf "$M_I_ATTEMPT" "$attempt" "$(tail -n 1 "$TMP_DIR/error.txt")")"
    sleep 10
  done
  dc exec -T app rm -f /tmp/netoryx-xui-pass /tmp/netoryx-telemt-token /tmp/netoryx-panel-pass /tmp/netoryx-pg-key /tmp/netoryx-pg-cert || true
  if [ "$ok" != 1 ]; then
    die "$(printf "$M_D_REGISTER" "$(tail -n 1 "$TMP_DIR/error.txt")")"
  fi
  cat "$TMP_DIR/result.json" >>"$LOG_FILE"
  info "$(printf "$M_I_REGISTERED" "$LOG_FILE")"
  if grep -q '"warnings": \[\]' "$TMP_DIR/result.json"; then :; else
    sed -n '/"warnings"/,/\]/p' "$TMP_DIR/result.json" | sed 's/^/    /'
  fi
}

wait_https() {
  if [ "$DRY" = 1 ]; then
    info "$(printf "$M_I_WOULD_HTTPS" "$DOMAIN")"
    return 0
  fi
  info "$M_I_LE"
  if wait_for "HTTPS on $DOMAIN" 40 6 curl -fsS -o /dev/null --max-time 8 "https://$DOMAIN/healthz"; then
    info "$(printf "$M_I_HTTPS_UP" "$DOMAIN")"
  else
    warn "$(printf "$M_W_HTTPS_DOWN" "$DOMAIN")"
  fi
}

install_helper() {
  run install -m 0755 "$INSTALL_DIR/deploy/netoryx" /usr/local/bin/netoryx
  if [ "$DRY" = 0 ]; then
    helper_defaults >/etc/default/netoryx.new
    if [ -s /etc/default/netoryx.new ]; then mv /etc/default/netoryx.new /etc/default/netoryx; else rm -f /etc/default/netoryx.new; fi
  fi
}

# Contents of /etc/default/netoryx (the helper sources it as root): non-default locations only, shell-quoted.
helper_defaults() {
  if [ "$INSTALL_DIR" != /opt/netoryx ]; then printf 'NETORYX_DIR=%q\n' "$INSTALL_DIR"; fi
  if [ "$SECRETS_DIR" != /root/netoryx ]; then printf 'NETORYX_SECRETS_DIR=%q\n' "$SECRETS_DIR"; fi
  return 0
}

hex_of() { printf '%s' "$1" | od -An -tx1 | tr -d ' \n'; }

show_qr() {
  if have qrencode; then qrencode -t ANSIUTF8 "$1" || true; fi
}

rule() { printf '%s\n' "============================================================"; }

summary() {
  step_ok
  printf '\n'
  rule
  printf '  %s\n' "$M_X_TITLE"
  rule
  if [ "$DRY" = 1 ]; then
    printf '  %s\n' "$M_X_DRY"
    if [ "$HAS_PANEL" = 1 ]; then printf '  %s\n  %s\n' "$M_X_TGONLY" "$M_X_TGONLY_HOWTO"; fi
    return 0
  fi
  local link
  link="$(dc exec -T app /app/.venv/bin/python -m app create-admin-link 2>/dev/null | tail -n 1 || true)"
  printf '\n'
  if [ -n "$BOT_USERNAME" ]; then printf '  %s: https://t.me/%s  (@%s)\n' "$M_X_BOT" "$BOT_USERNAME" "$BOT_USERNAME"; fi
  printf '  %s: https://%s\n' "$M_X_SUB" "$DOMAIN"
  if [ -n "$link" ]; then
    printf '\n  %s:\n    %s\n' "$M_X_ADMIN" "$link"
  else
    printf '\n  %s\n' "$M_X_ADMIN_NONE"
  fi
  if [ "$WITH_XUI" = 1 ]; then
    printf '\n'
    if [ -f "$INSTALL_DIR/deploy/caddy-sites/panel.caddy" ]; then
      # shellcheck disable=SC2059
      printf "  $M_X_PANEL_PUBLISHED\n" "$DOMAIN"
    else
      printf '  %s\n' "$M_X_PANEL_TUNNEL"
      printf '    ssh -L %s:127.0.0.1:%s root@%s\n' "$XUI_PORT" "$XUI_PORT" "$SERVER_IP"
      # shellcheck disable=SC2059
      printf "    $M_X_PANEL_TUNNEL2\n" "$XUI_PORT"
    fi
  elif installable_panel; then
    local ui_port ui_path
    ui_port="$(panel_ui_port "$PANEL")"
    ui_path="$(panel_ui_path "$PANEL")"
    printf '\n'
    # shellcheck disable=SC2059
    printf "  $M_X_PANEL_ADMIN\n" "$(panel_label "$PANEL")" "$(panel_creds)"
    printf '  %s\n' "$M_X_PANEL_UI"
    printf '    ssh -L %s:127.0.0.1:%s root@%s\n' "$ui_port" "$ui_port" "$SERVER_IP"
    # shellcheck disable=SC2059
    printf "    $M_X_PANEL_UI2\n" "$ui_port" "$ui_path"
  fi
  printf '\n  %s: %s/\n  %s\n' "$M_X_CREDS" "$SECRETS_DIR" "$M_X_CREDS_NOTE"
  if [ "$HAS_PANEL" = 1 ]; then
    local tg_link
    tg_link="$(dc exec -T app /app/.venv/bin/python -m app tg-only-link 2>/dev/null | tail -n 1 || true)"
    if printf '%s' "$tg_link" | grep -Eq '^https://'; then
      printf '\n  %s:\n  %s: https://%s/tg\n  %s: %s\n  %s\n' "$M_X_TGONLY" "$M_X_TGONLY_PAGE" "$DOMAIN" "URL" "$tg_link" "$M_X_TGONLY_HOWTO"
      show_qr "$tg_link"
    else
      printf '\n  %s\n' "$M_X_TGONLY_NONE"
    fi
  fi
  if [ "$WITH_TGPROXY" = 1 ]; then
    local creds="$SECRETS_DIR/telemt-credentials" secret tls_hex proxy_link
    secret="$(cred_get "$creds" OWNER_SECRET)"
    tls_hex="$(hex_of "$TG_DOMAIN")"
    proxy_link="tg://proxy?server=$SERVER_IP&port=443&secret=ee$secret$tls_hex"
    printf '\n  %s:\n    %s\n    https://t.me/proxy?server=%s&port=443&secret=ee%s%s\n' "$M_X_TGPROXY" "$proxy_link" \
      "$SERVER_IP" "$secret" "$tls_hex"
    show_qr "$proxy_link"
  fi
  if [ "$WITH_SOCKS" = 1 ]; then
    # shellcheck disable=SC2059
    printf "\n  $M_X_SOCKS\n" "$SOCKS_PORT"
  fi
  printf '\n  %s:\n    %s\n    %s\n    %s\n' "$M_X_NEXT" "$M_X_NEXT1" "$M_X_NEXT2" "$M_X_NEXT3"
  printf '\n  %s:\n' "$M_X_CHEAT"
  printf '    netoryx status | logs [service] | update | backup | admin-link | restart | uninstall\n'
  printf '\n  %s: %s\n' "$M_X_LOG" "$LOG_FILE"
  rule
}

# --------------------------------------------------------------------------------------------------------------
# Update only
# --------------------------------------------------------------------------------------------------------------
config_hash() {
  (cd "$INSTALL_DIR" && cat docker-compose.yml deploy/Caddyfile deploy/haproxy/haproxy.cfg 2>/dev/null | sha256sum | cut -c1-16)
}

do_update() {
  STEPS_TOTAL=4
  [ -f "$(env_file)" ] || die "$(printf "$M_D_NO_INSTALL" "$INSTALL_DIR")"
  step "$M_S_UPD_CHECK"
  if [ "$DRY" = 0 ] && [ "$(id -u)" != 0 ]; then die "$M_D_ROOT (sudo)"; fi
  local before after
  before="$(config_hash)"
  step "$M_S_UPD_CODE"
  fetch_code
  after="$(config_hash)"
  step "$M_S_UPD_BUILD"
  if [ "$DRY" = 1 ]; then
    run docker compose up -d --build --no-deps app
  else
    run dc up -d --build --no-deps app
    wait_for "$M_L_APP" 60 5 app_healthy || die "$M_D_APP_DOWN"
  fi
  step "$M_S_UPD_DONE"
  [ "$DRY" = 1 ] || install -m 0755 "$INSTALL_DIR/deploy/netoryx" /usr/local/bin/netoryx
  if [ "$before" != "$after" ]; then
    say "$M_U_CHANGED1"
    say "$M_U_CHANGED2"
    say "$M_U_CHANGED3  cd $INSTALL_DIR && docker compose up -d"
  else
    say "$M_U_PLAIN"
  fi
}

# --------------------------------------------------------------------------------------------------------------
main() {
  trap 'on_error $LINENO' ERR
  trap cleanup EXIT
  parse_args "$@"
  load_messages
  if [ "$DRY" = 0 ]; then
    mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
    : >>"$LOG_FILE" 2>/dev/null || LOG_FILE="/dev/null"
    # the log holds the output of every install command: readable by root only
    [ "$LOG_FILE" = /dev/null ] || chmod 600 "$LOG_FILE" 2>/dev/null || true
  fi
  say "$M_X_HEADER$([ "$DRY" = 1 ] && echo " ($M_X_DRYRUN)")"
  if [ "$UPDATE_ONLY" = 1 ]; then
    detect_arch
    do_update
    return 0
  fi
  step "$M_S_CHECK"
  preflight
  step "$M_S_SETTINGS"
  collect_settings
  step "$M_S_DOCKER"
  install_packages
  install_docker
  setup_swap
  step "$M_S_CODE"
  fetch_code
  write_env
  write_edge_files
  write_panel_files
  # early, so `netoryx logs|uninstall` are there even if a later step fails
  install_helper
  step "$M_S_FIREWALL"
  setup_firewall
  selinux_note
  step "$M_S_START"
  start_stack
  step "$M_S_VPN"
  configure_xui
  panel_pre_bootstrap
  bootstrap_servers
  panel_post_bootstrap
  step "$M_S_HTTPS"
  wait_https
  step "$M_S_FINISH"
  summary
}

# Run only when executed (also through curl | bash); sourcing the file, as the tests do, just defines the functions.
if [ -z "${BASH_SOURCE[0]:-}" ] || [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
