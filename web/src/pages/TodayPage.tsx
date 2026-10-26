import { Alert, Badge, Button, Card, Grid, Group, NumberInput, Stack, Text, ThemeIcon, Title } from "@mantine/core";
import { IconCheck, IconCircleDashed } from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { go } from "../App";
import { api, type RunInfo, type RunKind } from "../api/client";
import AlertFunnel from "../components/AlertFunnel";
import JobCard from "../components/JobCard";
import { isToday, when } from "../lib/format";
import { useStartRun } from "../lib/runs";
import { useAction } from "../lib/useAction";

const ORDER = { apply: 0, maybe: 1, skip: 2 } as const;

function lastRun(runs: RunInfo[] | undefined, kind: RunKind): RunInfo | undefined {
  return runs?.find((r) => r.kind === kind && r.status === "done");
}

function Step(props: { n: number; done: boolean; title: string; detail: ReactNode; action?: ReactNode }) {
  return (
    <Group justify="space-between" wrap="nowrap" align="center" py={6}>
      <Group gap="sm" wrap="nowrap">
        <ThemeIcon radius="xl" size="md" color={props.done ? "teal" : "gray"} variant={props.done ? "filled" : "light"}>
          {props.done ? <IconCheck size={14} /> : <IconCircleDashed size={14} />}
        </ThemeIcon>
        <div>
          <Text fw={500} size="sm">{props.n}. {props.title}</Text>
          <Text size="xs" c="dimmed">{props.detail}</Text>
        </div>
      </Group>
      {props.action}
    </Group>
  );
}

function Checklist() {
  const status = useQuery({ queryKey: ["status"], queryFn: api.status });
  const runs = useQuery({ queryKey: ["runs"], queryFn: api.runs, refetchInterval: 10000 });
  const health = useQuery({ queryKey: ["health"], queryFn: api.health });
  const start = useStartRun();
  const [k, setK] = useState<number>(10);
  const [maxExtract, setMaxExtract] = useState<number>(30);
  const s = status.data;
  const busy = !!health.data?.running || start.isPending;
  const fetched = lastRun(runs.data, "fetch");
  const agent = lastRun(runs.data, "agent_daily");
  const prepared = lastRun(runs.data, "agent_prepare");
  if (!s) return null;

  const run = (kind: RunKind, label: string, extra?: object) => (
    <Button size="xs" variant="light" disabled={busy} onClick={() => start.mutate({ kind, ...extra })}>{label}</Button>
  );
  return (
    <Card withBorder padding="md">
      <Title order={5} mb="xs">Today</Title>
      <Step n={1} done={isToday(fetched?.finished_at)} title="Fetch new jobs"
        detail={`APIs + alert emails, then embedding · last: ${when(fetched?.finished_at)} · ${s.new_today} new today`}
        action={run("fetch", "Fetch")} />
      <Step n={2} done={s.inbox_open === 0} title="Paste LinkedIn/XING descriptions"
        detail={s.inbox_open ? `${s.inbox_open} waiting` : "nothing waiting"}
        action={s.inbox_open ? <Button size="xs" variant="light" onClick={() => go("inbox")}>Open inbox</Button> : null} />
      <Step n={3} done={s.unembedded === 0} title="Import & embed pasted texts"
        detail={s.unembedded ? `${s.unembedded} posting(s) not embedded` : "all embedded"}
        action={s.unembedded ? run("embed", "Embed") : null} />
      <Step n={4} done={isToday(agent?.finished_at) && s.pending_extraction === 0} title="Run the daily agent"
        detail={`extract, rank, write fit summaries · ${s.pending_extraction} fresh posting(s) to extract · last: ${when(agent?.finished_at)}${agent?.summary ? ` → ${agent.summary}` : ""}`}
        action={
          <Group gap={6} wrap="nowrap">
            <NumberInput size="xs" w={70} min={1} max={30} value={k} onChange={(v) => setK(Number(v) || 10)} aria-label="shortlist size" />
            <NumberInput size="xs" w={70} min={1} max={100} value={maxExtract} onChange={(v) => setMaxExtract(Number(v) || 30)} aria-label="max extractions" />
            {run("agent_daily", "Run", { k, maxExtract })}
          </Group>
        } />
      <Step n={5} done={s.queued_today === 0} title="Review today's queue"
        detail={`${s.queued_today} job(s) to select or skip, below · only ads posted in the last 2 days`} />
      <Step n={6} done={s.selected === 0} title="Prepare drafts (tailored résumé, letters you asked for)"
        detail={`${s.selected} selected job(s) waiting · last: ${when(prepared?.finished_at)}`}
        action={s.selected ? run("agent_prepare", "Prepare") : null} />
      {!health.data?.claude && <Alert color="red" mt="sm">Claude Code not found: set CLAUDE_BIN in .env to run the agents.</Alert>}
      {health.data && !health.data.neo4j && (
        <Alert color="orange" mt="sm">Neo4j is not running: the agent needs it to rank. Start it from the top bar.</Alert>
      )}
    </Card>
  );
}

