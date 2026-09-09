import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { MaterialEditorSheet } from "../src/features/library/MaterialEditorSheet";
import { TrashView } from "../src/features/library/TrashView";

const item = {
  id: "e1",
  type: "entity",
  title: "林渡",
  content: "邮差",
  preview: "邮差",
  revision: 2,
  is_pinned: false,
  deleted_at: null,
  purge_after: null,
  record: {},
};

it("edits a material with its current revision", async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(
    <MaterialEditorSheet
      item={item}
      onSave={save}
      onClose={vi.fn()}
      onDelete={vi.fn()}
    />,
  );
  await userEvent.clear(screen.getByLabelText("素材标题"));
  await userEvent.type(screen.getByLabelText("素材标题"), "林渡邮差");
  await userEvent.click(screen.getByRole("button", { name: "保存素材" }));
  expect(save).toHaveBeenCalledWith(
    "entity",
    expect.objectContaining({ title: "林渡邮差", revision: 2 }),
  );
});

it("keeps permanent deletion disabled during retention", () => {
  render(
    <TrashView
      items={[
        {
          ...item,
          deleted_at: new Date().toISOString(),
          purge_after: new Date(Date.now() + 30 * 86400000).toISOString(),
        },
      ]}
      onRestore={vi.fn()}
      onPurge={vi.fn()}
    />,
  );
  expect(screen.getByRole("button", { name: "永久删除林渡" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "恢复林渡" })).toBeEnabled();
});

it('edits aliases without replacing other profile values or advanced state changes', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<MaterialEditorSheet item={{ ...item, record: { profile: { aliases: ['小渡'], occupation: '邮差', nested: { home: '雾城' } }, state: { location: '城门' } } }}
    onSave={save} onClose={vi.fn()} onDelete={vi.fn()} />);
  expect(screen.getByLabelText('别名与称呼')).toHaveValue('小渡');
  await userEvent.clear(screen.getByLabelText('别名与称呼'));
  await userEvent.type(screen.getByLabelText('别名与称呼'), ' 阿渡, 小林\n阿渡');
  await userEvent.click(screen.getByText('类型字段（高级）'));
  const advanced = screen.getByLabelText('类型字段 JSON');
  const fields = JSON.parse((advanced as HTMLTextAreaElement).value);
  expect(fields.profile.aliases).toEqual(['阿渡', '小林']);
  fields.state.location = '钟楼'; fields.profile.occupation = '信使';
  fireEvent.change(advanced, { target: { value: JSON.stringify(fields) } });
  expect(screen.getByLabelText('别名与称呼')).toHaveValue('阿渡\n小林');
  await userEvent.click(screen.getByRole('button', { name: '保存素材' }));
  expect(save).toHaveBeenCalledWith('entity', expect.objectContaining({ fields: {
    profile: { aliases: ['阿渡', '小林'], occupation: '信使', nested: { home: '雾城' } }, state: { location: '钟楼' },
  } }));
});

it('preserves aliases edited in advanced JSON when the alias field is untouched', async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<MaterialEditorSheet item={{ ...item, record: { profile: { aliases: ['小渡'], occupation: '邮差' } } }}
    onSave={save} onClose={vi.fn()} onDelete={vi.fn()} />);
  await userEvent.click(screen.getByText('类型字段（高级）'));
  fireEvent.change(screen.getByLabelText('类型字段 JSON'), { target: { value: JSON.stringify({ profile: { aliases: ['新称呼'], occupation: '邮差' }, state: {} }) } });
  expect(screen.getByLabelText('别名与称呼')).toHaveValue('新称呼');
  await userEvent.click(screen.getByRole('button', { name: '保存素材' }));
  expect(save).toHaveBeenCalledWith('entity', expect.objectContaining({ fields: {
    profile: { aliases: ['新称呼'], occupation: '邮差' }, state: {},
  } }));
});

it.each(['text then JSON', 'JSON then text'])('keeps the last alias edit when editing %s', async order => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<MaterialEditorSheet item={{ ...item, record: { profile: { aliases: ['小渡'], occupation: '邮差' }, state: { location: '城门' } } }}
    onSave={save} onClose={vi.fn()} onDelete={vi.fn()} />);
  await userEvent.click(screen.getByText('类型字段（高级）'));
  const aliases = screen.getByLabelText('别名与称呼');
  const advanced = screen.getByLabelText('类型字段 JSON');
  const editText = () => fireEvent.change(aliases, { target: { value: '阿渡, 小林' } });
  const editJSON = () => fireEvent.change(advanced, { target: { value: JSON.stringify({
    profile: { aliases: ['最终称呼'], occupation: '信使' }, state: { location: '钟楼' },
  }) } });
  if (order === 'text then JSON') { editText(); editJSON(); } else { editJSON(); editText(); }
  const expectedAliases = order === 'text then JSON' ? ['最终称呼'] : ['阿渡', '小林'];
  expect(JSON.parse((advanced as HTMLTextAreaElement).value).profile.aliases).toEqual(expectedAliases);
  expect(aliases).toHaveValue(order === 'text then JSON' ? '最终称呼' : '阿渡, 小林');
  await userEvent.click(screen.getByRole('button', { name: '保存素材' }));
  expect(save).toHaveBeenCalledWith('entity', expect.objectContaining({ fields: {
    profile: { aliases: expectedAliases, occupation: '信使' }, state: { location: '钟楼' },
  } }));
});

it.each(['{"profile":', '[]', '{"profile":"invalid","state":{}}', '{"profile":null,"state":{}}'])('preserves incomplete or invalid JSON %s while editing aliases and still validates on save', async invalidJSON => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<MaterialEditorSheet item={{ ...item, record: { profile: { aliases: ['小渡'], occupation: '邮差' }, state: {} } }}
    onSave={save} onClose={vi.fn()} onDelete={vi.fn()} />);
  await userEvent.click(screen.getByText('类型字段（高级）'));
  const aliases = screen.getByLabelText('别名与称呼');
  const advanced = screen.getByLabelText('类型字段 JSON');
  fireEvent.change(advanced, { target: { value: invalidJSON } });
  expect(aliases).toHaveValue('小渡');
  fireEvent.change(aliases, { target: { value: '阿渡' } });
  expect(advanced).toHaveValue(invalidJSON);
  await userEvent.click(screen.getByRole('button', { name: '保存素材' }));
  expect(await screen.findByRole('alert')).toBeVisible();
  expect(save).not.toHaveBeenCalled();
  expect(aliases).toHaveValue('阿渡');
  expect(advanced).toHaveValue(invalidJSON);
  fireEvent.change(advanced, { target: { value: JSON.stringify({ profile: { aliases: ['最终称呼'], occupation: '信使' }, state: {} }) } });
  expect(aliases).toHaveValue('最终称呼');
  await userEvent.click(screen.getByRole('button', { name: '保存素材' }));
  expect(save).toHaveBeenCalledWith('entity', expect.objectContaining({ fields: {
    profile: { aliases: ['最终称呼'], occupation: '信使' }, state: {},
  } }));
});
