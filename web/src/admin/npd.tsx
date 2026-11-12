import { useState, type FormEvent } from 'react';
import { formatDate, formatMoney, mutate, safeExternalUrl, useApi } from '../shared/api';
import { ActionButton, Badge, Modal, PageTitle, Pagination, Panel, Resource, Stats, Table, useAdmin, type Row } from './ui';

const BASE = '/npd_receipts';
const STATUSES = ['', 'pending', 'registered', 'failed', 'cancel_pending', 'cancelled'];

type Overview = { enabled: boolean; logged_in: boolean; inn: string | null; send_to_user: boolean; providers: string[]; counts: Record<string, number> };

function SmsLogin({ close, done }: { close: () => void; done: () => void }) {
  const { tr } = useAdmin();
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const [token, setToken] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      if (!token) { const r = await mutate<{ challenge_token: string }>('admin', `${BASE}/auth/sms/start`, { phone }); setToken(r.challenge_token); }
      else { await mutate('admin', `${BASE}/auth/sms/verify`, { phone, challenge_token: token, code }); done(); }
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  }
  return <Modal title={tr('Вход в «Мой налог» по SMS', 'Мой налог SMS login')} close={close}><form onSubmit={submit}>
    <p className="dialog-description">{token ? tr('Введите код из SMS.', 'Enter the code from the SMS.') : tr('Телефон, привязанный к «Мой налог», в формате 79001234567.', 'The phone linked to Мой налог, as 79001234567.')}</p>
    {!token && <label className="field"><span>{tr('Телефон', 'Phone')}</span><input required value={phone} placeholder="79001234567" onChange={e => setPhone(e.target.value.trim())}/></label>}
    {token && <label className="field"><span>{tr('Код', 'Code')}</span><input required autoFocus inputMode="numeric" value={code} onChange={e => setCode(e.target.value.trim())}/></label>}
    {error && <div className="inline-error" role="alert">{error}</div>}
    <footer><button type="button" className="btn secondary" onClick={close}>{tr('Отмена', 'Cancel')}</button><button disabled={busy} className="btn primary">{token ? tr('Войти', 'Log in') : tr('Отправить код', 'Send code')}</button></footer>
  </form></Modal>;
}

export function NpdReceiptsPage() {
  const { tr, locale, can } = useAdmin();
  const [status, setStatus] = useState('');
  const [offset, setOffset] = useState(0);
  const [sms, setSms] = useState(false);
  const overview = useApi<Overview>(`${BASE}/overview`);
  const list = useApi<{ items: Row[]; total: number }>(`${BASE}?limit=25&offset=${offset}${status ? `&status=${status}` : ''}`);
  const reload = () => { overview.reload(); list.reload(); };
  const o = overview.data;
  const owner = can('settings_secrets');
  const edit = can('payments_edit');
  return <>
    <PageTitle eyebrow={tr('ФИНАНСЫ', 'FINANCE')} title={tr('Чеки «Мой налог»', 'NPD receipts')}
      subtitle={tr('Доход самозанятого по каждой оплате регистрируется в «Мой налог». За правильность чеков отвечает владелец.', 'Self-employed income is registered in Мой налог for every payment. The owner is responsible for the receipts.')}>
      {owner && <button className="btn secondary" onClick={() => setSms(true)}>{tr('Войти по SMS', 'Log in by SMS')}</button>}
      {owner && <ActionButton path={`${BASE}/auth/password`} onDone={reload}>{tr('Войти по ИНН и паролю', 'Log in with INN and password')}</ActionButton>}
      {owner && o?.logged_in && <ActionButton path={`${BASE}/auth/logout`} danger onDone={reload} confirm={tr('Забыть токены?', 'Forget the tokens?')}>{tr('Выйти', 'Log out')}</ActionButton>}
    </PageTitle>
    {o && <Stats values={[
      { label: tr('Регистрация', 'Registration'), value: o.enabled ? tr('включена', 'on') : tr('выключена', 'off'), detail: o.providers.join(', ') },
      { label: tr('Кабинет', 'Cabinet'), value: o.logged_in ? tr('вход выполнен', 'logged in') : tr('нет входа', 'not logged in'), detail: o.inn || '—' },
      { label: tr('Зарегистрировано', 'Registered'), value: o.counts.registered || 0 },
      { label: tr('Ожидают / ошибки', 'Pending / failed'), value: `${(o.counts.pending || 0) + (o.counts.cancel_pending || 0)} / ${o.counts.failed || 0}` },
    ]}/>}
    <Panel title={tr('Чеки', 'Receipts')} aside={<select aria-label={tr('Статус', 'Status')} value={status} onChange={e => { setStatus(e.target.value); setOffset(0); }}>{STATUSES.map(s => <option key={s} value={s}>{s || tr('все', 'all')}</option>)}</select>}>
      <Resource resource={list} empty={!list.data?.items?.length}>
        <Table rows={list.data?.items || []} columns={[
          { title: tr('Дата', 'Date'), render: r => <div><strong>{formatDate(r.operation_time, locale)}</strong><small>{r.payment_id}</small></div> },
          { title: tr('Покупатель', 'Buyer'), render: r => <small>{r.user_id} · {r.provider}</small> },
          { title: tr('Услуга', 'Service'), render: r => <div><strong>{formatMoney(r.amount_kop, locale)}</strong><small>{r.service_name}</small></div> },
          { title: tr('Статус', 'Status'), render: r => <div><Badge value={r.status}/>{r.last_error && <small className="text-danger">{r.last_error}</small>}<small>{tr('попыток', 'attempts')}: {r.attempts}</small></div> },
          { title: tr('Чек', 'Receipt'), render: r => { const url = r.receipt_url ? safeExternalUrl(r.receipt_url) : null; return url ? <a href={url} target="_blank" rel="noreferrer">{tr('открыть', 'open')}</a> : <small>—</small>; } },
          { title: '', render: r => edit ? <div className="row-actions">
            {['failed', 'pending', 'cancel_pending'].includes(r.status) && <ActionButton path={`${BASE}/${r.id}/retry`} onDone={reload}>{tr('Повторить', 'Retry')}</ActionButton>}
            {['registered', 'pending', 'failed'].includes(r.status) && <ActionButton path={`${BASE}/${r.id}/cancel`} danger onDone={reload} confirm={tr('Аннулировать чек в «Мой налог»?', 'Annul the receipt in Мой налог?')}>{tr('Аннулировать', 'Annul')}</ActionButton>}
          </div> : null },
        ]}/>
        <Pagination total={list.data?.total} offset={offset} setOffset={setOffset}/>
      </Resource>
    </Panel>
    {sms && <SmsLogin close={() => setSms(false)} done={() => { setSms(false); reload(); }}/>}
  </>;
}
