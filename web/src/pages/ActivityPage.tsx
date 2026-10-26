import { Badge, Card, Grid, NavLink, Stack, Text, Title } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { RunView } from "../components/RunDrawer";
import { duration, RUN_COLOR, when } from "../lib/format";

export default function ActivityPage() {
  const runs = useQuery({ queryKey: ["runs"], queryFn: api.runs, refetchInterval: 5000 });
  const [open, setOpen] = useState<number | null>(null);
  const shown = open ?? runs.data?.[0]?.id ?? null;
  return (
    <Stack>
      <Title order={4}>Activity</Title>
      <Grid>
        <Grid.Col span={{ base: 12, md: 4 }}>
          {runs.data?.length === 0 && <Text c="dimmed" size="sm">No runs yet. Start one from the Today page.</Text>}
          {runs.data?.map((r) => (
            <NavLink key={r.id} active={r.id === shown} onClick={() => setOpen(r.id)} label={r.label}
              description={`${when(r.started_at)} · ${duration(r.started_at, r.finished_at)}`}
              rightSection={<Badge size="xs" color={RUN_COLOR[r.status]}>{r.status}</Badge>} />
          ))}
        </Grid.Col>
        <Grid.Col span={{ base: 12, md: 8 }}>
          <Card withBorder>{shown ? <RunView id={shown} /> : <Text c="dimmed">Pick a run.</Text>}</Card>
        </Grid.Col>
      </Grid>
    </Stack>
  );
}
