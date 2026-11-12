import { describe, expect, it } from 'vitest';
import { appIcon } from './appIcons';
import { payCategory } from './payCategories';
import { remainingShare, splitFlag, subTone } from './polish';

describe('polish helpers', () => {
  it('splits a leading flag from a server name', () => {
    expect(splitFlag('\u{1F1E9}\u{1F1EA} Germany')).toEqual({ flag: '\u{1F1E9}\u{1F1EA}', text: 'Germany' });
    expect(splitFlag('Plain name')).toEqual({ flag: '', text: 'Plain name' });
  });

  it('classifies subscription status', () => {
    expect(subTone('active', 10, false)).toBe('ok');
    expect(subTone('active', 3, false)).toBe('soon');
    expect(subTone('active', 1, true)).toBe('ok');
    expect(subTone('expired', 0, false)).toBe('expired');
  });

  it('clamps the remaining share', () => {
    expect(remainingShare(15, 30, false)).toBe(0.5);
    expect(remainingShare(40, 30, false)).toBe(1);
    expect(remainingShare(0, 30, true)).toBe(1);
  });

  it('maps providers to a category with a card default', () => {
    expect(payCategory({ key: 'stars', title: 'Stars' })).toBe('stars');
    expect(payCategory({ key: 'cryptobot', title: 'Crypto' })).toBe('crypto');
    expect(payCategory({ key: 'x', title: 'Pay by SBP' })).toBe('sbp');
    expect(payCategory({ key: 'manual', title: 'Transfer', kind: 'manual' })).toBe('manual');
    expect(payCategory({ key: 'brand-new-gateway', title: 'Pay' })).toBe('card');
    expect(payCategory(null)).toBe('wallet');
  });

  it('knows the bundled app icons', () => {
    expect(appIcon('happ')).toBeTruthy();
    expect(appIcon('unknown-app')).toBeUndefined();
  });
});
