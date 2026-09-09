import type { InfiniteData, QueryClient } from '@tanstack/react-query';
import type { LibraryPage } from '../../lib/types';

/** Mutations invalidate sort anchors; restart bounded lists without touching job history. */
export async function refreshLibrary(client: QueryClient, projectId: string) {
  const lists = { queryKey: ['library', projectId], predicate: (query: { queryKey: readonly unknown[] }) =>
    ['page', 'ledger', 'trash'].includes(String(query.queryKey[2])) };
  await client.cancelQueries(lists);
  client.setQueriesData<InfiniteData<LibraryPage>>(lists, old => old ? {
    pages: old.pages.slice(0, 1), pageParams: old.pageParams.slice(0, 1),
  } : old);
  await client.invalidateQueries({ queryKey: ['library', projectId] });
}
