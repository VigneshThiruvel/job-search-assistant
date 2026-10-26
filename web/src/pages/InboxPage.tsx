import { Alert, Anchor, Badge, Button, Card, Checkbox, Group, Stack, Text, TextInput, Textarea, Title } from "@mantine/core";
import { IconExternalLink } from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type InboxItem } from "../api/client";
import { useStartRun } from "../lib/runs";
import { useAction } from "../lib/useAction";

export default function InboxPage() {
  const items = useQuery({ queryKey: ["inbox"], queryFn: api.inbox });
  const status = useQuery({ queryKey: ["status"], queryFn: api.status });
  const start = useStartRun();
  const unembedded = status.data?.unembedded ?? 0;
  return (
    <Stack>
      <Group justify="space-between">
        <div>
          <Title order={4}>Paste descriptions</Title>
          <Text size="sm" c="dimmed">
            LinkedIn and XING are never scraped: open each link in your browser, copy the description, paste it here.
          </Text>
        </div>
        {unembedded > 0 && (
          <Button onClick={() => start.mutate({ kind: "embed" })} loading={start.isPending}>
            Embed {unembedded} pasted
          </Button>
        )}
      </Group>
      {items.isError && <Alert color="red">{items.error.message}</Alert>}
      {items.data?.length === 0 && (
        <Card withBorder><Text c="dimmed">Nothing to paste. New jobs from your alert emails appear here after "Fetch".</Text></Card>
      )}
      {items.data?.map((item) => <PasteForm key={item.job_id} item={item} />)}
    </Stack>
  );
}

function PasteForm({ item }: { item: InboxItem }) {
  const [description, setDescription] = useState("");
  const [notes, setNotes] = useState("");
  const [expired, setExpired] = useState(false);
  const save = useAction(api.paste, `Saved: ${item.title}`);
  return (
    <Card withBorder padding="md">
      <Stack gap="xs">
        <Group justify="space-between" wrap="nowrap">
          <div style={{ minWidth: 0 }}>
            <Group gap={6} wrap="nowrap">
              <Badge size="sm" variant="light">{item.source}</Badge>
              <Text fw={600} truncate>{item.title}</Text>
            </Group>
            <Text size="sm" c="dimmed">{item.company} · {item.location ?? "?"} · email {item.posted_at?.slice(0, 10)}</Text>
          </div>
          <Anchor href={item.url} target="_blank" size="sm">
            <Group gap={4} wrap="nowrap">Open <IconExternalLink size={14} /></Group>
          </Anchor>
        </Group>
        <Textarea autosize minRows={4} maxRows={14} placeholder="Paste the full job description"
          value={description} onChange={(e) => setDescription(e.currentTarget.value)} disabled={expired && !description} />
        <Group align="center">
          <TextInput style={{ flex: 1 }} size="xs" placeholder="Notes for the agent (optional)" value={notes} onChange={(e) => setNotes(e.currentTarget.value)} />
          <Checkbox size="xs" label="Posting expired" checked={expired} onChange={(e) => setExpired(e.currentTarget.checked)} />
          <Button size="xs" loading={save.isPending} disabled={!expired && description.trim().length < 200}
            onClick={() => save.mutate([item.job_id, { description, notes: notes || null, expired }])}>
            Save
          </Button>
        </Group>
        {!expired && description.trim().length > 0 && description.trim().length < 200 && (
          <Text size="xs" c="dimmed">That looks too short for a full description ({description.trim().length} characters).</Text>
        )}
      </Stack>
    </Card>
  );
}
