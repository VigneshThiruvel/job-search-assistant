import { Alert, Badge, Button, Card, CopyButton, Grid, Group, Modal, NavLink, ScrollArea, Stack, Tabs, Text, TextInput, Title } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { IconDownload, IconExternalLink } from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, type Card as CardT, type Detail, type Status } from "../api/client";
import { wordDiff } from "../lib/diff";
import { STATUS_COLOR } from "../lib/format";
import { useStartRun } from "../lib/runs";
import { useAction } from "../lib/useAction";

const GROUPS: { status: Status; title: string }[] = [
  { status: "selected", title: "Drafting" },
  { status: "ready", title: "Ready to send" },
  { status: "applied", title: "Applied" },
  { status: "skipped", title: "Skipped" },
];

function selectedFromHash(): number | null {
  const id = Number(window.location.hash.split("/")[1]);
  return Number.isFinite(id) && id > 0 ? id : null;
}

export default function ApplicationsPage() {
  const all = useQuery({ queryKey: ["applications", "all"], queryFn: () => api.applications() });
  const [open, setOpen] = useState<number | null>(selectedFromHash);
  const start = useStartRun();
  useEffect(() => {
    if (open === null && all.data) setOpen(all.data.find((c) => c.status === "ready" || c.status === "selected")?.job_id ?? null);
  }, [all.data, open]);
  const drafting = all.data?.filter((c) => c.status === "selected").length ?? 0;

  return (
    <Grid>
      <Grid.Col span={{ base: 12, md: 4 }}>
        {drafting > 0 && (
          <Button fullWidth mb="sm" onClick={() => start.mutate({ kind: "agent_prepare" })} loading={start.isPending}>
            Prepare {drafting} draft(s)
          </Button>
        )}
        <ScrollArea.Autosize mah="calc(100vh - 140px)">
          {GROUPS.map((g) => {
            const cards = all.data?.filter((c) => c.status === g.status) ?? [];
            if (!cards.length) return null;
            return (
              <div key={g.status}>
                <Group gap={6} mt="sm" mb={4}>
                  <Text size="xs" fw={700} tt="uppercase" c="dimmed">{g.title}</Text>
                  <Badge size="xs" variant="light" color={STATUS_COLOR[g.status]}>{cards.length}</Badge>
                </Group>
                {cards.map((c) => (
                  <NavLink key={c.job_id} active={open === c.job_id} onClick={() => setOpen(c.job_id)} label={c.title}
                    description={`${c.company}${c.channel === "alerts" ? " · LinkedIn/XING" : ""}`} />
                ))}
              </div>
            );
          })}
          {all.data && all.data.every((c) => c.status === "queued") && (
            <Text size="sm" c="dimmed">Select jobs on the Today page; they show up here.</Text>
          )}
        </ScrollArea.Autosize>
      </Grid.Col>
      <Grid.Col span={{ base: 12, md: 8 }}>
        {open ? <DetailView jobId={open} /> : <Card withBorder><Text c="dimmed">Pick a job.</Text></Card>}
      </Grid.Col>
    </Grid>
  );
}

