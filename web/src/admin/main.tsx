import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter, Link, NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { Activity, ArrowUpRight, Bell, BookOpen, Boxes, Check, ChevronDown, CircleHelp, CreditCard, DatabaseBackup, Gift, Globe2, LayoutDashboard, ListChecks, LogOut, Menu, MessageSquare, Megaphone, Network, PanelLeftClose, Search, Server, Settings2, ShieldCheck, SlidersHorizontal, Smartphone, Ticket, Users, Wallet, X } from 'lucide-react';
import { adminApi, mutate, setAdminCsrf, setAdminUnauthorizedHandler } from '../shared/api';
import { AdminContext, Loading, type Principal, useAdmin } from './ui';
import { Dashboard, UsersPage, SubscriptionsPage } from './overview';
import { PlansPage, PanelsPage, LocationsPage, EndpointsPage } from './infrastructure';
import { PaymentsPage, PromoPage, ReferralsPage, BroadcastsPage, TicketsPage, CampaignsPage } from './business';
import { SettingsPage, TextsPage, AdminsPage, BackupsPage, SystemPage, ModePage } from './operations';
import { ModuleManagerPage } from './modules';
import { MenuButtonsPage } from './menu';
import { WebhooksPage } from './webhooks';
import { TelegramOnlyPage } from './telegramOnly';
import { NpdReceiptsPage } from './npd';
import './styles.css';

const fragment = new URLSearchParams(window.location.hash.slice(1));
const loginToken = fragment.get('token');
if (loginToken) window.history.replaceState(null, '', window.location.pathname + window.location.search);
const initialAuth = loginToken ? mutate<Principal>('admin', '/auth/magic', { token: loginToken }) : adminApi<Principal>('/auth/me');

const navigation = [
  { group: ['ОБЗОР', 'OVERVIEW'], items: [
    ['/', 'Обзор', 'Overview', LayoutDashboard, 'view_dashboard'],
    ['/users', 'Пользователи', 'Users', Users, 'users_view'],
    ['/subscriptions', 'Подписки', 'Subscriptions', ListChecks, 'users_view'],
  ] },
  { group: ['ИНФРАСТРУКТУРА', 'INFRASTRUCTURE'], items: [
    ['/panels', 'Серверы', 'Servers', Server, 'panels'],
    ['/locations', 'Локации', 'Locations', Globe2, 'panels'],
    ['/endpoints', 'Подключения', 'Endpoints', Network, 'panels'],
    ['/telegram-only', 'Доступ к Telegram', 'Telegram access', Smartphone, 'panels'],
  ] },
  { group: ['МАГАЗИН', 'SHOP'], items: [
    ['/plans', 'Тарифы', 'Plans', Boxes, 'users_view'],
    ['/payments', 'Платежи', 'Payments', CreditCard, 'payments_view'],
    ['/npd-receipts', 'Чеки «Мой налог»', 'NPD receipts', CreditCard, 'payments_view'],
    ['/promo', 'Промокоды и подарки', 'Promo & gifts', Gift, 'promo'],
    ['/referral', 'Партнёрская программа', 'Referrals', Wallet, 'referral'],
    ['/campaigns', 'Кампании', 'Campaigns', ArrowUpRight, 'promo'],
  ] },
  { group: ['УПРАВЛЕНИЕ', 'MANAGEMENT'], items: [
    ['/broadcasts', 'Рассылки', 'Broadcasts', Megaphone, 'broadcasts'],
    ['/tickets', 'Поддержка', 'Support', Ticket, 'tickets'],
    ['/texts', 'Тексты', 'Texts', BookOpen, 'texts'],
    ['/menu', 'Главное меню', 'Main menu', Menu, 'view_dashboard'],
    ['/settings', 'Настройки', 'Settings', SlidersHorizontal, 'settings'],
    ['/admins', 'Администраторы', 'Administrators', ShieldCheck, 'admins'],
    ['/backups', 'Резервные копии', 'Backups', DatabaseBackup, 'backups'],
    ['/mode', 'Режим работы', 'Product mode', Settings2, 'settings'],
    ['/system', 'Система', 'System', Activity, 'system'],
    ['/modules', '\u041c\u043e\u0434\u0443\u043b\u0438', 'Modules', Boxes, 'system'],
    ['/webhooks', '\u0412\u0435\u0431\u0445\u0443\u043a\u0438', 'Webhooks', Bell, 'webhooks'],
  ] },
] as const;

