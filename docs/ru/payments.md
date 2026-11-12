# Платежи

English: [../payments.md](../payments.md)

Netoryx зачисляет деньги на кошелёк, а подписки покупаются с кошелька. Каждая платёжная система включается и настраивается в
веб-админке (**Settings**, группы «Payments: ...») или переменными `*_ENABLED` и переменными с учётными данными в `.env`
(см. `.env.example`). Включённые системы появляются кнопками в боте, Mini App и веб-кабинете.

Правила обращения с деньгами, общие для всех платёжных систем:

- Запись о платеже создаётся **до того**, как клиент увидит ссылку.
- Платёж зачисляется **один раз**: вебхуком платёжной системы или фоновой сверкой, которая опрашивает открытые платежи каждые
  `PAYMENT_RECONCILE_INTERVAL_SEC` (30 с), смотря что случится раньше. Вебхук и опрос вместе всё равно дают одно зачисление.
- Телу вебхука никогда не верят само по себе: нужна корректная подпись или IP источника, а там, где у платёжной системы есть
  API статуса, платёж перед зачислением запрашивается повторно.
- Открытые платежи истекают через `PAYMENT_MAX_PENDING_HOURS` (24).
- Секреты не попадают в логи и журнал аудита.

## URL вебхуков

Вебхуки приходят на `https://<domain>/webhooks/<key>`, где `<key>` указан в таблице ниже. Домен должен быть публичным
HTTPS-адресом (`PUBLIC_BASE_URL`). Если в таблице указано «automatic», URL передаётся с каждым счётом, и в кабинете платёжной
системы ничего настраивать не нужно. Если указано «cabinet», вставьте URL туда.

Валюта: магазин работает в рублях. Платёжные системы, берущие деньги в другой валюте (Stripe, PayPal, криптовалютные),
пересчитывают по курсу, который вы задаёте, а для TON и USDT - при желании по автоматическому курсу (`CRYPTO_RATE_AUTO`, из
CoinGecko, кэшируется на `CRYPTO_RATE_CACHE_SEC`, запасной вариант - ваш фиксированный `*_RATE_RUB`). Кошелёк всегда получает
сумму в рублях.

