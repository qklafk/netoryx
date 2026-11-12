import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { PANEL_KINDS, USER_LANGUAGES, panelPatch, ticketsQuery } from './catalog';

const root = resolve(__dirname, '../../..');
const read = (path: string) => readFileSync(resolve(root, path), 'utf-8');

describe('admin form catalogs', () => {
  it('offers every panel backend the server supports', () => {
    const block = read('app/db/models.py').split('class PanelKind')[1].split('class EndpointKind')[0];
    const kinds = [...block.matchAll(/^\s+[A-Z]+ = "([a-z]+)"/gm)].map(m => m[1]);
    expect(kinds.length).toBeGreaterThan(5);
    expect([...PANEL_KINDS].sort()).toEqual(kinds.sort());
  });

  it('offers every interface language the server enables by default', () => {
    const match = read('app/config.py').match(/enabled_languages:[\s\S]*?default_factory=lambda: \[([^\]]*)\]/);
    const languages = [...(match?.[1] ?? '').matchAll(/"([a-z]+)"/g)].map(m => m[1]);
    expect(languages.length).toBe(7);
    expect([...USER_LANGUAGES]).toEqual(languages);
  });

  it('lists all tickets when no status is chosen', () => {
    expect(ticketsQuery('', 25)).toBe('limit=25&offset=25&status=open&status=answered&status=closed');
    expect(ticketsQuery('answered', 0)).toBe('limit=25&offset=0&status=answered');
  });
});

describe('panelPatch', () => {
  const panel = { name: 'DE-1', url: 'https://p.example.com', username: 'root', tls_verify: true, is_active: true, options: { header: '***' } };
  const same = { ...panel, password: '', api_token: '' };

  it('sends only what changed so non-owners can switch a server off', () => {
    expect(panelPatch(panel, same)).toEqual({});
    expect(panelPatch(panel, { ...same, is_active: false })).toEqual({ is_active: false });
    expect(panelPatch(panel, { ...same, name: 'DE-2' })).toEqual({ name: 'DE-2' });
  });

  it('includes credentials and the address only when edited', () => {
    expect(panelPatch(panel, { ...same, password: 'new' })).toEqual({ password: 'new' });
    expect(panelPatch(panel, { ...same, url: 'https://q.example.com' })).toEqual({ url: 'https://q.example.com' });
    expect(panelPatch(panel, { ...same, options: { header: 'x' } })).toEqual({ options: { header: 'x' } });
  });
});
