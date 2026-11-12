import { useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { AlertTriangle, ArrowRight, ArrowUpRight, Check, CircleCheck, CreditCard, Gift, Plus, RefreshCw, ShieldCheck, Smartphone, Sparkles, Upload, Wallet, X } from 'lucide-react';
import { appApi, formatDate, formatMoney, mutate, requestId, useApi } from '../shared/api';
import type { BuyResult, Catalog, ChainTransfer, Payment, Plan, Provider } from './types';
import { PayMethods } from './payMethods';
import { PayIcon, payCategory } from './payCategories';
import { EmptyArt, SuccessCheck, haptic } from './polish';
import { CopyButton, Dialog, Empty, LinkBox, LoadState, openExternal, plainText, Spinner, useAction, useSession, type Translate } from './ui';

/** What happens after the buyer picks this provider (blockchain and manual transfers need extra steps). */
export function ProviderHint({ provider, t }: { provider: Provider | undefined; t: Translate }) {
  if (provider?.kind === 'onchain') return <p className="mi-muted">{t('Адрес и точную сумму в криптовалюте по текущему курсу покажем на следующем шаге.', 'The address and the exact crypto amount at the current rate are shown on the next step.')}</p>;
  if (provider?.kind === 'manual') return <p className="mi-muted">{t('Реквизиты для перевода покажем на следующем шаге, затем прикрепите чек.', 'Transfer details are shown on the next step; then attach the receipt.')}</p>;
  return null;
}

export function SaveCardOption({ provider, checked, disabled, change, t }: { provider: Provider | undefined; checked: boolean; disabled?: boolean; change(value: boolean): void; t: Translate }) {
  if (!provider?.save_card) return null;
  return <label className="mi-checkbox"><input type="checkbox" checked={checked} disabled={disabled} onChange={e => change(e.target.checked)}/><CreditCard size={17}/>{t('Сохранить карту для автопродления', 'Save the card for auto-renewal')}</label>;
}

const MAX_RECEIPT_BYTES = 10 * 1024 * 1024;

/** Amount in the provider's own currency (Stripe, PayPal, ...) when it is not rubles. */
function foreignAmount(payment: Payment) {
  const currency = (payment.provider_currency || '').toUpperCase();
  return payment.provider_amount && currency && !['RUB', 'XTR'].includes(currency) && !payment.transfer ? `${payment.provider_amount} ${currency}` : null;
}

function TransferDetails({ transfer }: { transfer: ChainTransfer }) {
  const { t } = useSession();
  return <div className="mi-transfer">
    <p className="mi-muted">{t(`Сеть: ${transfer.network}. Переведите точную сумму на адрес ниже.`, `Network: ${transfer.network}. Send the exact amount to the address below.`)}</p>
    {transfer.wallet_url && <button className="mi-button mi-full" onClick={() => openExternal(transfer.wallet_url!)}><Wallet size={17}/>{t('Открыть кошелёк', 'Open wallet')}<ArrowUpRight size={17}/></button>}
    <LinkBox value={transfer.address} label={t('Адрес', 'Address')}/>
    <LinkBox value={transfer.amount} label={t(`Сумма, ${transfer.currency}`, `Amount, ${transfer.currency}`)}/>
    {transfer.comment && <LinkBox value={transfer.comment} label={t('Комментарий (обязательно)', 'Comment (required)')}/>}
    {transfer.notes.map((note, index) => <div className="mi-tip" key={index}>{index === 0 ? <AlertTriangle size={18}/> : <ShieldCheck size={18}/>}<p>{plainText(note)}</p></div>)}
    {transfer.deep_link && <LinkBox value={transfer.deep_link} label={t('Ссылка для кошелька', 'Wallet link')}/>}
  </div>;
}

function ManualTransfer({ payment, uploaded }: { payment: Payment; uploaded(result: Payment): void }) {
  const { t, me, notify } = useSession(); const { busy, run } = useAction(); const input = useRef<HTMLInputElement>(null);
  const manual = payment.manual!;
  async function send(file: File | undefined) {
    if (!file) return;
    if (file.size > MAX_RECEIPT_BYTES) { notify(t('Файл больше 10 МБ. Сожмите фото или отправьте PDF поменьше.', 'The file is larger than 10 MB. Compress the photo or send a smaller PDF.'), true); return; }
    await run(() => appApi<Payment>(`/payments/receipt?payment_id=${encodeURIComponent(payment.id)}`, { method: 'POST', body: file, headers: { 'Content-Type': 'application/octet-stream' } }), result => { uploaded(result); notify(t('Чек отправлен на проверку', 'Receipt sent for review')); });
    if (input.current) input.current.value = '';
  }
  return <div className="mi-transfer">
    <p className="mi-muted">{t(`Переведите ${formatMoney(payment.amount_kop, me.language)} по реквизитам ниже, затем прикрепите фото или PDF чека. Администратор проверит перевод и подтвердит оплату.`, `Transfer ${formatMoney(payment.amount_kop, me.language)} using the details below, then attach a photo or PDF of the receipt. An administrator will verify the transfer and confirm the payment.`)}</p>
    {manual.details && <LinkBox value={manual.details} label={t('Реквизиты', 'Payment details')}/>}
    {manual.receipt_sent && <div role="status" className="mi-success-panel"><CircleCheck size={21}/><p>{t('Чек получен. Мы подтвердим оплату после проверки.', 'Receipt received. We will confirm the payment once it is verified.')}</p></div>}
    <input ref={input} type="file" accept="image/jpeg,image/png,application/pdf" hidden onChange={e => send(e.target.files?.[0])}/>
    <button className="mi-button mi-full" disabled={busy} onClick={() => input.current?.click()}>{busy ? <Spinner/> : <Upload size={17}/>} {manual.receipt_sent ? t('Заменить чек', 'Replace receipt') : t('Прикрепить чек', 'Attach receipt')}</button>
    <p className="mi-muted mi-center">{t('JPEG, PNG или PDF, до 10 МБ.', 'JPEG, PNG or PDF, up to 10 MB.')}</p>
  </div>;
}

export function PaymentDialog({ payment, close, complete }: { payment: Payment; close(): void; complete(): void }) {
  const { t, me, notify } = useSession();
  const resource = useApi<Payment>(`/payments/${payment.id}`, 'app');
  const [uploaded, setUploaded] = useState<Payment | null>(null);
  const current = uploaded && (!resource.data || !resource.data.manual?.receipt_sent) ? uploaded : resource.data || payment;
  const status = current.status;
  const isPending = ['created', 'pending'].includes(status);
  const transfer = isPending ? current.transfer : null;
  const manual = isPending ? current.manual : null;
  const foreign = foreignAmount(current);
  const reported = useRef(false);
  useEffect(() => { if (!isPending) return; const timer = setInterval(resource.reload, 6000); return () => clearInterval(timer); }, [isPending, resource.reload]);
  useEffect(() => { if (status === 'succeeded' && !reported.current) { reported.current = true; haptic('success'); complete(); notify(t('Оплата получена', 'Payment received')); } }, [status, complete, notify, t]);
  function pay() {
    if (current.invoice_link && window.Telegram?.WebApp) window.Telegram.WebApp.openInvoice(current.invoice_link, () => resource.reload());
    else if (current.invoice_link || current.url) openExternal(current.invoice_link || current.url!);
  }
  const headline = transfer ? `${transfer.amount} ${transfer.currency}` : formatMoney(current.amount_kop, me.language);
  return <Dialog title={t('Оплата', 'Payment')} close={close}><div className="mi-payment-summary"><div className={`mi-payment-icon ${status === 'succeeded' ? 'success' : ''}`}>{status === 'succeeded' ? <SuccessCheck/> : <PayIcon category={payCategory({ key: current.provider, title: '', kind: transfer ? 'onchain' : manual ? 'manual' : undefined })} size={34}/>}</div><strong>{headline}</strong>{transfer && <span className="mi-muted">≈ {formatMoney(current.amount_kop, me.language)}</span>}{foreign && <span className="mi-muted">{t(`К списанию у провайдера: ${foreign}`, `Charged by the provider: ${foreign}`)}</span>}<p>{status === 'succeeded' ? t('Платёж успешно зачислен', 'Your payment was received') : isPending ? (manual ? t('Ожидаем перевод и чек', 'Waiting for your transfer and receipt') : t('Ожидаем подтверждение платежа', 'Waiting for payment confirmation')) : status === 'canceled' ? t('Платёж отменён', 'Payment canceled') : status === 'expired' ? t('Срок оплаты истёк', 'Payment expired') : t('Возврат платежа', 'Payment refunded')}</p></div>{resource.error && <p className="mi-inline-error">{resource.error.message}</p>}{isPending && <>{transfer && <TransferDetails transfer={transfer}/>}{manual && <ManualTransfer payment={current} uploaded={setUploaded}/>}{!transfer && !manual && (current.url || current.invoice_link) && <button className="mi-button mi-full" onClick={pay}>{t('Перейти к оплате', 'Continue to payment')}<ArrowUpRight size={17}/></button>}{current.save_card && <div className="mi-tip"><CreditCard size={18}/><p>{t('Карта будет сохранена для автопродления: если при продлении не хватит баланса, недостающая сумма спишется с неё. Удалить карту можно в разделе «Баланс».', 'The card will be saved for auto-renewal: if your balance is short when the subscription renews, the missing amount is charged to it. You can remove the card under Balance.')}</p></div>}<button className="mi-button mi-secondary mi-full" disabled={resource.loading} onClick={resource.reload}>{resource.loading ? <Spinner/> : <RefreshCw size={17}/>} {manual ? t('Обновить статус', 'Refresh status') : t('Я оплатил — проверить', "I've paid — check")}</button><p className="mi-muted mi-center">{manual ? t('Статус обновится после проверки чека администратором.', 'The status updates after an administrator checks the receipt.') : t('После оплаты статус обновится автоматически. Можно вернуться в кабинет.', 'Your status updates automatically after payment. You can return to your account.')}</p></>}{!isPending && <button className="mi-button mi-full" onClick={close}>{t('Готово', 'Done')}</button>}</Dialog>;
}

export function ShopPage() {
  const { me, subs, selected, t, refresh, notify } = useSession();
  const catalog = useApi<Catalog>('/plans', 'app'); const [search] = useSearchParams();
  const [plan, setPlan] = useState<Plan | null>(null); const [target, setTarget] = useState(search.get('new') || !selected || selected.unlimited ? 'new' : String(selected.id));
  const [gift, setGift] = useState(false); const [provider, setProvider] = useState(''); const [recurring, setRecurring] = useState(false); const [label, setLabel] = useState(''); const [saveCard, setSaveCard] = useState(false);
  const [payment, setPayment] = useState<Payment | null>(null); const [giftResult, setGiftResult] = useState<BuyResult['gift']>(null);
  const { busy, run } = useAction(); const intent = useRef(requestId());
  useEffect(() => { intent.current = requestId(); }, [plan?.id, target, gift, provider, recurring, label, saveCard]);
  const availableProviders = me.features.payments ? catalog.data?.providers || [] : [];
  const selectedProvider = availableProviders.find(item => item.key === provider);
  const canUseBalance = Boolean(me.features.wallet) && (catalog.data?.balance_kop ?? 0) >= (plan ? (gift ? plan.gift_price_kop : plan.price_kop) : 0);
  const balanceSelected = !provider;
  // The multi-month plan with the lowest per-month price is highlighted when there is a choice.
  const popularId = (() => { const list = (catalog.data?.plans || []).filter(item => !item.hidden); const multi = list.filter(item => item.duration_days > 35); if (list.length < 2 || !multi.length) return null; return multi.reduce((best, item) => item.monthly_price_kop < best.monthly_price_kop ? item : best).id; })();
  async function buy() {
    if (!plan || balanceSelected && !canUseBalance) return;
    await run(() => mutate<BuyResult>('app', `/plans/${plan.id}/buy`, { request_id: intent.current, provider: provider || null, gift, recurring: Boolean(provider === 'stars' && recurring && !gift), save_card: Boolean(saveCard && selectedProvider?.save_card && !gift), ...(!gift ? { new_subscription: target === 'new', subscription_id: target === 'new' ? null : Number(target), label: target === 'new' ? label.trim() || null : null } : {}) }), result => {
      intent.current = requestId(); setPlan(null);
      if (result.result === 'payment' && result.payment) { setPayment(result.payment); }
      else if (result.result === 'gift' && result.gift) { setGiftResult(result.gift); refresh(); catalog.reload(); }
      else { refresh(); catalog.reload(); notify(t('Подписка активирована', 'Subscription activated')); }
    });
  }
  function openPlan(item: Plan) { setPlan(item); setProvider(me.features.wallet && (catalog.data?.balance_kop ?? 0) >= item.price_kop ? '' : availableProviders[0]?.key || ''); setRecurring(false); setGift(false); setSaveCard(false); }
  return <><p className="mi-intro">{t('Выберите срок. Остаток действующей подписки сохранится при продлении.', 'Choose a duration. Renewal keeps your remaining subscription days.')}</p><LoadState {...catalog} retry={catalog.reload}>{catalog.data && <>{me.features.wallet && <div className="mi-balance-strip"><Wallet size={18}/><span>{t('Ваш баланс', 'Your balance')}</span><strong>{formatMoney(catalog.data.balance_kop, me.language)}</strong></div>}<div className="mi-plan-grid">{catalog.data.plans.map(item => <article className={`mi-card mi-plan-card ${item.id === popularId ? 'mi-plan-featured' : ''}`} key={item.id}>{item.id === popularId && <span className="mi-plan-popular">{t('Популярный', 'Most popular')}</span>}{item.discount_percent > 0 && <span className="mi-plan-discount">−{item.discount_percent}%</span>}<span className="mi-eyebrow">{t(`${item.duration_days} ДНЕЙ`, `${item.duration_days} DAYS`)}</span><h2>{item.title}</h2><div className="mi-plan-price">{item.gift_price_kop > item.price_kop && <s className="mi-price-old">{formatMoney(item.gift_price_kop, me.language)}</s>}{formatMoney(item.price_kop, me.language)}<small>{t('за весь период', 'for the full period')}</small></div>{item.duration_days > 35 && <p className="mi-muted">≈ {formatMoney(item.monthly_price_kop, me.language)} / {t('месяц', 'month')}</p>}<ul className="mi-check-list"><li><Check size={16}/>{item.device_limit ? t(`До ${item.device_limit} устройств`, `Up to ${item.device_limit} devices`) : t('Без лимита устройств', 'Unlimited devices')}</li><li><Check size={16}/>{item.traffic_limit_gb ? `${item.traffic_limit_gb} GB` : t('Без лимита трафика', 'Unlimited traffic')}</li>{item.includes_tg_proxy && <li><Check size={16}/>{t('Прокси для Telegram', 'Telegram proxy')}</li>}</ul><button className="mi-button mi-full" onClick={() => openPlan(item)}>{t('Выбрать тариф', 'Choose plan')}<ArrowRight size={17}/></button></article>)}</div>{!catalog.data.plans.length && <Empty icon={<EmptyArt/>} title={t('Тарифы скоро появятся', 'Plans are coming soon')}><p>{t('Уточните доступные варианты у поддержки.', 'Ask support about available options.')}</p></Empty>}</>}</LoadState>{plan && catalog.data && <Dialog title={plan.title} close={() => !busy && setPlan(null)}><div className="mi-checkout-total"><span>{t('К оплате', 'Total')}</span><strong>{provider === 'stars' ? `⭐ ${gift ? plan.gift_price_stars : plan.price_stars}` : formatMoney(gift ? plan.gift_price_kop : plan.price_kop, me.language)}</strong></div>{catalog.data.gifts_available && <label className="mi-checkbox"><input type="checkbox" checked={gift} disabled={busy} onChange={e => setGift(e.target.checked)}/><Gift size={17}/>{t('Купить в подарок', 'Buy as a gift')}</label>}{!gift && <><label className="mi-field"><span>{t('Что оформляем?', 'What would you like to do?')}</span><select disabled={busy} value={target} onChange={e => setTarget(e.target.value)}><option value="new">{t('Новая подписка', 'New subscription')}</option>{subs.filter(sub => !sub.unlimited).map(sub => <option key={sub.id} value={sub.id}>{t('Продлить', 'Renew')}: {sub.label || sub.plan?.title || `#${sub.id}`}</option>)}</select></label>{target === 'new' && <label className="mi-field"><span>{t('Название (необязательно)', 'Name (optional)')}</span><input value={label} maxLength={64} placeholder={t('Например, рабочие устройства', 'For example, work devices')} disabled={busy} onChange={e => setLabel(e.target.value)}/></label>}</>}<PayMethods value={provider} disabled={busy} providers={availableProviders} walletOn={Boolean(me.features.wallet)} canUseBalance={canUseBalance} balanceKop={catalog.data.balance_kop} onChange={key => { setProvider(key); setRecurring(false); setSaveCard(false); }}/>{!canUseBalance && !availableProviders.length && <p className="mi-inline-error">{t('Сейчас нет доступных способов оплаты. Обратитесь в поддержку.', 'No payment methods are currently available. Contact support.')}</p>}{provider && provider !== 'stars' && <p className="mi-muted">{t('Оплатите недостающую сумму. После зачисления подписка активируется автоматически.', 'Pay the missing amount. Your subscription activates automatically once payment is received.')}{selectedProvider && selectedProvider.min_amount_kop > plan.price_kop && ` ${t('Минимум провайдера', 'Provider minimum')}: ${formatMoney(selectedProvider.min_amount_kop, me.language)}.`}</p>}<ProviderHint provider={selectedProvider} t={t}/>{!gift && <SaveCardOption provider={selectedProvider} checked={saveCard} disabled={busy} change={setSaveCard} t={t}/>}{provider === 'stars' && !gift && plan.duration_days === 30 && <label className="mi-checkbox"><input type="checkbox" checked={recurring} disabled={busy} onChange={e => setRecurring(e.target.checked)}/>{t('Продлевать через Stars каждые 30 дней', 'Renew with Stars every 30 days')}</label>}<button className="mi-button mi-full" disabled={busy || balanceSelected && !canUseBalance || !balanceSelected && !selectedProvider} onClick={buy}>{busy ? <Spinner/> : provider ? <CreditCard size={18}/> : <Wallet size={18}/>} {gift ? t('Купить подарок', 'Buy gift') : target === 'new' ? t('Оформить подписку', 'Create subscription') : t('Продлить подписку', 'Renew subscription')}</button><p className="mi-muted mi-center">{t('Проверьте тариф и выбранную подписку перед оплатой.', 'Check the plan and selected subscription before paying.')}</p></Dialog>}{payment && <PaymentDialog payment={payment} close={() => { setPayment(null); refresh(); catalog.reload(); }} complete={() => { refresh(); catalog.reload(); }}/>} {giftResult && <Dialog title={t('Подарок готов', 'Your gift is ready')} close={() => setGiftResult(null)}><div className="mi-payment-summary"><Gift size={40}/><h3>{giftResult.plan.title}</h3><p>{t('Передайте этот код или ссылку получателю.', 'Send this code or link to the recipient.')}</p></div><LinkBox value={giftResult.link || giftResult.code} label={t('Подарочная ссылка', 'Gift link')}/><button className="mi-button mi-full" onClick={() => setGiftResult(null)}>{t('Готово', 'Done')}</button></Dialog>}</>;
}
