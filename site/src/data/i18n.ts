import type { Lang } from './site';

type T<V> = Record<Lang, V>;

export const hero: T<{
  tagline: string;
  lines: string[];
  sub: string;
  install: string;
  github: string;
  telegram: string;
  stats: { n: string; label: string }[];
}> = {
  ru: {
    tagline: 'Свой VPN-сервис в Telegram-боте.',
    lines: ['Свой VPN-сервис', 'в Telegram-боте.'],
    stats: [
      { n: '6', label: 'панелей' },
      { n: '28', label: 'способов оплаты' },
      { n: '7', label: 'языков' },
      { n: '1', label: 'команда' },
    ],
    sub: 'На вашем сервере · 6 панелей · 28 способов оплаты · приватный и стабильный доступ',
    install: 'Установить одной командой',
    github: 'GitHub',
    telegram: 'Telegram-канал',
  },
  en: {
    tagline: 'Your own VPN service in a Telegram bot.',
    lines: ['Your own VPN service', 'in a Telegram bot.'],
    stats: [
      { n: '6', label: 'panels' },
      { n: '28', label: 'payment methods' },
      { n: '7', label: 'languages' },
      { n: '1', label: 'command' },
    ],
    sub: 'Self-hosted · 6 panels · 28 payment methods · private, stable access',
    install: 'Install in one command',
    github: 'GitHub',
    telegram: 'Telegram channel',
  },
};

export const install: T<{ title: string; text: string; copy: string; copied: string; note: string; more: string }> = {
  ru: {
    title: 'Установка одной командой',
    text: 'На чистом сервере, от root. Установщик спросит токен бота (из @BotFather) и ваш Telegram ID, поставит Docker и всё остальное, а в конце выдаст одноразовую ссылку в веб-админку.',
    copy: 'Копировать',
    copied: 'Скопировано',
    note: 'Минимум: 2 vCPU, 2 ГБ ОЗУ, 20 ГБ диска, открытые порты 80 и 443.',
    more: 'Подробная инструкция',
  },
  en: {
    title: 'One-command install',
    text: 'On a fresh server, as root. The installer asks for a bot token (from @BotFather) and your Telegram ID, installs Docker and everything else, and prints a one-time link to the web admin at the end.',
    copy: 'Copy',
    copied: 'Copied',
    note: 'Minimum: 2 vCPU, 2 GB RAM, 20 GB disk, ports 80 and 443 open.',
    more: 'Full installation guide',
  },
};

export interface Card {
  title: string;
  text: string;
  link: string;
  label: string;
  external?: boolean;
}

export const paths = (lang: Lang, beginners: boolean): { title: string; cards: Card[] } => {
  const ru = lang === 'ru';
  return {
    title: ru ? 'Выберите свой путь' : 'Choose your path',
    cards: [
      {
        title: ru ? 'Продавать VPN-подписки' : 'Sell VPN subscriptions',
        text: ru
          ? 'Режим магазина: тарифы, пробный период, 28 способов оплаты, реферальная программа, веб-админка с ролями.'
          : 'Shop mode: plans, trials, 28 payment methods, referrals and a web admin with roles.',
        link: 'payments',
        label: ru ? 'Платежи' : 'Payments',
      },
      {
        title: ru ? 'VPN для себя, семьи и друзей' : 'VPN for yourself, family and friends',
        text: ru
          ? 'Личный режим: приглашения и участники, без платежей. Свой сервер и свои правила.'
          : 'Personal mode: invites and members, no payments. Your server, your rules.',
        link: 'faq',
        label: ru ? 'Быстрый старт и FAQ' : 'Quick start and FAQ',
      },
      {
        title: ru ? 'Новичкам: шаг за шагом' : 'Beginners: step by step',
        text: ru
          ? 'Что купить, как подключиться к серверу и что нажимать. Без лишней теории.'
          : 'What to buy, how to connect to the server and what to click. No extra theory.',
        link: beginners ? 'beginners' : 'installation',
        label: ru ? (beginners ? 'Руководство для новичков' : 'Установка') : beginners ? 'Beginners guide' : 'Installation',
      },
      {
        title: ru ? 'Разработчикам' : 'Developers',
        text: ru
          ? 'Исходный код под AGPL-3.0, исходящие вебхуки, публичные API-ключи, правила участия и CLA.'
          : 'AGPL-3.0 source, outgoing webhooks, public API keys, contribution rules and the CLA.',
        link: 'webhooks',
        label: ru ? 'Вебхуки и API' : 'Webhooks and API',
      },
    ],
  };
};

