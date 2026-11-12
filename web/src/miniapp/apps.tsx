import { ArrowUpRight, Download, Link2, Power } from 'lucide-react';
import { appIcon, detectPlatform } from './appIcons';
import { haptic } from './polish';
import type { ClientApp } from './types';
import { openExternal, useSession } from './ui';

export const PLATFORM_NAMES: Record<string, string> = { ios: 'iOS', android: 'Android', windows: 'Windows', macos: 'macOS', linux: 'Linux', tvos: 'Apple TV' };

export function AppIcon({ app }: { app: Pick<ClientApp, 'key' | 'title'> }) {
  const src = appIcon(app.key);
  if (src) return <img className="mi-app-icon" src={src} width={48} height={48} alt="" loading="lazy"/>;
  return <span className="mi-app-icon mi-app-fallback" aria-hidden="true">{(app.title || app.key).slice(0, 1).toUpperCase()}</span>;
}

/** Apps for the device's platform first; the registry already lists recommended apps first. */
export function orderApps(apps: ClientApp[], platform = detectPlatform()): ClientApp[] {
  if (!platform) return apps;
  return [...apps.filter(app => app.platforms.includes(platform)), ...apps.filter(app => !app.platforms.includes(platform))];
}

export function AppList({ apps, active }: { apps: ClientApp[]; active: boolean }) {
  const { t } = useSession();
  const platform = detectPlatform();
  const ordered = orderApps(apps, platform);
  return <div className="mi-app-list">{ordered.map((app, index) => {
    const downloads = [...app.downloads].sort((a, b) => Number(b.platform === platform) - Number(a.platform === platform));
    return <article className={`mi-card mi-app-card ${index === 0 ? 'mi-app-recommended' : ''}`} key={app.key}>
      <header><AppIcon app={app}/><div><strong>{app.title}</strong><small>{app.platforms.map(p => PLATFORM_NAMES[p] || p).join(' · ')}</small></div>{index === 0 && <span className="mi-badge mi-badge-good mi-badge-recommended">{t('Рекомендуем', 'Recommended')}</span>}</header>
      <ol className="mi-steps">
        <li><span className="mi-step-no">1</span><div><b>{t('Скачайте приложение', 'Download the app')}</b><div className="mi-step-actions">{downloads.length ? downloads.map(download => <button key={`${download.platform}-${download.url}`} className="mi-chip-link" onClick={() => { haptic(); openExternal(download.url); }}><Download size={13}/>{download.label}<ArrowUpRight size={11}/></button>) : <small className="mi-muted">{t('Найдите приложение в магазине', 'Find the app in your store')}</small>}</div></div></li>
        <li><span className="mi-step-no">2</span><div><b>{t('Добавьте подписку', 'Add your subscription')}</b><div className="mi-step-actions"><button className="mi-button mi-small" disabled={!active} onClick={() => { haptic(); openExternal(app.open_url); }}><Link2 size={14}/>{t('Подключить', 'Connect')}</button></div></div></li>
        <li><span className="mi-step-no">3</span><div><b>{t('Включите VPN', 'Turn on the VPN')}</b><small className="mi-muted"><Power size={12}/> {t('Откройте приложение и нажмите кнопку подключения', 'Open the app and tap the connect button')}</small></div></li>
      </ol>
    </article>;
  })}</div>;
}
