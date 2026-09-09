import type { LibraryPage } from '../src/lib/types';

/** Explicit bounded API fixtures; unknown paths still reach each test's strict handler. */
export function emptyLibraryPage(url: string): LibraryPage | undefined {
  const path = new URL(url, 'http://fixture').pathname;
  if (!path.endsWith('/library/page') && !path.endsWith('/trash/page')) return undefined;
  return { items: [], total: 0, counts: {}, next_cursor: null };
}

export function emptyWorkspaceView(url: string, nodes: unknown[] = []) {
  const path = new URL(url, 'http://fixture').pathname;
  const views = { ideas: { ideas: [] }, story: { nodes, plots: [] },
    entities: { entities: [], relations: [] }, knowledge: { canon_facts: [], timeline: [] },
    style: { styles: [], memory_conflicts: [] }, library: { memory_conflicts: [] } };
  const view = path.split('/workspace/views/')[1];
  return view && view in views ? views[view as keyof typeof views] : undefined;
}