export const features: T<{ title: string; items: { title: string; text: string }[] }> = {
  ru: {
    title: 'Что внутри',
    items: [
      { title: '6 панелей', text: '3x-ui (2.x и 3.x), Remnawave 3.0+, Marzban, Marzneshin, PasarGuard, Hiddify, любое количество одновременно. Импорт существующих пользователей.' },
      { title: '28 способов оплаты', text: 'Stars, 6 криптовалютных, 17 российских шлюзов, Stripe, PayPal, ручной перевод, свой HTTP-шлюз. Зачисление ровно один раз.' },
      { title: '7 языков', text: 'Русский, английский, украинский, казахский, узбекский, персидский, китайский.' },
      { title: 'Mini App и веб-кабинет', text: 'Веб-админка на React с ролями, Telegram Mini App и кабинет клиента в обычном браузере.' },
      { title: 'Доступ к Telegram', text: 'Одна общая ссылка и страница, которой можно делиться: через VPN идёт только Telegram, остальное напрямую, а сервер не пускает этого клиента никуда ещё.' },
      { title: 'Приватность по умолчанию', text: 'IP клиентов не пишутся в логи и базу, HWID хранится только как хеш с ключом, журнал доступа Xray выключен.' },
      { title: 'Подписки и устройства', text: 'Несколько подписок на пользователя, лимит устройств, дополнения, подарки, промокоды, трёхрежимная реферальная программа, автопродление.' },
      { title: 'Одна команда', text: 'Установка, обновление, полная копия сервера и переезд. Ubuntu 22.04/24.04/26.04, Debian 12/13, AlmaLinux и Rocky 9, x86_64 и ARM64.' },
    ],
  },
  en: {
    title: "What's inside",
    items: [
      { title: '6 panels', text: '3x-ui (2.x and 3.x), Remnawave 3.0+, Marzban, Marzneshin, PasarGuard, Hiddify, any number at once. Import of existing users.' },
      { title: '28 payment methods', text: 'Stars, 6 crypto, 17 Russian gateways, Stripe, PayPal, manual transfer, a custom HTTP gateway. Credited exactly once.' },
      { title: '7 languages', text: 'Russian, English, Ukrainian, Kazakh, Uzbek, Persian, Chinese.' },
      { title: 'Mini App and web cabinet', text: 'A React web admin with roles, a Telegram Mini App and a customer cabinet in a plain browser.' },
      { title: 'Telegram access', text: 'One shared link and a page you can share: only Telegram goes through the VPN, the rest is direct, and the server lets that client reach nothing else.' },
      { title: 'Privacy by default', text: 'No customer IPs in logs or the database, HWIDs stored only as a keyed hash, Xray access log off.' },
      { title: 'Subscriptions and devices', text: 'Several subscriptions per user, device limit, add-ons, gifts, promo codes, three-mode referrals, auto-renew.' },
      { title: 'One command', text: 'Install, update, a full server backup and moving to a new server. Ubuntu 22.04/24.04/26.04, Debian 12/13, AlmaLinux and Rocky 9, x86_64 and ARM64.' },
    ],
  },
};

export const hosting: T<{
  title: string;
  text: string;
  req: string[];
  neutral: string;
  guide: string;
}> = {
  ru: {
    title: 'Где арендовать сервер',
    text: 'Для начала хватит небольшого VPS.',
    req: ['Ubuntu 24.04 (лучше всего проверена) или другая поддерживаемая система', '2 vCPU, 2 ГБ ОЗУ, 20 ГБ диска', 'Доступ root и открытые порты 80 и 443'],
    neutral: 'Подойдёт любой провайдер, который даёт такой сервер. Подробности и советы по выбору страны — в руководстве.',
    guide: 'Как выбрать сервер',
  },
  en: {
    title: 'Where to rent a server',
    text: 'A small VPS is enough to start.',
    req: ['Ubuntu 24.04 (best tested) or another supported system', '2 vCPU, 2 GB RAM, 20 GB disk', 'Root access and ports 80 and 443 open'],
    neutral: 'Any provider that offers such a server will do. See the guide for details and tips on choosing a country.',
    guide: 'How to choose a server',
  },
};