export default function TodayPage() {
  const queue = useQuery({ queryKey: ["applications", "today"], queryFn: api.queue });
  const queued = { ...queue, data: queue.data?.cards };
  const dismiss = useAction(api.dismissOlder, "Older jobs dismissed (marked skipped)");
  const runs = useQuery({ queryKey: ["runs"], queryFn: api.runs });
  const lastAgent = runs.data?.find((r) => r.kind === "agent_daily");
  const ids = queued.data?.map((c) => c.job_id) ?? [];
  const coverage = useQuery({ queryKey: ["coverage", ids], queryFn: () => api.coverage(ids), enabled: ids.length > 0 });
  const sorted = [...(queued.data ?? [])].sort((a, b) => ORDER[a.recommendation] - ORDER[b.recommendation]);
  const lists = [
    { title: "LinkedIn / XING", hint: "where you apply", cards: sorted.filter((c) => c.channel === "alerts") },
    { title: "Other sites", hint: "strong matches only", cards: sorted.filter((c) => c.channel === "other") },
  ];
  return (
    <Stack>
      <Checklist />
      {queue.isError && <Alert color="red">{queue.error?.message}</Alert>}
      {(queue.data?.older ?? 0) > 0 && (
        <Group gap="xs">
          <Text size="sm" c="dimmed">
            {queue.data!.older} older queued job(s) hidden: queued before today, or the ad is more than 2 days old.
          </Text>
          <Button size="compact-xs" variant="subtle" loading={dismiss.isPending} onClick={() => dismiss.mutate([])}>
            Dismiss them
          </Button>
        </Group>
      )}
      <Grid>
        {lists.map((l) => (
          <Grid.Col key={l.title} span={{ base: 12, md: 6 }}>
            <Group gap="xs" mb="xs">
              <Title order={5}>{l.title}</Title>
              <Badge variant="light">{l.cards.length}</Badge>
              <Text size="xs" c="dimmed">{l.hint}</Text>
            </Group>
            <Stack gap="sm">
              {l.title === "LinkedIn / XING" && <AlertFunnel />}
              {l.cards.length === 0 && (
                <Text size="sm" c="dimmed">
                  {l.title === "LinkedIn / XING"
                    ? "Nothing to review from LinkedIn/XING. The panel above says why."
                    : "Nothing to review from other sites (they need ≥ 70% skill coverage)."}
                </Text>
              )}
              {l.cards.map((c) => (
                <JobCard key={c.job_id} card={c} coverage={coverage.data?.[String(c.job_id)]}
                  isNew={!!lastAgent && c.queued_at >= lastAgent.started_at} />
              ))}
            </Stack>
          </Grid.Col>
        ))}
      </Grid>
    </Stack>
  );
}
