import { useState } from 'react';
import { Copy, KeyRound, Plus, Send } from 'lucide-react';
import { copyText, mutate, useApi } from '../shared/api';
import { ActionButton, Badge, FormDialog, Modal, PageTitle, Panel, Resource, Table, itemsOf, useAdmin, type Row } from './ui';

type Catalog = { events: string[]; retry_delays_sec: number[] };

const when = (value: string | null | undefined, locale: string) => value ? new Intl.DateTimeFormat(locale, { dateStyle: 'short', timeStyle: 'medium' }).format(new Date(value)) : '—';
const eventsOf = (value: unknown): string[] => String(value || '*').split(/[\s,]+/).map(s => s.trim()).filter(Boolean);

function SecretDialog({ secret, close }: { secret: string; close: () => void }) {
  const { tr, flash } = useAdmin();
  return <Modal title={tr('Секрет подписи', 'Signing secret')} close={close}>
    <p className="dialog-description">{tr('Сохраните секрет сейчас: он показывается только один раз. С его помощью получатель проверяет заголовок X-Webhook-Signature.', 'Save the secret now: it is shown only once. The receiver uses it to verify the X-Webhook-Signature header.')}</p>
    <div className="field full"><code style={{ wordBreak: 'break-all' }}>{secret}</code></div>
    <footer><button className="btn secondary" onClick={async () => { await copyText(secret); flash(tr('Скопировано', 'Copied')); }}><Copy size={15}/>{tr('Копировать', 'Copy')}</button><button className="btn primary" onClick={close}>{tr('Готово', 'Done')}</button></footer>
  </Modal>;
}

function Deliveries({ endpoint, close }: { endpoint: Row; close: () => void }) {
  const { tr, locale } = useAdmin();
  const [status, setStatus] = useState('');
  const r = useApi(`/webhooks/${endpoint.id}/deliveries?limit=100${status ? `&status=${status}` : ''}`);
  return <Modal wide title={`${tr('Доставки', 'Deliveries')} · #${endpoint.id}`} close={close}>
    <div className="toolbar"><select aria-label={tr('Статус', 'Status')} value={status} onChange={e => setStatus(e.target.value)}><option value="">{tr('Все статусы', 'All statuses')}</option>{['pending', 'sent', 'failed', 'dead'].map(s => <option key={s}>{s}</option>)}</select></div>
    <Resource resource={r} empty={!itemsOf(r.data).length}>
      <Table rows={itemsOf(r.data)} columns={[
        { title: tr('Событие', 'Event'), render: d => <div><strong>{d.event_type}</strong><small><code>{d.event_id}</code></small></div> },
        { title: tr('Статус', 'Status'), render: d => <div><Badge value={d.status}/><small>{tr('попыток', 'attempts')}: {d.attempts}{d.last_status_code ? ` · HTTP ${d.last_status_code}` : ''}</small></div> },
        { title: tr('Создано', 'Created'), render: d => <small>{when(d.created_at, locale)}</small> },
        { title: tr('Следующая попытка / ошибка', 'Next attempt / error'), render: d => <small className={d.last_error ? 'text-danger' : ''}>{d.status === 'sent' ? when(d.sent_at, locale) : d.status === 'failed' || d.status === 'pending' ? when(d.next_attempt_at, locale) : ''}{d.last_error ? <><br/>{d.last_error}</> : null}</small> },
        { title: '', render: d => <ActionButton path={`/webhooks/deliveries/${d.id}/redeliver`} onDone={r.reload}><Send size={14}/>{tr('Повторить', 'Redeliver')}</ActionButton> },
      ]}/>
    </Resource>
  </Modal>;
}