| Платёжная система | Ключ | Что настроить | Вебхук |
|---|---|---|---|
| Telegram Stars | `stars` | `STARS_ENABLED`, `STARS_RUB_PER_STAR` (рублей на кошелёк за одну звезду, по умолчанию 1,5). Пожертвования: `DONATIONS_ENABLED`. Оплата тарифов может использовать встроенную 30-дневную подписку Stars. | нет (обновления бота) |
| CryptoBot | `cryptobot` | `CRYPTOBOT_TOKEN` (токен Crypto Pay API), `CRYPTOBOT_ACCEPTED_ASSETS`, `CRYPTOBOT_TESTNET` | cabinet (в настройках приложения Crypto Pay) |
| Heleket | `heleket` | `HELEKET_MERCHANT_ID`, `HELEKET_API_KEY`, настройки валюты | automatic |
| NOWPayments | `nowpayments` | `NOWPAYMENTS_API_KEY`, `NOWPAYMENTS_IPN_SECRET`, `NOWPAYMENTS_PAY_CURRENCY` | automatic |
| Plisio | `plisio` | `PLISIO_API_KEY`, необязательно `PLISIO_CURRENCY` | automatic |
| TON (напрямую) | `ton` | `TON_WALLET_ADDRESS`, необязательно `TON_API_KEY`, `TON_RATE_RUB` | нет (опрашивает блокчейн) |
| USDT TRC-20 (напрямую) | `usdt_trc20` | `USDT_TRC20_ADDRESS`, необязательно `USDT_TRC20_API_KEY`, `USDT_TRC20_RATE_RUB` | нет (опрашивает TronGrid) |
| YooKassa | `yookassa` | `YOOKASSA_SHOP_ID`, `YOOKASSA_SECRET_KEY`, необязательно чеки 54-ФЗ (`YOOKASSA_RECEIPT_*`, `YOOKASSA_VAT_CODE`) | cabinet; проверка по IP источника (`YOOKASSA_CHECK_IP`) |
| YooMoney | `yoomoney` | `YOOMONEY_WALLET`, `YOOMONEY_NOTIFICATION_SECRET`, `YOOMONEY_PAYMENT_TYPE` (`AC` карта, `PC` кошелёк) | cabinet (HTTP-уведомления) |
| FreeKassa | `freekassa` | `FREEKASSA_SHOP_ID`, `FREEKASSA_SECRET1`, `FREEKASSA_SECRET2`, необязательно `FREEKASSA_API_KEY` | cabinet; метод уведомления (GET или POST) задаётся в настройках магазина там же, принимаются оба |
| Robokassa | `robokassa` | `ROBOKASSA_MERCHANT_LOGIN`, `ROBOKASSA_PASSWORD1`, `ROBOKASSA_PASSWORD2`, `ROBOKASSA_HASH_ALGORITHM` (`md5` или `sha256`, должен совпадать с кабинетом), `ROBOKASSA_TEST_MODE` | cabinet (ResultURL) |
| Platega | `platega` | `PLATEGA_MERCHANT_ID`, `PLATEGA_SECRET`, необязательно фиксированный `PLATEGA_PAYMENT_METHOD` | cabinet |
| WATA | `wata` | `WATA_TOKEN` (токен доступа терминала), `WATA_SANDBOX` | cabinet (вебхук терминала); публичный ключ загружается или закрепляется в `WATA_PUBLIC_KEY` |
| RollyPay | `rollypay` | `ROLLYPAY_API_KEY`, `ROLLYPAY_SIGNING_SECRET`, необязательно `ROLLYPAY_PAYMENT_METHOD` (`sbp` или `card`) | cabinet: `callback_url` кассы (колбэки проверяются по HMAC-SHA256, статус запрашивается повторно). Не проверялась на реальных деньгах |
| cisPay | `cispay` | `CISPAY_SHOP_ID`, `CISPAY_API_KEY` (им же подписываются вебхуки), необязательно `CISPAY_PAYMENT_METHOD` (`card` или `sbp`) | cabinet: `webhook_url` магазина (HMAC-SHA256 тела; статус запрашивается повторно). Тестовые платежи никогда не зачисляются |
| TabPay | `tabpay` | `TABPAY_API_KEY`, `TABPAY_WEBHOOK_SECRET` (отдельный секрет подписи), необязательно `TABPAY_PAYMENT_METHOD` | cabinet: вкладка Webhook магазина (подпись с меткой времени; статус запрашивается повторно). Тестовые платежи никогда не зачисляются |
| SeverPay | `severpay` | `SEVERPAY_MID`, `SEVERPAY_TOKEN`, `SEVERPAY_CLIENT_EMAIL` | cabinet: `WEBHOOK_URL` терминала (HMAC-SHA256 от JSON; статус запрашивается повторно) |
| Cashera | `cashera` | `CASHERA_API_KEY`, `CASHERA_SECRET`, необязательно `CASHERA_PAYMENT_METHOD` | включите уведомления в кабинете; URL передаётся с каждым счётом. Вебхуки несут ключ и секрет в заголовках, поэтому статус всегда запрашивается повторно. Расчёт в USD |
| ParityPay | `paritypay` | `PARITYPAY_SHOP_ID`, `PARITYPAY_SECRET_KEY`, `PARITYPAY_SECRET_KEY2` (подписывает вебхуки), необязательно `PARITYPAY_SERVICE` | URL передаётся с каждым счётом |
| Aurapay | `aurapay` | `AURAPAY_SHOP_ID`, `AURAPAY_API_KEY`, `AURAPAY_SECRET_KEY2` (подписывает вебхуки), необязательно `AURAPAY_SERVICE` | URL передаётся с каждым счётом |
| Antilopay | `antilopay` | `ANTILOPAY_PROJECT_ID`, `ANTILOPAY_SECRET_ID`, `ANTILOPAY_SECRET_KEY` (ваш закрытый ключ RSA), `ANTILOPAY_CALLBACK_KEY` (их открытый ключ) | cabinet (URL колбэка) |
| CloudPayments | `cloudpayments` | `CLOUDPAYMENTS_PUBLIC_ID`, `CLOUDPAYMENTS_API_SECRET` | cabinet: укажите один и тот же URL для уведомлений Check, Pay и Fail |
| Tribute | `tribute` | `TRIBUTE_API_KEY` (им же подписываются вебхуки), необязательно `TRIBUTE_SHOP_ID` | cabinet (настройки API-ключа) |
| lava.top | `lavatop` | `LAVATOP_API_KEY`, `LAVATOP_OFFER_ID`, `LAVATOP_WEBHOOK_SECRET`, `LAVATOP_PAYMENT_METHOD` | cabinet; аутентификация по `X-Api-Key` или Basic |
| Stripe | `stripe` | `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_CURRENCY`, `STRIPE_RUB_PER_UNIT` | cabinet: события `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `checkout.session.async_payment_failed`, `checkout.session.expired` |
| PayPal | `paypal` | `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_WEBHOOK_ID`, `PAYPAL_CURRENCY`, `PAYPAL_RUB_PER_UNIT`, `PAYPAL_SANDBOX` | cabinet: события `CHECKOUT.ORDER.APPROVED`, `PAYMENT.CAPTURE.*` |
| Ручной перевод на карту / СБП | `manual` | `MANUAL_TRANSFER_ENABLED`, `MANUAL_TRANSFER_DETAILS` (показываются покупателю) | нет: покупатель присылает чек, а администратор платежей подтверждает его |
| Custom HTTP | `custom` | `CUSTOM_*`, описаны в `.env.example` и в админке | automatic |

Если проверка подписи или IP источника выполняется для платёжной системы, стоящей за обратным прокси, приложение должно видеть
настоящий адрес клиента; Docker-стек уже передаёт его через Caddy.

## О чём стоит знать

- **Ничто из этого не проверялось на реальных деньгах.** Платёжные системы реализованы по документации провайдеров и
  проверены на имитациях и подписанных образцах. Сделайте небольшой тестовый платёж через каждую включаемую систему и
  проверьте зачисление.
- **lava.top** требует продукт с *динамической ценой*; укажите его идентификатор оффера в `LAVATOP_OFFER_ID`.
- **Прямой USDT** определяет платежи по сумме: каждый открытый счёт получает уникальную долю цента, а покупатель должен
  отправить ровно показанную сумму. Недоплата автоматически не зачисляется, она остаётся в ожидании, пока решит
  администратор. Прямой TON определяет платежи по тексту комментария, который подставляет ссылка `ton://`.
