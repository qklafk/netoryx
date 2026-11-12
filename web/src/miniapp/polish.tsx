import type { ReactNode } from 'react';

/** Light tap feedback inside Telegram; silently does nothing elsewhere. */
export function haptic(kind: 'light' | 'success' | 'error' = 'light') {
  try {
    const feedback = window.Telegram?.WebApp?.HapticFeedback;
    if (!feedback) return;
    if (kind === 'light') feedback.impactOccurred('light'); else feedback.notificationOccurred(kind);
  } catch { /* haptics are optional */ }
}

export type SubTone = 'ok' | 'soon' | 'expired';
export const EXPIRING_SOON_DAYS = 3;
export function subTone(state: string, daysLeft: number, unlimited: boolean): SubTone {
  if (state !== 'active') return 'expired';
  return !unlimited && daysLeft <= EXPIRING_SOON_DAYS ? 'soon' : 'ok';
}

/** Share of the period that is left, 0..1 (unlimited -> full). */
export function remainingShare(daysLeft: number, periodDays: number, unlimited: boolean): number {
  if (unlimited) return 1;
  return Math.max(0, Math.min(1, daysLeft / Math.max(1, periodDays, daysLeft)));
}

export function RingProgress({ share, tone, label, caption }: { share: number; tone: SubTone; label: string; caption: string }) {
  const radius = 34; const circumference = 2 * Math.PI * radius;
  return <div className={`mi-ring mi-tone-${tone}`} role="img" aria-label={`${label} ${caption}`}>
    <svg viewBox="0 0 80 80" width="84" height="84"><circle className="mi-ring-track" cx="40" cy="40" r={radius}/><circle className="mi-ring-value" cx="40" cy="40" r={radius} strokeDasharray={circumference} strokeDashoffset={circumference * (1 - share)} transform="rotate(-90 40 40)"/></svg>
    <div><strong>{label}</strong><small>{caption}</small></div>
  </div>;
}

export function UsageBar({ used, limit }: { used: number | null; limit: number }) {
  if (!limit || used === null) return null;
  const share = Math.max(0, Math.min(1, used / (limit * 1024 ** 3)));
  const tone = share > 0.9 ? 'expired' : share > 0.75 ? 'soon' : 'ok';
  return <div className={`mi-usage mi-tone-${tone}`} role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(share * 100)}><span style={{ width: `${Math.max(2, share * 100)}%` }}/></div>;
}

/** Leading flag emoji (a pair of regional indicators) of a server name, and the name without it. */
export function splitFlag(name: string): { flag: string; text: string } {
  const match = /^\s*([\u{1F1E6}-\u{1F1FF}]{2})\s*/u.exec(name);
  return match ? { flag: match[1], text: name.slice(match[0].length) } : { flag: '', text: name };
}

export function SkeletonList({ rows = 3 }: { rows?: number }) {
  return <div className="mi-skeleton-list" aria-busy="true">{Array.from({ length: rows }, (_, i) => <div key={i} className="mi-skeleton mi-skeleton-card"><span className="mi-skeleton-line" style={{ width: '38%' }}/><span className="mi-skeleton-line" style={{ width: '72%' }}/><span className="mi-skeleton-line" style={{ width: '54%' }}/></div>)}</div>;
}

export function EmptyArt(): ReactNode {
  return <svg className="mi-empty-art" viewBox="0 0 160 120" width="160" height="120" aria-hidden="true">
    <ellipse cx="80" cy="106" rx="52" ry="7" className="mi-art-shadow"/>
    <path d="M80 12 L124 28 V58 C124 82 106 98 80 106 C54 98 36 82 36 58 V28 Z" className="mi-art-shield"/>
    <path d="M80 25 L112 37 V58 C112 76 99 88 80 95 C61 88 48 76 48 58 V37 Z" className="mi-art-inner"/>
    <circle cx="80" cy="55" r="9" className="mi-art-dot"/>
    <path d="M80 64 V76" className="mi-art-stroke"/>
    <circle cx="26" cy="30" r="3" className="mi-art-spark"/><circle cx="138" cy="22" r="2.5" className="mi-art-spark"/><circle cx="142" cy="74" r="3.5" className="mi-art-spark"/>
  </svg>;
}

export function SuccessCheck({ size = 76 }: { size?: number }) {
  return <svg className="mi-success-check" viewBox="0 0 52 52" width={size} height={size} aria-hidden="true"><circle className="mi-success-circle" cx="26" cy="26" r="23"/><path className="mi-success-tick" d="M15 27 L23 35 L38 18"/></svg>;
}
