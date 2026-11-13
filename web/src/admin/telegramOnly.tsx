import { Copy, RefreshCw, RotateCw } from 'lucide-react';
import { copyText, useApi } from '../shared/api';
import { ActionButton, Badge, PageTitle, Panel, Resource, useAdmin } from './ui';

type ServerRow = { panel_id: number; title: string; country: string | null; ready: boolean; error: string | null };
type Skipped = { panel_id: number; name: string; reason: string; country: string | null };
type View = { enabled: boolean; url: string; page: string; qr_png_base64: string; ready: boolean; servers: ServerRow[]; skipped: Skipped[]; excluded_countries: string[] };

const BASE = '/tg_only';

export function TelegramOnlyPage() {
  const { tr, flash } = useAdmin();
  const r = useApi<View>(BASE);
  const view = r.data;
  const reason = (s: Skipped) => s.reason === 'country' ? tr(`страна ${s.country} в списке исключений`, `country ${s.country} is on the exclusion list`)
    : s.reason === 'unknown_country' ? tr('у локации нет флага страны', 'the location has no country flag')
    : s.reason === 'no_inbound' ? tr('нет подходящего VLESS-инбаунда (Reality или TLS)', 'no suitable VLESS inbound (Reality or TLS)')
    : tr('нет активных подключений', 'no active endpoints');
  const copy = async (text: string) => { await copyText(text); flash(tr('Скопировано', 'Copied')); };
  return <>
    <PageTitle eyebrow={tr('ИНФРАСТРУКТУРА', 'INFRASTRUCTURE')} title={tr('Доступ к Telegram', 'Telegram access')} subtitle={tr('Одна общая ссылка для всех: через VPN идёт только Telegram, остальное работает напрямую. Её можно публиковать и пересылать, чтобы людям было как открыть Telegram (и вашего бота). Сервер не пускает этого клиента никуда, кроме Telegram.', 'One shared link for everyone: only Telegram goes through the VPN, everything else connects directly. You can publish and forward it so people can open Telegram (and your bot). The server lets that client reach nothing but Telegram.')}>
      <ActionButton path={`${BASE}/sync`} onDone={() => { flash(tr('Серверы обновлены', 'Servers updated')); r.reload(); }}><RefreshCw size={14}/>{tr('Обновить серверы', 'Refresh servers')}</ActionButton>
    </PageTitle>
    <Resource resource={r}>
      {view && !view.enabled && <Panel><p>{tr('Функция выключена (настройка TG_ONLY_ENABLED).', 'The feature is off (setting TG_ONLY_ENABLED).')}</p></Panel>}
      {view && view.enabled && <>
        <Panel title={tr('Страница для людей', 'Page for people')}>
          <p className="dialog-description">{tr('Делитесь этим адресом: он не меняется, а ссылка подписки на странице всегда актуальна. Страница подскажет, как поставить Happ и добавить подключение.', 'Share this address: it never changes, and the subscription link on the page is always current. The page explains how to install Happ and add the connection.')}</p>
          <div className="field full"><code style={{ wordBreak: 'break-all' }}>{view.page}</code></div>
          <div className="toolbar"><button className="btn secondary" onClick={() => copy(view.page)}><Copy size={15}/>{tr('Копировать', 'Copy')}</button></div>
        </Panel>
        <Panel title={tr('Ссылка подписки', 'Subscription link')}>
          <p className="dialog-description">{tr('Для Happ и v2RayTun («Добавить подписку по ссылке») или по QR-коду. Если ссылка разошлась слишком широко, выпустите новую: старая и старые ключи перестанут работать, а адрес страницы останется прежним.', 'For Happ and v2RayTun ("Add subscription by URL") or by QR code. If the link spreads too far, issue a new one: the old link and keys stop working while the page address stays the same.')}</p>
          <div className="field full"><code style={{ wordBreak: 'break-all' }}>{view.url}</code></div>
          {view.qr_png_base64 && <img alt="QR" width={200} height={200} style={{ background: '#fff', padding: 8, borderRadius: 8 }} src={`data:image/png;base64,${view.qr_png_base64}`}/>}
          <div className="toolbar">
            <button className="btn secondary" onClick={() => copy(view.url)}><Copy size={15}/>{tr('Копировать', 'Copy')}</button>
            <ActionButton danger path={`${BASE}/rotate`} confirm={tr('Выпустить новую ссылку? Старая ссылка подписки и старые ключи подключения перестанут работать.', 'Issue a new link? The old subscription link and the old connection keys stop working.')} onDone={() => { flash(tr('Выпущена новая ссылка', 'A new link was issued')); r.reload(); }}><RotateCw size={14}/>{tr('Новая ссылка', 'New link')}</ActionButton>
          </div>
        </Panel>
        <Panel title={tr('Серверы', 'Servers')}>
          {!view.servers.length && <p>{tr('Подходящих серверов пока нет. Нужен сервер 3x-ui с VLESS-инбаундом (Reality или TLS) в стране, где Telegram работает.', 'No suitable server yet. You need a 3x-ui server with a VLESS inbound (Reality or TLS) in a country where Telegram works.')}</p>}
          {view.servers.map(s => <p key={s.panel_id}><Badge value={s.ready}>{s.ready ? tr('готов', 'ready') : tr('настраивается', 'preparing')}</Badge> <strong>{s.title}</strong>{s.error && <small className="text-danger"> {s.error}</small>}</p>)}
          {view.skipped.length > 0 && <>
            <h3>{tr('Не подходят', 'Not offered')}</h3>
            {view.skipped.map(s => <p key={s.panel_id}><strong>{s.name}</strong> <small>{reason(s)}</small></p>)}
          </>}
          <p><small>{tr('Исключены страны', 'Excluded countries')}: {view.excluded_countries.join(', ') || '—'}. {tr('Меняется в настройках (группа «Доступ к Telegram»): лимит трафика в месяц, порог оповещения, страны.', 'Change them in settings (group "Telegram access"): monthly traffic limit, alert threshold, countries.')}</small></p>
        </Panel>
      </>}
    </Resource>
  </>;
}