function Login({ onLogin, initialError }: { onLogin: (p: Principal) => void; initialError: string }) {
  const [token, setToken] = useState(''); const [error, setError] = useState(initialError); const [busy, setBusy] = useState(false);
  return <div className="login-page"><div className="login-brand"><span className="brand-mark"><ShieldCheck size={24}/></span> VPN<span className="brand-word">CONTROL</span></div><section className="login-card"><span className="eyebrow">ПАНЕЛЬ УПРАВЛЕНИЯ / CONTROL PANEL</span><h1>Всё под контролем.</h1><p>Откройте одноразовую ссылку из команды <strong>/web</strong> в Telegram-боте. Или вставьте её ниже.</p><p className="muted">Open the one-time link from your bot’s /web command, or paste it here.</p><form onSubmit={async e => { e.preventDefault(); setBusy(true); setError(''); try { let value = token.trim(); if (value.includes('#')) value = new URLSearchParams(value.split('#')[1]).get('token') || ''; const principal = await mutate<Principal>('admin', '/auth/magic', { token: value }); setToken(''); onLogin(principal); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } }}><label className="field"><span>Одноразовая ссылка / One-time link</span><input autoComplete="off" type="password" required placeholder="https://…/admin/login#token=…" value={token} onChange={e => setToken(e.target.value)}/></label>{error && <div className="inline-error" role="alert">{error}</div>}<button className="btn primary login-submit" disabled={busy}>{busy ? '…' : 'Войти / Sign in'}<ArrowUpRight size={17}/></button></form><div className="login-note"><ShieldCheck size={17}/><span>Ссылка действует один раз. / The link can only be used once.</span></div></section><footer>VPN Control · Self-hosted, your rules.</footer></div>;
}

function Shell({ logout, locale, setLocale }: { logout: () => void; locale: string; setLocale: (s: string) => void }) {
  const { me, tr, can } = useAdmin(); const [open, setOpen] = useState(false); const location = useLocation();
  useEffect(() => { setOpen(false); }, [location.pathname]);
  const current = navigation.flatMap(g => [...g.items]).find(i => i[0] !== '/' && location.pathname.startsWith(i[0])) || navigation[0].items[0];
  const groups = navigation.map(g => ({ ...g, items: g.items.filter(i => can(i[4])) })).filter(g => g.items.length);
  return <div className="admin-shell">{open && <button className="sidebar-backdrop" aria-label="Close menu" onClick={() => setOpen(false)}/>}<aside className={`sidebar ${open ? 'open' : ''}`}><Link to="/" className="brand"><span className="brand-mark"><ShieldCheck size={22}/></span><span>VPN <b>CONTROL</b></span><span className="version-tag">1.0</span></Link><div className="workspace"><span className="workspace-icon"><Server size={17}/></span><div><strong>{tr('Мой VPN', 'My VPN')}</strong><span>{tr('Рабочее пространство', 'Workspace')}</span></div><ChevronDown size={15}/></div><nav aria-label={tr('Основная навигация', 'Main navigation')}>{groups.map(g => <div className="nav-group" key={g.group[0]}><span className="nav-group-title">{tr(g.group[0], g.group[1])}</span>{g.items.map(([path, ru, en, Icon]) => <NavLink key={path} to={path} end={path === '/'} className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}><Icon size={18}/><span>{tr(ru, en)}</span></NavLink>)}</div>)}</nav><div className="sidebar-bottom"><span className="live-dot"/><span>{tr('Собственная инфраструктура', 'Your own infrastructure')}</span><ShieldCheck size={15}/></div></aside><div className="main-area"><header className="topbar"><div className="breadcrumb"><button className="icon-btn mobile-menu" aria-label={tr('Открыть меню', 'Open menu')} onClick={() => setOpen(true)}><Menu size={21}/></button><span className="desktop-muted">{tr('Рабочее пространство', 'Workspace')}</span><span className="crumb-slash">/</span><strong>{tr(current[1], current[2])}</strong></div><div className="topbar-actions"><select aria-label="Language" className="language-select" value={locale} onChange={e => setLocale(e.target.value)}><option value="ru">RU</option><option value="en">EN</option></select><div className="topbar-divider"/><span className="avatar">{String(me.tg_id).slice(-2)}</span><div className="admin-identity"><strong>{tr('Администратор', 'Administrator')}</strong><span>{me.role} · {me.tg_id}</span></div><button className="icon-btn" title={tr('Выйти', 'Sign out')} aria-label={tr('Выйти', 'Sign out')} onClick={logout}><LogOut size={17}/></button></div></header><main className="content"><Routes><Route path="/" element={can('view_dashboard') ? <Dashboard/> : <Navigate to="/users" replace/>}/><Route path="/users/*" element={<UsersPage/>}/><Route path="/subscriptions" element={<SubscriptionsPage/>}/><Route path="/plans" element={<PlansPage/>}/><Route path="/panels" element={<PanelsPage/>}/><Route path="/locations" element={<LocationsPage/>}/><Route path="/endpoints" element={<EndpointsPage/>}/><Route path="/telegram-only" element={<TelegramOnlyPage/>}/><Route path="/payments" element={<PaymentsPage/>}/><Route path="/npd-receipts" element={<NpdReceiptsPage/>}/><Route path="/promo" element={<PromoPage/>}/><Route path="/referral" element={<ReferralsPage/>}/><Route path="/campaigns" element={<CampaignsPage/>}/><Route path="/broadcasts" element={<BroadcastsPage/>}/><Route path="/tickets" element={<TicketsPage/>}/><Route path="/texts" element={<TextsPage/>}/><Route path="/menu" element={<MenuButtonsPage/>}/><Route path="/settings" element={<SettingsPage/>}/><Route path="/admins" element={<AdminsPage/>}/><Route path="/backups" element={<BackupsPage/>}/><Route path="/system" element={<SystemPage/>}/><Route path="/modules" element={<ModuleManagerPage/>}/><Route path="/webhooks" element={<WebhooksPage/>}/><Route path="/mode" element={<ModePage/>}/><Route path="/login" element={<Navigate to="/" replace/>}/><Route path="*" element={<div className="not-found"><h1>404</h1><p>{tr('Страница не найдена', 'Page not found')}</p><Link to="/" className="btn primary">{tr('На главную', 'Back to overview')}</Link></div>}/></Routes></main><footer className="app-footer"><span>VPN CONTROL</span><span>{tr('Ваш сервис. Ваши правила.', 'Your service. Your rules.')}</span></footer></div></div>;
}