function DetailView({ jobId }: { jobId: number }) {
  const detail = useQuery({ queryKey: ["application", jobId], queryFn: () => api.application(jobId) });
  if (detail.isError) return <Alert color="red">{detail.error.message}</Alert>;
  if (!detail.data) return <Text c="dimmed">Loading…</Text>;
  const d = detail.data;
  return (
    <Card withBorder padding="lg">
      <Stack gap="sm">
        <Group justify="space-between" wrap="nowrap" align="flex-start">
          <div>
            <Title order={4}>{d.card.title}</Title>
            <Group gap={6}>
              <Text c="dimmed" size="sm">{d.card.company} · {d.card.location ?? "?"}</Text>
              <Badge size="sm" color={STATUS_COLOR[d.card.status]}>{d.card.status}</Badge>
              {d.card.needs_resume && <Badge size="sm" variant="outline">résumé pending</Badge>}
              {d.card.needs_letter && <Badge size="sm" variant="outline">letter pending</Badge>}
            </Group>
          </div>
          <Button component="a" href={d.card.apply_url} target="_blank" variant="light" size="xs" rightSection={<IconExternalLink size={14} />}>
            Apply here
          </Button>
        </Group>
        <Actions card={d.card} />
        <Text size="sm">{d.card.fit_summary}</Text>
        {d.card.note && <Text size="sm" fs="italic">Your note: {d.card.note}</Text>}
        <Tabs defaultValue={d.resume ? "resume" : "fit"}>
          <Tabs.List>
            <Tabs.Tab value="resume">Résumé</Tabs.Tab>
            <Tabs.Tab value="letter">Letter</Tabs.Tab>
            <Tabs.Tab value="fit">Fit</Tabs.Tab>
            <Tabs.Tab value="posting">Posting</Tabs.Tab>
          </Tabs.List>
          <Tabs.Panel value="resume" pt="md"><ResumeView detail={d} /></Tabs.Panel>
          <Tabs.Panel value="letter" pt="md"><LetterView detail={d} /></Tabs.Panel>
          <Tabs.Panel value="fit" pt="md"><FitView jobId={jobId} /></Tabs.Panel>
          <Tabs.Panel value="posting" pt="md">
            <Text size="sm" className="prewrap">{d.description || "(no description)"}</Text>
          </Tabs.Panel>
        </Tabs>
      </Stack>
    </Card>
  );
}

function Actions({ card }: { card: CardT }) {
  const [note, setNote] = useState("");
  const [confirm, { open, close }] = useDisclosure(false);
  const select = useAction(api.select, "Résumé redraft requested");
  const letter = useAction(api.letter, "Letter requested");
  const applied = useAction(api.applied, "Marked as applied. Good luck!");
  const skip = useAction(api.skip, "Skipped");
  const closed = card.status === "applied";
  const n = note || null;
  return (
    <>
      <Group gap="xs">
        <TextInput size="xs" style={{ flex: 1, minWidth: 180 }} placeholder="Note for the redraft (what to stress)"
          value={note} onChange={(e) => setNote(e.currentTarget.value)} disabled={closed} />
        <Button size="xs" variant="default" disabled={closed} onClick={() => select.mutate([card.job_id, false, n])}>Redraft résumé</Button>
        <Button size="xs" variant="default" disabled={closed} onClick={() => letter.mutate([card.job_id, n])}>
          {card.has_letter ? "Redraft letter" : "Request letter"}
        </Button>
        <Button size="xs" variant="default" color="red" disabled={closed || card.status === "skipped"} onClick={() => skip.mutate([card.job_id, n])}>Skip</Button>
        <Button size="xs" disabled={closed} onClick={open}>I applied</Button>
      </Group>
      <Modal opened={confirm} onClose={close} title="Mark as applied?" centered>
        <Text size="sm" mb="md">Only do this after you submitted the application yourself. It can't be undone here.</Text>
        <Group justify="flex-end">
          <Button variant="default" onClick={close}>Cancel</Button>
          <Button onClick={() => { applied.mutate([card.job_id, n]); close(); }}>Yes, I applied</Button>
        </Group>
      </Modal>
    </>
  );
}

function Diff({ before, after }: { before: string; after: string }) {
  return (
    <span>
      {wordDiff(before, after).map((p, i) => (
        <span key={i} className={p.kind === "added" ? "diff-added" : p.kind === "removed" ? "diff-removed" : undefined}>{p.text}</span>
      ))}
    </span>
  );
}

