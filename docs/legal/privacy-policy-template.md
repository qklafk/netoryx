# Privacy policy: template for Netoryx operators

> **Template, not legal advice.** Adapt it with a lawyer for your jurisdiction and for the countries of your customers
> before publishing. Replace every `[PLACEHOLDER]`, delete what does not apply, and keep the text true to how your
> installation is actually configured. The authors of Netoryx do not operate your service and bear no responsibility for
> the text you publish.

Publish the finished text at a public URL and set it as `PRIVACY_URL` (see [configuration.md](../configuration.md)).
Russian version: [ru/legal/privacy-policy-template.md](../ru/legal/privacy-policy-template.md).

---

# Privacy Policy of [SERVICE NAME]

Effective from: [DATE]. Version: [VERSION].

## 1. Who we are

The service [SERVICE NAME] is operated by [OPERATOR NAME / LEGAL ENTITY / SOLE PROPRIETOR, REGISTRATION NUMBER],
[ADDRESS / COUNTRY] (the "Operator"). Contact for privacy questions: [E-MAIL OR TELEGRAM CONTACT].

## 2. What data we process

| Data | Why | Notes |
|---|---|---|
| Telegram ID, username, display name, interface language | Identify your account, send messages, support | Received from Telegram when you start the bot |
| Referral link data (who invited you) | Referral program | Only if the program is on |
| Subscription data: plan, start and end dates, limits, subscription token, the accounts created on VPN servers | Provide and renew the service | |
| Payment records, wallet balance and transaction history, promo codes, withdrawal requests | Accept payments, keep accounts, resolve disputes | [Tax receipts are issued where required: DELETE IF NOT USED] |
| Support tickets and messages | Answer your requests | |
| E-mail address | Only if you choose e-mail login to the web cabinet | [DELETE IF CABINET E-MAIL LOGIN IS OFF] |
| Device identifier hash, device model and operating system as reported by your VPN app | Enforce the device limit of your plan | The identifier is stored only as a one-way HMAC hash, not in readable form. Not seen for [30] days: forgotten |
| Traffic counters (bytes used) | Enforce traffic limits | |
| Administrative audit records | Security and accountability | Records which staff member changed what |
| Backups of the above | Recovery after failures | Kept for [BACKUP RETENTION] |

## 3. What we do not collect

- We do **not** record the websites you visit or the content of your traffic.
- By default we do **not** store your IP address in the application, its logs or its database. [IF YOU CHANGED
  CLIENT_IP_LOGGING TO masked OR full, DESCRIBE IT HERE: what is stored, why and for how long.]
- The VPN server's access log is switched off. [CORRECT THIS IF YOU ENABLED IT.]
- We do **not** see or store your card number. Payments are processed by the payment provider you choose
  ([LIST PROVIDERS]), under their own privacy policies. We receive only the payment status, amount and a reference.

## 4. Legal basis [DELETE OR ADAPT TO YOUR LAW, e.g. GDPR]

We process data to perform our contract with you (providing the service), to comply with legal obligations (accounting,
taxes) and for our legitimate interests (security, abuse prevention). Where the law requires consent, we ask for it
before you use the service.

## 5. Who receives data

- Telegram, as the messaging platform you use.
- Payment providers you choose, to process your payment.
- Hosting and infrastructure providers that run our servers: [HOSTING PROVIDERS, COUNTRIES].
- Authorities, where required by a valid legal request under [JURISDICTION] law.
- We do not sell personal data and do not use it for advertising.

## 6. Retention

Account and subscription data: while your account exists and [PERIOD] after. Payment and accounting records: [PERIOD
REQUIRED BY LAW]. Support tickets: [PERIOD]. Device hashes: forgotten after [30] days without use. Backups: [PERIOD].
Expired accounts are removed from VPN servers after [30] days.

## 7. Your rights

Depending on your country you may have the right to access, correct, delete or export your data, to restrict or object
to processing and to withdraw consent. Write to [CONTACT]; we answer within [PERIOD]. Deleting your account removes your
subscriptions from our servers; records we must keep by law are retained for the legal period.

## 8. Security

Access to the administration interface is limited to authorized staff; secrets are encrypted; connections to our site
use HTTPS. No system is perfectly secure; we will notify affected users and authorities of a data breach where the law
requires it.

## 9. Children

The service is not intended for persons under [AGE].

## 10. International transfers

Our servers are located in [COUNTRIES]. [DESCRIBE SAFEGUARDS IF REQUIRED.]

## 11. Changes

We may update this policy. The new version is published at [URL] with a new effective date; where required, we ask for
your acceptance again.

## 12. Contact

[OPERATOR NAME], [E-MAIL OR TELEGRAM CONTACT], [ADDRESS]. Supervisory authority: [AUTHORITY, IF APPLICABLE].