export const footer: T<{ license: string; legal: string; legalUrl: string; coc: string; telegram: string; text: string }> = {
  ru: {
    license: 'Лицензия AGPL-3.0',
    legal: 'Правовая информация',
    legalUrl: 'https://github.com/qklafk/netoryx/blob/main/LEGAL.ru.md',
    coc: 'Кодекс поведения',
    telegram: 'Telegram-канал',
    text: 'Netoryx — свободное программное обеспечение.',
  },
  en: {
    license: 'AGPL-3.0 license',
    legal: 'Legal notice',
    legalUrl: 'https://github.com/qklafk/netoryx/blob/main/LEGAL.md',
    coc: 'Code of Conduct',
    telegram: 'Telegram channel',
    text: 'Netoryx is free software.',
  },
};

export const labels: T<Record<'install' | 'how' | 'paths' | 'features' | 'panels' | 'hosting' | 'faq' | 'docs' | 'start', string>> = {
  ru: { install: 'Установка', how: 'Как это работает', paths: 'Выберите путь', features: 'Возможности', panels: 'Панели', hosting: 'Сервер', faq: 'Вопросы', docs: 'Документация', start: 'Установить' },
  en: { install: 'Install', how: 'How it works', paths: 'Choose your path', features: 'Features', panels: 'Panels', hosting: 'Server', faq: 'FAQ', docs: 'Docs', start: 'Install' },
};

export const how: T<{ title: string; steps: { title: string; text: string }[] }> = {
  ru: {
    title: 'От чистого сервера до работающего сервиса',
    steps: [
      { title: 'Арендуйте сервер', text: 'Небольшого VPS хватит: 2 vCPU, 2 ГБ ОЗУ, Ubuntu 24.04 или другая поддерживаемая система.' },
      { title: 'Одна команда', text: 'Установщик спрашивает токен бота и ваш Telegram ID, ставит Docker, панель, базу и HTTPS.' },
      { title: 'Бот готов', text: 'Тарифы, платежи, тексты и меню настраиваются в веб-админке. Ссылка на вход приходит в конце установки.' },
      { title: 'Клиенты подключаются', text: 'Клиент открывает бота, берёт пробный период или тариф и подключает подписку в Happ, INCY, v2RayTun или Hiddify.' },
    ],
  },
  en: {
    title: 'From a fresh server to a working service',
    steps: [
      { title: 'Rent a server', text: 'A small VPS is enough: 2 vCPU, 2 GB RAM, Ubuntu 24.04 or another supported system.' },
      { title: 'One command', text: 'The installer asks for a bot token and your Telegram ID, then sets up Docker, the panel, the database and HTTPS.' },
      { title: 'Your bot is ready', text: 'Plans, payments, texts and menus are configured in the web admin. The sign-in link arrives at the end of the install.' },
      { title: 'Customers connect', text: 'A customer opens the bot, takes a trial or a plan and imports the subscription into Happ, INCY, v2RayTun or Hiddify.' },
    ],
  },
};

export const panelsRow: T<{ title: string; text: string; more: string }> = {
  ru: { title: 'Поддерживаемые панели', text: 'Любое количество панелей одновременно. Существующих пользователей можно импортировать.', more: 'О панелях' },
  en: { title: 'Supported panels', text: 'Any number of panels at once. Existing users can be imported.', more: 'About panels' },
};

