// Hosting providers listed on the "Choosing a VPS" page (alphabetical, identical styling).
//
// Fields:
//   name         Display name of the provider.
//   url          Link to the provider (all of them carry the project's referral code, see the disclosure).
//   icon         Square icon in public/partners/ (shown in greyscale, in colour on hover).
//   description  One neutral, factual line per language.

export interface Provider {
  name: string;
  url: string;
  icon: string;
  description: { ru: string; en: string };
}

export const providers: Provider[] = [
  {
    name: 'FirstVDS',
    url: 'https://firstvds.ru/?from=1273239',
    icon: 'partners/firstvds.png',
    description: { ru: 'VPS и выделенные серверы.', en: 'VPS and dedicated servers.' },
  },
  {
    name: 'Hostinger',
    url: 'https://www.hostinger.com?REFERRALCODE=K1UGGAHKY0ZC',
    icon: 'partners/hostinger.png',
    description: { ru: 'VPS и хостинг сайтов.', en: 'VPS and website hosting.' },
  },
  {
    name: 'One Dash',
    url: 'https://rdp-onedash.ru/r/b434b',
    icon: 'partners/onedash.png',
    description: { ru: 'VPS и VDS серверы.', en: 'VPS and VDS servers.' },
  },
  {
    name: 'RUVDS',
    url: 'https://ruvds.com/pr173048',
    icon: 'partners/ruvds.svg',
    description: { ru: 'VPS, дата-центры в России и за рубежом.', en: 'VPS, data centers in Russia and abroad.' },
  },
  {
    name: 'Selectel',
    url: 'https://selectel.ru/?ref_code=b04a2bb7da',
    icon: 'partners/selectel.png',
    description: { ru: 'Облачные и выделенные серверы.', en: 'Cloud and dedicated servers.' },
  },
  {
    name: 'Timeweb Cloud',
    url: 'https://timeweb.com/ru/?i=150867&a=373',
    icon: 'partners/timeweb.png',
    description: { ru: 'Облачные серверы.', en: 'Cloud servers.' },
  },
  {
    name: 'VDSina',
    url: 'https://vdsina.ru/?partner=huh82jvd73sa',
    icon: 'partners/vdsina.png',
    description: { ru: 'VPS и выделенные серверы.', en: 'VPS and dedicated servers.' },
  },
  {
    name: 'Vultr',
    url: 'https://www.vultr.com/?ref=9935207',
    icon: 'partners/vultr.png',
    description: { ru: 'Облачные серверы, дата-центры в разных странах.', en: 'Cloud servers, data centers in many countries.' },
  },
].sort((a, b) => a.name.localeCompare(b.name, 'en', { sensitivity: 'base' }));
