import { Badge, Button, Group, Loader, Tooltip } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconDatabase, IconRobot } from "@tabler/icons-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { api } from "../api/client";
import { useRuns, useStartRun } from "../lib/runs";

// Health is polled anyway, so it doubles as the "a run just ended" signal: then every
// query refetches (queue, inbox, counts), whether or not the run's drawer is open.
function useRunFinished(runningId: number | undefined) {
  const client = useQueryClient();
  const last = useRef<number | undefined>(undefined);
  useEffect(() => {
    const ended = last.current;
    last.current = runningId;
    if (ended === undefined || ended < 0 || runningId === ended) return; // -1 = nothing ran
    client.invalidateQueries();
    api.run(ended, 1_000_000).then(({ run }) =>
      notifications.show({
        color: run.status === "done" ? "teal" : "red",
        title: `${run.label}: ${run.status}`,
        message: run.summary ?? "See Activity for the log.",
        autoClose: 12000,
      }),
    );
  }, [runningId, client]);
}

export default function HealthBar() {
  const health = useQuery({ queryKey: ["health"], queryFn: api.health, refetchInterval: 3000 });
  const start = useStartRun();
  const { watch } = useRuns();
  const h = health.data;
  useRunFinished(h ? (h.running?.id ?? -1) : undefined);
  if (health.isError) return <Badge color="red">API offline</Badge>;
  if (!h) return <Loader size="xs" />;
  return (
    <Group gap="xs" wrap="nowrap">
      {h.outdated !== false && ( // missing field = a server older than this UI
        <Tooltip label="The code changed since the API server started. Stop it (Ctrl+C) and run: uv run python -m job_search_assistant.api">
          <Badge color="red" variant="filled">Restart API server</Badge>
        </Tooltip>
      )}
      {h.running && (
        <Button size="compact-sm" variant="light" leftSection={<Loader size={12} />} onClick={() => watch(h.running!.id)}>
          {h.running.label}…
        </Button>
      )}
      <Tooltip label={h.neo4j ? "Knowledge graph is up" : "Needed for coverage, fit and skills"}>
        {h.neo4j ? (
          <Badge color="teal" variant="light" leftSection={<IconDatabase size={12} />}>Neo4j</Badge>
        ) : (
          <Button size="compact-sm" color="orange" variant="light" leftSection={<IconDatabase size={14} />}
            loading={start.isPending} disabled={!!h.running} onClick={() => start.mutate({ kind: "neo4j" })}>
            Start Neo4j
          </Button>
        )}
      </Tooltip>
      <Tooltip label={h.claude ?? "Claude Code not found: set CLAUDE_BIN in .env"}>
        <Badge color={h.claude ? "teal" : "red"} variant="light" leftSection={<IconRobot size={12} />} visibleFrom="xs">
          Claude
        </Badge>
      </Tooltip>
    </Group>
  );
}
