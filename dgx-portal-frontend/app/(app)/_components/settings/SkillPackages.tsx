"use client";

// Skills as Claude Code defines them (SKILL.md + files): the account's list,
// imports (.zip or Git repository) and a package browser — Overview and
// Content tabs, file tree, Markdown preview or source — like a Claude plugin
// page. Manual creation and editing stay in the settings dialog's form.

import { useCallback, useEffect, useMemo, useState } from "react";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Switch } from "@astryxdesign/core/Switch";
import { List, ListItem } from "@astryxdesign/core/List";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { Layout, LayoutContent, LayoutFooter } from "@astryxdesign/core/Layout";
import { TabList, Tab } from "@astryxdesign/core/TabList";
import { TreeList } from "@astryxdesign/core/TreeList";
import { CodeBlock } from "@astryxdesign/core/CodeBlock";
import { FileInput } from "@astryxdesign/core/FileInput";
import { TextInput } from "@astryxdesign/core/TextInput";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { useToast } from "@astryxdesign/core/Toast";
import {
  ArrowDownTrayIcon,
  ArrowPathIcon,
  CodeBracketIcon,
  DocumentIcon,
  EyeIcon,
  FolderIcon,
  PencilSquareIcon,
  PlusIcon,
  SparklesIcon,
  TrashIcon,
} from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";
import { ClientTimestamp } from "@/lib/client-timestamp";
import { SafeMarkdown as Markdown } from "@/lib/markdown";
import { getJSON, postFormData, sendJSON } from "@/lib/api";
import { langFromName } from "@/lib/playground-thread";
import { useCsrf } from "@/lib/useCsrf";

export type SkillSummary = {
  id: number;
  name: string;
  description: string;
  source: "manual" | "zip" | "git";
  source_url?: string | null;
  source_ref?: string | null;
  source_path?: string | null;
  version: string;
  synced_at?: string | null;
  created_at: string;
  enabled: boolean;
  file_count: number;
};

type SkillFile = { path: string; size: number; content: string };
type SkillDetail = SkillSummary & { instructions: string; files: SkillFile[] };
type ImportResult = {
  ok: boolean;
  error?: string;
  imported?: { id: number; name: string }[];
  skipped?: { name: string; reason: string }[];
};

// The proxy refuses bodies over 20 MB (next.config.ts proxyClientMaxBodySize).
const ZIP_MAX_BYTES = 20 * 1024 * 1024;

const SOURCE_LABEL: Record<SkillSummary["source"], string> = {
  manual: "Écrite ici",
  zip: "Importée (.zip)",
  git: "Dépôt Git",
};

/** Paths → TreeList items (folders first, then files). */
function buildTree(files: SkillFile[], selected: string, onSelect: (p: string) => void) {
  type Node = { dirs: Map<string, Node>; files: string[] };
  const root: Node = { dirs: new Map(), files: [] };
  for (const f of files) {
    const parts = f.path.split("/");
    let node = root;
    for (const dir of parts.slice(0, -1)) {
      if (!node.dirs.has(dir)) node.dirs.set(dir, { dirs: new Map(), files: [] });
      node = node.dirs.get(dir)!;
    }
    node.files.push(f.path);
  }
  const toItems = (node: Node, prefix: string): Parameters<typeof TreeList>[0]["items"] => [
    ...[...node.dirs.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([name, child]) => ({
      id: `${prefix}${name}/`,
      label: name,
      isExpanded: true,
      startContent: <Icon icon={FolderIcon} size="sm" />,
      children: toItems(child, `${prefix}${name}/`),
    })),
    ...node.files.sort().map((path) => ({
      id: path,
      label: path.split("/").pop() ?? path,
      isSelected: path === selected,
      onClick: () => onSelect(path),
      startContent: <Icon icon={DocumentIcon} size="sm" />,
    })),
  ];
  return toItems(root, "");
}

