// Value lists the admin forms offer; they mirror the backend enums and defaults.

/** Panel backends that can be added from the admin UI (PanelKind). */
export const PANEL_KINDS = ['xui', 'remnawave', 'marzban', 'hiddify', 'pasarguard', 'marzneshin', 'telemt'] as const;

/** Interface languages users can pick (ENABLED_LANGUAGES). */
export const USER_LANGUAGES = ['ru', 'en', 'uk', 'kk', 'uz', 'fa', 'zh'] as const;

export const TICKET_STATUSES = ['open', 'answered', 'closed'] as const;

/** Query string for the ticket inbox. The API lists only open tickets unless statuses are given, so "all" lists them. */
export function ticketsQuery(status: string, offset: number, limit = 25): string {
  const statuses = status ? [status] : [...TICKET_STATUSES];
  return `limit=${limit}&offset=${offset}&${statuses.map(s => `status=${encodeURIComponent(s)}`).join('&')}`;
}

type Row = Record<string, any>;

/**
 * Body of a panel edit. The API treats a request that mentions the URL, login, options or secrets as owner-only,
 * so only fields that really changed are sent and an administrator can still rename or switch a server off.
 */
export function panelPatch(panel: Row, values: Row): Row {
  const body: Row = {};
  if (values.name !== panel.name) body.name = values.name;
  if (values.url !== panel.url) body.url = values.url;
  if ((values.username || null) !== (panel.username || null)) body.username = values.username || null;
  if (!!values.tls_verify !== !!panel.tls_verify) body.tls_verify = !!values.tls_verify;
  if (!!values.is_active !== !!panel.is_active) body.is_active = !!values.is_active;
  if (JSON.stringify(values.options ?? {}) !== JSON.stringify(panel.options ?? {})) body.options = values.options;
  if (values.password) body.password = values.password;
  if (values.api_token) body.api_token = values.api_token;
  return body;
}