export function WebhooksPage() {
  const { tr, locale, flash } = useAdmin();
  const r = useApi('/webhooks');
  const catalog = useApi<Catalog>('/webhooks/catalog');
  const [dialog, setDialog] = useState<null | { endpoint?: Row }>(null);
  const [secret, setSecret] = useState('');
  const [deliveries, setDeliveries] = useState<Row | null>(null);
  const rows = itemsOf(r.data);
  const hint = tr('Через запятую, например purchase, payment.*; «*» — все события. Доступно: ', 'Comma-separated, e.g. purchase, payment.*; "*" means every event. Available: ') + (catalog.data?.events || []).join(', ');
  const fields = [
    { key: 'url', label: 'URL', required: true, placeholder: 'https://crm.example.com/webhooks/vpn', hint: tr('Только https. Адреса во внутренних сетях запрещены.', 'https only. Addresses in private networks are refused.') },
    { key: 'events', label: tr('События', 'Events'), type: 'textarea', hint },
    { key: 'description', label: tr('Описание', 'Description') },
  ];
  return <>
    <PageTitle eyebrow={tr('ИНТЕГРАЦИИ', 'INTEGRATIONS')} title={tr('Исходящие вебхуки', 'Outgoing webhooks')} subtitle={tr('Подписанные HTTP-уведомления о событиях для CRM и других систем. Неудачные доставки повторяются с растущей паузой.', 'Signed HTTP notifications about events for CRMs and other systems. Failed deliveries are retried with growing delays.')}>
      <button className="btn primary" onClick={() => setDialog({})}><Plus size={16}/>{tr('Добавить адрес', 'Add endpoint')}</button>
    </PageTitle>
    <Panel>
      <Resource resource={r} empty={!rows.length}>
        <Table rows={rows} columns={[
          { title: 'URL', render: e => <div><strong style={{ wordBreak: 'break-all' }}>{e.url}</strong><small>#{e.id}{e.description ? ` · ${e.description}` : ''}</small></div> },
          { title: tr('События', 'Events'), render: e => <small>{(e.events || []).join(', ')}</small> },
          { title: tr('Состояние', 'State'), render: e => <div><Badge value={e.is_active}>{e.is_active ? tr('включён', 'active') : tr('выключен', 'disabled')}</Badge>{e.disabled_reason && <small className="text-danger">{e.disabled_reason}</small>}<small>{tr('в очереди', 'queued')}: {e.pending} · {tr('ошибок подряд', 'failures in a row')}: {e.consecutive_failures}</small></div> },
          { title: tr('Последняя доставка', 'Last delivery'), render: e => <small>{tr('успех', 'success')}: {when(e.last_success_at, locale)}<br/>{tr('ошибка', 'failure')}: {when(e.last_failure_at, locale)}</small> },
          { title: '', render: e => <div className="row-actions">
            <ActionButton path={`/webhooks/${e.id}/test`} disabled={!e.is_active} onDone={res => { flash(res.ok ? tr('Тестовое событие доставлено', 'Test event delivered') : `${tr('Не доставлено', 'Not delivered')}: ${res.error || res.status_code}`, !res.ok); r.reload(); }}><Send size={14}/>{tr('Тест', 'Test')}</ActionButton>
            <button className="btn secondary" onClick={() => setDeliveries(e)}>{tr('Доставки', 'Deliveries')}</button>
            <button className="btn secondary" onClick={() => setDialog({ endpoint: e })}>{tr('Изменить', 'Edit')}</button>
            <ActionButton path={`/webhooks/${e.id}`} method="PATCH" body={{ is_active: !e.is_active }} onDone={r.reload}>{e.is_active ? tr('Выключить', 'Disable') : tr('Включить', 'Enable')}</ActionButton>
            <ActionButton path={`/webhooks/${e.id}/rotate-secret`} confirm={tr('Выпустить новый секрет? Старый перестанет работать сразу.', 'Issue a new secret? The old one stops working immediately.')} onDone={res => setSecret(res.secret)}><KeyRound size={14}/>{tr('Новый секрет', 'Rotate secret')}</ActionButton>
            <ActionButton danger path={`/webhooks/${e.id}`} method="DELETE" confirm={tr('Удалить адрес и историю доставок?', 'Delete the endpoint and its delivery history?')} onDone={r.reload}>{tr('Удалить', 'Delete')}</ActionButton>
          </div> },
        ]}/>
      </Resource>
      {catalog.data && <p className="panel-note">{tr('Повторы после ошибки через (сек): ', 'Retries after a failure in (sec): ')}{catalog.data.retry_delays_sec.join(', ')}</p>}
    </Panel>
    {dialog && <FormDialog
      title={dialog.endpoint ? tr('Изменить вебхук', 'Edit webhook') : tr('Новый вебхук', 'New webhook')}
      fields={fields}
      initial={dialog.endpoint ? { url: dialog.endpoint.url, events: (dialog.endpoint.events || []).join(', '), description: dialog.endpoint.description || '' } : { events: '*' }}
      close={() => setDialog(null)}
      submit={async v => {
        const body = { url: v.url, events: eventsOf(v.events), description: v.description || '' };
        if (dialog.endpoint) await mutate('admin', `/webhooks/${dialog.endpoint.id}`, body, 'PATCH');
        else { const created = await mutate<Row>('admin', '/webhooks', body); setSecret(created.secret); }
        r.reload();
      }}/>}
    {secret && <SecretDialog secret={secret} close={() => setSecret('')}/>}
    {deliveries && <Deliveries endpoint={deliveries} close={() => { setDeliveries(null); r.reload(); }}/>}
  </>;
}
