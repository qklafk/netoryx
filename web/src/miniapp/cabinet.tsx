import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { Link, Route, Routes, useLocation, useNavigate, useParams } from 'react-router-dom';
import { ArrowUpRight, Globe2, LogOut, Mail, ShieldCheck } from 'lucide-react';
import { appApi, cabinetApi, mutate, setCabinetCsrf } from '../shared/api';
import { detectLanguage, translate } from './i18n';
import { Spinner, openExternal, useAction, useSession } from './ui';

interface LoginConfig { brand: string; link: boolean; telegram: boolean; email: boolean; bot_username: string | null }
interface CabinetSession { user_id: number; csrf_token: string; expires_at: string }
interface EmailState { email: string | null; available: boolean }

const text = () => { const lang = detectLanguage(navigator.language); return (ru: string, en: string) => translate(lang, ru, en); };

function Shell({ title, children }: { title: string; children?: ReactNode }) {
  return <main className="mini-shell mi-gate"><div className="mi-brand-mark"><ShieldCheck size={32}/></div><h1>{title}</h1>{children}</main>;
}

// Opened from the bot (or from an e-mail): swaps the one-time token in the URL for a session cookie.
function RedeemPage() {
  const t = text(); const { token = '' } = useParams(); const navigate = useNavigate();
  const started = useRef(false); const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (started.current) return; started.current = true;
    cabinetApi<CabinetSession>('/redeem', { method: 'POST', body: JSON.stringify({ token }) })
      .then(session => { setCabinetCsrf(session.csrf_token); navigate('/', { replace: true }); })
      .catch(e => setError(e instanceof Error ? e.message : String(e)));
  }, [token, navigate]);
  if (error) return <Shell title={t('Не удалось войти', 'Unable to sign in')}><p>{error}</p><Link className="mi-button" to="/">{t('К странице входа', 'Back to sign in')}</Link></Shell>;
  return <Shell title={t('Входим…', 'Signing you in…')}><Spinner/></Shell>;
}

// Telegram Login Widget redirects here with the signed user data in the query string.
function TelegramReturn() {
  const t = text(); const location = useLocation(); const navigate = useNavigate();
  const started = useRef(false); const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (started.current) return; started.current = true;
    const payload = Object.fromEntries(new URLSearchParams(location.search));
    cabinetApi<CabinetSession>('/telegram', { method: 'POST', body: JSON.stringify(payload) })
      .then(session => { setCabinetCsrf(session.csrf_token); navigate('/', { replace: true }); })
      .catch(e => setError(e instanceof Error ? e.message : String(e)));
  }, [location.search, navigate]);
  if (error) return <Shell title={t('Не удалось войти', 'Unable to sign in')}><p>{error}</p><Link className="mi-button" to="/">{t('К странице входа', 'Back to sign in')}</Link></Shell>;
  return <Shell title={t('Входим через Telegram…', 'Signing in with Telegram…')}><Spinner/></Shell>;
}

// Opened from the confirmation e-mail: attaches the address to the account.
function VerifyPage() {
  const t = text(); const { token = '' } = useParams();
  const started = useRef(false); const [state, setState] = useState<{ ok: boolean; message: string } | null>(null);
  useEffect(() => {
    if (started.current) return; started.current = true;
    cabinetApi<EmailState>('/verify-email', { method: 'POST', body: JSON.stringify({ token }) })
      .then(r => setState({ ok: true, message: r.email || '' }))
      .catch(e => setState({ ok: false, message: e instanceof Error ? e.message : String(e) }));
  }, [token]);
  if (!state) return <Shell title={t('Подтверждаем адрес…', 'Confirming the address…')}><Spinner/></Shell>;
  return <Shell title={state.ok ? t('E-mail подтверждён', 'E-mail confirmed') : t('Не удалось подтвердить', 'Unable to confirm')}><p>{state.ok ? t(`Адрес ${state.message} привязан к аккаунту. Теперь им можно пользоваться для входа.`, `${state.message} is now linked to your account and can be used to sign in.`) : state.message}</p><Link className="mi-button" to="/">{t('В кабинет', 'Open the cabinet')}</Link></Shell>;
}

