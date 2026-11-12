import { createContext, useContext, useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { AlertCircle, Check, ChevronLeft, ChevronRight, LoaderCircle, RefreshCw, X } from 'lucide-react';
import { ApiError, mutate, resultProblem, type Scope } from '../shared/api';

export type Row = Record<string, any>;
export type Principal = { tg_id: number; role: string; permissions: string[]; csrf_token: string };
type AdminContextValue = { me: Principal; locale: string; tr: (ru: string, en: string) => string; can: (permission: string) => boolean; flash: (text: string, bad?: boolean) => void };
export const AdminContext = createContext<AdminContextValue>(null!);
export const useAdmin = () => useContext(AdminContext);
export const titleOf = (row: Row) => row.username ? `@${row.username}` : row.first_name || String(row.tg_id || row.user_id || '—');
export const itemsOf = (data: any): Row[] => Array.isArray(data) ? data : data?.items || [];

export function PageTitle({ eyebrow, title, subtitle, children }: { eyebrow?: string; title: string; subtitle?: string; children?: ReactNode }) {
  return <header className="page-title"><div>{eyebrow && <span className="eyebrow">{eyebrow}</span>}<h1>{title}</h1>{subtitle && <p>{subtitle}</p>}</div><div className="page-actions">{children}</div></header>;
}
export function Panel({ title, children, aside, className = '' }: { title?: string; children: ReactNode; aside?: ReactNode; className?: string }) {
  return <section className={`panel ${className}`}>{(title || aside) && <div className="panel-heading"><h2>{title}</h2>{aside}</div>}{children}</section>;
}
export function Badge({ value, children }: { value?: string | boolean | null; children?: ReactNode }) {
  const { tr } = useAdmin();
  const good = ['active', 'succeeded', 'paid', 'enabled', 'healthy', 'done', 'true'].includes(String(value));
  const bad = ['disabled', 'expired', 'canceled', 'refunded', 'down', 'rejected', 'false'].includes(String(value));
  const names: Record<string, [string, string]> = { active: ['Активна', 'Active'], expired: ['Истекла', 'Expired'], disabled: ['Отключена', 'Disabled'], pending: ['Ожидание', 'Pending'], succeeded: ['Оплачен', 'Succeeded'], canceled: ['Отменён', 'Canceled'], refunded: ['Возвращён', 'Refunded'], created: ['Создан', 'Created'], paid: ['Выплачено', 'Paid'], approved: ['Одобрено', 'Approved'], rejected: ['Отклонено', 'Rejected'], open: ['Открыт', 'Open'], answered: ['Есть ответ', 'Answered'], closed: ['Закрыт', 'Closed'], draft: ['Черновик', 'Draft'], running: ['Отправляется', 'Running'], done: ['Готово', 'Done'], true: ['Да', 'Yes'], false: ['Нет', 'No'] };
  return <span className={`badge ${good ? 'positive' : bad ? 'negative' : 'neutral'}`}><span className="status-dot"/>{children || (names[String(value)] ? tr(...names[String(value)]) : String(value || '—'))}</span>;
}
export function Loading() { const { tr } = useAdmin(); return <div className="state-block"><LoaderCircle className="spin" size={24}/><span>{tr('Загружаем данные…', 'Loading…')}</span></div>; }
export function Empty({ text }: { text?: string }) { const { tr } = useAdmin(); return <div className="state-block empty"><span className="empty-symbol">◇</span><strong>{text || tr('Пока ничего нет', 'Nothing here yet')}</strong><p>{tr('Данные появятся после первых действий.', 'Data will appear after the first activity.')}</p></div>; }
export function ErrorState({ error, retry }: { error: Error; retry?: () => void }) { const { tr } = useAdmin(); return <div className="error-block" role="alert"><AlertCircle size={20}/><div><strong>{tr('Не удалось загрузить данные', 'Unable to load data')}</strong><p>{error.message}</p></div>{retry && <button className="btn secondary" onClick={retry}><RefreshCw size={15}/>{tr('Повторить', 'Retry')}</button>}</div>; }
export function Resource({ resource, children, empty = false }: { resource: { error: Error | null; loading: boolean; reload: () => void; data: any }; children: ReactNode; empty?: boolean }) {
  return resource.error ? <ErrorState error={resource.error} retry={resource.reload}/> : resource.loading && !resource.data ? <Loading/> : empty ? <Empty/> : <>{children}</>;
}
export function Table({ columns, rows, keyField = 'id', onRow }: { columns: { title: string; key?: string; render?: (row: Row) => ReactNode; className?: string }[]; rows: Row[]; keyField?: string; onRow?: (row: Row) => void }) {
  if (!rows.length) return <Empty/>;
  return <div className="table-scroll"><table><thead><tr>{columns.map((c, i) => <th key={i} className={c.className}>{c.title}</th>)}</tr></thead><tbody>{rows.map((r, i) => <tr key={r[keyField] ?? i} className={onRow ? 'clickable' : ''} onClick={onRow ? () => onRow(r) : undefined}>{columns.map((c, j) => <td key={j} className={c.className}>{c.render ? c.render(r) : r[c.key || ''] ?? '—'}</td>)}</tr>)}</tbody></table></div>;
}
export function Pagination({ total = 0, offset, setOffset, limit = 25 }: { total?: number; offset: number; setOffset: (n: number) => void; limit?: number }) {
  const { tr } = useAdmin(); return <div className="pagination"><span>{total ? `${offset + 1}–${Math.min(offset + limit, total)} ${tr('из', 'of')} ${total}` : tr('Нет записей', 'No records')}</span><div><button className="icon-btn" aria-label={tr('Предыдущая страница', 'Previous page')} disabled={!offset} onClick={() => setOffset(Math.max(0, offset - limit))}><ChevronLeft size={17}/></button><button className="icon-btn" aria-label={tr('Следующая страница', 'Next page')} disabled={offset + limit >= total} onClick={() => setOffset(offset + limit)}><ChevronRight size={17}/></button></div></div>;
}
export function Modal({ title, children, close, wide = false }: { title: string; children: ReactNode; close: () => void; wide?: boolean }) {
  const ref = useRef<HTMLDivElement>(null); const id = useId();
  useEffect(() => {
    const previous = document.activeElement as HTMLElement;
    const overflow = document.body.style.overflow; document.body.style.overflow = 'hidden';
    ref.current?.focus();
    const handler = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close();
      if (event.key === 'Tab') {
        const elements = ref.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea, select, a[href]');
        if (!elements?.length) return;
        const first = elements[0], last = elements[elements.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    };
    window.addEventListener('keydown', handler);
    return () => { document.body.style.overflow = overflow; window.removeEventListener('keydown', handler); previous?.focus(); };
  }, [close]);
  return <div className="modal-overlay" onMouseDown={e => { if (e.target === e.currentTarget) close(); }}><div ref={ref} role="dialog" aria-modal="true" aria-labelledby={id} tabIndex={-1} className={`modal ${wide ? 'wide' : ''}`}><header><h2 id={id}>{title}</h2><button onClick={close} className="icon-btn" aria-label="Close"><X size={21}/></button></header>{children}</div></div>;
}
export type FieldSpec = { key: string; label: string; type?: string; required?: boolean; hint?: string; min?: number; max?: number; step?: string; options?: (string | { value: string; label: string })[]; placeholder?: string };
export function FormFields({ fields, values, setValues }: { fields: FieldSpec[]; values: Row; setValues: (values: Row) => void }) {
  return <div className="form-grid">{fields.map(f => <label key={f.key} className={`field ${f.type === 'textarea' || f.type === 'json' ? 'full' : ''} ${f.type === 'checkbox' ? 'checkbox-field' : ''}`}><span>{f.label}</span>{f.type === 'checkbox' ? <input type="checkbox" checked={Boolean(values[f.key])} onChange={e => setValues({ ...values, [f.key]: e.target.checked })}/> : f.options ? <select required={f.required} value={values[f.key] ?? ''} onChange={e => setValues({ ...values, [f.key]: e.target.value })}>{!f.required && <option value="">—</option>}{f.options.map(o => <option key={typeof o === 'string' ? o : o.value} value={typeof o === 'string' ? o : o.value}>{typeof o === 'string' ? o : o.label}</option>)}</select> : f.type === 'textarea' || f.type === 'json' ? <textarea rows={f.type === 'json' ? 7 : 4} required={f.required} placeholder={f.placeholder} value={values[f.key] ?? ''} onChange={e => setValues({ ...values, [f.key]: e.target.value })}/> : <input type={f.type || 'text'} required={f.required} min={f.min} max={f.max} step={f.step} placeholder={f.placeholder} autoComplete={f.type === 'password' ? 'new-password' : undefined} value={values[f.key] ?? ''} onChange={e => setValues({ ...values, [f.key]: e.target.value })}/>} {f.hint && <small>{f.hint}</small>}</label>)}</div>;
}
export function FormDialog({ title, fields, initial = {}, submit, close, submitLabel, danger = false, description }: { title: string; fields: FieldSpec[]; initial?: Row; submit: (values: Row) => Promise<unknown>; close: () => void; submitLabel?: string; danger?: boolean; description?: string }) {
  const { tr, flash } = useAdmin(); const [values, setValues] = useState<Row>(initial); const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  async function save(event: FormEvent) { event.preventDefault(); setBusy(true); setError(''); try { const parsed = { ...values }; for (const f of fields) { if (f.type === 'number') parsed[f.key] = values[f.key] === '' || values[f.key] == null ? null : Number(values[f.key]); if (f.type === 'json') parsed[f.key] = JSON.parse(values[f.key] || '{}'); if (f.type === 'datetime-local') parsed[f.key] = values[f.key] ? new Date(values[f.key]).toISOString() : null; } await submit(parsed); flash(tr('Изменения сохранены', 'Changes saved')); close(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); } }
  return <Modal title={title} close={close}><form onSubmit={save}>{description && <p className="dialog-description">{description}</p>}<FormFields fields={fields} values={values} setValues={setValues}/>{error && <div className="inline-error" role="alert">{error}</div>}<footer><button type="button" className="btn secondary" onClick={close}>{tr('Отмена', 'Cancel')}</button><button disabled={busy} className={`btn ${danger ? 'danger' : 'primary'}`}>{busy ? <LoaderCircle className="spin" size={16}/> : <Check size={16}/>} {submitLabel || tr('Сохранить', 'Save')}</button></footer></form></Modal>;
}
export function ActionButton({ path, body = {}, method = 'POST', children, onDone, danger = false, confirm, disabled = false }: { path: string; body?: unknown; method?: string; children: ReactNode; onDone?: (result: any) => void; danger?: boolean; confirm?: string; disabled?: boolean }) {
  const { flash, tr } = useAdmin(); const [busy, setBusy] = useState(false);
  return <button disabled={busy || disabled} className={`btn ${danger ? 'danger subtle' : 'secondary'}`} onClick={async e => { e.stopPropagation(); if (confirm && !window.confirm(confirm)) return; setBusy(true); try { const result = await mutate('admin', path, body, method); const problem = resultProblem(result); if (problem) flash(`${tr('Выполнено с ошибками', 'Completed with errors')}: ${problem}`, true); else flash(tr('Готово', 'Done')); onDone?.(result); } catch (error) { flash((error as Error).message, true); } finally { setBusy(false); } }}>{busy && <LoaderCircle size={15} className="spin"/>}{children}</button>;
}
export function KeyValues({ data }: { data: Record<string, ReactNode> }) { return <dl className="key-values">{Object.entries(data).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value ?? '—'}</dd></div>)}</dl>; }
export function Stats({ values }: { values: { label: string; value: ReactNode; detail?: ReactNode; icon?: ReactNode }[] }) { return <div className="stats-grid">{values.map(s => <div className="stat-card" key={s.label}><div className="stat-label">{s.label}{s.icon}</div><strong>{s.value}</strong>{s.detail && <span>{s.detail}</span>}</div>)}</div>; }
export const apiMutation = (path: string, body?: unknown, method?: string, scope: Scope = 'admin') => mutate(scope, path, body, method);
