import { Check } from 'lucide-react';
import { formatMoney } from '../shared/api';
import { haptic } from './polish';
import { PayIcon, payCategory } from './payCategories';
import type { Provider } from './types';
import { useSession } from './ui';

/** Payment method picker: wallet balance first, then every provider with a category icon. */
export function PayMethods({ value, onChange, providers, walletOn, canUseBalance, balanceKop, disabled }: {
  value: string; onChange(key: string): void; providers: Provider[]; walletOn: boolean; canUseBalance: boolean; balanceKop: number; disabled?: boolean;
}) {
  const { t, me } = useSession();
  const row = (key: string, title: string, note: string | null, category: ReturnType<typeof payCategory>, off = false) => (
    <button type="button" role="radio" aria-checked={value === key} key={key || 'balance'} disabled={disabled || off} className={`mi-pay-method ${value === key ? 'selected' : ''}`} onClick={() => { haptic(); onChange(key); }}>
      <span className="mi-pay-icon"><PayIcon category={category}/></span>
      <span><strong>{title}</strong>{note && <small>{note}</small>}</span>
      {value === key && <Check size={17} className="mi-pay-check"/>}
    </button>
  );
  return <div className="mi-field"><span>{t('Способ оплаты', 'Payment method')}</span><div className="mi-pay-methods" role="radiogroup" aria-label={t('Способ оплаты', 'Payment method')}>
    {walletOn && row('', t('С баланса', 'From balance'), `${formatMoney(balanceKop, me.language)}${canUseBalance ? '' : t(' — недостаточно', ' — insufficient funds')}`, 'wallet', !canUseBalance)}
    {providers.map(item => row(item.key, item.title, null, payCategory(item)))}
  </div></div>;
}