function SkillBrowser({ skill, onClose, onChanged, onEdit, onDelete }: {
  skill: SkillDetail;
  onClose: () => void;
  onChanged: () => void;
  onEdit: (s: SkillDetail) => void;
  onDelete: (s: SkillDetail) => void;
}) {
  const t = useT();
  const csrf = useCsrf();
  const showToast = useToast();
  const [tab, setTab] = useState<"overview" | "content">("overview");
  const [selected, setSelected] = useState(
    skill.files.find((f) => f.path.toLowerCase() === "skill.md")?.path ?? skill.files[0]?.path ?? "",
  );
  const [view, setView] = useState<"preview" | "code">("preview");
  const [syncing, setSyncing] = useState(false);
  const file = skill.files.find((f) => f.path === selected);
  const isMarkdown = /\.(md|markdown)$/i.test(selected);
  const tree = useMemo(() => buildTree(skill.files, selected, setSelected), [skill.files, selected]);

  async function sync() {
    setSyncing(true);
    const r = await sendJSON<{ ok: boolean; error?: string }>(`/api/skills/${skill.id}/sync`, csrf)
      .catch(() => ({ ok: false, error: undefined }));
    setSyncing(false);
    showToast({ body: r.ok ? t("Compétence synchronisée.") : (r.error ?? t("La synchronisation a échoué.")),
                type: r.ok ? "info" : "error" });
    if (r.ok) onChanged();
  }

  const meta = [
    t(SOURCE_LABEL[skill.source]),
    skill.version ? `v${skill.version}` : "",
    `${skill.file_count || skill.files.length} ${t("fichier(s)")}`,
  ].filter(Boolean).join(" · ");

  return (
    <Dialog isOpen onOpenChange={(o) => { if (!o) onClose(); }} purpose="form" width={900}>
      <Layout
        header={<DialogHeader title={skill.name} subtitle={meta} hasDivider
                              onOpenChange={(o) => { if (!o) onClose(); }} />}
        content={
          <LayoutContent padding={4} isScrollable>
            <VStack gap={4}>
              <TabList value={tab} onChange={(v) => setTab(v as "overview" | "content")} hasDivider>
                <Tab value="overview" label={t("Aperçu")} />
                <Tab value="content" label={`${t("Contenu")} · ${skill.files.length}`} />
              </TabList>
              {tab === "overview" ? (
                <VStack gap={3}>
                  <Text>{skill.description}</Text>
                  {skill.source === "git" && skill.source_url ? (
                    <Text type="supporting" color="secondary">
                      {skill.source_url}
                      {skill.source_ref ? ` @ ${skill.source_ref}` : ""}
                      {skill.source_path ? ` / ${skill.source_path}` : ""}
                      {skill.synced_at ? <> · {t("synchronisée le")} <ClientTimestamp value={skill.synced_at} /></> : null}
                    </Text>
                  ) : null}
                  <Markdown>{skill.instructions}</Markdown>
                </VStack>
              ) : (
                <HStack gap={4} vAlign="start">
                  <VStack gap={1} width={240}>
                    <TreeList items={tree} density="compact" />
                  </VStack>
                  <VStack gap={2} width="100%">
                    <HStack hAlign="between" vAlign="center" gap={2}>
                      <Text type="code" color="secondary">/{selected}</Text>
                      {isMarkdown ? (
                        <SegmentedControl value={view} onChange={(v) => setView(v as "preview" | "code")}
                                          label={t("Affichage")} size="sm">
                          <SegmentedControlItem value="preview" label={t("Aperçu")}
                                                icon={<Icon icon={EyeIcon} size="sm" />} />
                          <SegmentedControlItem value="code" label={t("Source")}
                                                icon={<Icon icon={CodeBracketIcon} size="sm" />} />
                        </SegmentedControl>
                      ) : null}
                    </HStack>
                    {file ? (
                      isMarkdown && view === "preview"
                        ? <Markdown>{file.content.replace(/^---\n[\s\S]*?\n---\n/, "")}</Markdown>
                        : <CodeBlock code={file.content} language={isMarkdown ? "markdown" : langFromName(selected)}
                                     hasLineNumbers maxHeight="60vh" />
                    ) : (
                      <Text type="supporting">{t("Aucun fichier.")}</Text>
                    )}
                  </VStack>
                </HStack>
              )}
            </VStack>
          </LayoutContent>
        }
        footer={
          <LayoutFooter>
            <HStack hAlign="between" gap={2} wrap="wrap">
              <HStack gap={2}>
                <Button label={t("Supprimer")} variant="ghost" size="sm"
                        icon={<Icon icon={TrashIcon} size="sm" />} onClick={() => onDelete(skill)} />
                {skill.source === "manual" ? (
                  <Button label={t("Modifier")} variant="ghost" size="sm"
                          icon={<Icon icon={PencilSquareIcon} size="sm" />} onClick={() => onEdit(skill)} />
                ) : null}
              </HStack>
              <HStack gap={2}>
                {skill.source === "git" ? (
                  <Button label={t("Synchroniser")} variant="secondary" size="sm" isLoading={syncing}
                          icon={<Icon icon={ArrowPathIcon} size="sm" />} onClick={() => void sync()} />
                ) : null}
                <Button label={t("Télécharger (.zip)")} variant="secondary" size="sm"
                        icon={<Icon icon={ArrowDownTrayIcon} size="sm" />}
                        onClick={() => {
                          // A plain link: the browser saves the .zip the route sends.
                          const a = document.createElement("a");
                          a.href = `/api/skills/${skill.id}/download`;
                          a.download = `${skill.name}.zip`;
                          a.click();
                        }} />
              </HStack>
            </HStack>
          </LayoutFooter>
        }
      />
    </Dialog>
  );
}