function TelegramWidget({ bot }: { bot: string }) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const host = box.current; if (!host) return;
    const script = document.createElement('script');
    script.async = true; script.src = 'https://telegram.org/js/telegram-widget.js?22';
    script.setAttribute('data-telegram-login', bot); script.setAttribute('data-size', 'large'); script.setAttribute('data-radius', '11');
    script.setAttribute('data-request-access', 'write'); script.setAttribute('data-auth-url', `${location.origin}/cabinet/login/telegram`);
    host.appendChild(script);
    return () => { host.replaceChildren(); };
  }, [bot]);
  return <div ref={box} className="mi-center" style={{ minHeight: 44 }}/>;
}

function EmailLogin() {
  const t = text(); const [email, setEmail] = useState(''); const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ error: boolean; text: string } | null>(null);
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setMessage(null);
    try {
      await cabinetApi('/email/request', { method: 'POST', body: JSON.stringify({ email }) });
      setMessage({ error: false, text: t('Если этот адрес привязан к аккаунту, мы отправили на него ссылку для входа. Проверьте почту, в том числе папку «Спам».', 'If this address is linked to an account, we sent a sign-in link to it. Check your inbox, including spam.') });
    } catch (e) { setMessage({ error: true, text: e instanceof Error ? e.message : String(e) }); }
    finally { setBusy(false); }
  }
  return <form className="mi-card mi-form-card" onSubmit={submit}><label className="mi-field"><span>{t('Вход по e-mail', 'Sign in by e-mail')}</span><input type="email" value={email} maxLength={254} autoComplete="email" placeholder="name@example.com" onChange={e => setEmail(e.target.value)} required/></label><button className="mi-button mi-full" disabled={busy || !email.trim()}>{busy ? <Spinner/> : <Mail size={17}/>} {t('Получить ссылку', 'Send the link')}</button>{message && <p role={message.error ? 'alert' : 'status'} className={message.error ? 'mi-inline-error' : 'mi-muted'}>{message.text}</p>}</form>;
}

function LoginPage() {
  const t = text(); const [config, setConfig] = useState<LoginConfig | null>(null); const [error, setError] = useState<string | null>(null);
  useEffect(() => { cabinetApi<LoginConfig>('/config').then(setConfig).catch(e => setError(e instanceof Error ? e.message : String(e))); }, []);
  if (error) return <Shell title={t('Кабинет недоступен', 'The cabinet is unavailable')}><p>{error}</p></Shell>;
  if (!config) return <Shell title={t('Загружаем…', 'Loading…')}><Spinner/></Shell>;
  const none = !config.link && !config.telegram && !config.email;
  return <Shell title={t('Вход в личный кабинет', 'Sign in to your account')}>
    <p>{t('Управляйте подпиской, оплатой и подключением из обычного браузера.', 'Manage your subscription, payments and connection from a regular browser.')}</p>
    {config.telegram && config.bot_username && <section className="mi-section"><TelegramWidget bot={config.bot_username}/></section>}
    {config.email && <EmailLogin/>}
    {config.link && <p className="mi-tip"><Globe2 size={20}/><span>{t('Ссылка для входа выдаётся ботом: откройте бота в Telegram, нажмите «Веб-кабинет» в профиле или отправьте команду /cabinet.', 'The bot issues a sign-in link: open the bot in Telegram, tap "Web cabinet" in the profile or send /cabinet.')}{config.bot_username && <> <a href={`https://t.me/${config.bot_username}`} target="_blank" rel="noreferrer">@{config.bot_username}</a></>}</span></p>}
    {none && <p className="mi-inline-error">{t('Способы входа не настроены. Обратитесь к администратору.', 'No sign-in method is configured. Contact the administrator.')}</p>}
  </Shell>;
}

function CabinetGate({ app }: { app: ReactNode }) {
  const t = text(); const [state, setState] = useState<'loading' | 'in' | 'out'>('loading');
  useEffect(() => {
    cabinetApi<CabinetSession>('/session').then(session => { setCabinetCsrf(session.csrf_token); setState('in'); }).catch(() => setState('out'));
  }, []);
  if (state === 'loading') return <Shell title={t('Загружаем…', 'Loading…')}><Spinner/></Shell>;
  return state === 'in' ? <>{app}</> : <LoginPage/>;
}

