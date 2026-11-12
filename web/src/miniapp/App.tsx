import { useCallback, useEffect, useState } from 'react';
import { Link, NavLink, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { ArrowLeft, ArrowUpRight, CheckCircle2, CircleAlert, Globe2, House, MoreHorizontal, ShieldCheck, ShoppingBag, Wallet } from 'lucide-react';
import { ApiError, isCabinet, useApi } from '../shared/api';
import { CabinetProblem } from './cabinet';
import { RTL_LANGUAGES, detectLanguage, translate } from './i18n';
import { SessionContext, Spinner } from './ui';
import type { Me, Subscription } from './types';
import { ConnectPage, DevicesPage, HomePage, MorePage, ProfilePage, PromoPage, ProxyPage, ReferralPage, ShopPage, SupportPage, WalletPage } from './pages';

function TelegramRequired({ error, retry }: { error?: string; retry?(): void }) {
  const lang = detectLanguage(navigator.language);
  const t = (ru: string, en: string) => translate(lang, ru, en);
  return <main className="mini-shell mi-gate"><div className="mi-brand-mark"><ShieldCheck size={32}/></div><span className="mi-eyebrow">VPN · MINI APP</span><h1>{t('Ваше личное подключение', 'Your private connection')}</h1><p>{error || t('Откройте VPN-бота в Telegram и нажмите кнопку Mini App, чтобы безопасно перейти к своим подпискам.', 'Open your VPN bot in Telegram and tap the Mini App button to access your subscriptions securely.')}</p>{retry && <button className="mi-button" onClick={retry}>{t('Попробовать снова', 'Try again')}</button>}<a className="mi-button mi-secondary" href="https://telegram.org/" target="_blank" rel="noreferrer">{t('Открыть Telegram', 'Open Telegram')}<ArrowUpRight size={18}/></a></main>;
}

export function App() {
  const sdk = window.Telegram?.WebApp;
  const cabinet = isCabinet();
  const hasContext = Boolean(sdk?.initData) || cabinet;
  const profile = useApi<Me>(hasContext ? '/me' : null, 'app');
  const subscriptions = useApi<{ items: Subscription[] }>(profile.data?.features.subscription ? '/subscriptions' : null, 'app');
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [toast, setToast] = useState<{ message: string; error: boolean } | null>(null);
  const location = useLocation(); const navigate = useNavigate();
  const notify = useCallback((message: string, error = false) => { setToast({ message, error }); sdk?.HapticFeedback?.notificationOccurred(error ? 'error' : 'success'); }, [sdk]);
  const refresh = useCallback(() => { profile.reload(); subscriptions.reload(); }, [profile.reload, subscriptions.reload]);
  useEffect(() => { const scheme = sdk?.colorScheme || (window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark'); document.documentElement.dataset.scheme = scheme; }, [sdk]);
  useEffect(() => { sdk?.ready(); sdk?.expand(); const bar = sdk?.colorScheme === 'light' ? '#eef2f5' : '#101d27'; sdk?.setHeaderColor(bar); sdk?.setBackgroundColor(bar); }, [sdk]);
  useEffect(() => { if (!toast) return; const timer = setTimeout(() => setToast(null), 5000); return () => clearTimeout(timer); }, [toast]);
  useEffect(() => { window.scrollTo({ top: 0 }); const back = () => navigate(-1); if (location.pathname === '/') sdk?.BackButton?.hide(); else sdk?.BackButton?.show(); sdk?.BackButton?.onClick(back); return () => sdk?.BackButton?.offClick(back); }, [location.pathname, navigate, sdk]);
  useEffect(() => { if (!profile.data) return; const root = document.documentElement; root.lang = profile.data.language; root.dir = RTL_LANGUAGES.has(profile.data.language) ? 'rtl' : 'ltr'; }, [profile.data?.language]);
  // The page title follows the operator's brand name (BRAND_NAME), not a fixed product name.
  useEffect(() => { if (profile.data?.brand) document.title = profile.data.brand; }, [profile.data?.brand]);
  useEffect(() => { if (cabinet && profile.error instanceof ApiError && profile.error.status === 401) window.location.assign('/cabinet/'); }, [cabinet, profile.error]);
  if (!hasContext) return <TelegramRequired/>;
  if (profile.loading && !profile.data) return <div className="mini-shell mi-gate"><Spinner/><p>Загружаем профиль / Loading profile</p></div>;
  if (cabinet && (profile.error || !profile.data)) return <CabinetProblem message={profile.error?.message} retry={profile.reload}/>;
  if (profile.error || !profile.data) return <TelegramRequired error={profile.error?.message} retry={profile.reload}/>;
  const me = profile.data; const subs = subscriptions.data?.items || [];
  const selected = subs.find(sub => sub.id === selectedId) || subs[0] || null;
  const t = (ru: string, en: string) => translate(me.language, ru, en);
  const feature = (key: string) => Boolean(me.features[key]);
  const routes = [
    { path: '/', label: t('Главная', 'Home'), icon: House, on: true },
    { path: '/connect', label: t('Подключить', 'Connect'), icon: Globe2, on: feature('subscription') },
    { path: '/plans', label: t('Тарифы', 'Plans'), icon: ShoppingBag, on: me.mode === 'shop' && feature('plans') },
    { path: '/wallet', label: t('Баланс', 'Wallet'), icon: Wallet, on: feature('wallet') },
    { path: '/more', label: t('Ещё', 'More'), icon: MoreHorizontal, on: true },
  ].filter(item => item.on);
  const sectionTitle: Record<string, string> = { '/connect': t('Подключение', 'Connection'), '/plans': t('Тарифы', 'Plans'), '/wallet': t('Кошелёк', 'Wallet'), '/more': t('Ваш кабинет', 'Your account'), '/devices': t('Устройства', 'Devices'), '/referral': t('Пригласить друзей', 'Invite friends'), '/promo': t('Подарки и промокоды', 'Gifts & promo codes'), '/proxy': t('Прокси для Telegram', 'Telegram proxy'), '/support': t('Поддержка', 'Support'), '/profile': t('Профиль', 'Profile') };
  return <SessionContext.Provider value={{ me, subs, selected, select: setSelectedId, t, refresh, notify }}><div className="mini-shell"><header className="mi-header"><Link to="/" className="mi-wordmark">{me.brand_logo_url ? <img className="mi-logo" src={me.brand_logo_url} alt="" width={28} height={28} referrerPolicy="no-referrer" onError={event => { event.currentTarget.style.display = 'none'; }}/> : <ShieldCheck size={23}/>}<span>{me.brand}</span></Link><Link to="/profile" className="mi-avatar" aria-label={t('Открыть профиль', 'Open profile')}>{(me.first_name || me.username || 'V').slice(0, 1).toUpperCase()}</Link></header><main className="mi-main">{location.pathname !== '/' && <div className="mi-page-heading"><button className="mi-icon-button" onClick={() => navigate(-1)} aria-label={t('Назад', 'Back')}><ArrowLeft size={19}/></button><h1>{sectionTitle[location.pathname] || t('Поддержка', 'Support')}</h1></div>}{subscriptions.error && feature('subscription') && <div role="alert" className="mi-inline-error">{subscriptions.error.message}<button onClick={subscriptions.reload}>{t('Повторить', 'Retry')}</button></div>}<Routes><Route path="/" element={<HomePage loading={subscriptions.loading}/>}/><Route path="/connect" element={feature('subscription') ? <ConnectPage/> : <Navigate to="/" replace/>}/><Route path="/plans" element={feature('plans') && me.mode === 'shop' ? <ShopPage/> : <Navigate to="/" replace/>}/><Route path="/wallet" element={feature('wallet') ? <WalletPage/> : <Navigate to="/" replace/>}/><Route path="/devices" element={feature('subscription') ? <DevicesPage/> : <Navigate to="/" replace/>}/><Route path="/referral" element={feature('referral') ? <ReferralPage/> : <Navigate to="/" replace/>}/><Route path="/promo" element={feature('promo') || feature('gifts') ? <PromoPage/> : <Navigate to="/" replace/>}/><Route path="/proxy" element={feature('tg_proxy') ? <ProxyPage/> : <Navigate to="/" replace/>}/><Route path="/support/*" element={feature('tickets') ? <SupportPage/> : <Navigate to="/" replace/>}/><Route path="/more" element={<MorePage/>}/><Route path="/profile" element={<ProfilePage/>}/><Route path="*" element={<Navigate to="/" replace/>}/></Routes><footer className="mi-footer"><ShieldCheck size={12}/>{t('Ваш доступ. Под вашим контролем.', 'Your access. In your control.')}</footer></main><nav className="mi-bottom-nav" aria-label={t('Главная навигация', 'Main navigation')}>{routes.map(({ path, label, icon: Icon }) => <NavLink to={path} end={path === '/'} key={path} className={({ isActive }) => isActive ? 'active' : ''}><Icon size={21}/><span>{label}</span></NavLink>)}</nav>{toast && <div className={`mi-toast ${toast.error ? 'mi-toast-error' : ''}`} role={toast.error ? 'alert' : 'status'}>{toast.error ? <CircleAlert size={18}/> : <CheckCircle2 size={18}/>}<span>{toast.message}</span><button onClick={() => setToast(null)} aria-label={t('Закрыть', 'Dismiss')}>×</button></div>}</div></SessionContext.Provider>;
}
