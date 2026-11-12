import { createContext, useContext, useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { AlertCircle, Check, Copy, LoaderCircle, QrCode, RefreshCw, X } from 'lucide-react';
import { copyText, safeExternalUrl } from '../shared/api';
import type { Me, Subscription } from './types';
import { SkeletonList, haptic } from './polish';

export type Translate = (ru: string, en: string) => string;
export interface Session {
  me: Me; subs: Subscription[]; selected: Subscription | null; select(id: number): void;
  t: Translate; refresh(): void; notify(message: string, error?: boolean): void;
}
export const SessionContext = createContext<Session | null>(null);
export function useSession() { const context = useContext(SessionContext); if (!context) throw new Error('Missing session'); return context; }
export const Spinner = () => <LoaderCircle className="mi-spin" size={22} aria-label="Loading"/>;
export function LoadState({ loading, error, retry, children }: { loading: boolean; error: Error | null; retry(): void; children: ReactNode }) {
  const { t } = useSession();
  if (loading) return <div className="mi-loading-block" role="status" aria-label={t('Загружаем…', 'Loading…')}><SkeletonList/></div>;
  if (error) return <div className="mi-empty mi-error" role="alert"><AlertCircle size={30}/><h3>{t('Не удалось загрузить', 'Unable to load')}</h3><p>{error.message}</p><button className="mi-button mi-secondary" onClick={retry}><RefreshCw size={16}/>{t('Повторить', 'Try again')}</button></div>;
  return <>{children}</>;
}
export function Empty({ icon, title, children }: { icon?: ReactNode; title: string; children?: ReactNode }) { return <div className="mi-empty">{icon}<h3>{title}</h3>{children}</div>; }
export function useAction() {
  const [busy, setBusy] = useState(false);
  const { notify, t } = useSession();
  async function run<T>(action: () => Promise<T>, success?: (result: T) => void) {
    if (busy) return;
    haptic();
    setBusy(true);
    try { const result = await action(); success?.(result); return result; }
    catch (error) { notify(error instanceof Error ? error.message : t('Не удалось выполнить действие', 'Action failed'), true); }
    finally { setBusy(false); }
  }
  return { busy, run };
}
export function Dialog({ title, children, close }: { title: string; children: ReactNode; close(): void }) {
  const ref = useRef<HTMLDialogElement>(null); const titleId = useId();
  useEffect(() => { const dialog = ref.current; dialog?.showModal(); return () => dialog?.close(); }, []);
  return <dialog ref={ref} className="mi-dialog" aria-labelledby={titleId} onCancel={close} onClick={event => { if (event.target === event.currentTarget) close(); }}><div className="mi-dialog-head"><h2 id={titleId}>{title}</h2><button className="mi-icon-button" aria-label="Close" onClick={close}><X size={20}/></button></div>{children}</dialog>;
}
export function Confirm({ title, children, action, close, danger = false }: { title: string; children: ReactNode; action(): Promise<unknown>; close(): void; danger?: boolean }) {
  const { t } = useSession(); const { busy, run } = useAction();
  return <Dialog title={title} close={close}><p className="mi-muted">{children}</p><div className="mi-actions"><button className="mi-button mi-secondary" disabled={busy} onClick={close}>{t('Отмена', 'Cancel')}</button><button className={`mi-button ${danger ? 'mi-danger' : ''}`} disabled={busy} onClick={() => run(action, close)}>{busy ? <Spinner/> : t('Подтвердить', 'Confirm')}</button></div></Dialog>;
}
export function CopyButton({ value, label }: { value: string; label?: string }) {
  const { t, notify } = useSession(); const [copied, setCopied] = useState(false);
  useEffect(() => { if (!copied) return; const timer = setTimeout(() => setCopied(false), 2200); return () => clearTimeout(timer); }, [copied]);
  return <button className="mi-button mi-secondary mi-small" onClick={async () => { try { await copyText(value); setCopied(true); } catch { notify(t('Не удалось скопировать. Выделите ссылку вручную.', 'Unable to copy. Select the link manually.'), true); } }}>{copied ? <Check size={16}/> : <Copy size={16}/>} {copied ? t('Скопировано', 'Copied') : label || t('Копировать', 'Copy')}</button>;
}
export function LinkBox({ value, qr, label }: { value: string; qr?: string | null; label?: string }) {
  const { t } = useSession(); const [showQr, setShowQr] = useState(false);
  return <div className="mi-linkbox">{label && <span className="mi-eyebrow">{label}</span>}<code tabIndex={0}>{value}</code><div className="mi-actions"><CopyButton value={value}/>{qr && <button className="mi-button mi-secondary mi-small" onClick={() => setShowQr(true)}><QrCode size={16}/>{t('QR-код', 'QR code')}</button>}</div>{showQr && qr && <Dialog title={t('Сканируйте для подключения', 'Scan to connect')} close={() => setShowQr(false)}><img className="mi-qr" src={qr} alt={t('QR-код ссылки', 'Link QR code')}/><p className="mi-muted mi-center">{t('Ссылка предоставляет доступ к вашей подписке. Не публикуйте её.', 'This link grants access to your subscription. Keep it private.')}</p></Dialog>}</div>;
}
export function openExternal(value: string, telegram = false) {
  const safe = safeExternalUrl(value); if (!safe) return;
  const sdk = window.Telegram?.WebApp;
  if (telegram && sdk) sdk.openTelegramLink(safe);
  else if (sdk && /^https?:/.test(safe)) sdk.openLink(safe);
  else window.open(safe, '_blank', 'noopener,noreferrer');
}
export const plainText = (value: string) => value.replace(/<[^>]*>/g, '').replaceAll('\\n', '\n');
export function SubSelector() {
  const { subs, selected, select, t } = useSession();
  if (subs.length < 2) return null;
  return <label className="mi-field mi-sub-selector"><span>{t('Подписка', 'Subscription')}</span><select value={selected?.id ?? ''} onChange={e => select(Number(e.target.value))}>{subs.map(sub => <option key={sub.id} value={sub.id}>{sub.label || sub.plan?.title || `${t('Подписка', 'Subscription')} #${sub.id}`}</option>)}</select></label>;
}
export function StatusBadge({ state, soon = false }: { state: string; soon?: boolean }) { const { t } = useSession(); return <span className={`mi-badge ${soon && state === 'active' ? 'mi-badge-warn' : state === 'active' ? 'mi-badge-good' : state === 'expired' ? 'mi-badge-bad' : ''}`}>{soon && state === 'active' ? t('Скоро истекает', 'Expiring soon') : state === 'active' ? t('Активна', 'Active') : state === 'disabled' ? t('Отключена', 'Disabled') : state === 'expired' ? t('Истекла', 'Expired') : t('Нет подписки', 'No subscription')}</span>; }
