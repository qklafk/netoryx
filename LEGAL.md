# Legal notice

Русская версия: [LEGAL.ru.md](LEGAL.ru.md)

This page explains, in plain language, what Netoryx is and who is responsible for what. It is **not legal advice**.

## What Netoryx is

Netoryx is free, open-source software (GNU AGPL-3.0-or-later, see [LICENSE](LICENSE)). It is a Telegram bot with a web
admin that people install **on their own servers** to sell or share access to **their own** VPN servers.

## What the authors do and do not do

- The authors and contributors provide software only. They **do not operate any VPN or proxy service**, do not host
  any operator's installation and have no access to it.
- Netoryx has **no telemetry**. It does not send usage statistics, installation lists, user data or crash reports to the
  authors or to any project-controlled server. The only optional outbound calls that involve the project's name are
  the installer and `netoryx update`, which download the source or a release from GitHub when you run them, and the
  optional release check (`UPDATE_CHECK_REPO`, off by default), which asks the GitHub API for the latest release number.
  Error reporting to Sentry exists only if the operator sets their own `SENTRY_DSN`. Other outbound calls go to
  services the operator configures: Telegram, VPN panels and payment providers.
- The authors receive no personal data of operators or of their customers.
- Operators' customers are the operators' customers. The authors have no relationship with them.

## No warranty

The software is provided "as is", without warranty of any kind, and the authors are not liable for damages arising from
its use. See sections 15 (Disclaimer of Warranty), 16 (Limitation of Liability) and 17 (Interpretation of Sections 15
and 16) of the [GNU AGPL-3.0](LICENSE).

## The operator is responsible

Whoever installs and runs Netoryx is the **operator** of the resulting service and is solely responsible for:

- complying with the laws of the countries where they operate and where their customers are: telecommunications and VPN
  regulation, licensing or registration duties, personal-data protection, consumer protection, advertising, payments,
  taxes and receipts, sanctions and export rules;
- publishing their own terms of service and privacy policy (templates are in [docs/legal](docs/legal/), also
  [in Russian](docs/ru/legal/)) and keeping them true to what their installation does;
- how their customers use the service, including handling abuse complaints and legal requests;
- the security of their servers, backups and the secrets they hold, and the payment-provider accounts they use.

If a rule of your country forbids running such a service, do not run it.

## Intended use

Netoryx is meant for privacy, security, secure remote access and protecting traffic on untrusted networks, for example
public Wi-Fi. Do not use it, or let your customers use it, for unlawful activity.

## Third-party software and trademarks

Netoryx works with third-party panels, payment providers and apps (3x-ui, Remnawave, Marzban, Telegram and others).
They are separate products under their own licenses and terms. All names are trademarks of their owners; their use here
only describes compatibility and does not imply endorsement.

## Contributions

Contributions are accepted under the [CLA](CLA.md), see [CONTRIBUTING.md](CONTRIBUTING.md).
