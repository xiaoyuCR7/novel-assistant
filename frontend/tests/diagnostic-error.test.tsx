import { afterEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ApiError, apiError } from '../src/lib/api';
import { DiagnosticError, diagnosticText } from '../src/components/DiagnosticError';

afterEach(() => vi.unstubAllGlobals());

it.each(['nested', 'root'])('preserves %s diagnostic ids separately from private server error text', async placement => {
  const detail = { code: 'PROVIDER_FAILED', message: 'private prompt and key', ...(placement === 'nested' ? { diagnostic_id: 'diag-1234' } : {}) };
  const error = await apiError(new Response(JSON.stringify({ detail, ...(placement === 'root' ? { diagnostic_id: 'diag-1234' } : {}) }), { status: 502 }));
  expect(error.diagnosticId).toBe('diag-1234');
  expect(error.message).toBe('private prompt and key');
});

it('copies only allowlisted metadata without raw messages, prompts, keys or current records', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal('navigator', { clipboard: { writeText } });
  const error = new ApiError(502, 'private prompt sk-secret', { content: 'private manuscript' }, 'PROVIDER_FAILED', 'private-job', 'diag-1234');
  render(<DiagnosticError error={error} />);
  fireEvent.click(screen.getByRole('button', { name: '复制诊断信息' }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith('HTTP：502\n错误代码：PROVIDER_FAILED\n诊断编号：diag-1234'));
  expect(diagnosticText(error)).not.toMatch(/private|sk-secret/);
});

it('offers a manually copyable sanitized fallback when clipboard is unavailable', async () => {
  vi.stubGlobal('navigator', {});
  render(<DiagnosticError error={new ApiError(500, 'failure', undefined, 'INTERNAL_ERROR', undefined, 'diag-1234')} />);
  fireEvent.click(screen.getByRole('button', { name: '复制诊断信息' }));
  expect(await screen.findByText(/手动复制/)).toBeVisible();
  expect((screen.getByLabelText('脱敏诊断信息') as HTMLTextAreaElement).value).toContain('diag-1234');
});
