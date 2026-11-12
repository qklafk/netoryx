import { useState } from 'react';
import { ArrowDown, ArrowUp, Copy, ExternalLink, FileText, Pencil, Plus, Smartphone } from 'lucide-react';
import { useApi } from '../shared/api';
import { ActionButton, Badge, FormDialog, PageTitle, Panel, Resource, Table, apiMutation, useAdmin, type FieldSpec, type Row } from './ui';

type MenuButtonRow = { id: number; kind: string; title: string; titles: Record<string, string>; payload: string; row: number; sort_order: number; audience: string; is_active: boolean };
type Builtin = { key: string; hidden: boolean };

const KIND_ICONS: Record<string, typeof ExternalLink> = { url: ExternalLink, webapp: Smartphone, copy_text: Copy, text_screen: FileText };

function KindPreview({ button }: { button: MenuButtonRow }) {
  const { tr } = useAdmin();
  const Icon = KIND_ICONS[button.kind] || ExternalLink;
  const labels: Record<string, string> = {
    url: tr('Откроет ссылку', 'Opens a link'),
    webapp: tr('Откроет Mini App', 'Opens a Mini App'),
    copy_text: tr('Скопирует текст', 'Copies text'),
    text_screen: tr('Покажет экран с текстом', 'Shows a text screen'),
  };
  return <div><strong><Icon size={14}/> {button.title}</strong><small>{labels[button.kind] || button.kind}</small><small className="monospace">{button.payload.length > 80 ? `${button.payload.slice(0, 80)}…` : button.payload}</small></div>;
}

