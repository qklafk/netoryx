import happ from './assets/apps/happ.png';
import hiddify from './assets/apps/hiddify.png';
import incy from './assets/apps/incy.png';
import shadowrocket from './assets/apps/shadowrocket.png';
import streisand from './assets/apps/streisand.png';
import v2rayng from './assets/apps/v2rayng.png';
import v2raytun from './assets/apps/v2raytun.png';

/** Client app key (see the backend app registry) -> bundled icon. */
export const APP_ICONS: Record<string, string> = { happ, hiddify, incy, shadowrocket, streisand, v2rayng, v2raytun };

export const appIcon = (key: string): string | undefined => APP_ICONS[key.toLowerCase()];

/** Platform of this device in the backend's naming (ios, android, macos, windows, linux), '' when unknown. */
export function detectPlatform(): string {
  const sdk = typeof window === 'undefined' ? undefined : window.Telegram?.WebApp;
  const reported = (sdk?.platform || '').toLowerCase();
  if (reported === 'ios' || reported === 'android' || reported === 'macos') return reported;
  const ua = typeof navigator === 'undefined' ? '' : navigator.userAgent.toLowerCase();
  if (/iphone|ipad|ipod/.test(ua)) return 'ios';
  if (/android/.test(ua)) return 'android';
  if (/mac os x|macintosh/.test(ua)) return 'macos';
  if (/windows/.test(ua)) return 'windows';
  if (/linux|x11/.test(ua)) return 'linux';
  return '';
}
