import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { StyleLab } from '../src/features/style/StyleLab';

it('distinguishes required, reference and disabled styles and pins directly', () => {
  const profiles = [
    { id: 'required', name: '必需文风', is_active: true, is_pinned: true, revision: 1, config: {} },
    { id: 'reference', name: '参考文风', is_active: true, is_pinned: false, revision: 2, config: {} },
    { id: 'disabled', name: '停用文风', is_active: false, is_pinned: true, revision: 3, config: {} },
  ];
  const pin = vi.fn().mockResolvedValue(undefined);
  render(<StyleLab profiles={profiles} candidates={[]} rules={[]} onConfirm={vi.fn()} onDisable={vi.fn()} onPinProfile={pin} />);
  expect(screen.getByText('必遵守')).toBeVisible();
  expect(screen.getByText('按需参考')).toBeVisible();
  expect(screen.getByText('已停用')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '固定方案 参考文风' }));
  expect(pin).toHaveBeenCalledWith(profiles[1], true);
});
