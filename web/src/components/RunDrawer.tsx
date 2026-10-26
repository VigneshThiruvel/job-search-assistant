import { Badge, Button, Drawer, Group, ScrollArea, Stack, Text } from "@mantine/core";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import { api } from "../api/client";
import { duration, RUN_COLOR } from "../lib/format";
import { useRuns } from "../lib/runs";

export default function RunDrawer() {
  const { watching, watch } = useRuns();
  return (
    <Drawer opened={watching !== null} onClose={() => watch(null)} position="right" size="lg" title="Run">
      {watching !== null && <RunView id={watching} />}
    </Drawer>
  );
}

function lineClass(line: string): string {
  if (line.startsWith("→")) return "log-line-tool";
  if (line.startsWith("✗")) return "log-line-error";
  if (line.startsWith("✓")) return "log-line-ok";
  return "";
}

export function RunView({ id }: { id: number }) {
  const [lines, setLines] = useState<string[]>([]);
  const offset = useRef(0);
  const bottom = useRef<HTMLDivElement>(null);
  const client = useQueryClient();

  useEffect(() => {
    setLines([]);
    offset.current = 0;
  }, [id]);

  // Poll every second while running: append only the new lines (offset), like `tail -f`.
  const run = useQuery({
    queryKey: ["run", id],
    queryFn: async () => {
      const log = await api.run(id, offset.current);
      if (log.lines.length) {
        offset.current = log.next_offset;
        setLines((prev) => [...prev, ...log.lines]);
      }
      return log.run;
    },
    refetchInterval: (q) => (q.state.data?.status === "running" || !q.state.data ? 1000 : false),
  });

  const info = run.data;
  useEffect(() => bottom.current?.scrollIntoView({ block: "end" }), [lines.length]);
  useEffect(() => {
    if (info && info.status !== "running") client.invalidateQueries(); // results changed data
  }, [info?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!info) return <Text c="dimmed">Loading…</Text>;
  return (
    <Stack>
      <Group justify="space-between">
        <Group gap="xs">
          <Text fw={600}>{info.label}</Text>
          <Badge color={RUN_COLOR[info.status]}>{info.status}</Badge>
          <Text size="sm" c="dimmed">{duration(info.started_at, info.finished_at)}</Text>
        </Group>
        {info.status === "running" && (
          <Button size="xs" color="red" variant="light" onClick={() => api.cancelRun(id)}>Cancel</Button>
        )}
      </Group>
      {info.report && (
        <div className="report">
          <Markdown>{info.report}</Markdown>
        </div>
      )}
      <ScrollArea h={info.report ? 260 : 560} type="auto" bg="var(--mantine-color-default-hover)" p="xs" style={{ borderRadius: 8 }}>
        <div className="log">
          {lines.map((l, i) => (
            <div key={i} className={lineClass(l)}>{l}</div>
          ))}
          <div ref={bottom} />
        </div>
      </ScrollArea>
    </Stack>
  );
}
