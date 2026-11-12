export interface Me {
  id: number; username: string | null; first_name: string | null; language: string; languages: string[];
  mode: 'shop' | 'personal'; brand: string; brand_logo_url?: string; bot_username: string | null; support_url: string | null;
  balance_kop: number; partner_balance_kop: number; has_subscription: boolean; trial_available: boolean;
  trial_days: number; unread_tickets: number; features: Record<string, boolean>;
}
export interface Subscription {
  id: number; label: string; exists: boolean; state: 'active' | 'expired' | 'disabled' | 'none';
  expires_at: string | null; days_left: number; unlimited: boolean; plan: { id: number; title: string; duration_days: number } | null;
  device_limit: number; traffic_limit_gb: number; traffic_used_bytes: number | null; auto_renew: boolean;
  auto_renew_available: boolean; is_trial: boolean; is_complimentary: boolean; stars_recurring: boolean;
}
export interface ClientApp {
  key: string; title: string; platforms: string[]; downloads: { platform: string; label: string; url: string }[];
  deeplink: string; open_url: string; supports_crypto_link: boolean;
}
export interface Connect {
  state: string; sub_url: string; whitelist_url: string | null; qr: string | null;
  happ_crypt_link: string | null; incy_crypt_link: string | null; platforms: string[]; platform: string | null;
  apps: ClientApp[];
  servers?: { id: number | string; name: string; subscription_url: string; profiles: { name: string; uri: string; qr?: string | null }[] }[];
}
export interface Plan { id: number; title: string; duration_days: number; price_kop: number; monthly_price_kop: number; discount_percent: number; price_stars: number; gift_price_kop: number; gift_price_stars: number; device_limit: number; traffic_limit_gb: number; includes_tg_proxy: boolean; hidden: boolean }
export interface Provider { key: string; title: string; min_amount_kop: number; recurring: boolean; kind?: 'redirect' | 'stars' | 'onchain' | 'manual'; save_card?: boolean }
export interface Catalog { plans: Plan[]; balance_kop: number; providers: Provider[]; gifts_available: boolean; topup: { min_kop: number; max_kop: number; quick_amounts_kop: number[] } }
export interface ChainTransfer { network: string; currency: string; address: string; amount: string; comment: string | null; wallet_url: string | null; deep_link: string | null; notes: string[] }
export interface Payment { id: string; status: string; amount_kop: number; url: string | null; invoice_link: string | null; provider: string; created_at: string; provider_amount?: string | null; provider_currency?: string | null; save_card?: boolean; transfer?: ChainTransfer | null; manual?: { details: string; receipt_sent: boolean } | null }
export interface SavedCards { enabled: boolean; items: { id: number; provider: string; title: string; created_at: string; last_used_at: string | null }[] }
export interface BuyResult { result: 'purchased' | 'gift' | 'payment'; expires_at?: string | null; gift?: { code: string; link: string | null; plan: { title: string } } | null; payment?: Payment | null; balance_kop: number }
export interface WalletData { balance_kop: number; partner_balance_kop: number; items: { id: number; kind: string; account: string; amount_kop: number; title: string; created_at: string }[]; next_before_id: number | null }
export interface Devices { device_limit: number; servers: { panel_id: number; server: string; supported: boolean; can_delete_one: boolean; unavailable: boolean; devices: { id: string; platform: string | null; model: string | null; os_version: string | null; user_agent: string | null; ip: string | null; created_at: string | null }[] }[]; last_reset_at: string | null; next_reset_at: string | null }
export interface Referral { mode: 'days' | 'balance' | 'partner'; code: string; link: string | null; share_text: string; qr: string | null; invited: number; paid_invited: number; earned_kop: number; earned_days: number; invitee_bonus_kop: number; inviter_bonus_kop: number; invitee_bonus_days: number; inviter_bonus_days: number; levels: { level: number; percent: number; users: number; paid_users: number; earned_kop: number; earned_days: number }[]; partner: { balance_kop: number; pending_withdrawal_kop: number; withdrawn_kop: number; withdrawals_available: boolean; min_withdrawal_kop: number; methods: string[]; withdrawals: { id: number; amount_kop: number; method: string; status: string; created_at: string; comment: string | null }[] } | null }
export interface SocksData { title: string; ready: boolean; link: { server: string; port: number; user: string; password: string; tme_url: string; tg_url: string } | null; qr: string | null }
export interface ProxyData { allowed: boolean; reason: string; limited: boolean; socks: SocksData[]; servers: { title: string; ready: boolean; links: { server: string; port: number; tme_url: string; tg_url: string }[]; web_url: string | null; qr: string | null }[] }
export interface Ticket { id: number; subject: string; status: string; created_at: string; updated_at: string }
export interface TicketView { ticket: Ticket; messages: { id: number; from_support: boolean; text: string | null; has_attachment: boolean; created_at: string }[] }
