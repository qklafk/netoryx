// Builds the Starlight content collection from the repository docs.
//   docs/*.md          -> English pages  (src/content/docs/en/<slug>.md)
//   docs/ru/*.md       -> Russian pages  (src/content/docs/<slug>.md)
//   site/extra-docs/   -> site-only pages (landing, hosting guide), same layout
// Relative links between docs and image paths are rewritten for the site. Pages that exist only
// in English are copied into the Russian tree with a short notice, so every Russian page resolves.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const siteDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const repoDir = path.resolve(siteDir, '..');
const docsDir = path.join(repoDir, 'docs');
const outDir = path.join(siteDir, 'src', 'content', 'docs');
const base = (process.env.BASE_PATH ?? '/netoryx').replace(/\/+$/, '');
const github = 'https://github.com/qklafk/netoryx';

const RU_NOTICE = ':::note\nЭта страница пока доступна только на английском языке.\n:::\n\n';

let errors = 0;
const fail = (msg) => {
  errors += 1;
  console.error(`sync-docs: ${msg}`);
};

const listMd = (dir) =>
  fs.existsSync(dir)
    ? fs
        .readdirSync(dir, { withFileTypes: true })
        .filter((e) => e.isFile() && /\.mdx?$/.test(e.name))
        .map((e) => e.name)
    : [];

/** Documents grouped by language: { slug: {file, ext} }. */
function collect() {
  const result = { en: {}, ru: {} };
  const add = (lang, dir, prefix) => {
    for (const name of listMd(dir)) {
      const slug = prefix + name.replace(/\.mdx?$/, '');
      result[lang][slug] = { file: path.join(dir, name), ext: path.extname(name) };
    }
  };
  add('en', docsDir, '');
  add('en', path.join(docsDir, 'legal'), 'legal/');
  add('ru', path.join(docsDir, 'ru'), '');
  add('ru', path.join(docsDir, 'ru', 'legal'), 'legal/');
  for (const lang of ['en', 'ru']) {
    const extra = path.join(siteDir, 'extra-docs', lang);
    add(lang, extra, '');
  }
  return result;
}

const docs = collect();

/** Language-neutral slug of a markdown file inside docs/, or null. */
const slugOfDocPath = (abs) => {
  let rel = path.relative(docsDir, abs).split(path.sep).join('/');
  if (rel.startsWith('..')) return null;
  rel = rel.replace(/^ru\//, '');
  return /\.mdx?$/.test(rel) ? rel.replace(/\.mdx?$/, '') : null;
};

const pageUrl = (lang, slug, hash = '') => `${base}${lang === 'en' ? '/en' : ''}/${slug}/${hash}`;

function rewriteTarget(target, srcFile, lang, isExtra) {
  if (/^([a-z][a-z0-9+.-]*:|\/\/|#)/i.test(target)) return target;
  const [rawPath, ...rest] = target.split('#');
  const hash = rest.length ? `#${rest.join('#')}` : '';
  if (isExtra && rawPath.startsWith('/')) return pageUrl(lang, rawPath.replace(/^\/|\/$/g, ''), hash);
  if (!rawPath) return target;
  const abs = path.resolve(path.dirname(srcFile), rawPath);
  const relRepo = path.relative(repoDir, abs).split(path.sep).join('/');
  if (relRepo.startsWith('docs/images/')) return `${base}/images/${relRepo.slice('docs/images/'.length)}`;
  const trimmed = abs.replace(/[\\/]+$/, '');
  if (
    fs.existsSync(trimmed) &&
    fs.statSync(trimmed).isDirectory() &&
    path.relative(docsDir, trimmed).replace(/^ru[\\/]?/, '') === 'legal'
  ) {
    return pageUrl(lang, 'legal/privacy-policy-template', hash);
  }
  const slug = slugOfDocPath(abs);
  if (slug && (docs.en[slug] || docs.ru[slug])) return pageUrl(lang, slug, hash);
  if (fs.existsSync(abs)) return `${github}/blob/main/${relRepo}${hash}`;
  fail(`${path.relative(repoDir, srcFile)}: broken link "${target}"`);
  return target;
}

function rewriteLinks(text, srcFile, lang, isExtra) {
  let fenced = false;
  return text
    .split('\n')
    .map((line) => {
      if (/^\s*(```|~~~)/.test(line)) {
        fenced = !fenced;
        return line;
      }
      if (fenced) return line;
      return line.replace(
        /(\]\()([^)\s]+)((?:\s+"[^"]*")?\))/g,
        (_m, a, t, c) => a + rewriteTarget(t, srcFile, lang, isExtra) + c,
      );
    })
    .join('\n');
}

function build(entry, lang, notice) {
  const isExtra = entry.file.startsWith(path.join(siteDir, 'extra-docs'));
  const text = fs.readFileSync(entry.file, 'utf8').replace(/\r\n/g, '\n');
  if (isExtra) {
    // hand-written pages carry their own frontmatter; only links need rewriting
    return rewriteLinks(text, entry.file, lang, true);
  }
  const lines = text.split('\n');
  const idx = lines.findIndex((l) => /^# /.test(l));
  if (idx < 0) {
    fail(`${path.relative(repoDir, entry.file)}: no H1 title`);
    return text;
  }
  const title = lines[idx].replace(/^# /, '').trim();
  lines.splice(idx, 1);
  const body = rewriteLinks(lines.join('\n').replace(/^\n+/, ''), entry.file, lang, false);
  return `---\ntitle: ${JSON.stringify(title)}\n---\n\n${notice}${body}`;
}

function write(target, content) {
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, content);
}

fs.rmSync(outDir, { recursive: true, force: true });
let count = 0;
const slugs = new Set([...Object.keys(docs.en), ...Object.keys(docs.ru)]);
for (const slug of slugs) {
  for (const lang of ['ru', 'en']) {
    let entry = docs[lang][slug];
    let notice = '';
    if (!entry && lang === 'ru') {
      // The default locale has to hold every page: reuse the English text.
      entry = docs.en[slug];
      notice = RU_NOTICE;
    }
    if (!entry) continue; // English falls back to the default locale on its own
    const target = path.join(outDir, lang === 'en' ? 'en' : '', `${slug}${entry.ext === '.mdx' ? '.mdx' : '.md'}`);
    write(target, build(entry, lang, notice));
    count += 1;
  }
}

// Static assets: images referenced by docs and the site logo.
const publicImages = path.join(siteDir, 'public', 'images');
const assets = path.join(siteDir, 'src', 'assets');
fs.rmSync(publicImages, { recursive: true, force: true });
fs.mkdirSync(publicImages, { recursive: true });
fs.mkdirSync(assets, { recursive: true });
for (const f of fs.readdirSync(path.join(docsDir, 'images'))) {
  fs.copyFileSync(path.join(docsDir, 'images', f), path.join(publicImages, f));
  fs.copyFileSync(path.join(docsDir, 'images', f), path.join(assets, f));
}

if (errors) {
  console.error(`sync-docs: ${errors} problem(s)`);
  process.exit(1);
}
console.log(`sync-docs: ${count} pages written`);
