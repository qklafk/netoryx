import fs from 'node:fs';
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import starlightLinksValidator from 'starlight-links-validator';

// Project page by default; set SITE_URL (and BASE_PATH="/" or "") for a custom domain.
const site = process.env.SITE_URL || 'https://qklafk.github.io';
const rawBase = process.env.BASE_PATH ?? '/netoryx';
const base = rawBase === '' || rawBase === '/' ? '/' : `/${rawBase.replace(/^\/+|\/+$/g, '')}`;

const hasBeginners = fs.existsSync(new URL('../docs/beginners.md', import.meta.url));

const item = (slug, ru, en) => ({ slug, translations: { en } , label: ru });

const sidebar = [
  {
    label: 'Начало работы',
    translations: { en: 'Getting started' },
    items: [
      item('installation', 'Установка', 'Installation'),
      ...(hasBeginners ? [item('beginners', 'Для новичков', 'For beginners')] : []),
      item('choosing-a-vps', 'Где арендовать сервер', 'Choosing a VPS'),
      item('configuration', 'Настройка', 'Configuration'),
      item('faq', 'FAQ', 'FAQ'),
      item('troubleshooting', 'Решение проблем', 'Troubleshooting'),
    ],
  },
  {
    label: 'Эксплуатация',
    translations: { en: 'Operating' },
    items: [
      item('upgrading', 'Обновление', 'Upgrading'),
      item('backups', 'Резервные копии', 'Backups'),
      item('privacy', 'Приватность', 'Privacy'),
    ],
  },
  {
    label: 'Интеграции',
    translations: { en: 'Integrations' },
    items: [
      item('panels', 'Панели', 'Panels'),
      item('payments', 'Платежи', 'Payments'),
      item('webhooks', 'Исходящие вебхуки', 'Outgoing webhooks'),
    ],
  },
  {
    label: 'Для клиентов',
    translations: { en: 'For customers' },
    items: [
      item('telegram-proxy', 'Прокси для Telegram', 'Telegram proxies'),
      item('web-cabinet', 'Веб-кабинет', 'Web cabinet'),
      item('npd-receipts', 'Чеки «Мой налог»', '"Мой налог" receipts'),
    ],
  },
  {
    label: 'Юридические шаблоны',
    translations: { en: 'Legal templates' },
    items: [
      item('legal/privacy-policy-template', 'Политика конфиденциальности', 'Privacy policy template'),
      item('legal/terms-of-service-template', 'Пользовательское соглашение', 'Terms of service template'),
    ],
  },
];

export default defineConfig({
  site,
  base,
  integrations: [
    starlight({
      title: 'Netoryx',
      description:
        'Self-hosted Telegram bot, web admin and Mini App for selling or sharing VPN access.',
      logo: {
        light: './src/assets/mark-on-light.png',
        dark: './src/assets/mark-on-dark.png',
        alt: 'Netoryx',
      },
      favicon: '/favicon.svg',
      defaultLocale: 'root',
      locales: {
        root: { label: 'Русский', lang: 'ru' },
        en: { label: 'English', lang: 'en' },
      },
      social: [
        { icon: 'github', label: 'GitHub', href: 'https://github.com/qklafk/netoryx' },
        { icon: 'telegram', label: 'Telegram', href: 'https://t.me/netoryx' },
      ],
      sidebar,
      customCss: ['./src/styles/custom.css'],
      components: {
        Footer: './src/components/Footer.astro',
        Hero: './src/components/Hero.astro',
        SocialIcons: './src/components/SocialIcons.astro',
      },
      plugins: [starlightLinksValidator({ errorOnRelativeLinks: false })],
    }),
  ],
});
