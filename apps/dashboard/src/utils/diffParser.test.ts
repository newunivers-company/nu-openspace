import { describe, expect, it } from 'vitest';

import { parseDiff } from './diffParser';


describe('parseDiff', () => {
  it('parses modified and newly created files', () => {
    const parsed = parseDiff(
      [
        '--- a/src/old.ts',
        '+++ b/src/old.ts',
        '@@ -1 +1 @@',
        '-old',
        '+new',
        '--- /dev/null',
        '+++ b/src/new.ts',
        '@@ -0,0 +1 @@',
        '+created',
      ].join('\n'),
    );

    expect(parsed).toHaveLength(2);
    expect(parsed[0].path).toBe('src/old.ts');
    expect(parsed[0].hunks[0].lines.map((line) => line.type)).toEqual([
      'del',
      'add',
    ]);
    expect(parsed[1].path).toBe('src/new.ts');
  });

  it('ignores empty and metadata-only diffs', () => {
    expect(parseDiff('')).toEqual([]);
    expect(parseDiff('--- a/empty\n+++ b/empty')).toEqual([]);
  });
});
