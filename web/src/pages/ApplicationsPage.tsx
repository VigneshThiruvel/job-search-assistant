import { Alert, Badge, Button, Card, CopyButton, Grid, Group, Modal, NavLink, ScrollArea, Stack, Tabs, Text, TextInput, Timeline, Title } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { IconDeviceFloppy, IconExternalLink, IconEye, IconFolderOpen } from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, type Card as CardT, type Detail, type Kind, type Outcome, type Status } from "../api/client";
import { wordDiff } from "../lib/diff";
import { STATUS_COLOR } from "../lib/format";
import { useStartRun } from "../lib/runs";
import { useAction } from "../lib/useAction";
import { notifications } from "@mantine/notifications";

const IN_PROGRESS: Status[] = ["applied", "screening", "interviewing", "offer"];
const CLOSED: Status[] = ["accepted", "declined", "rejected", "withdrawn", "ghosted"];
const AFTER_APPLYING: Status[] = [...IN_PROGRESS, ...CLOSED];
const GROUPS: { statuses: Status[]; title: string }[] = [
  { statuses: ["selected"], title: "Drafting" },
  { statuses: ["ready"], title: "Ready to send" },
  { statuses: IN_PROGRESS, title: "In progress" },
  { statuses: CLOSED, title: "Closed" },
  { statuses: ["skipped"], title: "Skipped" },
];
// What I can record next, by current status (any outcome may follow any other).
const OUTCOMES: { status: Outcome; label: string }[] = [
  { status: "screening", label: "Recruiter replied" },
  { status: "interviewing", label: "Interview" },
  { status: "offer", label: "Offer" },
  { status: "accepted", label: "Accepted" },
  { status: "declined", label: "I declined" },
  { status: "rejected", label: "Rejected" },
  { status: "withdrawn", label: "I withdrew" },
  { status: "ghosted", label: "Ghosted" },
];
const today = () => new Date().toLocaleDateString("en-CA"); // YYYY-MM-DD, local

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
        <TrackerStrip />
        <ScrollArea.Autosize mah="calc(100vh - 190px)">
          {GROUPS.map((g) => {
            const cards = all.data?.filter((c) => g.statuses.includes(c.status)) ?? [];
            if (!cards.length) return null;
            return (
              <div key={g.title}>
                <Group gap={6} mt="sm" mb={4}>
                  <Text size="xs" fw={700} tt="uppercase" c="dimmed">{g.title}</Text>
                  <Badge size="xs" variant="light" color={STATUS_COLOR[g.statuses[0]]}>{cards.length}</Badge>
                </Group>
                {cards.map((c) => (
                  <NavLink key={c.job_id} active={open === c.job_id} onClick={() => setOpen(c.job_id)} label={c.title}
                    description={`${c.company}${c.channel === "alerts" ? " · LinkedIn/XING" : ""}`}
                    rightSection={
                      g.statuses.length > 1 ? (
                        <Badge size="xs" variant={c.ghost_hint ? "filled" : "light"} color={c.ghost_hint ? "orange" : STATUS_COLOR[c.status]}>
                          {c.ghost_hint ? "no news" : c.status}
                        </Badge>
                      ) : undefined
                    } />
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

function TrackerStrip() {
  const stats = useQuery({ queryKey: ["tracker"], queryFn: api.tracker });
  const s = stats.data;
  if (!s || s.applied === 0) return null;
  const pct = (n: number) => `${Math.round((100 * n) / s.applied)}%`;
  const items = [
    ["Applied", `${s.applied}`],
    ["Replied", `${s.responded} (${pct(s.responded)})`],
    ["Interviews", `${s.interviewed}`],
    ["Offers", `${s.offers}`],
    ["Waiting", `${s.awaiting}`],
  ];
  return (
    <Card withBorder padding="xs" mb="xs">
      <Group gap="md" justify="space-between">
        {items.map(([label, value]) => (
          <div key={label}>
            <Text size="xs" c="dimmed">{label}</Text>
            <Text size="sm" fw={600}>{value}</Text>
          </div>
        ))}
      </Group>
    </Card>
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
        {AFTER_APPLYING.includes(d.card.status) ? <OutcomeActions card={d.card} /> : <Actions card={d.card} />}
        <Files detail={d} />
        <Text size="sm">{d.card.fit_summary}</Text>
        {d.card.note && <Text size="sm" fs="italic">Your note: {d.card.note}</Text>}
        <Tabs defaultValue={AFTER_APPLYING.includes(d.card.status) ? "history" : d.resume ? "resume" : "fit"}>
          <Tabs.List>
            <Tabs.Tab value="resume">Résumé</Tabs.Tab>
            <Tabs.Tab value="letter">Letter</Tabs.Tab>
            <Tabs.Tab value="fit">Fit</Tabs.Tab>
            <Tabs.Tab value="posting">Posting</Tabs.Tab>
            <Tabs.Tab value="history">History</Tabs.Tab>
          </Tabs.List>
          <Tabs.Panel value="history" pt="md"><HistoryView detail={d} /></Tabs.Panel>
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
  const start = useStartRun();
  const letter = useAction(api.letter);
  const applied = useAction(api.applied, "Marked as applied. Good luck!");
  const skip = useAction(api.skip, "Skipped");
  const [appliedOn, setAppliedOn] = useState(today());
  const n = note || null;
  return (
    <>
      <Group gap="xs">
        <TextInput size="xs" style={{ flex: 1, minWidth: 180 }} placeholder="Note for the redraft (what to stress)"
          value={note} onChange={(e) => setNote(e.currentTarget.value)} />
        <Button size="xs" variant="default" onClick={() => select.mutate([card.job_id, false, n])}>Redraft résumé</Button>
        <Button size="xs" variant="default" loading={letter.isPending || start.isPending}
          title="After checking the application portal: does this job want a letter?"
          onClick={() => letter.mutate([card.job_id, n], { onSuccess: () => start.mutate({ kind: "agent_prepare" }) })}>
          {card.has_letter ? "Redraft letter" : "Generate letter"}
        </Button>
        <Button size="xs" variant="default" color="red" disabled={card.status === "skipped"} onClick={() => skip.mutate([card.job_id, n])}>Skip</Button>
        <Button size="xs" onClick={open}>I applied</Button>
      </Group>
      <Modal opened={confirm} onClose={close} title="Mark as applied?" centered>
        <Text size="sm" mb="sm">Only do this after you submitted the application yourself. It can't be undone here.</Text>
        <Text size="sm" mb="sm">PDFs you saved for this job get renamed after the company and posting.</Text>
        <TextInput type="date" label="Applied on" size="xs" mb="md" value={appliedOn} max={today()}
          onChange={(e) => setAppliedOn(e.currentTarget.value)} />
        <Group justify="flex-end">
          <Button variant="default" onClick={close}>Cancel</Button>
          <Button onClick={() => { applied.mutate([card.job_id, n, appliedOn === today() ? null : appliedOn]); close(); }}>Yes, I applied</Button>
        </Group>
      </Modal>
    </>
  );
}

function OutcomeActions({ card }: { card: CardT }) {
  const [note, setNote] = useState("");
  const [date, setDate] = useState(today());
  const record = useAction(api.outcome, "Recorded");
  const days = card.last_event_at ? Math.floor((Date.now() - new Date(card.last_event_at).getTime()) / 86_400_000) : null;
  return (
    <Stack gap="xs">
      {card.ghost_hint && (
        <Alert color="orange" variant="light" p="xs">
          No news for {days} days. Mark it as ghosted? (Only if you think they won't answer: a late reply can still be recorded.)
        </Alert>
      )}
      <Group gap="xs" align="flex-end">
        <TextInput size="xs" style={{ flex: 1, minWidth: 160 }} placeholder="Note (who, which round, …)"
          value={note} onChange={(e) => setNote(e.currentTarget.value)} />
        <TextInput size="xs" type="date" value={date} max={today()} onChange={(e) => setDate(e.currentTarget.value)} />
      </Group>
      <Group gap={6}>
        {OUTCOMES.filter((o) => o.status !== card.status).map((o) => (
          <Button key={o.status} size="xs" variant={o.status === "ghosted" && card.ghost_hint ? "filled" : "default"}
            color={STATUS_COLOR[o.status]} loading={record.isPending}
            onClick={() => record.mutate([card.job_id, o.status, note || null, date === today() ? null : date], {
              onSuccess: () => setNote(""),
            })}>
            {o.label}
          </Button>
        ))}
      </Group>
    </Stack>
  );
}

const KIND_LABEL: Record<Kind, string> = { resume: "résumé", cover_letter: "letter" };

function Files({ detail }: { detail: Detail }) {
  const save = useAction(api.saveFile);
  const reveal = useAction(api.reveal);
  const kinds: Kind[] = [...(detail.resume ? ["resume" as const] : []), ...(detail.letter ? ["cover_letter" as const] : [])];
  if (!kinds.length) return null;
  const saved = Object.fromEntries(detail.files.map((f) => [f.kind, f]));
  return (
    <Group gap="xs">
      {kinds.map((kind) => {
        const f = saved[kind];
        return (
          <Group key={kind} gap={4}>
            <Button size="xs" variant="light" leftSection={<IconDeviceFloppy size={14} />} loading={save.isPending && save.variables?.[1] === kind}
              onClick={() => save.mutate([detail.card.job_id, kind], {
                onSuccess: (r) => notifications.show({ color: "teal", title: `Saved ${KIND_LABEL[kind]}`, message: r.path }),
              })}>
              {f ? `Save ${KIND_LABEL[kind]} again` : `Save ${KIND_LABEL[kind]} PDF`}
            </Button>
            {f?.exists && (
              <Button size="xs" variant="subtle" leftSection={<IconFolderOpen size={14} />} title={f.path}
                onClick={() => reveal.mutate([detail.card.job_id, kind])}>
                {f.path.split("/").pop()}
              </Button>
            )}
          </Group>
        );
      })}
    </Group>
  );
}

function HistoryView({ detail }: { detail: Detail }) {
  const events = detail.history;
  if (!events.length) return <Text c="dimmed" size="sm">No history yet.</Text>;
  return (
    <Timeline active={events.length - 1} bulletSize={14} lineWidth={2}>
      {events.map((e, i) => (
        <Timeline.Item key={i} color={STATUS_COLOR[e.to_status as Status] ?? "gray"} title={e.to_status}>
          <Text size="xs" c="dimmed">
            {new Date(e.at).toLocaleDateString()} · {e.source === "me" ? "you" : e.source}
          </Text>
          {e.note && <Text size="sm">{e.note}</Text>}
        </Timeline.Item>
      ))}
    </Timeline>
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
        <Button component="a" href={api.resumePdfUrl(detail.card.job_id)} target="_blank" size="xs" variant="default" leftSection={<IconEye size={14} />}>
          Preview PDF
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
    return <Text c="dimmed" size="sm">{detail.card.needs_letter ? "Letter requested: drafting starts with the next \"Prepare drafts\" run." : "No letter. If the application portal wants one, use \"Generate letter\" above."}</Text>;
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
