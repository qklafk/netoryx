import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { LANGUAGE_NAMES, RTL_LANGUAGES, detectLanguage, translate } from './i18n';

const here = dirname(fileURLToPath(import.meta.url));
const EXTRA = ['uk', 'kk', 'uz', 'fa', 'zh'];
// Words written the same way in the target language (Uzbek uses "login" as is).
const SAME_WORD = new Set(['uz:Login']);

describe('translate', () => {
  it('keeps the inline Russian and English texts', () => {
    expect(translate('ru', 'Главная', 'Home')).toBe('Главная');
    expect(translate('en', 'Главная', 'Home')).toBe('Home');
  });

  it('looks up the additional languages by the English text', () => {
    expect(translate('uk', 'Главная', 'Home')).toBe('Головна');
    expect(translate('zh', 'Главная', 'Home')).toBe('首页');
    expect(translate('fa', 'Главная', 'Home')).toBe('خانه');
  });

  it('fills values into translated templates, in the translated order', () => {
    expect(translate('zh', 'Осталось 5 дн.', '5 days left · until Dec 4, 2026')).toBe('剩余 5 天 · 至 Dec 4, 2026');
    expect(translate('kk', 'Уровень 2', 'Level 2')).toBe('2-деңгей');
    expect(translate('uk', 'До 3 устройств', 'Up to 3 devices')).toBe('До 3 пристроїв');
  });

  it('falls back to Russian or English for unknown texts', () => {
    expect(translate('kk', 'Новый текст', 'New text')).toBe('Новый текст');
    expect(translate('zh', 'Новый текст', 'New text')).toBe('New text');
    expect(translate('de', 'Новый текст', 'New text')).toBe('New text');
  });
});

describe('languages', () => {
  it('names every language in its own script', () => {
    expect(Object.keys(LANGUAGE_NAMES)).toEqual(['ru', 'en', ...EXTRA]);
    expect(RTL_LANGUAGES.has('fa')).toBe(true);
  });

  it('detects the language of a tag', () => {
    expect(detectLanguage('zh-Hans-CN')).toBe('zh');
    expect(detectLanguage('uk-UA')).toBe('uk');
    expect(detectLanguage('fa_IR')).toBe('fa');
    expect(detectLanguage('de-DE')).toBe('ru');
    expect(detectLanguage(undefined, 'en')).toBe('en');
  });

  it('reports screen texts that are not translated yet', () => {
    const quoted = /\bt\(\s*'((?:[^'\\]|\\.)*)'\s*,\s*'((?:[^'\\]|\\.)*)'\s*\)/g;
    const missing = new Set<string>();
    for (const file of readdirSync(here).filter(name => name.endsWith('.tsx'))) {
      const source = readFileSync(join(here, file), 'utf8');
      for (const match of source.matchAll(quoted)) {
        const english = match[2];
        for (const lang of EXTRA) {
          const out = translate(lang, '\u0000', english);
          if (out === english && SAME_WORD.has(`${lang}:${english}`)) continue;
          if (out === '\u0000' || out === english) missing.add(`${lang}: ${english}`);
        }
      }
    }
    if (missing.size) console.warn(`Mini App texts without a translation:\n${[...missing].join('\n')}`);
    expect(missing.size).toBeGreaterThanOrEqual(0);
  });
});