function ImportDialog({ mode, onClose, onDone }: {
  mode: "zip" | "git";
  onClose: () => void;
  onDone: () => void;
}) {
  const t = useT();
  const csrf = useCsrf();
  const [file, setFile] = useState<File | null>(null);
  const [url, setUrl] = useState("");
  const [ref, setRef] = useState("");
  const [path, setPath] = useState("");
  const [replace, setReplace] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ImportResult | null>(null);

  async function submit() {
    setBusy(true);
    setResult(null);
    let r: ImportResult;
    try {
      r = mode === "zip"
        ? await postFormData<ImportResult>("/api/skills/import", csrf, {
            file: file as File, ...(replace ? { replace: "1" } : {}) })
        : await sendJSON<ImportResult>("/api/skills/import-git", csrf, { url, ref, path, replace });
    } catch {
      r = { ok: false, error: t("Le serveur n'a pas répondu — réessaie.") };
    }
    setBusy(false);
    setResult(r);
    if (r.ok) onDone();
  }

  const canSubmit = mode === "zip" ? !!file : /^https:\/\//.test(url.trim());
  return (
    <Dialog isOpen onOpenChange={(o) => { if (!o) onClose(); }} purpose="form" width={560}>
      <Layout
        header={<DialogHeader
          title={mode === "zip" ? t("Importer une compétence (.zip)") : t("Importer depuis un dépôt Git")}
          subtitle={mode === "zip"
            ? t("Un dossier contenant un SKILL.md, ou un plugin / dépôt qui en contient plusieurs.")
            : t("GitHub, GitLab, Gitea/Codeberg ou une adresse .zip. Dépôts publics uniquement.")}
          hasDivider onOpenChange={(o) => { if (!o) onClose(); }} />}
        content={
          <LayoutContent padding={4}>
            <VStack gap={4}>
              {mode === "zip" ? (
                <FileInput label={t("Archive .zip")} value={file} accept=".zip,application/zip"
                           maxSize={ZIP_MAX_BYTES}
                           onChange={(f) => setFile(Array.isArray(f) ? f[0] ?? null : f)} />
              ) : (
                <VStack gap={3}>
                  <TextInput label={t("Adresse du dépôt ou du dossier")} value={url} onChange={setUrl}
                             placeholder="https://github.com/anthropics/skills/tree/main/document-skills" />
                  <HStack gap={3}>
                    <TextInput label={t("Branche")} value={ref} onChange={setRef} isOptional
                               placeholder="main" />
                    <TextInput label={t("Dossier")} value={path} onChange={setPath} isOptional
                               placeholder="skills/pdf" />
                  </HStack>
                </VStack>
              )}
              <Switch label={t("Remplacer les compétences du même nom")} value={replace} onChange={setReplace} />
              {result && !result.ok ? <Banner status="error" title={result.error ?? t("L'import a échoué.")} /> : null}
              {result?.imported?.length ? (
                <Banner status="success" title={t("{n} compétence(s) importée(s) : {names}")
                  .replace("{n}", String(result.imported.length))
                  .replace("{names}", result.imported.map((s) => s.name).join(", "))} />
              ) : null}
              {result?.skipped?.length ? (
                <Banner status="warning" title={t("Ignorées :") + " " + result.skipped
                  .map((s) => `${s.name} (${s.reason})`).join(" ; ")} />
              ) : null}
            </VStack>
          </LayoutContent>
        }
        footer={
          <LayoutFooter>
            <HStack hAlign="end" gap={2}>
              <Button label={result?.ok ? t("Fermer") : t("Annuler")} variant="ghost" onClick={onClose} />
              {!result?.ok ? (
                <Button label={t("Importer")} variant="primary" isLoading={busy}
                        isDisabled={!canSubmit} onClick={() => void submit()} />
              ) : null}
            </HStack>
          </LayoutFooter>
        }
      />
    </Dialog>
  );
}

