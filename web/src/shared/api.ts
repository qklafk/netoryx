import { useCallback, useEffect, useState } from 'react';

export type Scope = 'admin' | 'app' | 'cabinet';
let csrf = '';
let cabinetCsrf = '';
export const setAdminCsrf = (value: string) => { csrf = value; };
// The web cabinet is the Mini App interface served at /cabinet; it signs in with a cookie instead of Telegram initData.
export const isCabinet = () => location.pathname === '/cabinet' || location.pathname.startsWith('/cabinet/');
export const setCabinetCsrf = (value: string) => { cabinetCsrf = value; };

let onAdminUnauthorized: (() => void) | null = null;
/** Called when an admin request is rejected with 401 (the session expired or was revoked). */
export const setAdminUnauthorizedHandler = (handler: (() => void) | null) => { onAdminUnauthorized = handler; };

/** Validation errors name the failing fields in `details`; add the first ones to the message. */
export function describeError(body: any, fallback: string): string {
  const message = (typeof body?.error === 'object' && body.error !== null ? body.error.message : body?.message) || body?.detail || fallback;
  const details = Array.isArray(body?.details) ? body.details : [];
  const fields = details.slice(0, 3).map((d: any) => `${Array.isArray(d?.loc) ? d.loc.filter((p: string) => p !== 'body').join('.') : ''}: ${d?.msg ?? ''}`.trim()).filter(Boolean);
  return fields.length ? `${message} (${fields.join('; ')})` : message;
}

/** A short description when a successful response reports partial failures (failed counters or error lists), else null. */
export function resultProblem(result: any): string | null {
  if (!result || typeof result !== 'object') return null;
  const errors = Array.isArray(result.errors) ? result.errors.filter((e: unknown) => typeof e === 'string' && e) : [];
  const failed = typeof result.failed === 'number' ? result.failed : 0;
  if (!errors.length && failed <= 0) return null;
  return errors.length ? String(errors[0]) : `${failed} failed`;
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}

export async function request<T>(scope: Scope, path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set('Accept', 'application/json');
  if (init.body && !(init.body instanceof FormData) && !(init.body instanceof Blob)) headers.set('Content-Type', 'application/json');
  if (scope === 'admin' && csrf && !['GET', 'HEAD'].includes(init.method || 'GET')) headers.set('X-CSRF-Token', csrf);
  const mutating = !['GET', 'HEAD'].includes(init.method || 'GET');
  if (scope === 'app') {
    const initData = window.Telegram?.WebApp?.initData;
    if (initData) headers.set('Authorization', `tma ${initData}`);
    else if (cabinetCsrf && mutating) headers.set('X-CSRF-Token', cabinetCsrf);
  }
  if (scope === 'cabinet' && cabinetCsrf && mutating) headers.set('X-CSRF-Token', cabinetCsrf);
  const prefix = scope === 'cabinet' ? '/api/cabinet/auth' : `/api/${scope}`;
  const response = await fetch(`${prefix}${path}`, { ...init, headers, credentials: 'same-origin' });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    // The API uses both {error: {code, message}} and {error: "code", message}
    // depending on the route. Preserve the useful message in either shape.
    const error = typeof body.error === 'object' && body.error !== null ? body.error : body;
    const code = typeof body.error === 'string' ? body.error : error.code;
    if (response.status === 401 && scope === 'admin' && !path.startsWith('/auth/')) onAdminUnauthorized?.();
    throw new ApiError(response.status, code || 'request_failed', describeError(body, `HTTP ${response.status}`));
  }
  return response.status === 204 ? undefined as T : response.json();
}

export const adminApi = <T = any>(path: string, init?: RequestInit) => request<T>('admin', path, init);
export const cabinetApi = <T = any>(path: string, init?: RequestInit) => request<T>('cabinet', path, init);
export const appApi = <T = any>(path: string, init?: RequestInit) => request<T>('app', path, init);
export const mutate = <T = any>(scope: Scope, path: string, body: unknown = {}, method = 'POST') => request<T>(scope, path, { method, body: JSON.stringify(body) });

export function useApi<T = any>(path: string | null, scope: Scope = 'admin') {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(Boolean(path));
  const [revision, setRevision] = useState(0);
  const reload = useCallback(() => setRevision(n => n + 1), []);
  useEffect(() => {
    if (!path) { setLoading(false); return; }
    const controller = new AbortController();
    setLoading(true); setError(null);
    request<T>(scope, path, { signal: controller.signal }).then(setData).catch(e => { if (e.name !== 'AbortError') setError(e); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [path, scope, revision]);
  return { data, setData, error, loading, reload };
}

export const requestId = () => crypto.randomUUID().replaceAll('-', '');
export const formatMoney = (kop: number = 0, locale = 'ru') => new Intl.NumberFormat(locale, { style: 'currency', currency: 'RUB', maximumFractionDigits: kop % 100 ? 2 : 0 }).format(kop / 100);
export const formatDate = (date?: string | null, locale = 'ru') => date ? new Intl.DateTimeFormat(locale, { day: 'numeric', month: 'short', year: 'numeric' }).format(new Date(date)) : '—';
export const formatBytes = (bytes?: number | null) => { if (bytes == null) return '—'; if (!bytes) return '0 B'; const n = Math.min(4, Math.floor(Math.log(Math.max(1, bytes)) / Math.log(1024))); return `${(bytes / 1024 ** n).toFixed(n ? 1 : 0)} ${['B', 'KB', 'MB', 'GB', 'TB'][n]}`; };
export async function copyText(text: string) { await navigator.clipboard.writeText(text); }
export function safeExternalUrl(value: string): string | null { try { const url = new URL(value); return ['http:', 'https:', 'tg:', 'happ:', 'incy:', 'v2raytun:', 'hiddify:'].includes(url.protocol) ? value : null; } catch { return null; } }
