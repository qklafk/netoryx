interface TelegramWebApp {
  initData: string; colorScheme: 'light' | 'dark'; platform: string;
  ready(): void; expand(): void; close(): void;
  openLink(url: string): void; openTelegramLink(url: string): void;
  openInvoice(url: string, callback?: (status: string) => void): void;
  setHeaderColor(color: string): void; setBackgroundColor(color: string): void;
  HapticFeedback?: { notificationOccurred(type: 'success' | 'error' | 'warning'): void; impactOccurred(style: string): void };
  BackButton?: { show(): void; hide(): void; onClick(callback: () => void): void; offClick(callback: () => void): void };
}
interface Window { Telegram?: { WebApp?: TelegramWebApp } }