export function SkillPackages({ refreshKey, onCreate, onEdit, onDelete }: {
  /** Changes when the settings dialog reloads its data (after a create/edit/delete). */
  refreshKey: unknown;
  onCreate: () => void;
  onEdit: (s: { id: number; name: string; description: string; instructions: string }) => void;
  onDelete: (id: number, name: string) => void;
}) {
  const t = useT();
  const csrf = useCsrf();
  const [skills, setSkills] = useState<SkillSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<SkillDetail | null>(null);
  const [importing, setImporting] = useState<"zip" | "git" | null>(null);

  // `tick` asks for a reload after a local change (import, toggle failure).
  const [tick, setTick] = useState(0);
  const load = useCallback(() => setTick((n) => n + 1), []);
  useEffect(() => {
    let cancelled = false;
    getJSON<{ skills: SkillSummary[] }>("/api/skills")
      .then((r) => { if (!cancelled) { setSkills(r.skills); setError(null); } })
      .catch((e: Error) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [tick, refreshKey]);

  async function openSkill(id: number) {
    try {
      const r = await getJSON<{ skill: SkillDetail }>(`/api/skills/${id}`);
      setOpen(r.skill);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function toggle(s: SkillSummary, enabled: boolean) {
    setSkills((list) => list?.map((x) => (x.id === s.id ? { ...x, enabled } : x)) ?? list);
    const r = await sendJSON(`/api/skills/${s.id}/enabled`, csrf, { enabled }).catch(() => ({ ok: false }));
    if (!r.ok) load();
  }

  return (
    <VStack gap={3}>
      <HStack hAlign="between" vAlign="center" gap={3} wrap="wrap">
        <Text type="supporting" color="secondary">
          {t("Des compétences au format Claude Code (SKILL.md + fichiers). L'assistant et le playground les chargent quand elles correspondent à ta demande ; tape « / » pour en appeler une.")}
        </Text>
        <HStack gap={2}>
          <Button label={t("Importer (.zip)")} variant="secondary" size="sm"
                  icon={<Icon icon={ArrowDownTrayIcon} size="sm" />} onClick={() => setImporting("zip")} />
          <Button label={t("Depuis Git")} variant="secondary" size="sm"
                  icon={<Icon icon={CodeBracketIcon} size="sm" />} onClick={() => setImporting("git")} />
          <Button label={t("Nouvelle compétence")} variant="primary" size="sm"
                  icon={<Icon icon={PlusIcon} size="sm" />} onClick={onCreate} />
        </HStack>
      </HStack>
      {error ? <Banner status="error" title={error} /> : null}
      {skills && skills.length === 0 ? (
        <EmptyState
          icon={<Icon icon={SparklesIcon} size="lg" />}
          title={t("Aucune compétence pour l'instant.")}
          description={t("Crée une compétence, importe un .zip ou un dépôt Git (par exemple github.com/anthropics/skills).")}
        />
      ) : (
        <List>
          {(skills ?? []).map((s) => (
            <ListItem
              key={s.id}
              label={s.name}
              description={s.description}
              startContent={<Icon icon={s.source === "git" ? CodeBracketIcon : SparklesIcon} size="sm" color="secondary" />}
              onClick={() => void openSkill(s.id)}
              endContent={
                <HStack gap={2} vAlign="center">
                  <Badge label={t(SOURCE_LABEL[s.source])} variant="neutral" />
                  {s.version ? <Text type="supporting" color="secondary">v{s.version}</Text> : null}
                  <Switch label={t("Activée")} isLabelHidden value={s.enabled}
                          onChange={(v) => void toggle(s, v)} />
                </HStack>
              }
            />
          ))}
        </List>
      )}
      {open ? (
        <SkillBrowser
          skill={open}
          onClose={() => setOpen(null)}
          onChanged={() => { load(); void openSkill(open.id); }}
          onEdit={(s) => { setOpen(null); onEdit(s); }}
          onDelete={(s) => { setOpen(null); onDelete(s.id, s.name); }}
        />
      ) : null}
      {importing ? (
        <ImportDialog mode={importing} onClose={() => setImporting(null)} onDone={() => void load()} />
      ) : null}
    </VStack>
  );
}
