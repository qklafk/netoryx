import { Bitcoin, CreditCard, Landmark, Star, Wallet, Zap } from 'lucide-react';
import type { ReactNode } from 'react';
import type { Provider } from './types';

export type PayCategory = 'sbp' | 'card' | 'crypto' | 'stars' | 'wallet' | 'manual';

/** Gateways that take cryptocurrency; any other unknown provider is treated as a card/online payment. */
const CRYPTO = new Set(['cryptobot', 'heleket', 'nowpayments', 'plisio', 'ton', 'usdt_trc20']);

export function payCategory(provider: Pick<Provider, 'key' | 'title' | 'kind'> | null | undefined): PayCategory {
  if (!provider) return 'wallet';
  const key = provider.key.toLowerCase();
  if (key === 'stars' || provider.kind === 'stars') return 'stars';
  if (provider.kind === 'manual' || key === 'manual') return 'manual';
  if (provider.kind === 'onchain' || CRYPTO.has(key)) return 'crypto';
  if (/сбп|sbp/i.test(provider.title)) return 'sbp';
  return 'card';
}

// Optional artwork: assets/pay/<category>.png replaces the built-in icon when the file exists.
const OVERRIDES = import.meta.glob('./assets/pay/*.png', { eager: true, query: '?url', import: 'default' }) as Record<string, string>;

const ICONS: Record<PayCategory, typeof CreditCard> = { sbp: Zap, card: CreditCard, crypto: Bitcoin, stars: Star, wallet: Wallet, manual: Landmark };

export function PayIcon({ category, size = 20 }: { category: PayCategory; size?: number }): ReactNode {
  const custom = OVERRIDES[`./assets/pay/${category}.png`];
  if (custom) return <img className="mi-pay-img" src={custom} width={size} height={size} alt=""/>;
  const Icon = ICONS[category];
  return <Icon size={size} aria-hidden="true"/>;
}
