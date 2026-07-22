import { describe, expect, it } from 'vitest';

import { formatInstruction, formatPercent, truncate } from './format';


describe('format helpers', () => {
  it('formats percentages and truncates long values', () => {
    expect(formatPercent(0.825, 1)).toBe('82.5%');
    expect(truncate('abcdef', 3)).toBe('abc…');
  });

  it('shortens paths, whitespace, and instruction length', () => {
    const value = formatInstruction(
      '/home/user/project/src/components/Panel.tsx\n  run   tests',
      35,
    );

    expect(value).not.toContain('/home/user/project');
    expect(value).not.toContain('\n');
    expect(value.endsWith('…')).toBe(true);
  });
});
