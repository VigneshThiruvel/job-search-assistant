import { ActionIcon, Badge, Button, Card, Group, Progress, Spoiler, Stack, Switch, Text, TextInput, Tooltip } from "@mantine/core";
import { IconCheck, IconExternalLink, IconX } from "@tabler/icons-react";
import { useState } from "react";
import { api, type Card as CardT, type Coverage } from "../api/client";
import { REC_COLOR } from "../lib/format";
import { useAction } from "../lib/useAction";

export function CoverageBar({ coverage }: { coverage: Coverage | undefined }) {
  if (!coverage || coverage.coverage === null) return null;
  const pct = Math.round(coverage.coverage * 100);
  return (
    <Tooltip label="Share of required skills I have (partial = half)">
      <Group gap={6} wrap="nowrap">
        <Progress value={pct} w={90} size="sm" color={pct >= 70 ? "teal" : pct >= 40 ? "yellow" : "red"} />
        <Text size="xs" c="dimmed">{pct}%</Text>
      </Group>
    </Tooltip>
  );
}

export default function JobCard({ card, coverage, isNew }: { card: CardT; coverage?: Coverage; isNew?: boolean }) {
  const [note, setNote] = useState("");
  const [letter, setLetter] = useState(false);
  const select = useAction(api.select, `Selected: ${card.title}. Run "Prepare drafts" next.`);
  const skip = useAction(api.skip, `Skipped: ${card.title}`);

  return (
    <Card withBorder padding="md">
      <Stack gap={6}>
        <Group justify="space-between" wrap="nowrap" align="flex-start">
          <div style={{ minWidth: 0 }}>
            <Group gap={6} wrap="nowrap">
              {isNew && <Badge color="indigo" size="sm">new</Badge>}
              <Badge color={REC_COLOR[card.recommendation]} variant="light" size="sm">{card.recommendation}</Badge>
              <Text fw={600} truncate>{card.title}</Text>
            </Group>
            <Text size="sm" c="dimmed">{card.company} · {card.location ?? "?"}</Text>
          </div>
          <Tooltip label="Open the posting">
            <ActionIcon component="a" href={card.apply_url} target="_blank" variant="subtle" aria-label="Open the posting">
              <IconExternalLink size={18} />
            </ActionIcon>
          </Tooltip>
        </Group>
        <CoverageBar coverage={coverage} />
        {coverage && coverage.missing.length > 0 && (
          <Group gap={4}>
            <Text size="xs" c="dimmed">Missing:</Text>
            {coverage.missing.slice(0, 6).map((m) => (
              <Badge key={m} size="xs" color="red" variant="outline">{m}</Badge>
            ))}
          </Group>
        )}
        <Spoiler maxHeight={66} showLabel="more" hideLabel="less">
          <Text size="sm">{card.fit_summary}</Text>
        </Spoiler>
        <TextInput size="xs" placeholder="Note: what to stress, or why you skip" value={note} onChange={(e) => setNote(e.currentTarget.value)} />
        <Group justify="space-between">
          <Switch size="xs" label="cover letter" checked={letter} onChange={(e) => setLetter(e.currentTarget.checked)} />
          <Group gap="xs">
            <Button size="xs" variant="default" leftSection={<IconX size={14} />} loading={skip.isPending}
              onClick={() => skip.mutate([card.job_id, note || null])}>
              Skip
            </Button>
            <Button size="xs" leftSection={<IconCheck size={14} />} loading={select.isPending}
              onClick={() => select.mutate([card.job_id, letter, note || null])}>
              Select
            </Button>
          </Group>
        </Group>
      </Stack>
    </Card>
  );
}