export const faq: T<{ title: string; more: string; items: { q: string; a: string }[] }> = {
  ru: {
    title: 'Частые вопросы',
    more: 'Все вопросы и ответы',
    items: [
      { q: 'Что нужно для установки?', a: 'Linux-сервер (Ubuntu 22.04/24.04/26.04, Debian 12/13, AlmaLinux 9 или Rocky Linux 9; 2 vCPU и 2 ГБ ОЗУ или больше), доступ root и токен бота от @BotFather.' },
      { q: 'Нужен ли домен?', a: 'Нет. Без домена установщик использует адрес вида <ip>.sslip.io с настоящим HTTPS-сертификатом. Свой домен выглядит солиднее и переживает переезд сервера.' },
      { q: 'Как клиенты подключаются?', a: 'Открывают бота, берут пробный период или тариф и нажимают кнопку подключения: ссылка откроется в Happ, INCY, v2RayTun или Hiddify и импортирует подписку.' },
      { q: 'Отключит ли клиентов обновление?', a: 'Нет. «netoryx update» пересобирает только контейнер приложения. Перезапуск edge, caddy или панели соединения разорвёт.' },
      { q: 'Что записывается о трафике клиентов?', a: 'Никаких посещённых сайтов и содержимого, а по умолчанию и IP-адресов. Подробности на странице о приватности.' },
      { q: 'Какую панель выбрать?', a: 'По умолчанию 3x-ui: на ней работает всё. Remnawave, Marzban, PasarGuard и Marzneshin тоже ставятся одной командой и проверены на живом сервере.' },
      { q: 'Что такое «Доступ к Telegram»?', a: 'Одна общая ссылка и страница /tg: через VPN идёт только Telegram, остальное напрямую. Её можно публиковать — сервер не пускает этого клиента никуда, кроме Telegram.' },
      { q: 'Как переехать на другой сервер?', a: '«netoryx backup --full» сохраняет весь сервер одним архивом, «netoryx restore» возвращает его на новом сервере с той же панелью. Ссылки клиентов продолжают работать.' },
      { q: 'Можно ли продавать подписки?', a: 'Да. Лицензия AGPL-3.0-or-later разрешает коммерческое использование, отчислений и комиссий нет. За соблюдение законов отвечает тот, кто запускает сервис.' },
    ],
  },
  en: {
    title: 'Frequently asked questions',
    more: 'All questions and answers',
    items: [
      { q: 'What do I need to install it?', a: 'A Linux server (Ubuntu 22.04/24.04/26.04, Debian 12/13, AlmaLinux 9 or Rocky Linux 9; 2 vCPU and 2 GB RAM or more), root access and a bot token from @BotFather.' },
      { q: 'Do I need a domain?', a: 'No. Without one the installer uses an <ip>.sslip.io address with a real HTTPS certificate. A domain of your own looks better to customers and survives a server move.' },
      { q: 'How do customers connect?', a: 'They open the bot, start a trial or buy a plan and tap the connect button: the link opens in Happ, INCY, v2RayTun or Hiddify and imports the subscription.' },
      { q: 'Does updating disconnect customers?', a: '"netoryx update" rebuilds only the application container, so no. Restarting edge, caddy or the panel does.' },
      { q: "Is anything logged about customers' traffic?", a: 'No visited sites, no content and, by default, no IP addresses. See the privacy page for details.' },
      { q: 'Which panel should I pick?', a: '3x-ui by default: everything works on it. Remnawave, Marzban, PasarGuard and Marzneshin are also installed by one command and were tested on a live server.' },
      { q: 'What is Telegram access?', a: 'One shared link and a /tg page: only Telegram goes through the VPN, the rest is direct. It is safe to publish: the server lets that client reach nothing but Telegram.' },
      { q: 'How do I move to another server?', a: '"netoryx backup --full" saves the whole server in one archive and "netoryx restore" puts it back on a new server with the same panel. Links of existing customers keep working.' },
      { q: 'Can I sell subscriptions?', a: 'Yes. The AGPL-3.0-or-later license allows commercial use. There are no royalties or fees. Whoever runs the service is responsible for complying with the law.' },
    ],
  },
};

export const cta: T<{ title: string; text: string; install: string; github: string }> = {
  ru: { title: 'Запустите свой сервис сегодня', text: 'Один сервер, одна команда, свой бот и своя админка.', install: 'Установить', github: 'Код на GitHub' },
  en: { title: 'Launch your own service today', text: 'One server, one command, your own bot and your own admin.', install: 'Install', github: 'Source on GitHub' },
};
