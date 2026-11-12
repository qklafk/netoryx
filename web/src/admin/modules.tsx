import { useApi } from '../shared/api';
import { Badge, PageTitle, Panel, Resource, Table, useAdmin } from './ui';

type ModuleEntry = {
  id: string;
  title: string;
  description: string;
  version: string | number;
  category: string;
  kind: 'plugin' | 'legacy';
  installed: boolean;
  lifecycle_status: string;
  runtime_status: string;
  partially_migrated: boolean;
  migration_status: string;
  dependencies: string[];
  conflicts: string[];
  resources: { ram_mb?: number; disk_mb?: number };
  compatibility: { core: string; sdk: string };
  restart_required: boolean;
  scheduler?: { bound: boolean; owner: string; jobs: { id: string; scheduled: boolean; running: boolean; last_error: string | null }[]; suppressed_legacy_jobs?: string[] };
  last_error: string | null;
};

type Catalog = { core_available: boolean; plugins: ModuleEntry[]; legacy: ModuleEntry[] };

function list(values: string[]) { return values.length ? values.join(', ') : '\u2014'; }

export function ModuleManagerPage() {
  const { tr } = useAdmin();
  const catalog = useApi<Catalog>('/system/plugin-catalog');
  const data = catalog.data || { core_available: false, plugins: [], legacy: [] };
  const rows = [...data.plugins, ...data.legacy];
  return <>
    <PageTitle eyebrow={tr('\u041f\u041b\u0410\u0422\u0424\u041e\u0420\u041c\u0410', 'PLATFORM')} title={tr('\u041c\u043e\u0434\u0443\u043b\u0438', 'Modules')} subtitle={tr('\u041a\u0430\u0442\u0430\u043b\u043e\u0433 \u0440\u0430\u0437\u0434\u0435\u043b\u044f\u0435\u0442 \u043f\u043b\u0430\u0433\u0438\u043d\u044b \u0438 legacy-\u043a\u043e\u043c\u043f\u043e\u043d\u0435\u043d\u0442\u044b.', 'The catalog distinguishes plugins from legacy components.')}/>
    <Panel title={tr('\u041a\u0430\u0442\u0430\u043b\u043e\u0433 \u043c\u043e\u0434\u0443\u043b\u0435\u0439', 'Module catalog')} aside={!data.core_available ? <small>{tr('\u041f\u043b\u0430\u0433\u0438\u043d Core \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d; \u043f\u043e\u043a\u0430\u0437\u0430\u043d\u044b legacy-\u043c\u043e\u0434\u0443\u043b\u0438.', 'Plugin Core is unavailable; legacy modules are shown.')}</small> : undefined}>
      <Resource resource={catalog} empty={!rows.length}>
        <Table rows={rows} columns={[
          { title: tr('\u041c\u043e\u0434\u0443\u043b\u044c', 'Module'), render: (m) => <div><strong>{m.title}</strong><small>{m.id} \u00b7 v{m.version} \u00b7 {m.category}</small><small>{m.description}</small></div> },
          { title: tr('\u0421\u043e\u0441\u0442\u043e\u044f\u043d\u0438\u0435', 'State'), render: (m) => <div><Badge value={m.installed ? 'installed' : 'available'}>{m.installed ? tr('\u0443\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d', 'installed') : tr('\u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d', 'available')}</Badge><small>{m.lifecycle_status}</small></div> },
          { title: tr('Lifecycle / Runtime', 'Lifecycle / Runtime'), render: (m) => <div><Badge value={m.lifecycle_status}/><Badge value={m.runtime_status}/><small>{m.migration_status}{m.partially_migrated ? ` \u00b7 ${tr('\u0447\u0430\u0441\u0442\u0438\u0447\u043d\u043e \u043c\u0438\u0433\u0440\u0438\u0440\u043e\u0432\u0430\u043d', 'partially migrated')}` : ''}</small></div> },
          { title: tr('\u041f\u043b\u0430\u043d\u0438\u0440\u043e\u0432\u0449\u0438\u043a', 'Scheduler'), render: (m) => <small>{m.scheduler?.jobs?.length ? m.scheduler.jobs.map((job: { id: string; scheduled: boolean; running: boolean }) => `${job.id}: ${job.running ? 'running' : job.scheduled ? 'scheduled' : 'stopped'}`).join(', ') : m.scheduler?.suppressed_legacy_jobs?.length ? `suppressed: ${m.scheduler.suppressed_legacy_jobs.join(', ')}` : m.runtime_status === 'legacy-owned' ? tr('legacy owner', 'legacy owner') : '\u2014'}{m.restart_required && <><br/><span className="text-danger">{tr('\u043d\u0435\u043e\u0431\u0445\u043e\u0434\u0438\u043c \u043f\u0435\u0440\u0435\u0437\u0430\u043f\u0443\u0441\u043a', 'restart required')}</span></>}</small> },
          { title: tr('\u0422\u0440\u0435\u0431\u043e\u0432\u0430\u043d\u0438\u044f \u0438 \u0441\u043e\u0432\u043c\u0435\u0441\u0442\u0438\u043c\u043e\u0441\u0442\u044c', 'Requirements & compatibility'), render: (m) => <small>{tr('\u0417\u0430\u0432\u0438\u0441\u0438\u043c\u043e\u0441\u0442\u0438', 'Dependencies')}: {list(m.dependencies)}<br/>{tr('\u041a\u043e\u043d\u0444\u043b\u0438\u043a\u0442\u044b', 'Conflicts')}: {list(m.conflicts)}<br/>RAM: {m.resources.ram_mb || 0} MB, disk: {m.resources.disk_mb || 0} MB<br/>Core {m.compatibility.core}; SDK {m.compatibility.sdk}</small> },
          { title: tr('\u041e\u0448\u0438\u0431\u043a\u0430 / \u043e\u0433\u0440\u0430\u043d\u0438\u0447\u0435\u043d\u0438\u0435', 'Error / limitation'), render: (m) => <small className={m.last_error ? 'text-danger' : ''}>{m.last_error || (m.partially_migrated ? tr('Legacy scheduler remains the job owner.', 'Legacy scheduler remains the job owner.') : tr('\u0422\u043e\u043b\u044c\u043a\u043e \u043f\u0440\u043e\u0441\u043c\u043e\u0442\u0440.', 'Read-only.'))}</small> },
        ]}/>
      </Resource>
    </Panel>
  </>;
}