- **Stripe и PayPal** используют ваш фиксированный курс, поэтому изменение курса валюты меняет вашу маржу, пока вы не
  обновите курс.
- **YooKassa** больше не обслуживает самозанятых; убедитесь, что ваш юридический статус поддерживается, прежде чем на неё
  рассчитывать. Heleket и некоторые другие криптосервисы могут быть проблематичны для продавцов в подсанкционных
  юрисдикциях: решение за вами.
- **YooMoney** нельзя опрашивать без OAuth, поэтому проверенному уведомлению доверяют как есть; второй перевод по той же ссылке
  зачисляется как отдельное пополнение.
- **Platega** не имеет API рекуррентных платежей, поэтому сохранённые карты работают только с YooKassa и CloudPayments.
- **Системы без вебхуков** (TON, USDT, Stars, ручной перевод) полагаются на сверку и бота, поэтому следите, чтобы приложение
  работало непрерывно.

## Сохранённые карты и автопродление

Автопродление обычно берёт цену с кошелька. При `SAVED_CARDS_ENABLED=true` клиент может при оплате согласиться сохранить карту
(автоплатежи YooKassa, токены CloudPayments); если на балансе не хватает средств в момент продления, недостающая сумма
списывается с карты один раз за период. Рекуррентные платежи для вашего магазина должен заранее включить провайдер.

## Чеки «Мой налог»

Самозанятые продавцы в России могут автоматически регистрировать чек для каждого платежа.
См. [npd-receipts.md](npd-receipts.md).

## Проверка платежа

1. Включите одну платёжную систему в админке и, если у неё есть песочница или тестовый режим, включите его.
2. В боте откройте кошелёк, пополните на минимальную сумму, оплатите.
3. В админке раздел **Payments** показывает, как платёж переходит из ожидания в успешный, и запись в журнале. Если он остаётся в
   ожидании, проверьте URL вебхука в кабинете провайдера и `netoryx logs app` на строку «webhook ... rejected».
