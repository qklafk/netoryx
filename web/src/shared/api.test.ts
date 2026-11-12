import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, adminApi, describeError, resultProblem, setAdminUnauthorizedHandler } from './api';

function respond(status: number, body: unknown) {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })));
}

afterEach(() => { vi.unstubAllGlobals(); setAdminUnauthorizedHandler(null); });

describe('admin api errors', () => {
  it('names the failing fields of a validation error', async () => {
    respond(422, { error: 'validation_error', message: 'Request validation failed', details: [{ loc: ['body', 'bonus_days'], msg: 'Input should be a valid integer', type: 'int_type' }] });
    const error = await adminApi('/promo').catch(e => e as ApiError);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.message).toBe('Request validation failed (bonus_days: Input should be a valid integer)');
  });

  it('keeps plain messages in both error shapes', () => {
    expect(describeError({ error: 'forbidden', message: 'Not enough permissions' }, 'x')).toBe('Not enough permissions');
    expect(describeError({ error: { code: 'a', message: 'Nested' } }, 'x')).toBe('Nested');
    expect(describeError({}, 'HTTP 500')).toBe('HTTP 500');
  });

  it('reports an expired admin session once the user is signed in', async () => {
    const handler = vi.fn();
    setAdminUnauthorizedHandler(handler);
    respond(401, { error: 'unauthorized', message: 'Sign in' });
    await adminApi('/users').catch(() => undefined);
    expect(handler).toHaveBeenCalledTimes(1);
    await adminApi('/auth/me').catch(() => undefined);
    expect(handler).toHaveBeenCalledTimes(1);
  });
});

describe('resultProblem', () => {
  it('flags partial failures of a successful call', () => {
    expect(resultProblem({ found: true, synced: 1, failed: 2, skipped: 0, errors: ['panel A: timeout'] })).toBe('panel A: timeout');
    expect(resultProblem({ reset: 0, failed: 1, errors: [] })).toBe('1 failed');
    expect(resultProblem({ deleted: true, accounts_removed: 1, errors: ['Telegram Stars subscription could not be canceled'] })).toContain('Stars');
  });

  it('ignores clean results', () => {
    expect(resultProblem({ synced: 3, failed: 0, errors: [] })).toBeNull();
    expect(resultProblem(undefined)).toBeNull();
    expect(resultProblem({ ok: true })).toBeNull();
  });
});