function ResumeView({ detail }: { detail: Detail }) {
  const r = detail.resume;
  if (!r) return <Text c="dimmed" size="sm">No tailored résumé yet{detail.card.needs_resume ? ': run "Prepare drafts"' : ""}.</Text>;
  return (
    <Stack gap="md">
      <Group justify="space-between">
        <Text size="xs" c="dimmed">Green = new wording, struck through = original wording. Facts are checked against your résumé.</Text>
        <Button component="a" href={api.resumePdfUrl(detail.card.job_id)} target="_blank" size="xs" leftSection={<IconDownload size={14} />}>
          PDF
        </Button>
      </Group>
      <div>
        <Text fw={600} size="sm">Summary</Text>
        <Text size="sm"><Diff before={r.summary_before} after={r.summary_after} /></Text>
      </div>
      {r.entries.map((e) => (
        <div key={e.id}>
          <Text fw={600} size="sm">{e.heading}</Text>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {e.bullets.map((b, i) => (
              <li key={i}>
                <Text size="sm"><Diff before={b.sources.map((s) => s.text).join(" ")} after={b.text} /></Text>
              </li>
            ))}
          </ul>
        </div>
      ))}
      {r.skills && (
        <div>
          <Text fw={600} size="sm">Skills (new order)</Text>
          {r.skills.map((s) => <Text key={s.label} size="sm"><b>{s.label}:</b> {s.items.join(", ")}</Text>)}
        </div>
      )}
      {r.dropped.length > 0 && (
        <div>
          <Text fw={600} size="sm">Left out for this job</Text>
          {r.dropped.map((x) => <Text key={x} size="sm" c="dimmed">{x}</Text>)}
        </div>
      )}
    </Stack>
  );
}

function LetterView({ detail }: { detail: Detail }) {
  if (!detail.letter) {
    return <Text c="dimmed" size="sm">{detail.card.needs_letter ? 'Letter requested: run "Prepare drafts".' : "No letter. Use \"Request letter\" if this job wants one."}</Text>;
  }
  return (
    <Stack gap="sm">
      <Group justify="flex-end">
        <CopyButton value={detail.letter}>
          {({ copied, copy }) => <Button size="xs" variant="light" onClick={copy}>{copied ? "Copied" : "Copy letter"}</Button>}
        </CopyButton>
      </Group>
      <Card withBorder bg="var(--mantine-color-default-hover)"><Text size="sm" className="prewrap">{detail.letter}</Text></Card>
      <Text fw={600} size="sm">Claims and their sources</Text>
      {detail.claims.map((c, i) => (
        <div key={i}>
          <Text size="sm">{c.claim}</Text>
          <Text size="xs" c="dimmed">source: {c.evidence}</Text>
        </div>
      ))}
    </Stack>
  );
}

function FitView({ jobId }: { jobId: number }) {
  const fit = useQuery({ queryKey: ["fit", jobId], queryFn: () => api.fit(jobId) });
  if (!fit.data) return <Text c="dimmed" size="sm">Loading…</Text>;
  if (!fit.data.available) return <Alert color="orange">Start Neo4j (top bar) to see the skill fit.</Alert>;
  const color = (credit: number) => (credit >= 1 ? "teal" : credit > 0 ? "yellow" : "red");
  const groups = [
    { title: "Required", skills: fit.data.skills.filter((s) => s.required) },
    { title: "Nice to have", skills: fit.data.skills.filter((s) => !s.required) },
  ];
  return (
    <Stack gap="sm">
      {fit.data.coverage !== null && <Text size="sm">You cover <b>{Math.round((fit.data.coverage ?? 0) * 100)}%</b> of the required skills.</Text>}
      <Text size="xs" c="dimmed">Green: you have it · yellow: only a broader skill · red: missing. Hover for the skill that earns the credit.</Text>
      {groups.map((g) => g.skills.length > 0 && (
        <div key={g.title}>
          <Text fw={600} size="sm" mb={4}>{g.title}</Text>
          <Group gap={6}>
            {g.skills.map((s) => (
              <Badge key={s.skill} color={color(s.my_credit)} variant={s.my_credit >= 1 ? "light" : "outline"} title={s.via.length ? `via ${s.via.join(", ")}` : "missing"}>
                {s.skill}
              </Badge>
            ))}
          </Group>
        </div>
      ))}
    </Stack>
  );
}