function App() {
  const [me, setMe] = useState<Principal | null>(null); const [loading, setLoading] = useState(true); const [error, setError] = useState(''); const [locale, setLocale] = useState(localStorage.getItem('vpn-admin-language') || 'ru'); const [toast, setToast] = useState<{ text: string; bad: boolean } | null>(null);
  useEffect(() => { initialAuth.then(p => { setAdminCsrf(p.csrf_token); setMe(p); }).catch(e => { if (loginToken || e.status !== 401) setError(e.message); }).finally(() => setLoading(false)); }, []);
  useEffect(() => {
    setAdminUnauthorizedHandler(() => { setAdminCsrf(''); setMe(null); setError(localStorage.getItem('vpn-admin-language') === 'en' ? 'Your session has expired. Sign in again.' : 'Сессия истекла. Войдите снова.'); });
    return () => setAdminUnauthorizedHandler(null);
  }, []);
  useEffect(() => { if (!toast) return; const timer = setTimeout(() => setToast(null), 6000); return () => clearTimeout(timer); }, [toast]);
  const tr = (ru: string, en: string) => locale === 'en' ? en : ru;
  if (loading) return <div className="boot-screen"><ShieldCheck size={36}/><span>VPN CONTROL</span></div>;
  if (!me) return <Login onLogin={p => { setAdminCsrf(p.csrf_token); setMe(p); }} initialError={error}/>;
  return <AdminContext.Provider value={{ me, locale, tr, can: p => me.permissions.includes(p), flash: (text, bad = false) => setToast({ text, bad }) }}><Shell locale={locale} setLocale={lang => { localStorage.setItem('vpn-admin-language', lang); setLocale(lang); }} logout={async () => { try { await mutate('admin', '/auth/logout'); setAdminCsrf(''); setMe(null); } catch (e) { setToast({ text: (e as Error).message, bad: true }); } }}/>{toast && <div className={`toast ${toast.bad ? 'bad' : ''}`} role={toast.bad ? 'alert' : 'status'}>{toast.bad ? <CircleHelp size={20}/> : <Check size={20}/>}<span>{toast.text}</span><button className="icon-btn" onClick={() => setToast(null)} aria-label="Close"><X size={17}/></button></div>}</AdminContext.Provider>;
}

createRoot(document.getElementById('root')!).render(<BrowserRouter basename="/admin"><App/></BrowserRouter>);
