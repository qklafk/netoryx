import fs from 'node:fs';
import path from 'node:path';

export const GITHUB = 'https://github.com/qklafk/netoryx';
export const TELEGRAM = 'https://t.me/netoryx';
export const INSTALL_COMMAND =
  'curl -fsSL https://raw.githubusercontent.com/qklafk/netoryx/main/install.sh | sudo bash';

export type Lang = 'ru' | 'en';

export const BASE = (import.meta.env.BASE_URL ?? '/').replace(/\/+$/, '');

/** Site URL of a docs page. */
export const docUrl = (lang: Lang, slug: string): string => `${BASE}${lang === 'en' ? '/en' : ''}/${slug}/`;

/** The beginners guide is linked only once it exists in docs/. */
export const hasBeginners = fs.existsSync(path.resolve(process.cwd(), '..', 'docs', 'beginners.md'));

export const langOf = (locale: string | undefined): Lang => (locale === 'en' ? 'en' : 'ru');