export function CabinetRoot({ app }: { app: ReactNode }) {
  return <Routes>
    <Route path="/login/telegram" element={<TelegramReturn/>}/>
    <Route path="/login/:token" element={<RedeemPage/>}/>
    <Route path="/verify/:token" element={<VerifyPage/>}/>
    <Route path="*" element={<CabinetGate app={app}/>}/>
  </Routes>;
}

export function CabinetProblem({ message, retry }: { message?: string; retry?(): void }) {
  const t = text();
  return <Shell title={t('Не удалось открыть кабинет', 'Unable to open the cabinet')}><p>{message}</p>{retry && <button className="mi-button" onClick={retry}>{t('Попробовать снова', 'Try again')}</button>}<button className="mi-button mi-secondary" onClick={() => leave('/logout')}>{t('Выйти', 'Sign out')}</button></Shell>;
}

async function leave(path: string) {
  try { await cabinetApi(path, { method: 'POST', body: '{}' }); } catch { /* the session is already gone */ }
  setCabinetCsrf(''); window.location.assign('/cabinet/');
}

// Profile block: sign-in e-mail, sign out (in the cabinet) or "open in a browser" (in the Mini App).
export function CabinetSection({ inCabinet }: { inCabinet: boolean }) {
  const { t, notify } = useSession(); const { busy, run } = useAction();
  const [state, setState] = useState<EmailState | null>(null); const [email, setEmail] = useState('');
  const load = () => appApi<EmailState>('/cabinet/email').then(setState).catch(() => setState(null));
  useEffect(() => { load(); }, []);
  if (!state) return null;
  const addEmail = (event: FormEvent) => { event.preventDefault(); run(() => mutate('app', '/cabinet/email', { email }), () => { setEmail(''); notify(t('Мы отправили письмо со ссылкой для подтверждения', 'We sent you a confirmation e-mail')); }); };
  const removeEmail = () => run(() => mutate('app', '/cabinet/email', {}, 'DELETE'), () => { notify(t('Адрес удалён', 'Address removed')); load(); });
  const openInBrowser = () => run(() => mutate<{ url: string }>('app', '/cabinet/login-link', {}), result => { if (result) openExternal(result.url); });
  return <section className="mi-section"><h2>{t('Веб-кабинет', 'Web cabinet')}</h2>
    {!inCabinet && <><p className="mi-muted">{t('Кабинет работает в обычном браузере, если Telegram недоступен.', 'The cabinet works in a regular browser when Telegram is unavailable.')}</p><button className="mi-button mi-secondary mi-full" disabled={busy} onClick={openInBrowser}>{t('Открыть в браузере', 'Open in a browser')}<ArrowUpRight size={17}/></button></>}
    {state.available && <form className="mi-card mi-form-card" onSubmit={addEmail}><label className="mi-field"><span>{t('E-mail для входа', 'Sign-in e-mail')}{state.email ? `: ${state.email}` : ''}</span><input type="email" value={email} maxLength={254} placeholder="name@example.com" onChange={e => setEmail(e.target.value)} required/></label><div className="mi-actions"><button className="mi-button" disabled={busy || !email.trim()}><Mail size={16}/> {state.email ? t('Заменить адрес', 'Change address') : t('Привязать адрес', 'Link address')}</button>{state.email && <button type="button" className="mi-button mi-secondary" disabled={busy} onClick={removeEmail}>{t('Удалить', 'Remove')}</button>}</div><p className="mi-muted">{t('На адрес придёт письмо; вход по почте заработает после подтверждения.', 'We will e-mail a link; signing in by e-mail works once you confirm it.')}</p></form>}
    {inCabinet && <div className="mi-actions"><button className="mi-button mi-secondary" onClick={() => leave('/logout')}><LogOut size={16}/> {t('Выйти', 'Sign out')}</button><button className="mi-button mi-secondary" onClick={() => leave('/logout-all')}>{t('Выйти на всех устройствах', 'Sign out everywhere')}</button></div>}
  </section>;
}