export function MenuButtonsPage() {
  const { tr, can } = useAdmin();
  const editable = can('texts');
  const buttons = useApi<MenuButtonRow[]>('/menu-buttons');
  const builtins = useApi<Builtin[]>('/menu-buttons/builtins');
  const [editing, setEditing] = useState<MenuButtonRow | 'new' | null>(null);
  const rows = buttons.data || [];
  const audiences = [
    { value: 'all', label: tr('Все', 'Everyone') },
    { value: 'subscribers', label: tr('С активной подпиской', 'Active subscribers') },
    { value: 'non_subscribers', label: tr('Без подписки', 'Without a subscription') },
    { value: 'admins', label: tr('Администраторы', 'Administrators') },
  ];
  const fields: FieldSpec[] = [
    { key: 'kind', label: tr('Тип', 'Kind'), required: true, options: [
      { value: 'url', label: tr('Ссылка (http, https, tg)', 'Link (http, https, tg)') },
      { value: 'webapp', label: tr('Mini App (только https)', 'Mini App (https only)') },
      { value: 'copy_text', label: tr('Копировать текст (до 256 символов)', 'Copy text (up to 256 characters)') },
      { value: 'text_screen', label: tr('Экран с текстом (HTML Telegram)', 'Text screen (Telegram HTML)') },
    ] },
    { key: 'title', label: tr('Название (по умолчанию)', 'Title (default)'), required: true, max: 64 },
    { key: 'title_en', label: tr('Название на английском', 'English title'), hint: tr('Необязательно', 'Optional') },
    { key: 'audience', label: tr('Кому показывать', 'Shown to'), required: true, options: audiences },
    { key: 'row', label: tr('Ряд', 'Row'), type: 'number', min: 0, max: 99, hint: tr('Кнопки с одинаковым рядом стоят рядом', 'Buttons with the same row share a line') },
    { key: 'is_active', label: tr('Включена', 'Active'), type: 'checkbox' },
    { key: 'payload', label: tr('Ссылка, текст для копирования или текст экрана', 'Link, text to copy or screen text'), type: 'textarea', required: true },
  ];
  const initial = (b: MenuButtonRow | null): Row => b ? { ...b, title_en: b.titles?.en || '' } : { kind: 'url', audience: 'all', row: 0, is_active: true, title_en: '' };
  async function save(values: Row) {
    const { title_en, ...rest } = values;
    const current = editing && editing !== 'new' ? editing : null;
    const titles = { ...(current?.titles || {}) };
    if (title_en) titles.en = title_en; else delete titles.en;
    const body = { kind: rest.kind, title: rest.title, titles, payload: rest.payload, row: rest.row ?? 0, audience: rest.audience, is_active: Boolean(rest.is_active) };
    if (current) await apiMutation(`/menu-buttons/${current.id}`, body, 'PATCH'); else await apiMutation('/menu-buttons', body);
    buttons.reload();
  }
  const builtinLabels: Record<string, string> = {
    trial: tr('Пробный период', 'Free trial'), subscription: tr('Моя подписка', 'My subscription'), connect: tr('Подключение', 'Connect'),
    buy: tr('Купить / продлить', 'Buy / renew'), balance: tr('Баланс', 'Balance'), referral: tr('Пригласить друзей', 'Invite friends'),
    gift: tr('Подарки и промокоды', 'Gifts & promo codes'), tg_proxy: tr('Прокси для Telegram', 'Telegram proxy'), support: tr('Поддержка', 'Support'),
    instructions: tr('Инструкции', 'Instructions'), language: tr('Язык', 'Language'), mini_app: 'Mini App',
  };
  const hidden = (builtins.data || []).filter(b => b.hidden).map(b => b.key);
  return <>
    <PageTitle eyebrow={tr('БОТ', 'BOT')} title={tr('Главное меню', 'Main menu')} subtitle={tr('Свои кнопки после встроенных пунктов: ссылки, Mini App, копирование текста и экраны с текстом.', 'Custom buttons after the built-in entries: links, Mini Apps, copy-to-clipboard and text screens.')}>
      {editable && <button className="btn primary" onClick={() => setEditing('new')}><Plus size={16}/>{tr('Добавить кнопку', 'Add button')}</button>}
    </PageTitle>
    <Panel title={tr('Свои кнопки', 'Custom buttons')}>
      <Resource resource={buttons}>
        <Table rows={rows} columns={[
          { title: tr('Кнопка', 'Button'), render: b => <KindPreview button={b as MenuButtonRow}/> },
          { title: tr('Ряд', 'Row'), render: b => <span>{b.row} · {b.sort_order}</span> },
          { title: tr('Кому', 'Audience'), render: b => audiences.find(a => a.value === b.audience)?.label || b.audience },
          { title: tr('Статус', 'Status'), render: b => <Badge value={b.is_active ? 'active' : 'disabled'}>{b.is_active ? tr('включена', 'active') : tr('выключена', 'off')}</Badge> },
          ...(editable ? [{ title: '', render: (b: Row) => <div className="row-actions">
            <ActionButton path={`/menu-buttons/${b.id}/move`} body={{ direction: 'up' }} onDone={buttons.reload} disabled={rows[0]?.id === b.id}><ArrowUp size={15}/></ActionButton>
            <ActionButton path={`/menu-buttons/${b.id}/move`} body={{ direction: 'down' }} onDone={buttons.reload} disabled={rows[rows.length - 1]?.id === b.id}><ArrowDown size={15}/></ActionButton>
            <button className="btn secondary" onClick={() => setEditing(b as MenuButtonRow)}><Pencil size={15}/></button>
            <ActionButton path={`/menu-buttons/${b.id}`} method="DELETE" danger confirm={tr('Удалить кнопку?', 'Delete this button?')} onDone={buttons.reload}>{tr('Удалить', 'Delete')}</ActionButton>
          </div> }] : []),
        ]}/>
      </Resource>
    </Panel>
    <Panel title={tr('Встроенные пункты', 'Built-in entries')}>
      <Resource resource={builtins}>
        <div className="row-actions">{(builtins.data || []).map(b => {
          const next = b.hidden ? hidden.filter(k => k !== b.key) : [...hidden, b.key];
          return editable && can('settings')
            ? <ActionButton key={b.key} path="/menu-buttons/builtins" method="PUT" body={{ hidden: next }} onDone={builtins.reload}>{b.hidden ? '○' : '●'} {builtinLabels[b.key] || b.key}</ActionButton>
            : <Badge key={b.key} value={b.hidden ? 'disabled' : 'active'}>{builtinLabels[b.key] || b.key}</Badge>;
        })}</div>
        <small className="muted">{tr('Нажмите, чтобы скрыть или показать пункт. Кнопка админ-меню показывается всегда.', 'Click to hide or show an entry. The admin entry is always shown.')}</small>
      </Resource>
    </Panel>
    {editing && <FormDialog title={editing === 'new' ? tr('Новая кнопка', 'New button') : tr('Изменить кнопку', 'Edit button')} fields={fields} initial={initial(editing === 'new' ? null : editing)} submit={save} close={() => setEditing(null)}/>}
  </>;
}
